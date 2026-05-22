# 实验四：Deepseek-TUI 在 StarryOS 上的运行

## 实验概述

本实验以在 StarryOS 上运行 Deepseek-TUI 为目标，发现并修复了网络栈、进程管理、终端、epoll 等多个子系统的兼容性问题。共涉及 6 个 PR（#485、#500、#502、#504、#529、#535），其中 #504 由 JosephJoshua 提交并最终解决了核心的 UI 不刷新问题。

---

## PR #485：TCP loopback send 后缺少 poll_interfaces

**Bug 内容：** Deepseek-TUI 的自带测试集在 StarryOS 上卡死约 75 秒。任何使用 `reqwest` 或 `hyper-util` 的 HTTP 客户端在 `tokio::main(flavor = "current_thread")` 运行时上发起回环请求均会卡死。

**根因分析：** `TcpSocket::send()` 中 `poll_interfaces()` 只在数据写入 smoltcp TX buffer **之前**调用，写入之后没有再次调用。这意味着：

1. 数据被放入 server socket 的 smoltcp TX buffer
2. `send()` 返回给调用者
3. 回环包从未被真正发送——`router.dispatch()` / `loopback.send()` 没有执行
4. 对端（client）socket 的 RX buffer 一直为空
5. 通过 `epoll_ctl` 注册的 `InterestWaker` 永远不会被触发
6. client 的 `epoll_wait` 一直阻塞直到 TCP 定时器超时

**修复方法：** 在 `TcpSocket::send()` 中，写入成功后增加一次 `poll_interfaces()` 调用，使数据立即通过网络栈发送出去。

```rust
if result.is_ok() {
    poll_interfaces();
}
```

对于回环连接，`loopback.send()` 被执行，触发 `self.poll.wake()`，进而唤醒对端 socket 上阻塞的 `epoll_wait`。

**测试：** 
- `bugfix/bug-tcp-send-no-epoll-notify` — 回归测例：回环 TCP pair，`epoll_ctl(ADD, client_fd, EPOLLIN|EPOLLET)`，`write(server_fd)` 后 `epoll_wait` 必须立即返回。已加入四架构 bugfix 套件。
- `syscall/test-epoll-eventfd` — 31 个 epoll+eventfd 边缘触发正确性测例（排查过程中编写，用于排除 eventfd/EPOLLET 是卡死原因的可能性）。

**Review 迭代：**

| 轮次 | Review 类型 | 提交者 | 内容 |
|---|---|---|---|
| 1 | APPROVED | ZR233 | LGTM，代码格式正确，测试覆盖充分，语义符合 POSIX/Linux 规范 |
| 2 | APPROVED | ZR233 | 本轮重新审查未发现阻塞问题。验证：cargo fmt --check 通过；GitHub checks 为绿；bugfix 分组 discovery 通过；TCP send 后 poll_interfaces 及新增回归用例接入合理 |

---

## PR #500：Zombie PID 系统调用修复

**Bug 内容：** 在已退出但未被 `waitpid` 回收的 zombie 进程上，`getsid`、`getpgid`、`getpriority` 三个 syscall 返回 `ESRCH`，而 Linux 要求返回正确的会话 ID / 进程组 ID / nice 值。此外，非 root 同 UID 调用者对 zombie 进程执行 `kill(pid, 0)` 也会错误返回 `ESRCH`。

**根因分析：** 
1. `PROCESS_TABLE` 存的是 `Weak<ProcessData>`，进程退出后 `ProcessData` 被释放，`get_process_data(pid)` 返回 `ESRCH`，但 `Arc<Process>` 实际上还在父进程的 children 列表中存活到 `waitpid` 调用。
2. `check_kill_permission` 在进入 zombie-aware 分支前先调用 `get_task(target_pid)` 获取任务凭据，但 zombie 的 task 已被 GC，非 root 调用者提前拿到 `ESRCH`。
3. `register_zombie` 在 `process.exit()` **之后**调用，存在竞态窗口：父进程 `waitpid(WNOHANG)` 可在这之间完成回收并 `unregister_zombie`，随后退出线程又 late-register 一个永不被清理的 zombie 条目。

**修复方法：**

```rust
// ZOMBIE_PIDS: BTreeSet<Pid> → BTreeMap<Pid, ZombieEntry>
// ZombieEntry contains: Arc<Process> + Arc<Cred>
```

