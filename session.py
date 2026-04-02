"""
Session identity — mirrors Claude Code's STATE singleton + QueryEngine split.

A Session holds the three objects that define a single conversation:
history, token tracker, and task store. On /clear, Agent creates a new
Session (fresh UUID, empty history) rather than mutating the old one.
"""

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from memory.history import ConversationHistory
from memory.token_tracker import TokenTracker


@dataclass
class Session:
    session_id: str
    started_at: float
    history: ConversationHistory
    token_tracker: TokenTracker
    # task_store is injected after construction (circular import avoidance)
    task_store: object  # TaskStore

    @staticmethod
    def new(config: dict) -> "Session":
        from memory.tasks import TaskStore

        memory_dir = Path(__file__).parent / "memory"
        memory_dir.mkdir(exist_ok=True)
        history_file = memory_dir / "conversation.json"
        task_db = Path(__file__).parent / config.get("task_db", "memory/tasks.db")

        history = ConversationHistory(
            history_file,
            max_history=config.get("max_history", 100),
        )
        token_tracker = TokenTracker()
        task_store = TaskStore(task_db)

        # Wire task tools to the new store immediately
        try:
            from tools.task_tools import set_store
            set_store(task_store)
        except Exception:
            pass

        return Session(
            session_id=str(uuid.uuid4()),
            started_at=time.time(),
            history=history,
            token_tracker=token_tracker,
            task_store=task_store,
        )
