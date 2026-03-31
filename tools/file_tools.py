import sandbox

# ------------------------------------------------------------------
# File tools
# ------------------------------------------------------------------

def write_file(path: str, content: str) -> str:
    path = path.removeprefix("workspace/").removeprefix("workspace\\")
    resolved = sandbox.resolve(path)
    resolved.write_text(content, encoding="utf-8")
    result = f"Written {len(content)} chars to workspace/{resolved.relative_to(sandbox.WORKSPACE)}"
    if resolved.is_relative_to(sandbox.WORKSPACE / "tools") and path.endswith(".py"):
        from tools import reload_dynamic_tools, _dynamic_fns
        reload_dynamic_tools()
        result += f"\n[Tools] Reloaded — {len(_dynamic_fns)} dynamic tool(s) active."
    return result


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace old_string with new_string. Requires exactly one match."""
    path = path.removeprefix("workspace/").removeprefix("workspace\\")
    resolved = sandbox.resolve(path)
    if not resolved.exists():
        return f"File not found: {path}"
    content = resolved.read_text(encoding="utf-8")

    count = content.count(old_string)
    if count == 0:
        first_line = old_string.strip().splitlines()[0][:60] if old_string.strip() else ""
        lines = content.splitlines()
        hints = [(i + 1, l) for i, l in enumerate(lines) if first_line and first_line.strip()[:30] in l]
        hint_str = "\n".join(f"  line {n}: {l}" for n, l in hints[:3]) or "  (no similar lines)"
        return (
            f"edit_file FAILED: old_string not found (0 matches).\n"
            f"First line searched: {first_line!r}\n"
            f"Nearest matches:\n{hint_str}\n"
            f"Call grep_files() to locate the exact current text."
        )
    if count > 1:
        return (
            f"edit_file FAILED: old_string matched {count} locations — must be unique.\n"
            f"Expand old_string to include more surrounding context to make it unique."
        )

    new_content = content.replace(old_string, new_string, 1)
    resolved.write_text(new_content, encoding="utf-8")
    if resolved.is_relative_to(sandbox.WORKSPACE / "tools") and path.endswith(".py"):
        from tools import reload_dynamic_tools
        reload_dynamic_tools()
    return f"Edited workspace/{resolved.relative_to(sandbox.WORKSPACE)}"


def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    path = path.removeprefix("workspace/").removeprefix("workspace\\")
    resolved = sandbox.resolve(path)
    if not resolved.exists():
        return f"File not found: {path}"
    lines = resolved.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    if not (start_line and end_line):
        return (
            f"[BLOCKED] read_file requires start_line and end_line. "
            f"{path} has {total} lines. "
            f"Use grep_files(pattern, path) to find the relevant lines first, "
            f"then read_file(path, start_line, end_line) to read only that section."
        )
    s = max(0, start_line - 1)
    e = min(total, end_line)
    chunk = lines[s:e]
    numbered = "\n".join(f"{s + i + 1}: {l}" for i, l in enumerate(chunk))
    return f"[Lines {s + 1}-{s + len(chunk)} of {total}]\n{numbered}"


def grep_files(pattern: str, path: str = "", context_lines: int = 3) -> str:
    """Search for a regex pattern (or multi-pattern via 'a|b|c') across workspace files."""
    import re as _re
    target = sandbox.workspace_path(path.removeprefix("workspace/"))
    results = []
    skip_exts = {".pyc", ".db", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf"}
    try:
        compiled = _re.compile(pattern, _re.IGNORECASE)
    except _re.error as e:
        return f"Invalid regex pattern: {e}"
    for f in sorted(target.rglob("*")):
        if not f.is_file() or f.suffix in skip_exts:
            continue
        try:
            file_lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        file_results = []
        for i, line in enumerate(file_lines):
            if compiled.search(line):
                rel = str(f.relative_to(sandbox.WORKSPACE))
                ctx_start = max(0, i - context_lines)
                ctx_end = min(len(file_lines), i + context_lines + 1)
                snippet = "\n".join(f"  {ctx_start + j + 1}: {file_lines[ctx_start + j]}" for j in range(ctx_end - ctx_start))
                file_results.append(f"{rel}:{i + 1}: {line.strip()}\n{snippet}")
        results.extend(file_results)
        if len(results) >= 60:
            results = results[:60]
            results.append("… [truncated at 60 matches — narrow your pattern or path]")
            break
    return "\n\n".join(results) if results else "No matches found."


def list_files(directory: str = "") -> str:
    directory = directory.removeprefix("workspace/").removeprefix("workspace\\")
    target = sandbox.workspace_path(directory)
    if not target.exists():
        return f"Directory not found: {directory or 'workspace'}"
    entries = sorted(
        str(p.relative_to(sandbox.WORKSPACE))
        for p in target.rglob("*")
        if p.is_file()
    )
    return "\n".join(entries) if entries else "(workspace is empty)"


def list_tools() -> str:
    from tools import _dynamic_defs
    if not _dynamic_defs:
        return "No dynamic tools loaded yet. Write a tool to workspace/tools/<name>.py to create one."
    lines = []
    for d in _dynamic_defs:
        name = d.get("name", "?")
        desc = d.get("description", "")
        requires = d.get("requires", {})
        req_str = f"  requires: {', '.join(requires['pip'])}" if requires.get("pip") else ""
        lines.append(f"• {name}: {desc}{req_str}")
    return "\n".join(lines)
