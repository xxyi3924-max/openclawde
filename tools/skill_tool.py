"""
invoke_skill tool — expands a skill prompt and either returns it inline
or spawns a sub-agent (context: fork skills).
"""

import skills as skill_module

_agent_run_fn = None  # set by agent.py: set_skill_runner(fn)


def set_skill_runner(fn):
    """fn(prompt: str) -> str — runs a sub-agent with the given prompt."""
    global _agent_run_fn
    _agent_run_fn = fn


def invoke_skill(name: str, args: str = "") -> str:
    manager = skill_module.get_manager()
    skill = manager.get(name)

    if not skill:
        available = ", ".join(s.name for s in manager.list_all()) or "(none)"
        return (
            f"Skill '{name}' not found.\n"
            f"Available skills: {available}\n"
            f"Add .md files to skills/local/ to create new skills."
        )

    expanded = skill.expand(args)

    if skill.context == "fork":
        # Run in a sub-agent
        if _agent_run_fn is None:
            return f"[Skill:{name}] Sub-agent runner not available — returning inline.\n\n{expanded}"
        result = _agent_run_fn(expanded)
        return f"[Skill:{name} result]\n{result}"
    else:
        # Inline: return the expanded prompt so the agent executes it directly
        return f"[Skill:{name} — execute the following task]\n\n{expanded}"
