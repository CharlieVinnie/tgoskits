# Lab 1: ANSI 彩色输出

## 任务目标

为 "Hello, Arceos!" 输出添加 ANSI 彩色效果。要求输出中包含 "Hello, Arceos!"，且颜色通过 ANSI SGR 转义序列实现（如 `\x1b[32m`），在四个架构上均通过。

## 设计思路

实验只需修改一行代码，在 `println!` 输出字符串中嵌入 ANSI 转义序列。测试脚本验证的是串口输出的原始字节，ANSI 序列会原样出现，不需要考虑终端渲染兼容性。

## 实现过程

原始代码：

```rust
println!("[WithColor]: Hello, Arceos!");
```

修改后：

```rust
println!("[WithColor]: \x1b[32mHello, Arceos!\x1b[0m");
```

- `\x1b[32m` — 绿色前景
- `\x1b[0m` — 重置所有属性（避免后续输出也被染色）

测试：

```bash
bash scripts/test.sh
cargo xtask run --arch riscv64
cargo xtask run --arch x86_64
cargo xtask run --arch aarch64
cargo xtask run --arch loongarch64
```

测试脚本检查两件事：
1. 输出是否包含 "Hello, Arceos!"
2. 输出是否包含 ANSI SGR 序列（正则匹配 `\\x1b\[[0-9;]*m`）

## 测试结果

```
riscv64     ✓
x86_64      ✓
aarch64     ✓
loongarch64 ✓
```
