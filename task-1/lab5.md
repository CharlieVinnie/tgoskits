# Lab 5: SYS_MMAP 系统调用

## 任务目标

在 ArceOS 的 Linux 兼容层中实现 `SYS_MMAP` 系统调用。`mmap` 用于将文件或设备映射到进程地址空间，也用于匿名内存分配。

## 设计思路

ArceOS 没有完整的 VMA（虚拟内存区域）机制，无法像 Linux 那样做延迟分配。实现简化如下：

- 用原子 bump allocator 分配虚拟地址
- 直接将页映射到用户地址空间（`map_alloc`）
- 文件映射直接用 `read_at` 读入映射区域
- 权限通过 `MmapProt` → `MappingFlags` 转换

## 实现过程

### mmap 参数

```c
void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset);
```

- `prot`：保护位（PROT_READ、PROT_WRITE、PROT_EXEC）
- `flags`：MAP_SHARED、MAP_PRIVATE、MAP_ANONYMOUS
- `fd`：文件描述符（匿名映射传 -1）

### 代码实现

```rust
fn sys_mmap(
    _addr: *mut c_void,
    length: usize,
    prot: i32,
    flags: i32,
    fd: i32,
    offset: isize,
) -> isize {
    // 1. 解析权限和标志
    let prot = MmapProt::from_bits_truncate(prot);
    let flags = MmapFlags::from_bits_truncate(flags);
    let map_flags: MappingFlags = prot.into();

    // 2. 页对齐
    let aligned_len = (length + 0xFFF) & !0xFFF;

    // 3. 用 bump allocator 分配虚拟地址
    static MMAP_BASE: AtomicUsize = AtomicUsize::new(0x2000_0000);
    let map_addr = MMAP_BASE.fetch_add(aligned_len, Ordering::Relaxed);

    // 4. 获取用户地址空间
    let uspace_guard = crate::USER_ASPACE.lock();
    let uspace_arc = match uspace_guard.as_ref() {
        Some(a) => a.clone(),
        None => return neg_errno(LinuxError::ENOMEM),
    };
    drop(uspace_guard);

    // 5. 映射内存
    let mut uspace = uspace_arc.lock();
    if uspace.map_alloc(va!(map_addr), aligned_len, map_flags, true).is_err() {
        return neg_errno(LinuxError::ENOMEM);
    }

    // 6. 文件映射：读取内容到映射区域
    if !flags.contains(MmapFlags::MAP_ANONYMOUS) && fd >= 0 {
        let mut buf = alloc::vec![0u8; length];
        let read_result = with_file_fd(fd, |file| {
            file.read_at(offset as u64, &mut buf)
                .map_err(|e| LinuxError::from(e))
        });
        match read_result {
            Ok(n) => {
                if uspace.write(va!(map_addr), &buf[..n]).is_err() {
                    return neg_errno(LinuxError::EFAULT);
                }
            }
            Err(e) => return neg_errno(e),
        }
    }

    map_addr as isize
}
```

## 踩坑记录

### 1. 死锁：uspace_guard 未及时释放

首次运行 riscv64 时 mmap 返回 ENOMEM。

原因：获取 `USER_ASPACE` 的 `uspace_guard`（Mutex 锁守卫）后未 drop，就尝试 lock 从它 clone 出的 `uspace_arc`，导致死锁。`uspace_guard` 持有了 `USER_ASPACE` 的锁，而 `uspace_arc.lock()` 尝试获取同一个 `AxTaskSpace` 的内部锁——在单线程环境下，第二次 lock 永远不会成功。

修复：在 `uspace_arc.lock()` 之前显式 `drop(uspace_guard)`。

### 2. musl 交叉编译工具链安装

该实验需要 musl 交叉编译工具链构建 C 语言 payload：

```bash
wget https://musl.cc/riscv64-linux-musl-cross.tgz
wget https://musl.cc/aarch64-linux-musl-cross.tgz
wget https://musl.cc/x86_64-linux-musl-cross.tgz
# loongarch64 从 github.com/loong64/cross-tools 下载
```

遇到的问题：
- x86_64 musl 工具链不包含 `strip` 命令，需额外安装
- loongarch64 工具链 triple 为 `loongarch64-unknown-linux-musl`，但 xtask 期望 `loongarch64-linux-musl-gcc`，需创建符号链接

## 学到的内容

1. **锁的作用域管理**：`lock()` 返回的守卫在离开作用域前不会释放锁。显式 `drop` 是良好的习惯，避免意外的锁持有。

2. **AtomicUsize bump 的 Relaxed ordering 已足够**：`fetch_add` 本身是原子操作，mmap 不依赖其他内存操作的 happens-before 关系，Relaxed 在语义上是正确的。

## 测试结果

```
riscv64     ✓
x86_64      ✓
aarch64     ✓
loongarch64 ✓
```
