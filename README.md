# BigLab-B 实验报告

本仓库是 BigLab-B（AI4OSE 操作系统课程大实验）的个人实验报告汇总，包含了任务一（基础实验）和任务二（AI 驱动的 StarryOS 内核改进）的全部实验文档以及配套的 MCP 工具链。

## 目录结构

```
.
├── task-1/                        # 任务一：5 个 ArceOS 基础实验报告
│   ├── lab1.md                    #   ANSI 彩色输出
│   ├── lab2.md                    #   HashMap 支持
│   ├── lab3.md                    #   Bump Allocator
│   ├── lab4.md                    #   RAMFS Rename
│   └── lab5.md                    #   SYS_MMAP 系统调用
├── task-2/                        # 任务二：AI 辅助 StarryOS 内核改进
│   ├── lab1.md                    #   Kiro 开发环境搭建
│   ├── lab2.md                    #   syscall 缺陷修复与功能增强
│   ├── lab3.md                    #   BusyBox 兼容性修复
│   └── lab4.md                    #   Deepseek-TUI 在 StarryOS 上运行
├── mcp-servers/                   # MCP 工具链（AI 自动化内核开发的基础设施）
│   ├── create-tgoskits-test/      #   测试脚手架生成器（Python）
│   ├── git-guard/                 #   Git 安全门禁（Python）
│   └── run-tgoskits-test/         #   Docker/QEMU 测试执行器（Python）
└── .kiro/                         # Kiro AI 编程代理配置
    ├── hooks/                     #   工具调用前后钩子
    ├── agents/                    #   子代理定义（commit审查、Linux标准检查、syscall对比）
    └── specs/                     #   Bug 工作流规约
```

---

## 任务一：基础实验

五个实验基于 ArceOS（Rust 实现的轻量级模块化操作系统），从应用层到底层系统调用逐步深入。

### Lab 1 — ANSI 彩色输出

在 ArceOS 的 "Hello, Arceos!" 输出中嵌入 ANSI SGR 转义序列（`\x1b[32m` / `\x1b[0m`），实现绿色前景色输出。修改只有一行代码，测试脚本通过正则匹配验证原始字节流中的转义序列。四个架构（riscv64 / x86_64 / aarch64 / loongarch64）均通过。

### Lab 2 — HashMap 支持

为 ArceOS 的 `axstd`（no_std 环境）引入 `hashbrown` crate 作为依赖，将其 `HashMap` 和 `HashSet` 重新导出到 `axstd::collections` 模块下，使应用代码能以与标准库一致的接口使用哈希表。踩坑记录：若不启用 `default-hasher` feature，`HashMap::new()` 不可用（泛型参数 `S` 缺少 `Default` 实现）。

### Lab 3 — Bump Allocator

实现 double-ended bump 分配器 `EarlyAllocator`，用于内核启动早期的临时内存管理。字节分配从低地址向高地址增长，页分配从高地址向低地址增长，两者共享同一连续内存区域。字节释放用引用计数控制，页分配永不释放。

### Lab 4 — RAMFS Rename

为 ArceOS 的内存文件系统实现 `rename` 操作。修改两层代码：`axfs`（RootDirectory 层的路径路由）和 `axfs_ramfs`（DirNode 层的实际目录项重命名）。实现风格参考已有 `create` / `remove` 方法的模式，四架构通过。

### Lab 5 — SYS_MMAP 系统调用

在 ArceOS 的 Linux 兼容层中实现 `mmap` 系统调用。由于 ArceOS 没有完整 VMA 机制，采用简化设计：原子 bump allocator 分配虚拟地址 → `map_alloc` 映射到用户地址空间 → 文件映射用 `read_at` 读入。支持 PROT_READ/PROT_WRITE/PROT_EXEC 权限位和 MAP_ANONYMOUS/MAP_PRIVATE 标志。

---

## 任务二：AI 驱动的 StarryOS 内核改进

### Lab 1 — Kiro 开发环境配置

搭建了完整的 AI 辅助内核开发环境，核心内容包括：

- **MCP 服务**：三个自定义 MCP 工具，分别负责测试脚手架生成、Docker/QEMU 测试执行 + Linux 基线运行、Git 安全分支操作
- **子代理**：
  - `branch-commit-reviewer` — 审查 commit 与 diff 的一致性
  - `linux-standard-checker` — 基于 man pages / POSIX 标准验证 StarryOS 行为是否符合 Linux 语义
  - `syscall-comparison` — 在 Linux 宿主和 StarryOS QEMU 上同步运行测试，生成 syscall 轨迹对比（利用 QEMU debugcon 0xE9 端口捕获内核侧 syscall 返回值）
- **Bug 工作流**：`.kiro/specs/starry-bug-workflow/` 定义了「发现 → 报告 → 复现 → 分析 → 修复与验证 → 提交清理 → PR 生成」七阶段流水线，含状态机、数据模型、13 条正确性属性约束

### Lab 2 — syscall 缺陷修复与功能增强

选择文件 I/O 相关系统调用作为切入点，共提交 **11 个 PR**（#253–#355），覆盖：

- **文件描述符语义**：修复 `fcntl(F_DUPFD)` 返回最小可用 fd 的行为、`openat` 的 dirfd 校验逻辑
- **Vectored I/O**：修复 `readv`/`writev` 的 iovec 内存访问边界检查、部分读写时的 iov 推进逻辑
- **内存访问**：修复若干 syscall 中用户态指针校验缺失的问题
- **定时器**：完整实现了 POSIX 定时器 syscall 组（`timer_create` / `timer_settime` / `timer_gettime` / `timer_delete`）

### Lab 3 — BusyBox 兼容性修复

以 busybox 工具集在 StarryOS 上的运行为目标，发现并修复了三个内核缺陷，提交 **3 个 PR**（#375、#377、#378）：

- **#375**：`mkdir("/")` 返回 `EINVAL` 而非 `EEXIST` — VFS 层路径创建逻辑缺陷
- **#377**：进程控制相关修复
- **#378**：文件链接语义修复

### Lab 4 — Deepseek-TUI 在 StarryOS 上运行

以在 StarryOS 上运行 Deepseek-TUI（终端 AI 聊天工具）为目标，修复了多个子系统的兼容性问题，共涉及 **6 个 PR**（#485、#500、#502、#504、#529、#535）：

- **#485**：TCP loopback send 后缺少 `poll_interfaces` 调用，导致接收方收不到数据
- **#500**：进程管理相关修复
- **#502**：终端子系统修复
- **#504**（由 JosephJoshua 提交）：核心修复 — 解决了 UI 不刷新的根本问题
- **#529、#535**：epoll 和其他子系统兼容性修复

---

## MCP 工具链

三个 Python MCP 服务的简要说明（基于 `mcp.server.fastmcp` 框架）：

| 服务 | 作用 |
|------|------|
| `create-tgoskits-test` | 从模板快速生成 StarryOS 测试用例（支持 C 和 Rust），包含 4 架构 QEMU 配置 + 构建文件 + stub 源码 |
| `run-tgoskits-test` | 在 Docker/QEMU 中运行 StarryOS 测试（Docker volume 缓存避免重复编译），也可在 Linux 宿主上原生编译运行并可选 strace 追踪 |
| `git-guard` | Git 操作安全门禁：只有 `agent-*` 分支允许自由提交，保护 `dev` / `main` 分支不被 AI 误操作 |
