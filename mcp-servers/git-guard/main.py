import os
import subprocess
from mcp.server.fastmcp import FastMCP

# 1. Initialize the server
mcp = FastMCP("Guarded-Git-Server")

# 2. Configuration & State
# Defaults to the current working directory if GIT_BASE_DIR is not set
REPO_PATH = os.environ.get("GIT_BASE_DIR", os.getcwd())

# Comma-separated list of branch names that are allowed to be hard-reset to upstream.
# e.g. GIT_RESETTABLE_BRANCHES="main,develop,agent-fix"
_raw = os.environ.get("GIT_RESETTABLE_BRANCHES", "")
RESETTABLE_BRANCHES: set[str] = {b.strip() for b in _raw.split(",") if b.strip()}

def _get_current_branch() -> str:
    """Helper function to safely get the current git branch."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], 
            cwd=REPO_PATH, 
            text=True, 
            stderr=subprocess.STDOUT
        ).strip()
    except subprocess.CalledProcessError as e:
        return f"Error: Could not determine current branch. ({e.output.strip()})"

# 3. Tool: List Branches
@mcp.tool()
def list_branches() -> str:
    """
    List all local branches in the repository. 
    The current branch will be highlighted with a '*'.
    """
    try:
        result = subprocess.run(
            ["git", "branch"], 
            cwd=REPO_PATH, 
            capture_output=True, 
            text=True, 
            check=True
        )
        return result.stdout if result.stdout else "No branches found."
    except subprocess.CalledProcessError as e:
        return f"Git Error: {e.stderr}"

# 4. Tool: Switch Branch
@mcp.tool()
def switch_branch(branch_name: str) -> str:
    """
    Switch the working directory to an existing branch.
    """
    try:
        result = subprocess.run(
            ["git", "checkout", branch_name], 
            cwd=REPO_PATH, 
            capture_output=True, 
            text=True, 
            check=True
        )
        # Git checkout often prints to stderr even on success
        return f"Switched to branch '{branch_name}'.\n{result.stderr}"
    except subprocess.CalledProcessError as e:
        return f"Git Error Failed to switch branch:\n{e.stderr}"

# 5. Tool: Create Agent Branch
@mcp.tool()
def create_agent_branch(branch_name: str, base_branch: str | None = None) -> str:
    """
    Create and switch to a new branch.
    SECURITY RULE: The branch name MUST start with 'agent'.

    Args:
        branch_name: Name of the new branch (must start with 'agent').
        base_branch: Optional base branch to branch from. If omitted, branches from the current HEAD.
    """
    if not branch_name.startswith("agent"):
        return f"REJECTED: New branch names must begin with 'agent'. You requested '{branch_name}'."
    
    cmd = ["git", "checkout", "-b", branch_name]
    if base_branch:
        cmd.append(base_branch)

    try:
        result = subprocess.run(
            cmd, 
            cwd=REPO_PATH, 
            capture_output=True, 
            text=True, 
            check=True
        )
        msg = f"Successfully created and switched to '{branch_name}'"
        if base_branch:
            msg += f" (based on '{base_branch}')"
        return f"{msg}.\n{result.stderr}"
    except subprocess.CalledProcessError as e:
        return f"Git Error Failed to create branch:\n{e.stderr}"

# 6. Tool: Arbitrary Git Command (The Guarded Executor)
@mcp.tool()
def run_git_command(command_args: list[str]) -> str:
    """
    Run an arbitrary git command (e.g., ['commit', '-m', 'update init']).
    SECURITY RULE: This will ONLY execute if the current branch starts with 'agent'.
    """
    current_branch = _get_current_branch()
    
    # The Guard Check
    if not current_branch.startswith("agent"):
        return (
            f"REJECTED: Execution blocked. You are currently on branch '{current_branch}'.\n"
            f"Arbitrary git commands are only allowed on branches starting with 'agent'.\n"
            f"Please use the 'create_agent_branch' or 'switch_branch' tools first."
        )

    # Execution (if safe)
    try:
        # Prepend 'git' to the user's arguments
        full_command = ["git"] + command_args
        
        result = subprocess.run(
            full_command, 
            cwd=REPO_PATH, 
            capture_output=True, 
            text=True, 
            check=True
        )
        
        # Combine stdout and stderr, as some valid git commands output to stderr
        output = result.stdout + result.stderr
        return f"Success:\n{output}" if output.strip() else "Success (No output returned)."
        
    except subprocess.CalledProcessError as e:
        return f"Git Command Failed:\nExit Code: {e.returncode}\nError: {e.stderr}"
    except Exception as e:
        return f"System Error: {str(e)}"

# 7. Tool: Hard-reset a branch to its upstream
@mcp.tool()
def reset_branch_to_upstream(branch_name: str) -> str:
    """
    Hard-reset a branch to its upstream tracking branch.
    SECURITY RULE: Only branches listed in the GIT_RESETTABLE_BRANCHES env var are allowed.
    WARNING: This discards ALL local commits and working-tree changes on that branch.
    """
    if not RESETTABLE_BRANCHES:
        return (
            "REJECTED: No resettable branches configured. "
            "Set the GIT_RESETTABLE_BRANCHES env var (comma-separated)."
        )

    if branch_name not in RESETTABLE_BRANCHES:
        return (
            f"REJECTED: Branch '{branch_name}' is not in the allow-list.\n"
            f"Allowed branches: {', '.join(sorted(RESETTABLE_BRANCHES))}"
        )

    try:
        # Fetch latest from all remotes first
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=REPO_PATH, capture_output=True, text=True, check=True,
        )

        # Verify the branch has an upstream configured
        upstream = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", f"{branch_name}@{{upstream}}"],
            cwd=REPO_PATH, text=True, stderr=subprocess.STDOUT,
        ).strip()

        # Perform the hard reset
        result = subprocess.run(
            ["git", "checkout", branch_name],
            cwd=REPO_PATH, capture_output=True, text=True, check=True,
        )
        result = subprocess.run(
            ["git", "reset", "--hard", upstream],
            cwd=REPO_PATH, capture_output=True, text=True, check=True,
        )

        return (
            f"Success: Branch '{branch_name}' has been hard-reset to '{upstream}'.\n"
            f"{result.stdout}{result.stderr}"
        )
    except subprocess.CalledProcessError as e:
        output = getattr(e, "output", "") or e.stderr or ""
        if "no upstream configured" in output.lower() or "unknown revision" in output.lower():
            return f"Error: Branch '{branch_name}' has no upstream tracking branch configured."
        return f"Git Error:\n{output}"


if __name__ == "__main__":
    # Ensure the repository path actually exists before starting
    if not os.path.isdir(REPO_PATH):
        print(f"Warning: The path {REPO_PATH} does not exist or is not a directory.")
        
    mcp.run()