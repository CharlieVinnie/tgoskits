import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------
mcp = FastMCP("TGOSKits-Test-Runner")

logger = logging.getLogger("tgoskits-test")
logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WORKSPACE_ROOT = Path(os.environ.get(
    "TGOSKITS_WORKSPACE",
    "/home/charlie/biglabB/tgoskits",
))

RUN_LOG = WORKSPACE_ROOT / "run.log"

# Timeout (seconds) that starts AFTER "root@starry" appears in the output.
EXEC_TIMEOUT = 20

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ARCH_MAP = {
    "x86_64":      "x86_64-unknown-none",
    "aarch64":     "aarch64-unknown-none-softfloat",
    "riscv64":     "riscv64gc-unknown-none-elf",
    "loongarch64": "loongarch64-unknown-none-softfloat",
}


def _open_log(mode: str = "w"):
    """Open RUN_LOG for appending (creates parent dirs if needed)."""
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    return open(RUN_LOG, mode, buffering=1)  # line-buffered


def _log(fh, msg: str) -> None:
    """Write a timestamped line to the log file and to the MCP logger."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    fh.write(line + "\n")
    logger.info("%s", msg)



# ---------------------------------------------------------------------------
# Tool 1 — Docker / QEMU test runner
# ---------------------------------------------------------------------------

@mcp.tool()
def run_tgoskits_test(test_name: str, arch: str = "x86_64") -> str:
    """
    Run a kernel test inside the tgoskits Docker container.

    $WORKSPACE_ROOT is /home/charlie/biglabB/tgoskits (overridable via the
    TGOSKITS_WORKSPACE environment variable).

    All output is streamed line-by-line to $WORKSPACE_ROOT/run.log in real time.
    The execution timeout is hardcoded to 20 seconds and starts only AFTER the
    substring "root@starry" appears in the output (i.e. after the OS shell is
    ready). Compilation time is not counted.

    The return value contains the process exit code and everything printed after
    "root@starry" appeared. No pattern matching or verdict parsing is performed.

    Args:
        test_name: The test case name passed to `cargo starry test qemu -c`.
                   MUST be a plain name with NO slash characters — just the
                   final directory name of the test case.
                   Example: 'rust-hello'  (NOT 'qemu-smp1/rust-hello')
        arch: One of: x86_64 (default), aarch64, riscv64, loongarch64.
    """
    target = ARCH_MAP.get(arch)
    if target is None:
        return f"Invalid arch '{arch}'. Must be one of: {', '.join(ARCH_MAP)}"

    command = [
        "docker", "run", "--rm", "-t",
        "-v", f"{WORKSPACE_ROOT}:/workspace",
        "-v", "tgoskits-cargo-registry:/opt/cargo/registry",
        "-v", "tgoskits-cargo-git:/opt/cargo/git",
        "-v", "tgoskits-cargo-bin:/opt/cargo/bin",
        "-v", "tgoskits-rustup:/opt/rustup",
        "-v", "tgoskits-build-target:/workspace/target",
        "-w", "/workspace",
        "-e", "RUSTUP_HOME=/opt/rustup",
        "-e", "CARGO_HOME=/opt/cargo",
        "tgoskits",
        "cargo", "starry", "test", "qemu", "-t", target, "-c", test_name,
    ]

    EXEC_MARKER = "root@starry"
    lines: list[str] = []
    exec_started = False
    exec_start_time: float = 0.0
    timed_out = False

    with _open_log() as fh:
        _log(fh, f"=== run_tgoskits_test: test={test_name!r} arch={arch} exec_timeout={EXEC_TIMEOUT}s ===")
        _log(fh, f"CMD: {' '.join(command)}")

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None

            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                _log(fh, line)
                lines.append(line)

                if not exec_started and EXEC_MARKER in line:
                    exec_started = True
                    exec_start_time = time.monotonic()
                    _log(fh, f">> Execution marker seen — {EXEC_TIMEOUT}s timeout started")

                if exec_started:
                    elapsed = time.monotonic() - exec_start_time
                    if elapsed > EXEC_TIMEOUT:
                        timed_out = True
                        break

            if timed_out:
                proc.kill()
                _ = proc.wait()
                msg = f"TIMEOUT: test '{test_name}' on {arch} exceeded {EXEC_TIMEOUT}s after execution started."
                _log(fh, msg)
                return msg

            _ = proc.wait()
            ret_code = proc.returncode
            _log(fh, f"=== process exited: rc={ret_code} ===")

        except Exception as exc:
            msg = f"System Error executing Docker command: {exc}"
            _log(fh, msg)
            return msg

    output = "\n".join(lines)

    extracted = (
        output.split(EXEC_MARKER, 1)[1].strip()
        if EXEC_MARKER in output
        else "(root@starry marker never appeared — no shell output)"
    )
    return f"Return Value: {ret_code}\n\nOutput:\n{extracted}"


# ---------------------------------------------------------------------------
# Tool 2 — Native Linux host runner
# ---------------------------------------------------------------------------

@mcp.tool()
def run_tgoskits_test_on_linux_host(
    test_path: str,
    timeout: int = 30,
    strace_syscalls: list[str] | None = None,
) -> str:
    """
    Build and run a tgoskits test natively on the Linux host (no QEMU/Docker).

    $WORKSPACE_ROOT is /home/charlie/biglabB/tgoskits (overridable via the
    TGOSKITS_WORKSPACE environment variable).

    The test directory is given as a path relative to $WORKSPACE_ROOT. It is
    copied under /tmp before building so the source tree is never modified.

    Build strategy (checked in order):
      - If a 'c/' subdirectory exists  → cmake + make, then run the binary.
      - If a 'rust/' subdirectory exists → cargo run inside that directory.

    All output (compile + run) is appended to $WORKSPACE_ROOT/run.log.

    Args:
        test_path:        Path to the test directory, relative to $WORKSPACE_ROOT.
                          Example: 'test-suit/starryos/normal/test-1plus2'
        timeout:          Execution timeout in seconds (default 30). Applies to the
                          run step only; compilation has its own 120 s hard limit.
        strace_syscalls:  Optional list of syscall names to trace with strace.
                          When provided, the binary is run under:
                            strace -e trace=<syscall1>,<syscall2>,...
                          strace must be installed on the host.
                          Example: ["read", "write", "openat"]
    """
    import platform

    if platform.system() != "Linux":
        return f"REJECTED: This tool only runs on Linux. Detected: {platform.system()}"

    src_dir = WORKSPACE_ROOT / test_path
    if not src_dir.exists():
        return f"REJECTED: Path does not exist: {src_dir}"
    if not src_dir.is_dir():
        return f"REJECTED: Path is not a directory: {src_dir}"

    c_dir    = src_dir / "c"
    rust_dir = src_dir / "rust"

    has_c    = c_dir.exists()    and c_dir.is_dir()
    has_rust = rust_dir.exists() and rust_dir.is_dir()

    if not has_c and not has_rust:
        return f"REJECTED: No 'c/' or 'rust/' subdirectory found in {src_dir}"

    # Copy the test directory to /tmp to avoid polluting the source tree.
    safe_name = test_path.replace("/", "-").strip("-")
    tmp_root  = Path("/tmp") / f"tgoskits-{safe_name}-{int(time.time())}"
    shutil.copytree(src_dir, tmp_root)

    with _open_log() as fh:
        _log(fh, f"=== run_tgoskits_test_on_linux_host: test_path={test_path!r} timeout={timeout}s strace={strace_syscalls} ===")
        _log(fh, f"Copied {src_dir} → {tmp_root}")

        try:
            if has_c:
                result = _build_and_run_c(tmp_root / "c", safe_name, timeout, fh, strace_syscalls)
            else:
                result = _build_and_run_rust(tmp_root / "rust", timeout, fh, strace_syscalls)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
            _log(fh, f"Cleaned up {tmp_root}")

    return result


def _run_cmd(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    fh,
    label: str,
) -> tuple[int, str]:
    """Run a command, stream output to log, return (returncode, combined_output)."""
    _log(fh, f"[{label}] CMD: {' '.join(cmd)}")
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None

        deadline = time.monotonic() + timeout
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            _log(fh, f"[{label}] {line}")
            lines.append(line)
            if time.monotonic() > deadline:
                proc.kill()
                _ = proc.wait()
                _log(fh, f"[{label}] TIMEOUT after {timeout}s")
                return -1, "\n".join(lines)

        _ = proc.wait()
        rc = proc.returncode
        _log(fh, f"[{label}] exited rc={rc}")
        return rc, "\n".join(lines)

    except Exception as exc:
        msg = f"System Error: {exc}"
        _log(fh, f"[{label}] {msg}")
        return -1, msg


def _wrap_strace(cmd: list[str], syscalls: list[str] | None) -> list[str]:
    """Prepend strace to a command if syscalls are specified."""
    if not syscalls:
        return cmd
    return ["strace", "-e", f"trace={','.join(syscalls)}"] + cmd


def _build_and_run_c(c_dir: Path, binary_name: str, run_timeout: int, fh, strace_syscalls: list[str] | None = None) -> str:
    """cmake + make + run for a C test."""
    build_dir = c_dir / "build"
    build_dir.mkdir(exist_ok=True)

    # cmake configure
    rc, out = _run_cmd(
        ["cmake", ".."],
        cwd=build_dir,
        timeout=120,
        fh=fh,
        label="cmake",
    )
    if rc != 0:
        return f"cmake failed (exit {rc}):\n{out}"

    # make
    rc, out = _run_cmd(
        ["make", "-j4"],
        cwd=build_dir,
        timeout=120,
        fh=fh,
        label="make",
    )
    if rc != 0:
        return f"make failed (exit {rc}):\n{out}"

    # Locate the produced binary: prefer an executable named after the test,
    # otherwise pick the first executable file in the build dir.
    binary = build_dir / binary_name
    if not binary.exists():
        candidates = [
            p for p in build_dir.iterdir()
            if p.is_file() and os.access(p, os.X_OK) and not p.name.endswith(".so")
        ]
        if not candidates:
            return f"Build succeeded but no executable found in {build_dir}"
        binary = candidates[0]

    _log(fh, f"[run] binary={binary}")
    run_cmd = _wrap_strace([str(binary)], strace_syscalls)
    rc, out = _run_cmd(run_cmd, cwd=build_dir, timeout=run_timeout, fh=fh, label="run")
    if rc == -1:
        return f"TIMEOUT: test exceeded {run_timeout}s.\n\nOutput:\n{out}"
    if rc == 0:
        return f"Return Value: 0 | Result: Tests passed\n\nOutput:\n{out}"
    return f"Return Value: {rc} | Result: Tests failed (exit code {rc})\n\nOutput:\n{out}"


def _build_and_run_rust(rust_dir: Path, run_timeout: int, fh, strace_syscalls: list[str] | None = None) -> str:
    """cargo run for a Rust test."""
    if strace_syscalls:
        # Build first, then run the binary under strace.
        rc, out = _run_cmd(
            ["cargo", "build"],
            cwd=rust_dir,
            timeout=120,
            fh=fh,
            label="cargo-build",
        )
        if rc != 0:
            return f"cargo build failed (exit {rc}):\n{out}"

        # Find the produced binary in target/debug/
        debug_dir = rust_dir / "target" / "debug"
        candidates = [
            p for p in debug_dir.iterdir()
            if p.is_file() and os.access(p, os.X_OK) and not p.name.startswith(".")
        ]
        if not candidates:
            return f"cargo build succeeded but no executable found in {debug_dir}"
        binary = candidates[0]

        run_cmd = _wrap_strace([str(binary)], strace_syscalls)
        rc, out = _run_cmd(run_cmd, cwd=rust_dir, timeout=run_timeout, fh=fh, label="run")
    else:
        rc, out = _run_cmd(
            ["cargo", "run"],
            cwd=rust_dir,
            timeout=run_timeout,
            fh=fh,
            label="cargo-run",
        )

    if rc == -1:
        return f"TIMEOUT: test exceeded {run_timeout}s.\n\nOutput:\n{out}"
    if rc == 0:
        return f"Return Value: 0 | Result: Tests passed\n\nOutput:\n{out}"
    return f"Return Value: {rc} | Result: Tests failed (exit code {rc})\n\nOutput:\n{out}"


if __name__ == "__main__":
    mcp.run()
