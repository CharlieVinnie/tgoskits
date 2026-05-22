# Lab 3: Bump Allocator（碰撞分配器）

## 任务目标

在 `modules/bump_allocator/src/lib.rs` 中实现 `EarlyAllocator`，完成 `BaseAllocator`、`ByteAllocator` 和 `PageAllocator` 三个 trait 的全部方法。该分配器用于 ArceOS 内核启动早期、正式分配器就绪前的临时内存管理。

## 设计

采用 double-ended bump 策略，在单个连续内存区域上操作：

```
低地址                    b_pos     p_pos                 高地址
├────────────────────────┬──────────┬──────────────────────┤
│    已分配的字节         │  空闲区   │    已分配的页         │
│  (bytes grow forward)  │          │  (pages grow backward)│
start                    b_pos      p_pos                  end
```

- 字节分配从 `b_pos` 向上（高地址）增长
- 页分配从 `p_pos` 向下（低地址）增长
- 字节释放用引用计数控制：引用计数归零时 `b_pos` 重置回 `start`
- 页分配永不释放

## 实现过程

### 数据结构定义

```rust
pub struct EarlyAllocator<const PAGE_SIZE: usize> {
    start: usize,
    end: usize,
    b_pos: usize,
    p_pos: usize,
    count: usize,
}
```

### 辅助函数

```rust
const fn align_up(val: usize, align: usize) -> usize {
    (val + align - 1) & !(align - 1)
}

const fn align_down(val: usize, align: usize) -> usize {
    val & !(align - 1)
}
```

### BaseAllocator

`init` 将指针定位到区域两端：

```rust
fn init(&mut self, start: usize, size: usize) {
    self.start = start;
    self.end = start + size;
    self.b_pos = start;
    self.p_pos = start + size;
    self.count = 0;
}
```

`add_memory` 的设计经历了两轮修改（见下文"踩坑"）。

### ByteAllocator

字节分配在 `b_pos` 上做对齐后前移：

```rust
fn alloc(&mut self, layout: Layout) -> AllocResult<NonNull<u8>> {
    let alloc_start = align_up(self.b_pos, layout.align());
    let alloc_end = alloc_start.checked_add(layout.size())
        .ok_or(AllocError::NoMemory)?;
    if alloc_end > self.p_pos {
        return Err(AllocError::NoMemory);
    }
    self.b_pos = alloc_end;
    self.count += 1;
    Ok(unsafe { NonNull::new_unchecked(alloc_start as *mut u8) })
}
```

释放时递减引用计数，归零则重置：

```rust
fn dealloc(&mut self, _pos: NonNull<u8>, _layout: Layout) {
    self.count -= 1;
    if self.count == 0 {
        self.b_pos = self.start;
    }
}
```

### PageAllocator

页分配从 `p_pos` 向下：

```rust
fn alloc_pages(&mut self, num_pages: usize, align_pow2: usize) -> AllocResult<usize> {
    let size = num_pages * PAGE_SIZE;
    let alloc_end = self.p_pos;
    let alloc_start = align_down(alloc_end.checked_sub(size)
        .ok_or(AllocError::NoMemory)?, align_pow2);
    if alloc_start < self.b_pos {
        return Err(AllocError::NoMemory);
    }
    self.p_pos = alloc_start;
    Ok(alloc_start)
}
```

## 踩坑记录：add_memory 的跨架构行为差异

### 问题现象

在 riscv64 和 loongarch64 上一切正常，切到 x86_64 和 aarch64 时内核在启动早期发生 page fault。

### 原因分析

不同架构的空闲内存布局不同：

| 架构 | init 区域 | add_memory 调用次数 | add_memory 区域 |
|---|---|---|---|
| riscv64 | `.bss` 之后的大块连续区 | 0 次 | — |
| loongarch64 | 同上 | 0 次 | — |
| x86_64 | `0x100000 – 0x200000` | 1 次 | `0x272000 – 0x7fdf000` |
| aarch64 | 同上 | 1 次 | 与 x86_64 类似 |

x86_64 上两个空闲区域之间有内核代码段（`.text`、`.rodata`、`.data`）占据的 gap。如果 `add_memory` 简单地将 `[start, end)` 扩展到覆盖 gap，分配器可能返回内核代码段内的地址，写入时触发 page fault。

### 解决方案

`add_memory` 只接受严格相邻的新区域，不相邻的区域静默忽略：

```rust
fn add_memory(&mut self, start: usize, size: usize) -> AllocResult {
    let new_end = start + size;
    if start == self.end {
        self.end = new_end;
        self.p_pos += size;
    } else if new_end == self.start {
        self.start = start;
        self.b_pos = start + (self.b_pos - (start + size));
    }
    Ok(())
}
```

运行时总是用最大的空闲区域调用 `init`，后续 `add_memory` 带来的小片区域舍弃不影响功能，可用内存仍是 init 区域的大小。

## 测试结果

所有四个架构通过：

```
riscv64     ✓
x86_64      ✓
aarch64     ✓
loongarch64 ✓
```

## 学到的内容

1. **碰撞分配器的适用场景**：启动早期的内存分配模式是"分配多、释放少"，碰撞分配器在这种场景下效率极高（O(1) 分配），缺点是无法回收单个页分配。

2. **跨架构内存布局差异**：不同 CPU 架构的启动协议、MMU 页表布局、设备 MMIO 地址范围都不同，导致可用物理内存的分布方式完全不同。在 riscv64 上正确运行的代码可能在 x86_64 上触发 page fault，调试时不能只在一个架构上验证。

3. **接口契约的重要性**：`BaseAllocator::add_memory` 的语义是"添加可用内存"，但没有规定添加的区域必须连续。调用方可能多次调用，每次传入不同的碎片区域。实现者需要设计一个对任意调用模式都安全的策略，而不是假设调用方会按某种特定方式调用。

4. **优雅降级**：当新区域无法合并时返回 Error 会导致 panic，静默忽略则能继续运行。"可以做但没必要"的操作应返回 Ok 而不是 Err。
