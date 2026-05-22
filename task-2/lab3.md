# 实验三：BusyBox 兼容性修复

## 实验概述

本实验以 busybox 工具集在 StarryOS 上的运行为目标，发现并修复了三个导致 busybox 命令无法正常工作的内核缺陷：路径创建、进程控制、文件链接。共提交 3 个 PR（#375、#377、#378），覆盖 VFS 层、进程管理、文件系统三个子系统。

---

## PR #375：mkdir("/") 返回 EINVAL 而非 EEXIST

**Bug 内容：** `busybox mkdir -p /tmp/foo` 在 riscv64 上失败，报错 `can't create directory '/': Invalid argument`。

`busybox mkdir -p` 的内部实现 `bb_make_directory` 会遍历路径的每个 `/` 分隔符，对中间组件逐一调用 `mkdir`——第一个调用总是 `mkdir("/", 0777)`。Linux 对这个调用返回 `EEXIST`（根目录已存在），busybox 容忍此错误并跳过。StarryOS 返回 `EINVAL`，导致整个 `-p` 操作立即中止。

**根因分析：** `FsContext::create_dir` 调用 `resolve_nonexistent` 来拆分路径为 `(parent_dir, filename)`。对于没有 filename 组件的路径（`/`、`.`、`..`），`path.file_name()` 返回 `None`，`resolve_nonexistent` 返回 `VfsError::InvalidInput`（EINVAL）。这个返回值对 `resolve_nonexistent` 本身是正确的（它确实无法返回一个待创建的名字），但 `create_dir` 将其直接传播到用户空间，而没有检查该路径是否已存在。

**修复方法：** 在 `FsContext::create_dir` 中捕获 `resolve_nonexistent` 返回的 `InvalidInput`，然后直接解析该路径：
- 如果解析到一个已存在的目录 → 返回 `AlreadyExists`（EEXIST）
- 如果解析到一个非目录节点 → 返回 `NotADirectory`（ENOTDIR）
- 如果解析失败 → 透传错误

```rust
pub fn create_dir(&self, path: &str, mode: FilePerms) -> Result<(), AxError> {
    if path.as_str().is_empty() {
        return Err(AxError::NotFound);
    }
    match resolve_nonexistent(self, path, DerefSymlink::No) {
        Ok((dir, name)) => dir.create(name, FileType::Directory, mode)?,
        Err(VfsError::InvalidInput) => {
            match self.resolve(path, DerefSymlink::No) {
                Ok(loc) if loc.is_dir() => return Err(AxError::AlreadyExists),
                Ok(_) => return Err(AxError::NotADirectory),
                Err(e) => return Err(e.into()),
            }
        }
        Err(e) => return Err(e.into()),
    }
    Ok(())
}
```

**测试：** 新增 3 个 busybox shell 测例：`busybox_mkdir`、`busybox_mv`、`busybox_rmdir`。另增 `bug-mkdir-empty-path` C 测例覆盖 `mkdir("")` → ENOENT、`mkdir("/")` → EEXIST、`mkdir(".")` → EEXIST。

**Review 迭代（4 commits, 多轮 review）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | `mkdir("")` 被统一拦截为 InvalidInput 后会错误地返回 EEXIST，Linux 实际返回 ENOENT。建议先判断空路径返回 NotFound，再对可解析的无 filename 路径走 resolve 并映射为 AlreadyExists | 增加空路径守卫 |
| 2 | COMMENTED | CharlieVinnie | 已修复，并添加了对应测例 | — |
| 3 | CHANGES_REQUESTED | ZR233 | 测例 `qemu-*.toml` 放在 `normal/bugfix/` 子目录下但 xtask 的 normal 批量扫描只发现 `normal/` 的直接子目录，导致 `cargo xtask starry test qemu --arch riscv64 -c bug-mkdir-empty-path` 报 unknown case；另外 `fail_regex` 的大小写和输出格式不匹配 C 测例的实际失败输出 | 将测例并入 `bugfix` 批量测试；不再需要自定义 fail_regex |
| 4 | COMMENTED | CharlieVinnie | 已修正 | — |
| 5 | APPROVED | ZR233 | 验证结果：`cargo fmt --check` 通过；`cargo xtask clippy --package ax-fs-ng` 7/7 通过；`cargo xtask starry test qemu --arch riscv64 -c bugfix/bug-mkdir-empty-path` 输出正确。最终修复：`mkdir("")` → ENOENT、`mkdir("/")` 和 `mkdir(".")` → EEXIST、`mkdir("..")` → EEXIST（由 `resolve_nonexistent` 的 `AlreadyExists` 正确传播），全部与 Linux 一致 | 批准合并 |

---

## PR #377：vfork 语义实现与 CLONE_VM 下 execve 修复

**Bug 内容 1 — CLONE_VFORK 被静默忽略：** `do_clone` 中 `CLONE_VFORK` 标志被直接丢弃，父进程立即返回，与子进程在共享地址空间上产生竞态。违反 POSIX 保证（父进程挂起直到子进程调用 `execve` 或 `_exit`）。

