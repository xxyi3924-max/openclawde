"""
Skills system — prompt macros defined as .md files with YAML frontmatter.

Skills are loaded from (in priority order):
  1. {project}/skills/          local project skills
  2. ~/.claude/skills/          global user skills

Frontmatter fields (all optional except description):
  name:           Override skill name (default: filename stem)
  description:    One-line description shown in tool listing
  when-to-use:    Hint for the agent about when to invoke this skill
  argument-hint:  Display hint for arguments, e.g. "[pr_number]"
  allowed-tools:  Comma-separated list of tools the skill can use
  context:        "inline" (default) or "fork" (sub-agent)

Argument substitution in content:
  $ARGUMENTS      → replaced with full args string
  $0, $1, ...     → replaced with positional args (split by space)
  If no placeholder found, args are appended as "ARGUMENTS: {args}"
"""

import re
from pathlib import Path
from typing import Optional

# Try to load python-frontmatter; fall back to simple regex parser
try:
    import frontmatter as _fm
    _HAS_FRONTMATTER = True
except ImportError:
    _HAS_FRONTMATTER = False

_BASE = Path(__file__).parent
_PROJECT_SKILLS_DIR = _BASE / "examples"   # bundled examples
_USER_SKILLS_DIR = Path.home() / ".claude" / "skills"
_LOCAL_SKILLS_DIR = _BASE.parent / "skills" / "local"  # user-created project skills


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        content: str,
        when_to_use: str = "",
        argument_hint: str = "",
        allowed_tools: list[str] | None = None,
        context: str = "inline",
        source: str = "",
    ):
        self.name = name
        self.description = description
        self.content = content
        self.when_to_use = when_to_use
        self.argument_hint = argument_hint
        self.allowed_tools = allowed_tools or []
        self.context = context  # "inline" or "fork"
        self.source = source    # file path for debugging

    def expand(self, args: str = "") -> str:
        """
        Substitute $ARGUMENTS and positional $0, $1, ... into content.
        If no placeholders exist and args is non-empty, appends args at end.
        """
        content = self.content
        has_placeholder = False

        # Replace $ARGUMENTS
        if "$ARGUMENTS" in content:
            content = content.replace("$ARGUMENTS", args)
            has_placeholder = True

        # Replace positional $0, $1, ...
        parts = args.split() if args else []
        def _replace_pos(m):
            nonlocal has_placeholder
            has_placeholder = True
            idx = int(m.group(1))
            return parts[idx] if idx < len(parts) else ""

        content = re.sub(r'\$(\d+)(?!\w)', _replace_pos, content)

        # Append if no placeholder found and args provided
        if args and not has_placeholder:
            content = content.rstrip() + f"\n\nARGUMENTS: {args}"

        return content

    def __repr__(self):
        return f"Skill(name={self.name!r}, context={self.context!r})"


def _parse_skill_file(path: Path) -> Optional[Skill]:
    """Parse a .md skill file into a Skill object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[Skills] Cannot read {path}: {e}")
        return None

    meta = {}
    content = raw

    if _HAS_FRONTMATTER:
        try:
            post = _fm.loads(raw)
            meta = dict(post.metadata)
            content = post.content.strip()
        except Exception:
            pass
    else:
        # Minimal YAML frontmatter parser (handles simple key: value pairs)
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                fm_block = raw[3:end].strip()
                content = raw[end + 4:].strip()
                for line in fm_block.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip()

    name = str(meta.get("name", path.stem)).strip()
    description = str(meta.get("description", "")).strip()
    when_to_use = str(meta.get("when-to-use", meta.get("whenToUse", ""))).strip()
    argument_hint = str(meta.get("argument-hint", meta.get("argumentHint", ""))).strip()
    context = str(meta.get("context", "inline")).strip().lower()

    raw_tools = meta.get("allowed-tools", meta.get("allowedTools", ""))
    if isinstance(raw_tools, list):
        allowed_tools = [t.strip() for t in raw_tools if t.strip()]
    elif isinstance(raw_tools, str) and raw_tools:
        allowed_tools = [t.strip() for t in re.split(r"[,\s]+", raw_tools) if t.strip()]
    else:
        allowed_tools = []

    if not description:
        # Use first non-empty line of content as fallback description
        for line in content.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                description = line[:80]
                break

    return Skill(
        name=name,
        description=description,
        content=content,
        when_to_use=when_to_use,
        argument_hint=argument_hint,
        allowed_tools=allowed_tools,
        context=context,
        source=str(path),
    )


class SkillManager:
    """Loads and manages skills from disk."""

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self.reload()

    def reload(self):
        """Reload all skills from all skill directories."""
        self._skills = {}
        for skill_dir in [_PROJECT_SKILLS_DIR, _USER_SKILLS_DIR, _LOCAL_SKILLS_DIR]:
            if skill_dir.exists():
                for path in sorted(skill_dir.glob("*.md")):
                    skill = _parse_skill_file(path)
                    if skill:
                        self._skills[skill.name] = skill
                        print(f"[Skills] Loaded: {skill.name}  ({path.name})")

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name) or self._skills.get(name.replace("-", "_")) or self._skills.get(name.replace("_", "-"))

    def list_all(self) -> list[Skill]:
        return list(self._skills.values())

    def tool_description(self) -> str:
        """Dynamic description for the invoke_skill tool, listing available skills."""
        if not self._skills:
            return (
                "Invoke a saved skill by name. No skills currently loaded. "
                "Add .md files to skills/local/ to create skills."
            )
        lines = ["Invoke a saved skill. Available skills:"]
        for s in sorted(self._skills.values(), key=lambda x: x.name):
            hint = f" [{s.argument_hint}]" if s.argument_hint else ""
            when = f"  (use when: {s.when_to_use})" if s.when_to_use else ""
            lines.append(f"  - {s.name}{hint}: {s.description}{when}")
        return "\n".join(lines)

    def system_prompt_section(self) -> str:
        """Returns a section to inject into the system prompt about available skills."""
        if not self._skills:
            return ""
        lines = ["## Skills", "You have saved skills you can invoke with invoke_skill(name, args):"]
        for s in sorted(self._skills.values(), key=lambda x: x.name):
            hint = f" [{s.argument_hint}]" if s.argument_hint else ""
            when = f" — use when: {s.when_to_use}" if s.when_to_use else ""
            lines.append(f"  /{s.name}{hint}: {s.description}{when}")
        lines.append("Always invoke a skill instead of writing custom instructions for tasks it covers.")
        return "\n".join(lines)

    def __len__(self):
        return len(self._skills)


# Module-level singleton
_manager: SkillManager | None = None


def get_manager() -> SkillManager:
    global _manager
    if _manager is None:
        _manager = SkillManager()
    return _manager


def reload():
    """Reload all skills (call after adding new skill files)."""
    get_manager().reload()
