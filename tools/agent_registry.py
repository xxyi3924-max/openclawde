"""
Global registry for all background agent instances.

Enables:
  - Message passing: coordinator → worker via send_to_agent()
  - Abort signaling: stop_agent() sets abort event the agent checks each iteration
  - Progress tracking: tool counts + token usage per agent
  - Status queries: list_agents() for coordinator situational awareness

Mirrors Claude Code's LocalAgentTask state + InProcessTeammateTask patterns.
"""

import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def new_agent_id() -> str:
    return f"agent_{uuid.uuid4().hex[:8]}"


@dataclass
class AgentProgress:
    tool_use_count: int = 0
    # input_tokens: latest only (matches Claude Code: "cumulative latest value")
    input_tokens: int = 0
    # output_tokens: running sum (matches Claude Code: "cumulative sum")
    output_tokens: int = 0
    recent_activities: list = field(default_factory=list)  # last 5 tool names
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def record_tool(self, tool_name: str):
        self.tool_use_count += 1
        self.recent_activities.append(tool_name)
        if len(self.recent_activities) > 5:
            self.recent_activities.pop(0)

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0):
        if input_tokens:
            self.input_tokens = input_tokens  # latest, not cumulative
        if output_tokens:
            self.output_tokens += output_tokens  # cumulative sum


@dataclass
class AgentEntry:
    agent_id: str
    task_id: Optional[int]
    description: str
    abort_event: threading.Event = field(default_factory=threading.Event)
    message_queue: queue.Queue = field(default_factory=queue.Queue)
    progress: AgentProgress = field(default_factory=AgentProgress)
    # running | completed | failed | killed
    status: str = "running"


_registry: dict[str, AgentEntry] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

def register(agent_id: str, task_id: Optional[int], description: str) -> AgentEntry:
    entry = AgentEntry(agent_id=agent_id, task_id=task_id, description=description)
    with _lock:
        _registry[agent_id] = entry
    return entry


def deregister(agent_id: str):
    with _lock:
        _registry.pop(agent_id, None)


def get(agent_id: str) -> Optional[AgentEntry]:
    with _lock:
        return _registry.get(agent_id)


def find_by_task_id(task_id: int) -> Optional[AgentEntry]:
    with _lock:
        for entry in _registry.values():
            if entry.task_id == task_id:
                return entry
    return None


# ------------------------------------------------------------------
# Messaging (coordinator → worker)
# ------------------------------------------------------------------

def send_message(agent_id: str, message: str) -> bool:
    """Queue a message for a running background agent. Returns True if agent found."""
    entry = get(agent_id)
    if entry and entry.status == "running":
        entry.message_queue.put(message)
        return True
    return False


def drain_messages(agent_id: str) -> list[str]:
    """Non-blocking drain of all pending messages queued for this agent."""
    entry = get(agent_id)
    if not entry:
        return []
    messages = []
    while True:
        try:
            messages.append(entry.message_queue.get_nowait())
        except queue.Empty:
            break
    return messages


# ------------------------------------------------------------------
# Abort / stop
# ------------------------------------------------------------------

def abort(agent_id: str) -> bool:
    """Signal abort for an agent. Returns True if it was found and running."""
    entry = get(agent_id)
    if entry and entry.status == "running":
        entry.abort_event.set()
        entry.status = "killed"
        return True
    return False


def is_aborted(agent_id: str) -> bool:
    entry = get(agent_id)
    return entry is not None and entry.abort_event.is_set()


# ------------------------------------------------------------------
# Progress
# ------------------------------------------------------------------

def record_tool(agent_id: str, tool_name: str):
    entry = get(agent_id)
    if entry:
        entry.progress.record_tool(tool_name)


def record_tokens(agent_id: str, input_tokens: int = 0, output_tokens: int = 0):
    entry = get(agent_id)
    if entry:
        entry.progress.record_tokens(input_tokens, output_tokens)


def set_status(agent_id: str, status: str):
    entry = get(agent_id)
    if entry:
        entry.status = status


# ------------------------------------------------------------------
# Query
# ------------------------------------------------------------------

def get_all_statuses() -> list[dict]:
    with _lock:
        return [
            {
                "agent_id": e.agent_id,
                "task_id": e.task_id,
                "description": e.description,
                "status": e.status,
                "tool_use_count": e.progress.tool_use_count,
                "input_tokens": e.progress.input_tokens,
                "output_tokens": e.progress.output_tokens,
                "recent_activities": list(e.progress.recent_activities),
                "started_at": e.progress.started_at,
            }
            for e in _registry.values()
        ]


def get_running() -> list[dict]:
    return [s for s in get_all_statuses() if s["status"] == "running"]
