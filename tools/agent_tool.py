"""
run_agent / send_to_agent / stop_agent tools.

Mirrors Claude Code's AgentTool + SendMessageTool + TaskStopTool.

run_agent:
  - background=False  → spawn sub-agent in current thread, block until done
  - background=True   → spawn sub-agent in daemon thread, return immediately
                        Sub-agent is registered in agent_registry so it can receive
                        messages and be stopped.

send_to_agent:
  - Route a follow-up message to a running background agent by agent_id.
  - Supports broadcast: agent_id="*" sends to ALL running agents.
  - Sub-agent checks for pending messages between tool iterations.

stop_agent:
  - Set abort_event on a running agent; it will stop at its next check.
  - Releases owned tasks back to unowned.

Progress tracking:
  - Background agents report tool use + token counts to agent_registry.
  - list_agents() returns current status for all registered agents.

Tool filtering:
  - Sub-agents spawned here get a RESTRICTED tool set (no run_agent,
    send_to_agent, stop_agent) to prevent accidental infinite nesting.
  - Coordinator agents keep the full set.
"""

import threading
from pathlib import Path
from typing import Optional

from tools import agent_registry

_agent_factory = None     # set by agent.py via set_agent_factory()
_output_dir: Optional[Path] = None

# Tools sub-agents are NOT allowed to use (prevents infinite nesting)
_SUB_AGENT_BLOCKED_TOOLS = {"run_agent", "send_to_agent", "stop_agent"}


def set_agent_factory(fn, output_dir: Optional[Path] = None):
    """
    fn() -> Agent — factory that creates a fresh sub-agent instance.
    Called once by agent.py.__init__.
    """
    global _agent_factory, _output_dir
    _agent_factory = fn
    _output_dir = output_dir or (Path(__file__).parent.parent / "memory" / "agent_output")
    _output_dir.mkdir(parents=True, exist_ok=True)


def _filter_tools_for_subagent(tools: list[dict]) -> list[dict]:
    """Remove coordinator-only tools from a sub-agent's tool list."""
    return [t for t in tools if t["name"] not in _SUB_AGENT_BLOCKED_TOOLS]


def _apply_agent_type(sub, agent_type_name: str):
    """Apply a named agent type definition to a sub-agent instance."""
    try:
        import agents as _agents_mod
        at = _agents_mod.get_manager().get(agent_type_name)
        if at:
            sub._agent_type_name = at.name
            sub._agent_type_system_prompt = at.system_prompt
            if at.model:
                sub._active_model = at.model
                sub.model = at.model
            if at.max_turns is not None:
                sub._max_turns = at.max_turns
            # Build restricted tool list from type's disallowed list + coordinator-only block
            import tools as _tool_module
            base = _filter_tools_for_subagent(list(_tool_module.TOOL_DEFINITIONS))
            if at.disallowed_tools:
                blocked = set(at.disallowed_tools)
                sub._sub_agent_tools = [t for t in base if t["name"] not in blocked]
            else:
                sub._sub_agent_tools = base
            return True
    except Exception as e:
        print(f"[AgentTool] Could not apply agent type '{agent_type_name}': {e}")
    return False


