import sys

from invoke import Context, task

CYAN = "\033[36m"
GREEN = "\033[32m"
RESET = "\033[0m"


def run_step(ctx: Context, title: str, command: str) -> None:
    print(f"{CYAN}==> {title}{RESET}")
    ctx.run(command, pty=True)


@task
def check(ctx: Context) -> None:
    """Run linting, syntax checks, and the existing test suites."""
    python = sys.executable
    run_step(ctx, "Ruff", f"{python} -m ruff check --force-exclude")
    run_step(
        ctx,
        "Python syntax",
        f"{python} -m compileall -q tasks.py server.py portal.py local_index.py sync_data.py test_server.py test_data_sync.py",
    )
    run_step(ctx, "MCP server checks", f"{python} test_server.py")
    run_step(ctx, "Data and portal checks", f"{python} test_data_sync.py")
    print(f"{GREEN}All checks passed{RESET}")


@task
def format(ctx: Context) -> None:
    """Apply Ruff formatting and safe lint fixes to project Python files."""
    python = sys.executable
    run_step(ctx, "Ruff format", f"{python} -m ruff format .")
    run_step(ctx, "Ruff fix", f"{python} -m ruff check --fix --force-exclude .")
    print(f"{GREEN}Code formatted successfully{RESET}")