1. **`ZOMBIE_TABLE` 存储 `Arc<Process>`**：将 `ZOMBIE_PIDS` 从 `BTreeSet<Pid>` 升级为 `BTreeMap<Pid, ZombieEntry>`，在 `register_zombie` 时保存 `Arc<Process>`（持有 pgid/sid），`unregister_zombie` 时释放。
2. **修复注册竞态**：`register_zombie` 移到 `process.exit()` **之前**调用，原子性地发布 zombie 条目和 zombie 状态。
3. **凭据快照**：`ZombieEntry` 额外存储 `cred: Arc<Cred>`，`check_kill_permission` 在 `get_task` 返回 `ESRCH` 时回退到 `get_zombie_cred`，用快照凭据做权限检查。

**测试：**
- `bug-zombie-syscalls` — fork 子进程，同步确保 zombie 状态，验证 `getsid/getpgid/getpriority` 在 waitpid 前后行为正确
- `bug-kill-zombie-perm` — 非 root 同 UID 调用者验证 `kill(zombie, 0)` / `kill(zombie, SIGKILL)` 行为

**Review 迭代（5 commits，多轮 review）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | `process.exit()` 先发布 `is_zombie=true` 后 `register_zombie()`，父进程可在窗口内 `waitpid(WNOHANG)` 完成 reap，导致 zombie 表先 unregister 后 late register | `register_zombie` 移到 `process.exit()` 之前 |
| 2 | CHANGES_REQUESTED | ZR233 | zombie 表只存 `Arc<Process>`，但 `kill(pid>0)` 在进入 zombie-aware 分支前先做权限检查。非 root 同 UID 调用者因 task 被 GC 取不到凭据，仍返回 `ESRCH` | `ZombieEntry` 新增 `cred: Arc<Cred>` 快照 |
| 3 | APPROVED | ZR233 | 两个阻塞点已修复。本地验证：cargo fmt --check、clippy、bug-zombie-syscalls 和 bug-kill-zombie-perm 的 x86_64 QEMU 回归均通过 | 批准合并 |

---

## PR #502：TTY raw mode VMIN=0 下 poll_read 误报 POLLIN

**Bug 内容：** 非规范（raw）模式下，`LineDiscipline::poll_read()` 在 `VMIN=0` 时无条件返回 `true`，导致 `poll()`/`epoll()` 总在 tty fd 上报告 `POLLIN`，即使接收缓冲区为空。这使得交互式 TUI 在无输入时忙等而非休眠。

**根因分析：** `VMIN=0` 的语义是"`read()` 在缓冲区为空时立即返回 0 字节"——它不代表数据总是可用。`poll()` 应只在确实有字节时才报告 `POLLIN`。

```rust
// 修复前
let vmin = term.special_char(VMIN) as usize;
vmin == 0 || self.buf_rx.occupied_len() >= vmin

// 修复后
!self.buf_rx.is_empty() && (vmin == 0 || self.buf_rx.occupied_len() >= vmin)
```

**测试：** `bug-raw-terminal-polling` — 将 stdin 设为 raw 模式（`VMIN=0, VTIME=0`），fork 子进程调用 `poll(stdin, POLLIN, 1000ms)` 无输入，预期 poll 超时返回 0。

**Review 迭代：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | 新增回归测试在 termios raw/VMIN 设置失败时会误通过（静默跳过），且 `git diff --check` 报 trailing whitespace | 修改测试使失败时更响亮（添加错误检查），修复 whitespace |
| 2 | APPROVED | ZR233 | 验证通过 | 批准合并 |

---

## PR #529：UDP loopback dispatch 与 recv EAGAIN 语义

**Bug 内容 1 — recv 在未连接 UDP socket 上返回 ENOTCONN：** 非阻塞 UDP socket 在没有设置对端地址时调用 `recv()`（NULL src_addr），代码无条件调用 `remote_endpoint()` 获取对端，未连接时返回 `NotConnected` → 映射为 `ENOTCONN`。POSIX 要求空缓冲区时返回 `EAGAIN`/`EWOULDBLOCK`，且 Linux 允许在未连接 UDP socket 上调用 `recv()` 读取下一个任意源的数据报。

**Bug 内容 2 — sendto loopback 不触发 epoll 唤醒：** 与 #485 同类问题——`UdpSocket::send()` 在写入 smoltcp TX buffer 前调用了 `poll_interfaces()` 但写入后没有，回环包从未送达接收端。

**修复方法：**

1. **recv EAGAIN**：新增 `AnyDiscard` 变体——当没有设置对端且调用方未提供地址缓冲时，接受任意源的数据报。
2. **sendto 唤醒**：在 `send_poller` 完成后增加 `poll_interfaces()`，确保包在 `sendto` 返回前通过 loopback 设备分发到接收端。

**测试：** `test-epoll-network` 覆盖 5 个场景：
1. 网络包唤醒 `epoll_wait`（fork + sendto 回环）
2. 非阻塞 `recv` 返回 `EAGAIN`
3. `EPOLL_CTL_MOD` 保持兴趣集
4. Level-triggered 重武装
5. Edge-triggered 不重复触发

