import subprocess
import sys
from pathlib import Path

import sandbox

VENV_PYTHON = Path(__file__).parent.parent / ".venv" / "bin" / "python"


def _python_bin() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def run_python(code: str, timeout: int = 30) -> str:
    tmp = sandbox.WORKSPACE / "_run_tmp.py"
    tmp.write_text(code, encoding="utf-8")
    try:
        r = subprocess.run(
            [_python_bin(), str(tmp)],
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(sandbox.WORKSPACE),
        )
        out = ""
        if r.stdout:
            out += f"stdout:\n{r.stdout}"
        if r.stderr:
            out += f"\nstderr:\n{r.stderr}"
        if r.returncode != 0:
            out += f"\n(exit {r.returncode})"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
    finally:
        tmp.unlink(missing_ok=True)


def run_shell(command: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            command, shell=True,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(sandbox.WORKSPACE),
        )
        out = r.stdout or ""
        if r.stderr:
            out += f"\n[stderr] {r.stderr}"
        return out.strip() or f"(exit {r.returncode})"
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
