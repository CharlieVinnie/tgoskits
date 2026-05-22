# 实验一：Kiro 开发环境配置

## 实验概述

在 `tgoskits/.kiro/` 下配置了 Claude Code 插件化的 StarryOS 内核开发环境。该环境提供了自动化 bug 发现与修复工作流、Linux 标准验证、syscall 追踪对比等能力。

---

## 整体结构

```
tgoskits/.kiro/
├── hooks/                          # 事件钩子
│   ├── context-mode-posttooluse.json
│   ├── context-mode-posttooluse.kiro.hook
│   ├── context-mode-pretooluse.json
│   └── context-mode-pretooluse.kiro.hook
├── settings/
│   └── mcp.json                    # MCP 服务器配置
├── agents/                         # 子代理定义
│   ├── branch-commit-reviewer.md
│   ├── linux-standard-checker.md
│   └── syscall-comparison.md
└── specs/
    └── starry-bug-workflow/        # Bug 工作流规约
        ├── .config.kiro
        ├── requirements.md
        ├── design.md
        └── tasks.md
```

---

## Hooks：上下文模式集成

在 `hooks/` 中定义了 pre/post tool-use 钩子，用于与 `context-mode` 工具集成，实现每次工具调用前后的事件捕获和路由强制。

- **pretooluse**：在 `shell` 和 `read` 类工具调用前触发，向 context-mode 发送当前工具名，用于路由强制
- **posttooluse**：在所有工具调用后触发，向 context-mode 发送事件，用于调用记录追踪

每个钩子同时提供了 `.json` 和 `.kiro.hook` 两种格式，分别对应不同的 hook 加载机制。

---

## MCP 服务

`settings/mcp.json` 配置了四个 MCP 服务，其中三个为自定义实现，位于 `biglabB/mcp-servers/` 下，均使用 Python `mcp.server.fastmcp` 框架实现。

| 服务 | 作用 |
|---|---|
| `fetch` | URL 内容获取（第三方 mcp-server-fetch） |
| `git-guard` | Git 操作安全门禁 |
| `run-tgoskits-test` | Docker/QEMU 测试执行 + Linux 基线运行 |
| `create-tgoskits-test` | 测试目录脚手架生成 |

---

### git-guard：安全 Git 操作门禁

**解决的问题：** 在自动化 bug 工作流中，Claude 需要频繁执行 git 命令（创建分支、提交、rebase）。如果没有安全限制，可能会意外操作 `dev` 或 `main` 等保护分支。git-guard 提供了两层保护：

1. **分支命名门禁**：只有以 `agent-` 开头的分支允许执行任意 git 命令（`run_git_command`）
2. **重置保护**：只有配置在 `GIT_RESETTABLE_BRANCHES` 环境变量中的分支允许被 `reset --hard`

**暴露的工具：**

| 工具 | 功能 | 安全约束 |
|---|---|---|
| `list_branches` | 列出所有本地分支 | 无 |
| `switch_branch` | 切换到已有分支 | 无 |
| `create_agent_branch` | 创建并切换到新分支 | 分支名必须以 `agent` 开头 |
| `run_git_command` | 执行任意 git 命令 | 仅当当前分支以 `agent` 开头时允许 |
| `reset_branch_to_upstream` | Hard-reset 分支到上游 | 仅允许 `GIT_RESETTABLE_BRANCHES` 中列出的分支 |

`create_agent_branch` 拒绝非 `agent` 前缀的分支名，阻止意外在保护分支上开发。`run_git_command` 接受 `list[str]` 参数（如 `["commit", "-am", "fix msg"]`），在运行前检查当前分支名。`reset_branch_to_upstream` 在执行 `git reset --hard` 前先 `git fetch --all`，确保重置到最新的上游状态。

---

### run-tgoskits-test：Docker/QEMU 测试执行器

**解决的问题：** 在 StarryOS 开发流程中，运行一个测试需要经过编译内核、构建 rootfs、启动 QEMU、等待 OS 启动、执行测试、收集输出等多个步骤。这个工具封装了完整的 `cargo starry test qemu` 流程。

**暴露的工具：**

| 工具 | 用途 |
|---|---|
| `run_tgoskits_test` | 在 Docker/QEMU 中运行 StarryOS 测试 |
| `run_tgoskits_test_on_linux_host` | 在 Linux 宿主机上原生编译并运行同一测试 |

#### run_tgoskits_test

接收 `test_name`（测例目录名，不含斜杠）和 `arch`（四架构）。将架构名映射为 Rust target triple 后，构造 Docker 命令：

