"""
Named agent type definitions.

Each type is a .md file in agents/types/ with YAML frontmatter:
  name            — identifier passed to run_agent(agent_type=...)
  model           — "haiku" | "sonnet" | "opus" | omit to inherit parent model
  disallowed-tools — comma-separated tool names to strip from this agent's tool list

The body of the .md file becomes the agent's system prompt, replacing the
default SYSTEM_PROMPT from agent.py.

Loading order: agents/types/ (bundled). User can add types to ~/.claude/agents/.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_BUNDLED_DIR = Path(__file__).parent / "types"
_USER_DIR = Path.home() / ".claude" / "agents"


@dataclass
class AgentType:
    name: str
    system_prompt: str
    disallowed_tools: list[str] = field(default_factory=list)
    model: Optional[str] = None  # None = inherit parent model


class AgentTypeManager:
    def __init__(self):
        self._types: dict[str, AgentType] = {}
        self._load()

    def _load(self):
        for directory in [_BUNDLED_DIR, _USER_DIR]:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                try:
                    at = self._parse(path)
                    self._types[at.name] = at
                    print(f"[Agents] Loaded type: {at.name}  ({path.name})")
                except Exception as e:
                    print(f"[Agents] Failed to load {path.name}: {e}")

    def _parse(self, path: Path) -> AgentType:
        text = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = text

        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            body = m.group(2).strip()

        name = meta.get("name", path.stem)
        model_raw = meta.get("model", "").strip() or None
        disallowed = [
            t.strip()
            for t in meta.get("disallowed-tools", "").split(",")
            if t.strip()
        ]
        return AgentType(name=name, system_prompt=body, disallowed_tools=disallowed, model=model_raw)

    def get(self, name: str) -> Optional[AgentType]:
        return self._types.get(name)

    def list_all(self) -> list[AgentType]:
        return list(self._types.values())

    def names(self) -> list[str]:
        return list(self._types.keys())


_manager: Optional[AgentTypeManager] = None


def get_manager() -> AgentTypeManager:
    global _manager
    if _manager is None:
        _manager = AgentTypeManager()
    return _manager
