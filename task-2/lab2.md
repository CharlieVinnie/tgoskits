# 实验二：基于 QEMU 的 StarryOS 缺陷修复与功能增强

## 实验概述

本实验基于方案一（以 syscall 为引导的 StarryOS 改进），选择文件 I/O 相关的系统调用作为切入点，通过编写源码级测例在 QEMU 上运行，发现并修复 StarryOS 内核中的若干 POSIX 兼容性缺陷，最终扩展至 POSIX 定时器 syscall 的完整实现。共提交 11 个 PR（编号 #253–#355），覆盖文件描述符语义、vectored I/O、内存访问、定时器、测试基础设施等多个方面。

---

## 一、文件描述符语义修复

### PR #253：拒绝以写模式打开目录（EISDIR）

**Bug 内容：** `open("/tmp", O_WRONLY)` 在 StarryOS 上成功返回有效 fd，而 POSIX 要求拒绝以写模式打开目录（返回 EISDIR）。

**根因分析：** 在 `axfs-ng/src/highlevel/file.rs` 的 `_open()` 方法中，写入权限检查放在了 `if self.directory` 块内——该条件仅当调用者显式传入 `O_DIRECTORY` 时才为 `true`。当用户直接打开一个目录路径（如 `/tmp`）而不带 `O_DIRECTORY` 时，EISDIR 检查被完全跳过。

**修复方法：** 将写目录拒绝逻辑从 `if self.directory` 块移到 `loc.is_dir()` 分支中。这样，无论通过哪种方式到达目录路径，都会被正确拒绝。

```diff
- if self.directory {
-     if flags.contains(FileFlags::WRITE) {
-         return Err(VfsError::IsADirectory);
-     }
-     loc.check_is_dir()?;
- }
+ if self.directory {
+     loc.check_is_dir()?;
+ }
+ Ok(if loc.is_dir() {
+     if flags.contains(FileFlags::WRITE) {
+         return Err(VfsError::IsADirectory);
+     }
+     OpenResult::Dir(loc)
+ } else { ... })
```

**Review 迭代：** 无。PR 仅 1 个 commit，直接合并。

---

### PR #256：Pipe fd 错误码修复（EPIPE → ESPIPE/EINVAL）

**Bug 内容：** 多个 syscall 在 pipe fd 上返回了 `EPIPE`（管道已破裂），但实际语义是"该操作在此 fd 类型上不被支持"。涉及 `lseek`、`pread/pwrite`、`preadv/pwritev`、`fallocate`、`fadvise`、`ftruncate`、`fsync`、`fdatasync`。

**根因分析：** `File::from_fd()` 在无法将 fd 向下转型为 `File` 时，统一返回 `AxError::BrokenPipe`（映射为 EPIPE）。对于 pipe、socket 等非普通文件 fd，管道并未破裂，只是操作不适用。

**修复方法：**

1. 将 `File::from_fd()` 的默认返回值从 `AxError::BrokenPipe` 改为 `AxError::InvalidInput`（中立错误码）
2. 新增 `file_or_espipe()` 辅助函数，包装 `File::from_fd()`，将类型不匹配转换为 `ESPIPE`，同时保持 `EISDIR` 和 `EBADF` 不变
3. 在 `sys_lseek`、`sys_pread64`、`sys_pwrite64`、`sys_preadv2`、`sys_pwritev2`、`sys_fallocate` 中使用该辅助函数
4. 修正 `sys_fadvise64` 对 pipe 的检查

```diff
- pub fn from_fd(fd: FileFd) -> Result<&File, AxError> {
+ pub fn from_fd(fd: FileFd) -> Result<&File, AxError> {
      match &self.inner {
-         FileLike::File(_) | FileLike::Dir(_) => ...,
-         _ => Err(AxError::BrokenPipe),
+         FileLike::File(_) | FileLike::Dir(_) => ...,
+         _ => Err(AxError::InvalidInput),
      }
  }
```

**Review 迭代：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | COMMENTED | chyyuu | 修复仅区分了 file 和 pipe，但 fd 类型还有 socket、dev、dir 等，建议更全面的区分 | 回复引用 lseek man page ERRORS 章节：仅 ESPIPE 是 fd 类型相关的错误码，对所有非 File fd 类型统一返回 ESPIPE 符合 POSIX 规范 |

