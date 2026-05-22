# Lab 4: RAMFS Rename（内存文件系统重命名）

## 任务目标

为 ArceOS 的 RAMFS（内存文件系统）实现 `rename` 操作。需要修改两层代码：

1. **`axfs/src/root.rs`** — `RootDirectory` 层的 rename，负责将路径路由到正确的文件系统实例
2. **`axfs_ramfs/src/dir.rs`** — `DirNode` 层的 rename，实际执行目录项的重命名

## 设计思路

调用链：

```
std::fs::rename()
  → axfs::root::RootDirectory::rename()    // 路径路由
    → axfs_ramfs::dir::DirNode::rename()    // 实际文件操作
```

参考已有 `create` 和 `remove` 方法的模式。它们在两层都有对应的实现，结构清晰可复用。

## 实现过程

### 1. Vendor 本地 crate

将 `axfs` 和 `axfs_ramfs` 从 crates.io 复制到本地修改：

```bash
cargo clone axfs@0.3.0-preview.1
cargo clone axfs_ramfs@0.1.2
```

修改 `Cargo.toml` 中的依赖路径为本地路径。

### 2. RootDirectory::rename

`RootDirectory` 持有一个 `main_fs` 和一个挂载点列表。rename 需要标准化路径、找到源路径所属的文件系统、检查目标路径在同一个文件系统上、然后委托：

```rust
fn rename(&self, src_path: &str, dst_path: &str) -> VfsResult {
    let norm_src = self.normalize_path(src_path);
    let norm_dst = self.normalize_path(dst_path);
    if let Some((mount_fs, rest_src)) = self.find_best_mount(&norm_src) {
        // 源文件在挂载点上：目标必须在同一挂载点
        if let Some((_, rest_dst)) = self.find_best_mount(&norm_dst) {
            mount_fs.root_dir().rename(rest_src, rest_dst)
        } else {
            Err(axfs_vfs::VfsError::Unsupported)
        }
    } else {
        self.main_fs.root_dir().rename(&norm_src, &norm_dst)
    }
}
```

跨文件系统的 rename 在接口层面不支持（需要 copy + delete，但文件系统接口不提供此能力），直接返回 `Unsupported`。

### 3. DirNode::rename

使用已有 `split_path` 辅助函数分解路径：

```rust
let (src_name, src_rest) = split_path(src_path);
let (dst_name, dst_rest) = split_path(dst_path);
```

如果 `src_rest` 有值，说明需递归到子目录处理：

```rust
if let Some(src_rest) = src_rest {
    let subdir = match src_name {
        "" | "." => self.this.upgrade().ok_or(VfsError::NotFound)?,
        ".." => self.parent().ok_or(VfsError::NotFound)?,
        _ => self.children.read().get(src_name)
            .ok_or(VfsError::NotFound)?.clone(),
    };
    let new_dst = if let Some(dst_rest) = dst_rest {
        dst_rest
    } else {
        dst_name
    };
    return subdir.rename(src_rest, new_dst);
}
```

如果 `src_rest` 为 None，说明源和目标都在当前目录。在 children 的 BTreeMap 中删除旧 key 并插入新 key：

```rust
if let Some(_dst_rest) = dst_rest {
    return Err(VfsError::Unsupported); // 跨子目录 rename 不支持
}

let mut children = self.children.write();
let node = children.remove(src_name).ok_or(VfsError::NotFound)?;
children.insert(dst_name.into(), node);
Ok(())
```

同文件系统内但跨子目录的 rename（如 `dir1/file` → `dir2/file`）不在本练习支持范围内。

## 学到的内容

1. **系统调用与文件系统接口的区别**：Linux 的 `rename(2)` 支持跨目录操作，但底层的 RAMFS 只需要实现同目录 rename 即可满足练习要求——这是用户态 POSIX 接口和内核态文件系统接口之间的抽象层次差异。

## 测试结果

```
riscv64     ✓
x86_64      ✓
aarch64     ✓
loongarch64 ✓
```
