import os
import shutil
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("TGOSKits-Test-Creator")

WORKSPACE_ROOT = Path(os.environ.get(
    "TGOSKITS_WORKSPACE",
    "/home/charlie/biglabB/tgoskits",
))

# Templates are copied from these existing tests.
TEMPLATE_C    = WORKSPACE_ROOT / "test-suit/starryos/normal/qemu-smp1/test-vectored-io"
TEMPLATE_RUST = WORKSPACE_ROOT / "test-suit/starryos/normal/qemu-smp1/rust-hello"

# Default destination group — tests land under qemu-smp1/
DEFAULT_GROUP = WORKSPACE_ROOT / "test-suit/starryos/normal/qemu-smp1"

# Names used inside the template files that must be replaced with the new test name.
TEMPLATE_NAME_C    = "test-vectored-io"
TEMPLATE_NAME_RUST = "rust-hello"

# ── Stub source files written after the template main is cleared ──────────

STUB_MAIN_C = """\
#include <stdio.h>

int main(void) {
    // TODO: implement test logic
    printf("ALL TESTS PASSED\\n");
    return 0;
}
"""

STUB_MAIN_RS = """\
use std::{env, process};

// Embedded by the compiler from the file written by prebuild.sh.
// If prebuild.sh did not run, this will fail at compile time.
const PREBUILD_MARKER: &str = include_str!("prebuild_marker.txt");

fn main() {
    // TODO: implement test logic

    let marker = PREBUILD_MARKER.trim();
    assert_eq!(marker, "prebuild-ok", "prebuild marker mismatch: {marker:?}");

    println!("TEST PASSED");
}
"""


def _replace_in_file(path: Path, old: str, new: str) -> None:
    """Replace all occurrences of `old` with `new` in a text file."""
    text = path.read_text()
    if old in text:
        path.write_text(text.replace(old, new))


@mcp.tool()
def create_tgoskits_test(
    test_name: str,
    kind: str = "c",
) -> str:
    """
    Scaffold a new StarryOS test case under
    $WORKSPACE_ROOT/test-suit/starryos/normal/qemu-smp1/<test_name>.

    $WORKSPACE_ROOT is /home/charlie/biglabB/tgoskits (overridable via the
    TGOSKITS_WORKSPACE environment variable).

    The new test is created by copying an existing template test and replacing
    all occurrences of the template name with <test_name> in every file.
    The main source file (main.c or main.rs) is then replaced with a minimal
    stub so you start from a clean slate.

    Templates used:
      - kind="c"    → copied from qemu-smp1/test-vectored-io  (C + CMake)
      - kind="rust" → copied from qemu-smp1/rust-hello        (Rust + Cargo)

    Files created for kind="c":
      <test_name>/qemu-x86_64.toml
      <test_name>/qemu-aarch64.toml
      <test_name>/qemu-riscv64.toml
      <test_name>/qemu-loongarch64.toml
      <test_name>/c/CMakeLists.txt
      <test_name>/c/src/main.c          ← stub, edit this

    Files created for kind="rust":
      <test_name>/qemu-x86_64.toml
      <test_name>/qemu-aarch64.toml
      <test_name>/qemu-riscv64.toml
      <test_name>/qemu-loongarch64.toml
      <test_name>/rust/Cargo.toml
      <test_name>/rust/.gitignore
      <test_name>/rust/prebuild.sh
      <test_name>/rust/src/prebuild_marker.txt
      <test_name>/rust/src/main.rs      ← stub, edit this

    Args:
        test_name: Name of the new test (e.g. 'my-new-test'). Used as the
                   directory name and binary name. No slashes.
        kind:      'c' (default) or 'rust'.
    """
    if "/" in test_name or "\\" in test_name:
        return "REJECTED: test_name must not contain path separators."

    kind = kind.lower().strip()
    if kind not in ("c", "rust"):
        return f"REJECTED: kind must be 'c' or 'rust', got '{kind}'."

    dest = DEFAULT_GROUP / test_name
    if dest.exists():
        return f"REJECTED: '{dest}' already exists."

    if kind == "c":
        template      = TEMPLATE_C
        template_name = TEMPLATE_NAME_C
    else:
        template      = TEMPLATE_RUST
        template_name = TEMPLATE_NAME_RUST

    if not template.exists():
        return f"REJECTED: Template directory not found: {template}"

    # Copy the entire template tree, skipping the build artefact dirs.
    def _ignore(src: str, names: list[str]) -> list[str]:
        ignored = []
        for n in names:
            p = Path(src) / n
            # Skip Rust build output and any CMake build dirs
            if n in ("target", "build") and p.is_dir():
                ignored.append(n)
        return ignored

    shutil.copytree(template, dest, ignore=_ignore)

    # Replace the template name with the new test name in every text file.
    for path in sorted(dest.rglob("*")):
        if not path.is_file():
            continue
        try:
            _replace_in_file(path, template_name, test_name)
        except (UnicodeDecodeError, PermissionError):
            pass  # skip binary files

    # Clear the main source file to a clean stub.
    if kind == "c":
        stub_path = dest / "c" / "src" / "main.c"
        stub_path.write_text(STUB_MAIN_C)
    else:
        stub_path = dest / "rust" / "src" / "main.rs"
        stub_path.write_text(STUB_MAIN_RS)

    created = [
        str(p.relative_to(DEFAULT_GROUP))
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    ]
    return (
        f"Created {kind} test '{test_name}' with {len(created)} files:\n"
        + "\n".join(f"  {f}" for f in created)
        + f"\n\nEdit: {stub_path.relative_to(WORKSPACE_ROOT)}"
    )


if __name__ == "__main__":
    mcp.run()
