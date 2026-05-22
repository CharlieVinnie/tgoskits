# Requirements Document

## Introduction

This document defines the requirements for an end-to-end bug discovery and fix workflow for StarryOS. The workflow covers the full lifecycle: discovering bugs through Linux-compatible test suites and compatibility testing, reproducing them with minimal test cases, performing root cause analysis, implementing fixes, and verifying correctness. The workflow integrates with existing MCP tools (create_tgoskits_test, run_tgoskits_test, git_guard), the linux-compatible-testsuit framework, and Kiro skills to provide a systematic, repeatable process.

## Glossary

- **Test_Runner**: The Test_Executor's dual-mode capability (QEMU guest and Linux host) used to execute test suites against both StarryOS and the Linux_Baseline, producing structured marker output (`@@@ STARRY_TEST_*`) or regex-matched verdicts.
- **Compatibility_Analyzer**: A logical role performed by Kiro that compares StarryOS test results against Linux reference baselines to identify behavioral divergences. Not a separate tool or subagent.
- **Bug_Report**: A structured record containing the failing test name, observed vs expected behavior, architecture, syscall involved, and reproduction steps.
- **Test_Scaffold**: The `create_tgoskits_test` MCP tool that generates a new StarryOS test case directory with QEMU configs, CMakeLists.txt, prebuild script, and stub C source.
- **Test_Executor**: The `run_tgoskits_test` and `run_tgoskits_test_on_linux_host` MCP tools. The former runs a kernel test inside the tgoskits Docker container via QEMU for StarryOS testing; the latter compiles and runs the same test natively on the Linux host with gcc for quick smoke-testing and Linux_Baseline generation. Both return parsed results using success_regex/fail_regex from the test's QEMU toml config.
- **Linux_Standard_Checker**: The `linux-standard-checker` agent (`.kiro/agents/linux-standard-checker.md`) that answers yes/no questions about Linux standards by checking man pages, POSIX specs, and Linux Foundation refspecs. Used during root cause analysis to verify whether StarryOS behavior diverges from the documented standard.
- **Git_Guard**: The MCP tool that enforces branch naming conventions (agent-prefixed branches) for safe automated commits.
- **Regression_Test**: A test case specifically created to reproduce a discovered bug and verify that a fix resolves it without introducing new failures.
- **Workflow_Orchestrator**: A logical role performed by Kiro that coordinates the phases of bug discovery, reproduction, analysis, fix, and verification. Not a separate tool or subagent.
- **StarryOS**: The target unikernel/OS under test, built via the tgoskits workspace using Cargo.
- **Linux_Baseline**: The reference test results obtained by running the same test binaries on a standard Linux kernel, used as the ground truth for expected behavior.

## Requirements

### Requirement 1: User-Directed Bug Discovery via Man-Page-Driven Test Generation

**User Story:** As a StarryOS developer, I want to specify syscalls I care about and have the agent read the man pages to identify edge cases and generate strong tests, so that I can discover bugs through targeted, specification-driven testing rather than running a pre-existing suite.

#### Acceptance Criteria

1. WHEN the user specifies one or more syscalls to test via natural language in chat (e.g., "test mmap and brk"), THE Workflow_Orchestrator SHALL parse the syscall names from the user's message and read the man page for each syscall (via `man <syscall>`) to extract the full specification.
2. THE Workflow_Orchestrator SHALL analyze the retrieved specification to identify edge cases, error conditions, boundary values, and documented behavioral contracts for each syscall.
3. FOR the specified syscall (or group of related syscalls), THE Test_Scaffold SHALL generate a SINGLE comprehensive tgoskits test case named `test-<syscall-or-group>` that exercises ALL identified edge cases within one test binary. THE Git_Guard SHALL reset `dev` to `upstream/dev` and then create a branch named `agent-test-<syscall-or-group>` from it to hold this test.
4. WHEN the comprehensive test is created, THE Test_Executor SHALL run it on both the Linux host (to establish the Linux_Baseline) and on StarryOS via QEMU (riscv64 first).
5. WHEN the comprehensive test passes on Linux_Baseline but fails on StarryOS, THE Compatibility_Analyzer SHALL classify the divergence as a candidate bug and identify which specific edge case checks failed.
6. WHEN a test fails on both Linux_Baseline and StarryOS with the same exit code, THE Compatibility_Analyzer SHALL classify the result as a shared failure and exclude it from the candidate bug list.
7. IF the Test_Executor cannot compile a test for the target architecture, THEN THE Compatibility_Analyzer SHALL record the test as "build_failed" and include the compiler error output in the Bug_Report.