---

## 二、内存访问修复

### PR #296：NULL 指针 + 零长内存访问导致 pause() 立即返回

**Bug 内容：** `pause()` 在 StarryOS 上不阻塞，立即返回。`musl libc` 在 riscv64 上通过 `ppoll(NULL, 0, NULL, NULL)` 实现 `pause()`——无监视 fd、无超时，应无限期阻塞直到信号到达。StarryOS 却返回 `EFAULT`，导致 `pause()` 立即返回，父子进程信号通信完全失效。

**根因分析：** `UserPtr::get_as_mut_slice(len)` 无条件调用 `check_region()`，即使 `len=0`。对于 `ppoll(NULL, 0, ...)`，地址 `0x0` 未映射，`check_region` 返回 `AxError::BadAddress`。POSIX 允许 NULL 指针配合零计数——零长访问不触碰任何内存，指针值无关紧要。同样问题存在于 `UserConstPtr::get_as_slice`。

**修复方法：**

```rust
// UserPtr::get_as_mut_slice
pub fn get_as_mut_slice(&self, len: usize) -> Result<&mut [u8], AxError> {
    if len == 0 {
        return Ok(&mut []);  // 零长：跳过指针校验
    }
    self.check_region()?;
    // ...
}
```

**附加修复（x86_64）：** musl 在 x86_64 上使用原生 `pause` syscall（nr 34）而非 `ppoll`。StarryOS 没有对应处理，返回 ENOSYS（子进程退出码 40）。新增 `Sysno::pause` 分发，转发到 `sys_ppoll(0, 0, 0, 0, 0)`。

**Review 迭代：** 无。PR 仅 1 个 commit，直接合并。

---

## 三、lseek 语义修复

### PR #303：lseek 负数偏移返回 EINVAL（合并）

**Bug 内容：** `lseek(fd, -1, SEEK_SET)` 静默成功，`off_t` 的负值 `-1` 通过 `offset as _` 转换为 `u64::MAX`，`SeekFrom::Start` 将其存储为合法位置。

**根因分析：** `sys_lseek` 中 `SEEK_SET` 分支直接做 `SeekFrom::Start(offset as _)`。`offset` 是 `__kernel_off_t`（有符号），`as _` 转换静默将 `-1i64` 变为 `u64::MAX`，类型系统无法捕获此问题。

**修复方法：** 在 `sys_lseek` 的 `SEEK_SET` 分支添加显式 guard：

```rust
0 => {
    if offset < 0 {
        return Err(AxError::InvalidInput);  // → EINVAL
    }
    SeekFrom::Start(offset as _)
}
```

**Review 迭代：**

| 轮次 | Review 类型 | 提交者 | 内容 |
|---|---|---|---|
| 1 | APPROVED | ZR233 | verified regression，修复仅 1 个 commit，直接合并 |

---

## 四、目录 fd 写入与 vectored I/O 修复

### PR #324：writev/pwrite 在目录 fd 上返回 EBADF 而非 EISDIR

**Bug 内容：** `writev()` 和 `pwrite64()` 在 `O_RDONLY | O_DIRECTORY` 打开的 fd 上返回 `EISDIR`，正确行为应为 `EBADF`（fd 未以写权限打开）。

**根因分析：** 两个问题叠加：
1. `Directory::write()` 返回 `AxError::IsADirectory`——目录无法以写模式打开，任何写入尝试意味着 fd 不可写，应为 `EBADF`
2. `sys_pwrite64` 和 `sys_pwritev2` 使用 `file_or_espipe()` 透传了 `IsADirectory` 错误

**修复方法：**

```diff
- // Directory::write
- fn write(...) -> Result<usize, AxError> {
-     Err(AxError::IsADirectory)
- }
+ fn write(...) -> Result<usize, AxError> {
+     Err(AxError::BadFileDescriptor)
+ }
```

新增 `file_or_espipe_write()` 辅助函数，在 `file_or_espipe()` 基础上将 `IsADirectory` 转换为 `BadFileDescriptor`，应用于 `sys_pwrite64`、`sys_pwritev` 和 `sys_pwritev2`。

