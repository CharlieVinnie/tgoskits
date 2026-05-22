# Tasks

## Task 1: Parse Syscalls and Read Man Pages

- [x] Parse the user's natural language message to extract syscall names
- [x] For each syscall, run `man 2 <syscall>` via shell to retrieve the full specification
- [x] Analyze each man page to identify: normal behavior and return values, error conditions (each errno and when it occurs), boundary values (zero-length, negative offsets, NULL pointers), and interactions with file types (pipe, socket, directory)
- [x] Present the identified edge cases to the user for review before proceeding

**Requirements:** 1.1, 1.2

## Task 2: Create Branch and Scaffold Comprehensive Test

- [x] Call `reset_branch_to_upstream("dev")` to sync with upstream
- [x] Call `create_agent_branch("agent-test-<syscall-or-group>", "dev")` to create the test branch
- [x] Call `create_tgoskits_test(test_name="test-<syscall-or-group>")` to scaffold the test directory
- [x] Write a comprehensive C test source into `<test-dir>/c/src/main.c` that exercises ALL identified edge cases in a single binary, using PASS/FAIL markers with errno values
- [x] Commit the test source on the `agent-test-*` branch

**Requirements:** 1.3, 5.1, 5.5

## Task 3: Run Comprehensive Test on Linux Host and StarryOS

- [x] Call `run_tgoskits_test_on_linux_host(test_name="test-<syscall-or-group>", full_output=True)` to establish the Linux baseline
- [x] Call `run_tgoskits_test(test_name="test-<syscall-or-group>", arch="riscv64", full_output=True)` to get the StarryOS result
- [x] If the Linux host test fails to compile, record as "build_failed" and include compiler output — stop here
- [x] If the StarryOS test fails to compile or crashes, record accordingly

**Requirements:** 1.4, 1.7, 10.1, 10.2, 10.3

## Task 4: Compare Results and Generate Bug Reports

- [x] Parse both outputs for PASS/FAIL markers and errno values
- [x] For each check that passes on Linux but fails on StarryOS, create a Bug Report entry with: test name, architecture, syscall, expected behavior, observed behavior, exit code, and log excerpt
- [x] For checks that fail on both with the same exit code, classify as "shared failure" and exclude
- [x] If StarryOS produces no recognizable output, classify as "crash"
- [x] List each individual failing check with file, line number, message, and errno
- [x] Present the bug report summary to the user

**Requirements:** 1.5, 1.6, 2.1, 2.2, 2.3, 9.1 (state: discovered)

## Task 5: Create Targeted Reproduction Tests for Each Bug

For each distinct bug identified in Task 4:

- [x] Call `reset_branch_to_upstream("dev")`
- [x] Call `create_agent_branch("agent-bugfix-<syscall>-<short-description>", "dev")`
- [x] Call `create_tgoskits_test(test_name="bug-<syscall>-<short-description>")` to scaffold the reproduction test
- [x] Write a focused C test that reproduces only the specific failing edge case
- [x] Call `run_tgoskits_test(test_name="bug-<syscall>-<short-description>", arch="riscv64", full_output=True)` to confirm the test fails on StarryOS
- [x] If the test passes unexpectedly, flag as inconclusive and request manual review
- [x] Commit the reproduction test on the `agent-bugfix-*` branch

**Requirements:** 3.1, 3.2, 3.3, 3.4, 5.1, 5.2, 8.1, 9.1 (state: reproducing → reproduced)

## Task 6: Root Cause Analysis

For each reproduced bug:

- [x] Parse the reproduction test source for syscall invocations to identify the primary syscall
- [x] Map the failing syscall to the corresponding StarryOS kernel module or handler
- [x] Compare observed errno against expected errno and document the mismatch
- [x] If the semantics are ambiguous, invoke the `linux-standard-checker` agent with a specific yes/no question (e.g., "Should mmap return ENOMEM when length is zero?")
- [x] If linux-standard-checker returns INCONCLUSIVE, document the ambiguity and assume StarryOS behavior is wrong unless proven otherwise
- [x] Present the root cause analysis to the user

**Requirements:** 4.1, 4.2, 4.3, 4.4, 9.1 (state: analyzing)

## Task 7: Implement Fix and Verify on riscv64

- [x] Switch to the `agent-bugfix-<name>` branch (already created in Task 5)
- [x] Implement the fix in the StarryOS kernel source
- [x] Commit via `run_git_command(["commit", "-am", "<message>"])` with bug name, syscall, and reproduction test reference in the commit message
- [x] Call `run_tgoskits_test(test_name="bug-<syscall>-<desc>", arch="riscv64", full_output=True)` to verify the fix
- [x] If the test still fails, report fix as incomplete and provide updated test output — iterate on the fix
- [x] If the test passes, proceed to Task 8

**Requirements:** 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 9.1 (state: fixing → verifying)

## Task 8: Verify Fix on Remaining Architectures

Only after riscv64 passes:

- [ ] Call `run_tgoskits_test(test_name="bug-<syscall>-<desc>", arch="x86_64")` 
- [x] Call `run_tgoskits_test(test_name="bug-<syscall>-<desc>", arch="aarch64")`
- [x] Call `run_tgoskits_test(test_name="bug-<syscall>-<desc>", arch="loongarch64")`
- [x] Record per-architecture pass/fail status in the Bug Report
- [x] If any architecture fails, note it as an arch-specific issue in the Bug Report

**Requirements:** 8.1, 8.2, 8.3

## Task 9: Commit Cleanup and Branch Merging

After fix verification passes on all target architectures:

- [x] On the `agent-bugfix-*` branch, inspect the commit log relative to `dev` (e.g., `run_git_command(["log", "--oneline", "dev..HEAD"])`)
- [x] If there are more than two commits, use interactive rebase via `run_git_command` to squash them into exactly two: one commit for the reproduction test case, one commit for the OS fix
- [ ] Verify no work-in-progress, fixup, or debugging commits remain on the branch
- [ ] Switch to the `agent-test-*` branch and merge the cleaned-up `agent-bugfix-*` branch via `run_git_command(["merge", "agent-bugfix-<name>"])`
- [ ] Repeat for each bug's `agent-bugfix-*` branch
- [ ] Verify the `agent-test-*` branch has `1 + N` commits: 1 comprehensive test commit + N merge commits

**Requirements:** 11.1, 11.2, 11.3, 11.4

## Task 10: Rebase and Generate PR Message

- [x] Call `run_git_command(["fetch", "origin"])` then `run_git_command(["rebase", "upstream/dev"])` to incorporate latest upstream changes
- [x] If rebase conflicts occur, report to user for manual resolution
- [x] Generate a PR message following the structure in `tgoskits/tmp/pr/example-lseek-pipe-espipe.md` with sections: Title, Summary, Root Cause, Fix, Testing, and Architecture Notes (if applicable)
- [x] Write the PR message to `tgoskits/tmp/pr/<bug-name>.md`
- [x] Present the PR message to the user

**Requirements:** 5.6, 7.1, 7.2, 7.3, 9.1 (state: resolved)