```
docker run --rm -t \
  -v <workspace>:/workspace \
  -v tgoskits-cargo-{registry,git,bin}:/opt/cargo/{registry,git,bin} \
  -v tgoskits-rustup:/opt/rustup \
  -v tgoskits-build-target:/workspace/target \
  -w /workspace tgoskits \
  cargo starry test qemu -t <target> -c <test_name>
```

Docker volume 分离很关键：cargo registry/git/bin 和 rustup 作为独立 volume 在多次运行间缓存，`build-target` 也单独缓存，避免每次重建内核。

逐行读取 Docker 输出，找到 `root@starry` 标记（OS shell 就绪）后启动 20 秒执行超时计时。返回 `run.log` 中 `root@starry` 之后的所有输出和进程退出码。**编译内核的时间不计入超时**，因为缓存 volume 避免了重复编译。

#### run_tgoskits_test_on_linux_host

接收 `test_path`（相对路径）和可选的 `strace_syscalls` 列表。将测试目录复制到 `/tmp` 下（避免污染源码树），根据存在 `c/` 还是 `rust/` 子目录选择构建策略：

- **C 测试**：`cmake ..` → `make -j4` → 定位二进制 → 运行（如果传了 `strace_syscalls`，用 `strace -e trace=...` 包裹）
- **Rust 测试**：`cargo build` → 定位 `target/debug/` 下的二进制 → 可选的 strace 包裹运行

运行结束后清理 `/tmp` 下的副本。编译 120 秒超时，运行使用调用者传入的 timeout（默认 30 秒）。

---

### create-tgoskits-test：测试脚手架生成器

**解决的问题：** 创建一个新的 StarryOS 测试需要手动编写 7-8 个文件（4 个架构的 QEMU 配置、CMakeLists.txt、C 源码）。模板化生成避免重复劳动。

**工作流程：**

接收 `test_name` 和 `kind`（`c` 或 `rust`），从模板目录复制整个文件树，跳过构建产物目录，然后将所有文件中的模板名称替换为新的 `test_name`，最后将 main 源文件替换为干净的 stub。

- C 测试模板：`test-suit/starryos/normal/qemu-smp1/test-vectored-io`
- Rust 测试模板：`test-suit/starryos/normal/qemu-smp1/rust-hello`

**生成的文件结构（C 测试）：**
```
<test_name>/
├── qemu-x86_64.toml
├── qemu-aarch64.toml
├── qemu-riscv64.toml
├── qemu-loongarch64.toml
└── c/
    ├── CMakeLists.txt
    └── src/
        └── main.c          ← stub，编辑此文件
```

**设计要点：**
- 基于模板复制而非文件生成：QEMU 配置中的 `success_regex`、`fail_regex`、timeout、shell init cmd 等从模板继承，不需要在代码中逐项配置
- 全局替换使所有配置文件自动适配新测试名
- stub 替换避免了模板中的旧测试逻辑残留

---

### 三个自定义 MCP 的协作关系

```
 create_tgoskits_test     run_tgoskits_test      git-guard
     (脚手架生成)        (测试执行 + 基线)       (Git 安全门禁)
         │                     │                     │
         │  生成测试目录 ──────→│                     │
         │                     │  确认测试通过/失败   │
         │                     │                     │
         │                     │                     │  创建 agent 分支
         │                     │                     │  提交代码 / rebase
         │                     │                     │  清理历史
         │                     │                     │
         └──────── 被 bug 工作流规约编排 ────────────┘
                          (Kiro 协调)
```

三个服务各自独立，由 `specs/starry-bug-workflow/` 编排为完整流水线：脚手架生成 → 测试执行 → 安全分支操作。

---

## Agent 定义

`agents/` 下定义了三个子代理，分别承担不同的独立职责：

### branch-commit-reviewer

只读代理，审查当前分支上所有新增 commit 的 diff 是否与 commit message 一致。遍历每个 commit，比对 diff 内容和 message 中的声明，标记夸大、遗漏或模糊的描述，输出结构化审查报告。仅通过 `mcp_git_guard_run_git_command` 操作 git，不修改任何文件。

### linux-standard-checker

Linux 标准验证代理。它的核心职责是回答一个 yes/no 问题——"StarryOS 的这个行为是否符合 Linux 标准？"——并且**只允许在有确凿证据时给出答案**。

#### 工作流程