**Review 迭代：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | PR 合并了太多历史 commit，diff 过多无法审查 | 调整 git merge 方式，清理历史 |
| 2 | APPROVED | ZR233 | UDP 修复正确，zombie 基础设施验证通过。5 个场景在 x86_64 QEMU 中完整通过 | 批准合并 |
| 3 | APPROVED | ZR233 | 本轮复审确认 PR 已缩小到 UDP/epoll 相关改动 | — |

---

## PR #535：sigwaitinfo 等待被阻塞信号时永久挂起

**Bug 内容：** `sigwaitinfo()`/`rt_sigtimedwait()` 在等待默认 disposition 为 Ignore 且被线程信号掩码阻塞的信号（如 SIGCHLD）时永久挂起。

**三个子 Bug：**

1. **信号被静默丢弃**：`ProcessSignalManager::send_signal()` 在信号入队前调用了 `is_ignore()`。SIGCHLD 的默认 action 是 `Ignore`，信号因此被直接丢弃，从未进入 pending 队列。
2. **休眠线程不被唤醒**：当所有线程都阻塞了该信号时，`send_signal()` 返回 `None`，`send_signal_to_process()` 没有调用 `task.interrupt()`。卡在 `rt_sigtimedwait` 中的线程永远不会被唤醒去重新检查 pending 集合。
3. **SIGCHLD siginfo 字段不完整**：只支持 `CLD_EXITED`，未填充 `si_uid`，且将 wait-status 编码后的 `exit_code` 直接用作 `si_status`。

**修复方法：**

1. **精确的 sigwait_set**：用 `sigwait_set: SpinNoIrq<SignalSet>` 替代 `in_sigwait: AtomicBool`，精确记录每个线程等待的信号集。在 `is_ignore()` 早退前检查是否该信号被某线程的 `sigwait_set` 包含。
2. **定向线程唤醒**：当 `send_signal()` 返回 `None`（所有线程阻塞该信号）时，只唤醒 `sigwait_set` 中包含此信号的线程，避免对卡在 `waitpid` 等无关 syscall 的线程产生虚假 `EINTR`。
3. **正确的 SIGCHLD siginfo**：修正 `si_code`（`CLD_EXITED`/`CLD_KILLED`/`CLD_DUMPED`）、`si_status`（原始退出值 0-255 或信号号）、`si_uid`。

**测试：** `bug-sigwaitinfo-blocked-sigchld` — 阻塞 SIGCHLD → fork → `sigwaitinfo({SIGCHLD}, &si)` 必须返回 `si_signo == SIGCHLD`、`si_pid == child`。5 秒 alarm 作为硬超时防止挂起。

**Review 迭代（5 commits，多轮 review）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | sigwait_set 发布顺序存在竞态——"线程进入 sigwait 状态"和"临时调整 blocked mask"对投递方不是原子的 | 修复 sigwait_set 发布顺序 |
| 2 | CHANGES_REQUESTED | ZR233 | `SYS_exit_group` 在子进程中的信号行为不正确；线程定向投递 blocked/default-ignored signal 的 Linux 语义仍有缺口 | 修复 group_exit / SIGCHLD si_code 处理 |
| 3 | CHANGES_REQUESTED | ZR233 | `cargo test -p starry-signal` 中 `block_ignore_send_signal` 仍断言失败——`ThreadSignalManager::send_signal()` 线程级投递的 `is_ignore()` 早退问题未完全解决 | 修复线程级投递路径的 is_ignore 早退，更新 std 单测 |
| 4 | APPROVED | ZR233 | 本轮验证全部通过：cargo fmt、clippy、新增回归用例、std 单测 | 批准合并 |

---

## PR #504（JosephJoshua）：epoll LT 模式 drain 修复

**Bug 内容：** Deepseek-TUI 在未接收到键盘、鼠标等硬件中断时不会自动刷新 UI。根因是 LT 模式下 `epoll_wait(maxevents=N)` 对一次就绪的 fd 返回 N 份完全相同的副本而非 1 份，且存在并发 wake 丢失窗口，导致事件循环无法正常工作。

涉及 `weston / cage / libuv / Go netpoll` 等所有基于 `epoll_wait(maxevents>1)` 的事件循环。

**根因分析：** `Epoll::poll_events` 循环中存在两个问题：

1. **重复事件**：`pop_front` 取出 `interest`→写入 `out[count]`→`push_back` 回 `ready_queue`，下一轮 `pop_front` 拿到的还是同一个 `interest`。直到 `count` 填满 `out`——一次就绪的 fd 返回 `maxevents` 份副本。

