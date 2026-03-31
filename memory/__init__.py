"""
memory package — single injection point for system prompt context.

load_context() returns the string injected into the agent's cached system prompt.

Phase 1: loads workspace/notes/*.md (backward compat with v2)
Phase 6: prefers memory/MEMORY.md, falls back to notes/ if it doesn't exist yet.
"""

from pathlib import Path

_BASE = Path(__file__).parent
_MEMORY_FILE = _BASE / "MEMORY.md"

# Notes dir lives inside workspace (set by sandbox)
_NOTES_DIR = _BASE.parent / "workspace" / "notes"


def load_context(task_store=None) -> str:
    """
    Return the memory context block to inject into the system prompt.

    Priority:
    1. memory/MEMORY.md  (Phase 6 — structured, consolidated)
    2. workspace/notes/  (Phase 1 fallback — raw notes)

    Appends active task summary if task_store is provided.
    """
    parts = []

    # Phase 6: prefer MEMORY.md when it exists
    if _MEMORY_FILE.exists():
        content = _MEMORY_FILE.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"## Memory\n{content}")
    else:
        # Phase 1 fallback: load all notes
        if _NOTES_DIR.exists():
            notes = [p.read_text(encoding="utf-8") for p in sorted(_NOTES_DIR.glob("*.md"))]
            if notes:
                joined = "\n\n---\n\n".join(notes)
                parts.append(f"## Your saved notes\n{joined}")

    # Phase 4: inject active task summary
    if task_store is not None:
        summary = task_store.summary_for_prompt()
        if summary:
            parts.append(summary)

    return "\n\n".join(parts)