def run_agent(description: str, prompt: str, background: bool = False, agent_type: str = "worker") -> str:
    if _agent_factory is None:
        return "[Agent tool not initialized]"

    agent_id = agent_registry.new_agent_id()

    if not background:
        # ---- Synchronous path ----
        sub = _agent_factory()
        sub._agent_id = agent_id
        entry = agent_registry.register(agent_id, task_id=None, description=description)

        # Apply agent type (sets system prompt, model, tool restrictions)
        if not _apply_agent_type(sub, agent_type):
            # Fallback: just restrict coordinator tools
            import tools as _tool_module
            sub._sub_agent_tools = _filter_tools_for_subagent(list(_tool_module.TOOL_DEFINITIONS))

        try:
            result = sub.respond(prompt)
        finally:
            agent_registry.set_status(agent_id, "completed")
            agent_registry.deregister(agent_id)

        return (
            f"[Sub-agent: {description}]\n"
            f"agent_id: {agent_id}\n"
            f"tools used: {entry.progress.tool_use_count}\n\n"
            f"{result}"
        )

    # ---- Background (async) path ----
    from tools.task_tools import _store
    task_id: Optional[int] = None
    if _store:
        task_id = _store.create(description, prompt[:200])

    output_file = None
    if _output_dir and task_id is not None:
        output_file = _output_dir / f"{task_id}.txt"

    entry = agent_registry.register(agent_id, task_id=task_id, description=description)

    def _run_background():
        sub = _agent_factory()
        sub._agent_id = agent_id

        # Apply agent type
        if not _apply_agent_type(sub, agent_type):
            import tools as _tool_module
            sub._sub_agent_tools = _filter_tools_for_subagent(list(_tool_module.TOOL_DEFINITIONS))

        def _write(msg: str):
            if output_file:
                with open(output_file, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")

        sub.send_update = _write
        try:
            if _store and task_id is not None:
                _store.update(task_id, status="in_progress", owner=agent_id)

            result = sub.respond(prompt)

            _write(f"\n[DONE]\n{result}")
            if _store and task_id is not None:
                _store.update(task_id, status="completed", owner="", result=result[:200])
            agent_registry.set_status(agent_id, "completed")
        except Exception as e:
            _write(f"\n[ERROR] {e}")
            if _store and task_id is not None:
                _store.update(task_id, status="failed", result=str(e))
            agent_registry.set_status(agent_id, "failed")
        finally:
            # Release any tasks still owned by this agent
            if _store:
                _store.unassign(agent_id)

    t = threading.Thread(target=_run_background, daemon=True, name=f"agent-{agent_id}")
    t.start()

    id_str = f"task_id={task_id}, " if task_id is not None else ""
    return (
        f"[Background agent launched]\n"
        f"agent_id: {agent_id}\n"
        f"{id_str}"
        f"description: {description}\n"
        f"Use send_to_agent(agent_id, message) to send follow-up instructions.\n"
        f"Use stop_agent(agent_id) to terminate it.\n"
        f"Use get_agent_output({task_id}) to read streaming output."
    )


def get_agent_output(task_id: int) -> str:
    """Read streaming output from a background agent by task_id."""
    if _output_dir is None:
        return "[Output directory not configured]"
    path = _output_dir / f"{task_id}.txt"
    if not path.exists():
        return f"[No output yet for task {task_id}]"
    return path.read_text(encoding="utf-8") or "(empty)"


def send_to_agent(agent_id: str, message: str) -> str:
    """
    Send a follow-up message to a running background agent.
    agent_id="*" broadcasts to ALL running agents.

    The agent will receive the message between tool iterations and inject it
    into its conversation as a user message.
    """
    if agent_id == "*":
        running = agent_registry.get_running()
        if not running:
            return "No running agents."
        count = 0
        for entry in running:
            if agent_registry.send_message(entry["agent_id"], message):
                count += 1
        return f"Broadcast to {count} running agent(s): {message[:100]}"

    if agent_registry.send_message(agent_id, message):
        return f"Message queued for agent {agent_id}."
    entry = agent_registry.get(agent_id)
    if entry:
        return f"Agent {agent_id} exists but is {entry.status} — cannot receive messages."
    return f"Agent {agent_id} not found. Check list_agents() for active agent IDs."


def stop_agent(agent_id: str) -> str:
    """
    Stop a running background agent by sending it an abort signal.
    The agent will halt at its next iteration check.
    """
    entry = agent_registry.get(agent_id)
    if not entry:
        return f"Agent {agent_id} not found."
    if entry.status != "running":
        return f"Agent {agent_id} is already {entry.status}."

    aborted = agent_registry.abort(agent_id)
    if aborted:
        # Release any tasks the agent owned
        from tools.task_tools import _store
        if _store:
            _store.unassign(agent_id)
        return f"Agent {agent_id} ({entry.description}) abort signal sent."
    return f"Could not abort agent {agent_id}."


def list_agents() -> str:
    """List all registered agents with their status and progress."""
    statuses = agent_registry.get_all_statuses()
    if not statuses:
        return "(no agents registered)"
    lines = []
    for s in statuses:
        task_str = f"  task_id={s['task_id']}" if s["task_id"] is not None else ""
        lines.append(
            f"[{s['agent_id']}] {s['status'].upper():10s} {s['description']}"
            f"{task_str}"
        )
        lines.append(
            f"   tools={s['tool_use_count']}  "
            f"in={s['input_tokens']} out={s['output_tokens']}  "
            f"recent={s['recent_activities']}  "
            f"started={s['started_at']}"
        )
    return "\n".join(lines)