2. **Missed-wake 窗口**：`NoEvent` 分支只调用 `register_waker_only`，在 `consume()` 检查与 `register()` 之间到达的事件会落入旧 waker（看到 `in_ready_queue=true` 后放弃），被永久吞掉。

**修复方法：** 对齐 Linux `fs/eventpoll.c` 的 `ep_send_events`：

```rust
// 用 mem::take 一次性将整个 ready_queue 挪到本地 txlist
let mut txlist = mem::take(&mut self.ready_queue);

// 循环中每个 interest 只访问一次
for interest in txlist.iter_mut() { ... }

// LT 保留项放进 keep 队列
// 循环结束 splice 回 ready_queue，并显式 poll_ready.wake()
```

1. **`mem::take` 整体 drain**：将整个 `ready_queue` 一次性挪到本地 `txlist`，每个 `interest` 在循环中只访问一次。LT 保留的 `interest` 放进 `keep` 队列。
2. **循环后 splice 回填**：循环结束将 `keep` 整体 `splice` 回 `ready_queue`，并显式 `poll_ready.wake()`，确保期间被 fold 走的事件能唤醒并发 waiter。
3. **`check_and_register_waker`**：`NoEvent` 分支改用 `check_and_register_waker`，注册新 waker 后再读一次 `file.poll()`。匹配新事件就立刻 wake，否则 `consume()` 检查与 `register()` 之间到达的事件会落入旧 waker 被吞掉。
4. **并发 wake 隔离**：并发 wake 路径只往 `ready_queue` 里塞，不与 `txlist` 冲突。

**测试：** `test-epoll-lt`（4 架构 toml）：
- AF_UNIX `listen` + `EPOLL_CTL_ADD`，子进程 `connect+write`，父进程 `epoll_wait(maxevents=4)` 断言 `n == 1`。
- 修复前在 `listen_epoll_fires_once` 和 `client_data_epoll_fires_once` 上 FAIL，修复后 11/11 PASS。

**Review 迭代（3 commits）：**

| 轮次 | Review 类型 | 提交者 | 内容 | 处理 |
|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | ZR233 | txlist drain/requeue 期间仍有并发 waiter missed-wake 窗口，需要补一个唤醒或等价保证 | 第 3 个 commit 修复：增加 check_and_register_waker + splice 后 poll_ready.wake() |
| 2 | APPROVED | ZR233 | 本地阅读了 epoll 改动，未发现阻塞问题。`poll_events()` 改为先整体取走 ready queue、单轮消费后再回填需要保留的 interest，避免了 LT 模式下同一个 ready fd 在一次 `epoll_wait(maxevents > 1)` 中重复灌满返回数组。新增 `test-epoll-lt` 在本地通过 | 批准合并 |

---

## 总结

| PR | 作者 | 子系统 | 解决的问题 |
|---|---|---|---|
| #485 | CharlieVinnie | axnet-ng TCP | Deepseek-TUI 测试集卡死——TCP loopback send 缺少 poll_interfaces |
| #500 | CharlieVinnie | 进程管理 | zombie PID 上 getsid/getpgid/getpriority 返回 ESRCH；kill 权限检查缺少凭据快照 |
| #502 | CharlieVinnie | TTY | raw mode VMIN=0 下 poll 一直报告 POLLIN，TUI 忙等不休眠（UI 刷新问题的尝试修复） |
| #529 | CharlieVinnie | axnet-ng UDP | UDP recv 未连接时返回 ENOTCONN；sendto loopback 不触发 epoll 唤醒（UI 刷新问题的尝试修复） |
| #535 | CharlieVinnie | signal | sigwaitinfo 在等待被阻塞信号时永久挂起（UI 刷新问题的尝试修复） |
| #504 | JosephJoshua | epoll | **最终解决 UI 不刷新问题**——LT 模式重复事件 + missed-wake 窗口 |

---

## 最终运行方式

将所有修复合入后，以下流程可在 StarryOS 上运行 Deepseek-TUI。

1. Clone tgoskits 仓库到工作目录
2. 以 Alpine Docker 容器为编译环境，挂载工作目录，在容器内安装编译依赖后 clone 并静态编译 Deepseek-TUI
3. 用 `debugfs` 将编译好的 `deepseek-tui` 和 `deepseek-tui-tests` 二进制写入 StarryOS 各架构的 rootfs 镜像中
4. 编译 StarryOS 内核（`cargo xtask starry build --arch x86_64`）
5. 用 QEMU 启动 StarryOS，挂载注入后的 rootfs 镜像
6. 进入 StarryOS shell，直接运行 `deepseek-tui` 或 `deepseek-tui-tests runtime_api` 验证修复
7. 设置 API key 环境变量后，可以使用 `deepseek-tui` 来编写简单的 Hello world 程序，并在 deepseek-tui 界面中直接运行