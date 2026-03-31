from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
WORKSPACE = BASE_DIR / "workspace"
WORKSPACE.mkdir(exist_ok=True)

_extra_allowed: list[Path] = []


def grant_access(path: str):
    p = Path(path).expanduser().resolve()
    if p not in _extra_allowed:
        _extra_allowed.append(p)
        print(f"[Sandbox] Access granted to: {p}")


def revoke_access(path: str):
    p = Path(path).expanduser().resolve()
    if p in _extra_allowed:
        _extra_allowed.remove(p)


def resolve(path: str) -> Path:
    """Resolve a path, enforcing sandbox. Raises PermissionError if out of bounds."""
    p = Path(path)
    resolved = (WORKSPACE / p).resolve() if not p.is_absolute() else p.resolve()

    if resolved.is_relative_to(WORKSPACE):
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    for allowed in _extra_allowed:
        if resolved.is_relative_to(allowed):
            return resolved

    raise PermissionError(
        f"'{path}' is outside the sandbox. "
        "Tell me 'you may access <path>' to grant access."
    )


def workspace_path(relative: str = "") -> Path:
    return (WORKSPACE / relative).resolve() if relative else WORKSPACE
