"""Task tracker tools — backed by memory.tasks.TaskStore."""

# TaskStore instance is injected by agent.py at startup via set_store().
_store = None


def set_store(store):
    global _store
    _store = store


def _fmt_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "(no tasks)"
    lines = []
    for t in tasks:
        blocked_by = t.get("blocked_by", [])
        blocks = t.get("blocks", [])
        owner = t.get("owner", "")
        active_form = t.get("active_form", "")
        result = t.get("result", "")

        status_str = t["status"].upper()
        line = f"[{t['id']:3d}] {status_str:11s} {t['title']}"
        if owner:
            line += f"  [owner: {owner}]"
        if active_form:
            line += f"  [{active_form}]"
        if blocked_by:
            line += f"  (blocked by: {blocked_by})"
        if blocks:
            line += f"  (blocks: {blocks})"
        if result:
            line += f"  → {result[:60]}"
        lines.append(line)
        if t.get("description"):
            lines.append(f"      {t['description'][:80]}")
    return "\n".join(lines)


def create_task(
    title: str,
    description: str = "",
    depends_on: list = None,
    owner: str = "",
    metadata: dict = None,
) -> str:
    if _store is None:
        return "[Task tracker not initialized]"
    task_id = _store.create(title, description, depends_on, owner=owner, metadata=metadata)
    dep_str = f" (blocked by: {depends_on})" if depends_on else ""
    owner_str = f" (assigned to: {owner})" if owner else ""
    return f"Task created: #{task_id} — {title}{dep_str}{owner_str}"


def update_task(task_id: int, status: str, result: str = "") -> str:
    if _store is None:
        return "[Task tracker not initialized]"
    return _store.update(int(task_id), status=status, result=result)


def set_task_active_form(task_id: int, active_form: str) -> str:
    """Update the short status display string (e.g. 'Running tests...')."""
    if _store is None:
        return "[Task tracker not initialized]"
    return _store.update(int(task_id), active_form=active_form)


def set_task_owner(task_id: int, owner: str) -> str:
    """Assign or reassign a task to an agent_id."""
    if _store is None:
        return "[Task tracker not initialized]"
    return _store.update(int(task_id), owner=owner)


def claim_task(task_id: int, agent_id: str, check_agent_busy: bool = False) -> str:
    """
    Atomically mark task as in_progress and assign it to agent_id.

    Returns: claimed | blocked | already_claimed | agent_busy | not_found | already_done
    """
    if _store is None:
        return "[Task tracker not initialized]"
    result = _store.claim(int(task_id), agent_id, check_agent_busy=check_agent_busy)
    msgs = {
        "claimed": f"Task #{task_id} claimed by {agent_id}.",
        "not_found": f"Task #{task_id} not found.",
        "already_claimed": f"Task #{task_id} is already owned by another agent.",
        "blocked": f"Task #{task_id} has unmet dependencies — cannot start yet.",
        "agent_busy": f"Agent {agent_id} already has an in_progress task.",
        "already_done": f"Task #{task_id} is already completed or failed.",
    }
    return msgs.get(result, result)


def block_task(task_id: int, blocked_by_id: int) -> str:
    """Declare that task_id cannot start until blocked_by_id is completed."""
    if _store is None:
        return "[Task tracker not initialized]"
    return _store.block(int(task_id), int(blocked_by_id))


def list_tasks(filter: str = "all") -> str:
    if _store is None:
        return "[Task tracker not initialized]"
    tasks = _store.list_all(filter)
    return _fmt_tasks(tasks)


def get_task(task_id: int) -> str:
    if _store is None:
        return "[Task tracker not initialized]"
    task = _store.get(int(task_id))
    if not task:
        return f"Task #{task_id} not found."
    lines = [
        f"Task #{task['id']}: {task['title']}",
        f"  Status:      {task['status']}",
        f"  Owner:       {task.get('owner') or '(none)'}",
        f"  Active form: {task.get('active_form') or '(none)'}",
        f"  Description: {task.get('description') or '(none)'}",
        f"  Blocked by:  {task.get('blocked_by', [])}",
        f"  Blocks:      {task.get('blocks', [])}",
        f"  Result:      {task.get('result') or '(none)'}",
        f"  Created:     {task.get('created_at')}",
        f"  Updated:     {task.get('updated_at')}",
    ]
    return "\n".join(lines)


def complete_task(task_id: int, result: str = "") -> str:
    if _store is None:
        return "[Task tracker not initialized]"
    return _store.complete(int(task_id), result)


def unassign_agent_tasks(agent_id: str) -> str:
    """Release all in_progress tasks owned by this agent (call on agent shutdown)."""
    if _store is None:
        return "[Task tracker not initialized]"
    count = _store.unassign(agent_id)
    return f"Released {count} task(s) from agent {agent_id}."