**Review 迭代（2 commits）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | `sys_pwritev2` 仍使用 `file_or_espipe`，未切换到新增的 `file_or_espipe_write` 辅助函数 | 第 2 个 commit 修正 |
| 2 | APPROVED | ZR233 | verified writev/pwritev regression | 批准合并 |

---

### PR #326：preadv2/pwritev2 offset=-1 和标志位校验（合并）

**Bug 内容：** 两个不完整实现：
1. `offset=-1` 被拒绝（返回 EINVAL），但 Linux 语义要求 `offset=-1` 表示"使用当前文件偏移"
2. 所有 `RWF_*` 标志被静默接受，包括非法值

**根因分析：** syscall handler 中 `if offset < 0 { return Err(InvalidInput); }` 拒绝了所有负数（包括特殊值 `-1`），且 `_flags: u32` 参数被前缀 `_` 忽略。

**修复方法：**

1. 将 `offset < 0` 改为 `offset < -1`
2. `offset == -1` 时分发到 `get_file_like(fd).read()/.write()`（使用当前文件偏移，并正确处理 pipe）
3. 新增 `validate_rwf_flags()` 函数，对任何非零 flags 返回 EOPNOTSUPP

```diff
- if offset < 0 { return Err(InvalidInput); }
+ if offset < -1 { return Err(InvalidInput); }
+ 
+ let file = file_or_espipe(fd)?;
+ if offset == -1 {
+     // 使用当前文件偏移
+     return if write { file.write(buf) } else { file.read(buf) };
+ }
```

**附带修复：** 标志位校验暴露了已有测试 `bug-pwritev2-read-at` 的潜在 bug——它只传了 5 个参数给 `syscall()`，第 6 个 flags 参数为寄存器残留值。修正为显式传入 `flags=0`。

**Review 迭代（7 commits）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | COMMENTED | ZR233 | 测试中 errno 检查应收紧为 `EOPNOTSUPP` 而非接受 `EINVAL`，防止内核未来错误映射导致回归误判 | 已修改 errno 检查 |
| 2 | CHANGES_REQUESTED | ZR233 | `offset=-1` 测试未验证文件位置是否真正前移，建议增加 `lseek(SEEK_CUR)` 断言 | 增加 lseek SEEK_CUR 断言 |
| 3 | APPROVED | ZR233 | 验证通过，共 7 个 commit 逐步完善测试用例精准度 | 批准合并 |

---

### PR #327：vectored I/O 综合边缘测例集

**内容：** 添加 `test-vectored-io` 测试套件，覆盖 `readv`、`writev`、`pread64`、`pwrite64`、`preadv`、`pwritev`、`preadv2`、`pwritev2` 共 8 个 syscall 的 66 个测例，分 12 个章节：

1-2. 正常 scatter/gather + EBADF
3-4. EINVAL（负 iovcnt + 负偏移）
5. ESPIPE（pipe fd）
6-7. 零长 iovec + 边界条件
8. 目录 fd 交互
9. 部分读
10. EISDIR
11. writev 在 pipe 上的原子性
12. EPIPE（破裂管道）

合并了两个前置 bugfix 分支：`agent-bugfix-preadv-pwritev-negative-offset`（#326）和 `agent-bugfix-writev-dir-ebadf`（#324）。

**Review 迭代（2 commits）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | CMake 开启 `-Werror` 后，libc `readv/writev` wrapper 的负 iovcnt / 超大 iovcnt 测例被现代 GCC 诊断打断，存在构建风险 | 改用 raw syscall 绕过 libc wrapper |
| 2 | APPROVED | ZR233 | 验证通过，66 个测例覆盖 12 个场景 | 批准合并 |

---

## 五、POSIX 定时器完整实现

### PR #341：timer_create/settime/gettime/delete 实现

**Bug 内容：** POSIX 定时器 syscall 全部被 stub 为空操作：
- `timer_create | timer_gettime | timer_settime => Ok(0)`——不做任何事就返回成功
- `timer_delete` 落入 `ENOSYS` 通配分支

这导致无效参数被静默接受、重复的定时器 ID、错误的剩余时间、信号无法送达、删除操作未实现。

**修复方法：** 新增 `PosixTimerTable` 模块（`kernel/src/task/posix_timer.rs`），管理每个进程的 POSIX 定时器状态：