1. **接收问题**：来自 bug 工作流的根因分析阶段，例如"mmap 在 length=0 时是否应返回 ENOMEM？"或"`lseek` 在 pipe fd 上应返回什么 errno？"
2. **三级证据链查询**，按优先级依次尝试，找到第一条确凿证据即停止：
   - **man pages**（优先级最高）：在宿主机上运行 `man <topic>` 或 `man <section> <topic>`，解析输出中相关的规格细节。大部分 POSIX 系统调用行为可以直接从 man pages 确认。
   - **POSIX / The Open Group Base Specifications**：如果 man page 不够明确或不存在，抓取 `https://pubs.opengroup.org/onlinepubs/9699919799/` 上对应的函数/头文件页面。这是 POSIX 的权威来源。
   - **Linux Foundation Reference Specifications**：如果前两者都无法确认，查找 `https://refspecs.linuxfoundation.org/` 上的 LSB、FHS、ELF 等规范。
3. **回答**：格式为 `Answer: YES / NO / INCONCLUSIVE`，附带精确的 Source 引用（哪个 man page section、哪个 POSIX 页面 URL 或哪个 refspec 文档），以及具体的证据引用（quote 或 paraphrase）。
4. **INCONCLUSIVE 约束**：如果三个来源都无法提供明确证据，必须如实回答 `INCONCLUSIVE`，禁止基于常识或推测给出答案。这种情况下 bug 工作流会记录歧义，并采用保守策略（默认 StarryOS 行为是错误的）。

#### 在 harness 中的角色

`linux-standard-checker` 是 bug 根因分析中的一个门禁环节。当 bug 工作流发现 StarryOS 行为与 Linux 不一致时，并不会立即判定 StarryOS 有 bug——而是先问 checker 这个问题。因为有时 Linux 自身的语义在不同版本之间也有变化，或者某些边界情况没有被规范明确覆盖。checker 的存在确保了 bug 分类基于标准而非假设。

#### 与其他部分的关系

- **输入**：来自 bug 工作流的根因分析阶段（Phase 4: Analysis）
- **输出**：返回给 Kiro 的结论，影响 bug 分类（`real bug` vs `not a bug` vs `ambiguous`）
- **工具依赖**：`shell`（运行 man）、`web`（抓取 POSIX / refspecs 页面）

---

### syscall-comparison

Syscall 对比代理。它的核心职责是在两个环境上运行同一个测试、捕获 syscall 轨迹并保存为日志文件——**不负责分析差异**，只负责生成可对比的数据。

#### 工作流程

1. **输入**：caller 提供 `test_path`（相对于 tgoskits 工作区根目录的路径）和可选的 `syscalls` 列表。如果 caller 没有提供 syscall 列表，agent 会自动从测试源码中推断（读取 `c/src/main.c` 或 `rust/src/main.rs`，找 `syscall(SYS_xxx, ...)`、`libc::syscall(...)` 以及各类标准库调用与系统调用的映射关系）。

2. **Linux 宿主机运行**：调用 `@run-tgoskits-test/run_tgoskits_test_on_linux_host`，传参 `test_path`、`syscalls`（传给 strace 的 `-e trace=`）和 `timeout=30`。测试在 Linux 上用 gcc 原生编译运行，strace 捕获指定 syscall 的每次调用的参数和返回值。运行结束后用 shell `cp run.log` 保存为 `syscall-linux.log`。

3. **StarryOS QEMU 运行**：
   - 先读取 `test_path/qemu-x86_64.toml`，**在内存中保存原始内容**用于后续恢复。
   - 在 `args` 数组中插入 QEMU debugcon 参数：`-debugcon file:/workspace/syscall-starry.log` 和 `-global isa-debugcon.iobase=0xe9`。debugcon 是 QEMU 提供的一个 I/O 端口（0xE9），StarryOS 内核的 syscall 返回路径会向该端口输出每条 syscall 的返回值。
   - 调用 `@run-tgoskits-test/run_tgoskits_test` 用 QEMU 运行 StarryOS，执行同一个测试。
   - **运行结束后，无论 QEMU 运行是否成功，必须恢复 `qemu-x86_64.toml` 到原始状态。**

4. **输出**：生成两份日志文件：
   - `syscall-linux.log`：strace 输出，包含每条 syscall 的参数和返回值
   - `syscall-starry.log`：QEMU debugcon 输出，包含 StarryOS 内核每条 syscall 的返回值

5. **报告**：告知 caller 两份日志已生成，`qemu-x86_64.toml` 已恢复。

#### 设计要点

- **为什么不用 strace 收集 StarryOS 数据？** 因为 strace 是用户态工具，StarryOS 上没有也可以正常工作的是 strace（strace 本身依赖 ptrace，而 ptrace 在 StarryOS 上未实现）。所以 StarryOS 侧改用 QEMU debugcon——这是硬件级别的方案，不依赖 OS 内部设施，只需要内核在 syscall 返回路径上向 0xE9 端口写一条日志。