### Requirement 2: Structured Bug Report Generation

**User Story:** As a StarryOS developer, I want each discovered bug to be captured in a structured report, so that I can efficiently triage and prioritize fixes.

#### Acceptance Criteria

1. WHEN the Compatibility_Analyzer identifies a candidate bug, THE Workflow_Orchestrator SHALL generate a Bug_Report containing: test name, architecture, syscall under test, expected behavior (from Linux_Baseline), observed behavior (from StarryOS), exit code, and relevant log excerpts.
2. THE Bug_Report SHALL include the specific PASS/FAIL markers and errno values extracted from the test framework output (`PASS | file:line | msg` and `FAIL | file:line | msg | errno=N`).
3. IF multiple acceptance criteria within a single test fail, THEN THE Bug_Report SHALL list each individual failing check with its file, line number, and error description.

### Requirement 3: Targeted Bug Reproduction Test Creation

**User Story:** As a StarryOS developer, I want focused reproduction tests to be created only for the specific edge cases that failed in the comprehensive test, so that I can isolate each bug individually without generating unnecessary tests.

#### Acceptance Criteria

1. WHEN the comprehensive test from Requirement 1 reveals specific failing edge cases on StarryOS, THE Test_Scaffold SHALL create a targeted tgoskits test case for each distinct failure with the naming convention `bug-<syscall>-<short-description>`. THE Git_Guard SHALL create a branch named `agent-bugfix-<syscall>-<short-description>` from the `dev` branch to hold each reproduction test and its eventual fix.
2. THE Test_Scaffold SHALL generate QEMU config files for all supported architectures (x86_64, aarch64, riscv64, loongarch64) with appropriate success_regex and fail_regex patterns.
3. WHEN the reproduction test is created, THE Test_Executor SHALL run the test on riscv64 and confirm that the test fails on StarryOS, reproducing the original bug.
4. IF the reproduction test does not reproduce the bug (passes unexpectedly), THEN THE Workflow_Orchestrator SHALL flag the reproduction as inconclusive and request manual review.

### Requirement 4: Root Cause Analysis Support

**User Story:** As a StarryOS developer, I want guidance on narrowing down the root cause of a reproduced bug, so that I can implement a targeted fix.

#### Acceptance Criteria

1. WHEN a bug is successfully reproduced, THE Workflow_Orchestrator SHALL identify the primary syscall involved by parsing the test source code for syscall invocations.
2. THE Workflow_Orchestrator SHALL map the failing syscall to the corresponding StarryOS kernel module or handler based on the syscall number and architecture.
3. WHEN the failing test output includes errno values, THE Workflow_Orchestrator SHALL compare the observed errno against the expected errno and document the mismatch in the Bug_Report.
4. WHEN the root cause involves ambiguous syscall semantics, THE Workflow_Orchestrator SHALL invoke the Linux_Standard_Checker agent to answer specific yes/no questions about the expected behavior (e.g., "Should mmap return ENOMEM when length is zero?") before classifying the divergence as a bug.

### Requirement 5: Fix Implementation on Safe Branches

**User Story:** As a StarryOS developer, I want fixes to be implemented on isolated branches with proper naming, so that the main branch is protected from untested changes.

#### Acceptance Criteria

1. BEFORE creating any new branch, THE Git_Guard SHALL reset the `dev` branch to its upstream tracking branch (`upstream/dev`) to ensure the latest code is used as the base.
2. WHEN a fix is to be implemented, THE Git_Guard SHALL create a new branch with the naming pattern `agent-bugfix-<bug-name>` from the freshly-reset `dev` branch (or reuse the existing `agent-bugfix-*` branch if one was already created for the reproduction test).
3. THE Git_Guard SHALL reject any commit attempt on branches that do not start with `agent`.
4. WHEN a fix is committed, THE Workflow_Orchestrator SHALL include in the commit message: the bug name, the syscall affected, and a reference to the reproduction test.
5. ALL branches (both `agent-test-*` and `agent-bugfix-*`) SHALL be branched from the `dev` branch.
6. BEFORE finishing development on any `agent-*` branch, THE Git_Guard SHALL rebase the branch against `upstream/dev` to incorporate the latest upstream changes.

### Requirement 6: Fix Verification via Regression Testing

**User Story:** As a StarryOS developer, I want to verify that a fix resolves the bug without introducing regressions, so that I can confidently merge the change.

#### Acceptance Criteria