1. **timer_create**：验证 clock ID 和 `sigev_notify`，分配唯一定时器 ID，回写 ID 到用户空间
2. **timer_settime**：验证 timespec 边界（负值、`nsec >= 1e9`），跟踪相对于定时器时钟的截止时间，通过 `old_value` 返回旧间隔/剩余时间，注册到 alarm 系统用于信号送达
3. **timer_gettime**：返回当前剩余时间和间隔
4. **timer_delete**：从表中移除定时器，无效 ID 返回 EINVAL

**架构设计：** `PosixTimerTable` 以 `Arc` 形式存放在 `ProcessData` 上，集成到现有 alarm 基础设施（`ALARM_LIST` + `poll_timer`），使得 armed 定时器能唤醒睡眠进程并在到期时送达信号。

**Review 迭代（24 commits，+2107 / −28 行，14 个文件）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | `PosixTimerTable` 应放在 `ProcessData`（进程级）而非 `Thread`（线程级），因为 POSIX 定时器是进程资源，跨线程共享 | 迁移到 ProcessData |
| 2 | CHANGES_REQUESTED | ZR233 | `interval_sec` 字段与 POSIX `itimerspec` 语义不匹配；`itimerspec` 校验不完整（负值、`tv_nsec >= 1e9` 未检查） | 重构为直接存储 `itimerspec`，增加完整边界校验 |
| 3 | CHANGES_REQUESTED | ZR233 | `TIMER_ABSTIME` 在过去截止时间时的行为不正确（应立即到期而非返回错误）；周期定时器 re-registration 逻辑有误 | 实现立即到期 + 修复重注册逻辑 |
| 4 | CHANGES_REQUESTED | ZR233 | CPU-time clock（`CLOCK_PROCESS_CPUTIME_ID` 等）应返回 `ENOTSUP`；siginfo 缺少 `si_value` 字段和 `SI_TIMER` 标志 | 增加 clock ID 校验 + 修复 siginfo 填充 |
| 5 | CHANGES_REQUESTED | ZR233 | `clippy::too_many_arguments` 警告需处理；`timer_create` 非事务性——`vm_write` 回写失败后已分配的 timer ID 泄漏 | 增加 `#[allow]` + 添加回滚机制 |
| 6 | CHANGES_REQUESTED | ZR233 | 未使用的 import（`core::time::Duration`、`axsync::Mutex` 等）需清理；CI workflow 变更需调整 | 删除未使用导入，调整 CI 配置 |
| 7 | APPROVED | ZR233 | 综合验证通过。73 个检查点覆盖无效参数拒绝、定时器 ID 唯一性、arm/disarm 生命周期、剩余时间追踪、`TIMER_ABSTIME` 过去截止时间、信号送达、有效/无效/armed 定时器的删除共 6 个 syscall | 批准合并 |

---

## 六、测试基础设施

### PR #355：Python 测试流水线与 python-hello 测例

**内容：** 为 StarryOS 构建系统添加 Python 测试流水线，在已有的 C 和 Shell 流水线之外新增 `python` 变体。支持在 QEMU 测试用例中放置 `python/` 目录并自动执行 Python 脚本。

新增 `python-hello` 测例作为验证。

**Review 迭代：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | 符号链接安全检查导致 Alpine 内绝对符号链接（如 `/bin/busybox`）被按 host 根解析，触发 "escapes the staging root" 错误，QEMU 测试无法启动。建议将绝对 guest 链接目标映射到 `allowed_root` 下再检查 | 修改 `copy_dir_recursive` 的符号链接处理逻辑，将绝对 guest 链接映射到 staging root 下再验证 |
| 2 | APPROVED | ZR233 | 验证通过 | 批准合并 |

---

## 汇总统计

| 维度 | 数据 |
|---|---|
| PR 总数 | 11（#253–#355，含 2 个替代性 PR） |
| 合并 PR | 9 |
| 涉及文件变更 | 100+ 文件 |
| 新增代码 | ~4000 行 |
| 测例数量 | 12+ 个测试组，涵盖 140+ 检查点 |
| 跨架构验证 | riscv64 / x86_64 / aarch64 / loongarch64 |
| 主要 syscall 组 | 文件 I/O (lseek, readv/writev, preadv/pwritev)、POSIX 定时器、pipe/socket 语义 |