- **为什么只在 x86_64 上做？** debugcon（ISA debugcon）是 x86 特有的 QEMU 设备，其他架构没有这个能力。所以 syscall 对比只在 x86_64 上进行。多架构的行为差异由 bug 工作流的 Phase 8（Multi-Architecture Verification）另行覆盖。

- **为什么用 `cp` 而不是文件写入工具？** `run.log` 由测试工具生成，用 `cp` 复制是更简单可靠的方式。同时严禁把 `run.log` 复制为 `syscall-starry.log`——debugcon 在 QEMU 运行期间已经直接写入了该文件，覆盖它会销毁 syscall 轨迹。

#### 在 harness 中的角色

`syscall-comparison` 在以下场景中使用：

- **复杂的 bug 根因分析**：当 errno 和最终结果不足以判断问题时，对比 syscall 级别的行为差异。例如，StarryOS 和 Linux 的最终返回值相同，但中间调用了不同的 syscall 序列，或者传入了不同的参数。
- **新 syscall 实现验证**：在实现一个新的系统调用后，用来确认参数传递和返回值的拼写与 Linux 一致。
- **语义对标调试**：当某个库（如 musl、tokio）在 StarryOS 上表现异常但不确定是哪个 syscall 导致的，可以快速捕获两者轨迹来定位差异点。

#### 与其他部分的关系

- **工具依赖**：`@run-tgoskits-test`（运行测试）、`read`/`write`（读写 QEMU 配置）、`shell`（`cp` 等文件操作）
- **输出消费方**：由 Kiro 或开发者手动对比两份日志

---

## Bug 工作流规约

`specs/starry-bug-workflow/` 是一套完整的 requirements-first 工作流规约，定义了从 bug 发现到 PR 提交的端到端流程。

### 七阶段流程

```
发现 (Discovery) → 报告 (Reporting) → 复现 (Reproduction) 
→ 分析 (Analysis) → 修复与验证 (Fix & Verify) 
→ 提交清理 (Commit Cleanup) → PR 生成 (PR Generation)
```

### requirements.md

正式的规约文档，定义了 11 条需求及每条的可接受标准。涵盖用户故事驱动的 bug 发现（读 man page → 生成测例 → 对比 Linux 基线）、结构化 bug 报告生成、靶向复现测例创建、根因分析、安全分支上的修复实现、回归验证、PR 消息生成、多架构一致性、状态追踪、输出解析规范、commit 卫生。

### design.md

架构设计文档，包含完整的 pipeline 流程和状态机定义：

- **Bug 状态机**：`discovered → reproducing → reproduced → analyzing → fixing → verifying → resolved`，以及 `wontfix` 终止态
- **数据模型**：Bug Report、Test Result、Check Result 的结构化定义
- **命名约定**：`test-<syscall>` / `bug-<syscall>-<desc>` 测试命名，`agent-test-*` / `agent-bugfix-*` 分支命名
- **13 条正确性属性**（Properties）：形式化描述各阶段必须满足的不变式
- **错误处理**：编译失败、QEMU 超时、内核 panic、复现失败、架构差异等异常路径的处理策略

### tasks.md

可执行的 checklist，将流程分解为 10 个具体任务，每个任务标记完成状态、关联需求编号、包含前置条件检查（如"仅当 riscv64 通过后才扩展其他架构"、"超过 2 个 commit 时 squash"）。

---

## 工作流示例

以下是一次完整 bug 发现与修复的典型流程：

1. 用户说"测试 mmap 和 brk"
2. Kiro 读取 `man mmap` / `man brk` 提取边界条件
3. 通过 `create-tgoskits-test` 创建 `test-mmap-family` 综合测例
4. 在 Linux 上运行建立基线，在 StarryOS (riscv64) 上运行获取结果
5. 对比输出，发现 StarryOS 上与 Linux 行为不一致的 check
6. 为每个失败的 check 生成独立的 `bug-mmap-<desc>` 复现测例
7. 分析根因（必要时调用 `linux-standard-checker` 确认标准）
8. 在 `agent-bugfix-*` 分支上实现修复
9. 在四架构上验证修复
10. 清理 commit 历史并生成 PR 消息

---

## 与 starry-harness 的对比

| 维度 | starry-harness（参考） | .kiro（本地） |
|---|---|---|
| 定位 | Claude Code 插件（可分发） | 项目级工作流配置 |
| 组织方式 | 技能 + 代理（CLAUDE.md 驱动） | 规约 specs + 代理 agents |
| 钩子机制 | `hooks.json` + `session-load.sh` | context-mode pre/post tooluse |
| 交互方式 | 自然语言触发技能 | specs 驱动的结构化流程 |
| 状态持久化 | `docs/starry-reports/` | 对话上下文 + git 分支 |