1. WHEN a fix is committed, THE Test_Executor SHALL run the reproduction test on the target architecture and confirm that the test now passes on StarryOS.
2. WHEN the reproduction test passes, THE Test_Executor SHALL re-run only the reproduction test (not the full test suite) on StarryOS to confirm the fix, since full suite compilation is prohibitively expensive.
3. IF the reproduction test still fails after the fix, THEN THE Workflow_Orchestrator SHALL report the fix as incomplete and provide the updated test output for further investigation.
4. IF the full test suite reveals new failures not present before the fix, THEN THE Workflow_Orchestrator SHALL report each new failure as a potential regression with its Bug_Report.

### Requirement 7: Pull Request Message Generation

**User Story:** As a StarryOS developer, I want a well-written pull request message to be generated after a bug is fixed and verified, so that I can open a PR without writing the description from scratch.

#### Acceptance Criteria

1. WHEN a fix is verified (reproduction test passes on StarryOS), THE Workflow_Orchestrator SHALL generate a PR message that is distinct from the commit message and includes: a summary of the bug, the root cause, the fix approach, test results, and any architecture-specific notes.
2. THE Workflow_Orchestrator SHALL write the PR message to `tgoskits/tmp/pr/<bug-name>.md` (this path is already gitignored via `/tmp/*`).
3. THE PR message SHALL follow a structured format with sections: Title, Summary, Root Cause, Fix, Testing, and (if applicable) Architecture Notes. See `tgoskits/tmp/pr/example-lseek-pipe-espipe.md` as the reference example.

### Requirement 8: Multi-Architecture Consistency

**User Story:** As a StarryOS developer, I want bugs to be tested across all supported architectures, so that I can ensure fixes are not architecture-specific.

#### Acceptance Criteria

1. WHEN a reproduction test is created, THE Test_Executor SHALL run the test on the primary architecture (riscv64) first.
2. THE Workflow_Orchestrator SHALL record per-architecture pass/fail status in the Bug_Report.
3. ONLY AFTER the reproduction test passes on riscv64, THE Test_Executor SHALL run the test on the remaining architectures (x86_64, aarch64, loongarch64) to confirm cross-architecture correctness.

### Requirement 9: Workflow State Tracking

**User Story:** As a StarryOS developer, I want the workflow to track the state of each bug through its lifecycle, so that I can see progress and identify bottlenecks.

#### Acceptance Criteria

1. THE Workflow_Orchestrator SHALL maintain a status for each bug using one of the following states: `discovered`, `reproducing`, `reproduced`, `analyzing`, `fixing`, `verifying`, `resolved`, `wontfix`.
2. WHEN a bug transitions between states, THE Workflow_Orchestrator SHALL record the timestamp and the triggering event.
3. THE Workflow_Orchestrator SHALL provide a summary view listing all tracked bugs grouped by their current state.

### Requirement 10: Test Output Parsing and Normalization

**User Story:** As a StarryOS developer, I want test outputs from both Linux host and StarryOS QEMU runs to be parsed into a common format, so that automated comparison is reliable.

#### Acceptance Criteria

1. THE Compatibility_Analyzer SHALL parse the success_regex and fail_regex verdict strings from Test_Executor output (both QEMU and Linux host modes).
2. THE Compatibility_Analyzer SHALL parse the `PASS | file:line | msg` and `FAIL | file:line | msg | errno=N` lines from the test framework output when present.
3. WHEN a test produces no recognizable output (e.g., due to a kernel panic before output), THE Compatibility_Analyzer SHALL classify the test as `crash` and include the available serial/stdout output in the Bug_Report.
4. FOR ALL valid test outputs, parsing then normalizing then re-serializing SHALL produce an equivalent structured result (round-trip property).

### Requirement 11: Commit Hygiene and Branch Cleanliness

**User Story:** As a StarryOS developer, I want each branch to contain the minimum number of well-structured commits, so that the git history stays clean and reviewable.

#### Acceptance Criteria

1. WHEN work on an `agent-bugfix-*` branch is complete, THE branch SHALL contain as few commits as possible — ideally exactly two: one commit for the reproduction test case(s), and one commit for the OS fix.
2. IF additional commits accumulate during iterative development on an `agent-bugfix-*` branch, THE Workflow_Orchestrator SHALL use interactive rebase (via Git_Guard's `run_git_command`) to squash them into the ideal two-commit structure (test + fix) before the branch is considered ready for review.
3. WHEN work on the `agent-test-*` branch is complete, THE branch SHALL ideally contain `1 + N` commits where N is the number of bugs discovered: one commit for the comprehensive test case, and one merge commit for each `agent-bugfix-*` branch that was merged back.
4. THE Workflow_Orchestrator SHALL NOT leave work-in-progress, fixup, or debugging commits on any branch that is presented as ready for review.
