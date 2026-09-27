"""Common project checks, run with `inv check`."""
import shlex
import sys

from invoke import Context, task


def run_step(ctx: Context, title: str, command: str) -> None:
    print(f"\n==> {title}")
    ctx.run(command, pty=True)


@task
def check(ctx: Context) -> None:
    """Compile Python files and run the existing test suites."""
    python = shlex.quote(sys.executable)
    run_step(ctx, "Python syntax", f"{python} -m compileall -q server.py portal.py local_index.py sync_data.py")
    run_step(ctx, "MCP server checks", f"{python} test_server.py")
    run_step(ctx, "Data and portal checks", f"{python} test_data_sync.py")
