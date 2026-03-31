import sandbox

NOTES_DIR = sandbox.WORKSPACE / "notes"


def write_note(title: str, content: str) -> str:
    NOTES_DIR.mkdir(exist_ok=True)
    slug = title.lower().replace(" ", "_").replace("/", "-")
    path = NOTES_DIR / f"{slug}.md"
    path.write_text(f"# {title}\n\n{content}", encoding="utf-8")
    return f"Note saved: notes/{slug}.md"


def read_notes() -> str:
    if not NOTES_DIR.exists() or not list(NOTES_DIR.glob("*.md")):
        return "(no notes yet)"
    parts = []
    for p in sorted(NOTES_DIR.glob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def delete_note(title: str) -> str:
    slug = title.lower().replace(" ", "_").replace("/", "-")
    path = NOTES_DIR / f"{slug}.md"
    if not path.exists():
        return f"Note not found: {title}"
    path.unlink()
    return f"Deleted: notes/{slug}.md"