**Bug 内容 2 — execve 破坏 CLONE_VM 父进程地址空间：** `posix_spawn()`（musl 的 `system()` 使用）通过 `__clone(CLONE_VM|CLONE_VFORK|SIGCHLD)` 调用，子进程在父进程地址空间内的栈分片上运行。当子进程调用 `execve` 时，旧的实现锁住共享的 `Arc<AddrSpace>` 并调用 `uspace.clear()`——清除了父进程的页表。父进程恢复后持有悬垂的页表指针，导致段错误或卡死。表现为 `test-stat-family` 在 loongarch64 上的段错误。

**根因分析：**

Bug 1：`do_clone` 对 `CLONE_VFORK` 无处理，父进程不等待。

Bug 2：`sys_execve` 直接在共享的 `AddrSpace` 上调用 `clear()`——当 `CLONE_VM` 使父子进程共享同一个 `Arc<AddrSpace>` 时，这摧毁了父进程的内存映射。

**修复方法：**

**Bug 1 — vfork 父进程阻塞：** 在 `ProcessData` 中新增 `vfork_done: SpinNoIrq<Option<Arc<WaitQueue>>>` 字段。fork 时创建 `Arc` 交给子进程（`set_vfork_done()`），父进程在 spawn 子进程后阻塞于该 WaitQueue——此时所有锁已释放，无死锁风险。子进程在 `sys_execve`（CLOEXEC fd 关闭后）和 `do_exit`（`_exit` 路径）中唤醒父进程。`notify_vfork_done()` 使用 `take()`，即使 exec 和 exit 产生竞态，唤醒也至多触发一次。

```rust
// 父进程（clone.rs）：
child.set_vfork_done(self_wq.clone());
// ... spawn child ...
if child_vfork {
    self_wq.wait();  // 阻塞直到子进程 exec 或 _exit
}

// 子进程（execve.rs）：
let old = process_data.notify_vfork_done();
if let Some(wq) = old {
    wq.notify_one();  // 唤醒父进程
}
```

**Bug 2 — exec 不触碰父进程地址空间：** 将新程序加载到一个全新的 `AddrSpace` 中，然后通过 `ProcessData::replace_aspace()` 原子替换子进程的 `Arc` 指针，完全不碰父进程的 `Arc` 和页表。新增 `TaskInner::switch_page_table()` 辅助函数，将新根地址同时写入保存的 `TaskContext` 和当前硬件寄存器。

**sys_execve 重构为两阶段：**
- **阶段 1（可失败）：** 路径解析、ELF 加载、CLOEXEC fd 扫描——所有可能失败的操作在此阶段完成
- **阶段 2（不可失败/原子提交）：** 替换地址空间、切换页表、更新进程状态

**副作用——busybox 测试通过：** `busybox-bug` 测试集包含 `busybox_cpio` 和 `busybox_tar`，两者都使用 `timeout 10 sh -c '...'`。`timeout` 内部调用 `posix_spawn`，进而使用 `__clone(CLONE_VM|CLONE_VFORK|SIGCHLD)`。修复前后 busybox 测试通过。

**测试：** `bugfix/bug-vfork-sniff` 包含两个子测试，全部在四架构上验证：
1. 内存共享——子进程通过 `CLONE_VM` 修改栈变量，父进程验证 `vfork` 返回后修改可见
2. 父进程阻塞——`vfork` 前记录时钟；子进程 sleep 5s 后 `_exit(0)`；父进程阻塞必须 ≥4s

