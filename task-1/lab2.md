# Lab 2: HashMap 支持（基于 hashbrown）

## 任务目标

为 ArceOS 的 `axstd` 库添加 `HashMap` 和 `HashSet` 支持。ArceOS 运行在 `no_std` 环境下，`alloc::collections` 不包含 HashMap，需要借助第三方库提供该功能。

## 设计思路

选择 `hashbrown` crate（Rust 标准库 HashMap 的实际底层实现，基于 Google SwissTable 算法）作为依赖，将其 re-export 到 `axstd::collections` 模块下，使应用代码能以 `axstd::collections::HashMap` 方式使用，与标准库的 `std::collections::HashMap` 接口保持一致。

## 实现过程

### 1. Vendor 本地 axstd

由于需要修改 `axstd` 源码，先将其从 crates.io 复制到本地：

```bash
cargo clone axstd@0.3.0-preview.1
```

修改 `exercise-hashmap/Cargo.toml`，将依赖从版本号改为本地路径：

```toml
[dependencies]
axstd = { path = "./axstd", features = ["defplat"], optional = true }
```

### 2. 添加 hashbrown 依赖

在 `axstd/Cargo.toml` 中添加：

```toml
[dependencies.hashbrown]
version = "0.15"
default-features = false
features = ["inline-more", "default-hasher"]
```

`default-hasher` feature 会引入 `foldhash` crate，提供一个 `no_std` 兼容的默认哈希器。如果不启用该 feature，`HashMap::new()` 不可用，因为泛型参数 `S`（hasher）没有实现 `Default`。

### 3. 实现自定义 collections 模块

替换 `axstd/src/lib.rs` 中原本直接 re-export `alloc::collections` 的行，改为自定义模块：

```rust
pub use alloc::{boxed, format, string, vec};

pub mod collections {
    pub use alloc::collections::*;
    pub use hashbrown::{HashMap, HashSet};
}
```

这样 `axstd::collections` 既包含原有的 `BTreeMap`、`LinkedList`、`VecDeque`，也新增了 `HashMap` 和 `HashSet`。

## 踩坑记录：default-hasher feature

### 问题现象

添加 hashbrown 依赖后编译，`HashMap::new()` 报错找不到该方法。

### 原因分析

`HashMap<K, V, S>` 的第三个泛型参数 `S` 是哈希器类型，需要实现 `Default` 才能调用 `new()`。hashbrown 默认不包含 hasher 实现，需要用户自行指定（如 `HashMap::<K, V, RandomState>::default()`）。标准库隐藏了这个细节——它在 `std` 内部已经配置好了默认哈希器。

### 解决方案

启用 hashbrown 的 `default-hasher` feature，该 feature 引入 `foldhash` crate 提供 `DefaultHashBuilder`，使 `HashMap::new()` 直接可用。

## 学到的内容

1. **接口兼容性设计**：将 HashMap 放在 `axstd::collections` 下而非另起命名空间，让用户代码可无感知地从 `std` 切换到 `axstd`，只需修改 import 路径。

## 测试结果

```
riscv64     ✓
x86_64      ✓
aarch64     ✓
loongarch64 ✓
```