**Review 迭代（14 commits，多轮 review，跨越 9 天）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | `CLONE_VFORK` 不应移除 `CLONE_VM`，且必须阻塞父线程等待子进程 exec 或 _exit。当前实现暴露了错误语义而非返回不支持 | 保留 CLONE_VM，实现 WaitQueue 阻塞 |
| 2 | CHANGES_REQUESTED | ZR233 | **`cargo fmt --check` 失败**：`mmap.rs`（2 处）、`execve.rs`（第 6 行）、`task/mod.rs`（第 405 行）格式化不一致 | 运行 cargo fmt |
| 3 | CHANGES_REQUESTED（详细审查报告） | ZR233 | 核心设计评价：CLONE_VFORK+CLONE_VM 保留正确、两阶段 execve 正确、replace_aspace+switch_page_table 正确、vfork 唤醒覆盖两种退出路径、测例覆盖关键行为。阻塞问题仅 `cargo fmt`。非阻塞：`_aspace_arc` 变量名前缀暗示未使用、`busybox-tests.sh` 是否应在此 PR。回复：busybox 测试因 vfork 修复才得以通过，因此包含 | cargo fmt 已修复 |
| 4 | CHANGES_REQUESTED | ZR233 | axtask 仍在排查并发问题，暂不适合合入新功能 | 暂时搁置 |
| 5 | CHANGES_REQUESTED | ZR233 | **Lost wakeup 竞态**：裸 `WaitQueue::wait()` 可能丢失唤醒——子任务在 `spawn_task()` 后先运行，在 exec/_exit 中调用 `notify_vfork_done()`，此时父任务尚未进入 wait queue，`notify_one()` 返回 false，`vfork_done` 已被 take 清空，父任务随后永久阻塞 | 增加条件等待机制 |
| 6 | CHANGES_REQUESTED | ZR233 | **SMP 锁序死锁**：`notify_vfork_done()` 在持有 vfork_done 锁时调用 `notify_one()`，而父进程的 `wait_vfork_done()` 在 `wait_until` 里先锁 wait-queue 再在 condition 中锁 vfork_done。SMP 上形成反向锁序死锁 | 释放 vfork_done 锁后再通知，保证不会重新引入 lost wakeup |
| 7 | CHANGES_REQUESTED | ZR233 | `ProcessData::aspace` 改成私有字段后，`pseudofs/proc.rs:306`、`mmap.rs:227`、`mmap.rs:258` 仍直接访问该字段，starry-kernel 编译失败 | rebase 后未重新编译，已修正 |
| 8 | APPROVED | ZR233 | 本地验证：cargo fmt --check 通过；`cargo xtask clippy --package starry-kernel` 通过；`cargo xtask clippy --package ax-task` 通过；`cargo xtask starry test qemu --arch x86_64 -c syscall` 通过，test-vfork 输出 Memory shared / Parent blocked / ALL TESTS PASSED | **批准合并** |

---

## PR #378：tmpfs 硬链接读回空数据

**Bug 内容：** `busybox link`（硬链接）在 tmpfs 上创建的文件读回为空：

```
echo hello > /tmp/a
link /tmp/a /tmp/b
cat /tmp/b        # 输出为空！
```

**根因分析：** `DirNode::link()` 委托给文件系统的 `link()`，后者返回一个新的 `DirEntry`。这个新条目的 `user_data`（`TypeMap`）为空。对于 tmpfs，文件内容存储在 page cache 中，而 page cache 存放在 `user_data` 内作为 `FileUserData(Arc<PageCache>)`。当硬链接被打开时，`CachedFile::get_or_create` 在新条目的 `user_data` 中找不到已有缓存，创建一个全新的空缓存，因此所有读操作返回零字节。

**修复方法：** 文件系统层 `link()` 创建新 `DirEntry` 后，将源节点的 `user_data` 克隆到新条目中。由于 `TypeMap` 存储的是 `Arc` 值，克隆共享同一个底层 page cache，而非复制数据。

```rust
fn link(&self, old: &Arc<dyn VfsNode>, name: &str) -> VfsResult<Arc<dyn VfsNode>> {
    let new_entry = self.fs.link(&self.node, old, name)?;
    if let Some(data) = old.user_data() {
        new_entry.set_user_data(data.clone());
    }
    Ok(new_entry)
}
```

同时为 `TypeMap` 派生 `Clone`（`components/axfs-ng-vfs/src/node/mod.rs`）。

**测试：** 在 `busybox-tests.sh` 中新增 `busybox_link` 测例。riscv64（tmpfs）和 x86_64（ext4 + tmpfs）上验证通过。

**Review：**

| 轮次 | Review 类型 | 提交者 | 内容 |
|---|---|---|---|
| 1 | APPROVED | ZR233 | 已审查本 PR 的相关变更，未发现阻塞问题。因依赖 PR #377，需等 #377 合并后才能合入 |

---

## BusyBox 测例通过情况

| 测例 | 描述 | 通过的 PR |
|---|---|---|
| `busybox_mkdir` | `mkdir -p` 多级目录创建 | #375（mkdir("/") EEXIST 修复） |
| `busybox_mv` | `mv` 文件/目录移动 | #375 |
| `busybox_rmdir` | `rmdir` 删除目录 | #375 |
| `busybox_cpio` | `cpio` 归档解包（`timeout sh -c`） | #377（vfork + CLONE_VM execve 修复） |
| `busybox_tar` | `tar` 归档解包（`timeout sh -c`） | #377 |
| `busybox_link` | `link` 硬链接创建 + 读回验证 | #378（tmpfs 硬链接 page cache 共享） |

## 汇总

| PR | 子系统 | Commits | Review 轮次 | 关键问题 |
|---|---|---|---|---|
| #375 | VFS | 4 | CHANGES_REQUESTED × 2 → APPROVED | `mkdir("")` 空路径特殊处理 + 测例接入方式修正 |
| #377 | 进程管理 | 14 | CHANGES_REQUESTED × 7 → APPROVED | 两阶段 execve、SpinNoIrq 安全替换、lost wakeup、SMP 锁序死锁、编译修复 |
| #378 | VFS/tmpfs | 2 | APPROVED（一次性通过） | 无审查意见 |
