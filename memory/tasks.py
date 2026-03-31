"""
Task tracker — SQLite backend, WAL mode, thread-safe.

Schema mirrors Claude Code's Task type:
  id, title, description, active_form, status, owner,
  blocks, blocked_by, metadata, result, created_at, updated_at

Key operations:
  create()       — make a new pending task
  get()          — fetch by id
  list_all()     — filtered list
  update()       — set status, result, owner, active_form
  claim()        — atomic "mark in_progress + set owner", with dependency check
  block()        — add bidirectional dependency between two tasks
  unassign()     — clear owner from all tasks owned by an agent (on shutdown)
  complete()     — shorthand for update(status=completed)
  summary_for_prompt() — compact block for system prompt injection
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    description  TEXT    DEFAULT '',
    active_form  TEXT    DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'pending',
    owner        TEXT    DEFAULT '',
    blocks       TEXT    DEFAULT '[]',
    blocked_by   TEXT    DEFAULT '[]',
    metadata     TEXT    DEFAULT '{}',
    result       TEXT    DEFAULT '',
    created_at   TEXT,
    updated_at   TEXT
);
"""

# Migration: add columns introduced after initial schema
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN active_form TEXT DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN owner TEXT DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN blocks TEXT DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN blocked_by TEXT DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN metadata TEXT DEFAULT '{}'",
]

_VALID_STATUSES = {"pending", "in_progress", "completed", "failed"}


class TaskStore:
    """SQLite-backed task tracker with dependency blocking and agent ownership."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            with self._conn() as conn:
                conn.execute(_SCHEMA)
                # Apply migrations idempotently (ignore "duplicate column" errors)
                for sql in _MIGRATIONS:
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError:
                        pass

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        # Parse JSON fields so callers get plain lists/dicts
        for key in ("blocks", "blocked_by", "metadata"):
            try:
                d[key] = json.loads(d.get(key) or "[]" if key != "metadata" else d.get(key) or "{}")
            except Exception:
                d[key] = [] if key != "metadata" else {}
        return d

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str = "",
        depends_on: list[int] | None = None,
        owner: str = "",
        metadata: dict | None = None,
    ) -> int:
        now = self._now()
        blocked_by = json.dumps(depends_on or [])
        meta = json.dumps(metadata or {})
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO tasks "
                    "(title, description, active_form, status, owner, blocks, blocked_by, metadata, created_at, updated_at) "
                    "VALUES (?, ?, '', 'pending', ?, '[]', ?, ?, ?, ?)",
                    (title, description, owner, blocked_by, meta, now, now),
                )
                task_id = cur.lastrowid

        # If this task is declared blocked by others, register ourselves in their blocks list
        if depends_on:
            for dep_id in depends_on:
                self._add_to_blocks(dep_id, task_id)

        return task_id

    def _add_to_blocks(self, task_id: int, blocked_task_id: int):
        """Add blocked_task_id to task_id's blocks list (bidirectional linkage)."""
        with self._lock:
            with self._conn() as conn:
                row = conn.execute("SELECT blocks FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row:
                    blocks = json.loads(row["blocks"] or "[]")
                    if blocked_task_id not in blocks:
                        blocks.append(blocked_task_id)
                        conn.execute(
                            "UPDATE tasks SET blocks = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(blocks), self._now(), task_id),
                        )

    def get(self, task_id: int) -> dict | None:
        with self._lock:
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                return self._row_to_dict(row) if row else None

    def list_all(self, filter_status: str = "all") -> list[dict]:
        with self._lock:
            with self._conn() as conn:
                if filter_status == "all":
                    rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE status = ? ORDER BY id", (filter_status,)
                    ).fetchall()
                return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(
        self,
        task_id: int,
        status: str | None = None,
        result: str = "",
        owner: str | None = None,
        active_form: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        task = self.get(task_id)
        if not task:
            return f"Task {task_id} not found."

        if status is not None and status not in _VALID_STATUSES:
            return f"Invalid status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"

        # Dependency check when moving to in_progress
        if status == "in_progress":
            blocked_by = task.get("blocked_by", [])
            blockers = []
            for dep_id in blocked_by:
                dep = self.get(dep_id)
                if dep and dep["status"] != "completed":
                    blockers.append(f"#{dep_id} {dep['title']} ({dep['status']})")
            if blockers:
                return (
                    f"Task {task_id} is blocked by:\n"
                    + "\n".join(f"  - {b}" for b in blockers)
                    + "\nComplete dependencies first."
                )

        with self._lock:
            with self._conn() as conn:
                fields = ["updated_at = ?"]
                values: list = [self._now()]
                if status is not None:
                    fields.append("status = ?")
                    values.append(status)
                if result:
                    fields.append("result = ?")
                    values.append(result)
                if owner is not None:
                    fields.append("owner = ?")
                    values.append(owner)
                if active_form is not None:
                    fields.append("active_form = ?")
                    values.append(active_form)
                if metadata is not None:
                    fields.append("metadata = ?")
                    values.append(json.dumps(metadata))
                values.append(task_id)
                conn.execute(
                    f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values
                )

        parts = []
        if status:
            parts.append(f"→ {status}")
        if result:
            parts.append(f": {result}")
        if owner is not None:
            parts.append(f" (owner: {owner or 'unassigned'})")
        return f"Task {task_id} " + "".join(parts)

    def complete(self, task_id: int, result: str = "") -> str:
        return self.update(task_id, status="completed", result=result)

    # ------------------------------------------------------------------
    # Claim — atomic in_progress + owner assignment
    # ------------------------------------------------------------------

    def claim(
        self,
        task_id: int,
        agent_id: str,
        check_agent_busy: bool = False,
    ) -> str:
        """
        Atomically claim a task for an agent.

        Returns one of:
          "claimed"          — success
          "not_found"        — task doesn't exist
          "already_claimed"  — another agent owns it
          "blocked"          — unmet dependencies
          "agent_busy"       — agent already has an in_progress task (if check_agent_busy=True)
          "already_done"     — task is completed/failed
        """
        with self._lock:
            # Re-fetch inside lock for atomicity
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not row:
                    return "not_found"
                task = self._row_to_dict(row)

                if task["status"] in ("completed", "failed"):
                    return "already_done"

                current_owner = task.get("owner", "")
                if current_owner and current_owner != agent_id:
                    return "already_claimed"

                # Check dependencies
                blocked_by = task.get("blocked_by", [])
                for dep_id in blocked_by:
                    dep_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (dep_id,)).fetchone()
                    if dep_row and dep_row["status"] != "completed":
                        return "blocked"

                # Check if agent is already busy with another task
                if check_agent_busy:
                    busy = conn.execute(
                        "SELECT id FROM tasks WHERE owner = ? AND status = 'in_progress' AND id != ?",
                        (agent_id, task_id),
                    ).fetchone()
                    if busy:
                        return "agent_busy"

                # Claim it
                conn.execute(
                    "UPDATE tasks SET status = 'in_progress', owner = ?, updated_at = ? WHERE id = ?",
                    (agent_id, self._now(), task_id),
                )
        return "claimed"

    # ------------------------------------------------------------------
    # Bidirectional block setup
    # ------------------------------------------------------------------

    def block(self, task_id: int, blocked_by_id: int) -> str:
        """
        Declare that task_id is blocked by blocked_by_id.
        Updates both tasks: task_id.blocked_by += [blocked_by_id],
                            blocked_by_id.blocks += [task_id]
        """
        task = self.get(task_id)
        blocker = self.get(blocked_by_id)
        if not task:
            return f"Task {task_id} not found."
        if not blocker:
            return f"Task {blocked_by_id} not found."

        with self._lock:
            with self._conn() as conn:
                # Add to task_id.blocked_by
                blocked_by = json.loads(conn.execute(
                    "SELECT blocked_by FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()["blocked_by"] or "[]")
                if blocked_by_id not in blocked_by:
                    blocked_by.append(blocked_by_id)
                    conn.execute(
                        "UPDATE tasks SET blocked_by = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(blocked_by), self._now(), task_id),
                    )

                # Add to blocked_by_id.blocks
                blocks = json.loads(conn.execute(
                    "SELECT blocks FROM tasks WHERE id = ?", (blocked_by_id,)
                ).fetchone()["blocks"] or "[]")
                if task_id not in blocks:
                    blocks.append(task_id)
                    conn.execute(
                        "UPDATE tasks SET blocks = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(blocks), self._now(), blocked_by_id),
                    )

        return f"Task {task_id} is now blocked by #{blocked_by_id} {blocker['title']}"

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def unassign(self, agent_id: str) -> int:
        """Clear ownership of all tasks owned by agent_id. Returns count updated."""
        with self._lock:
            with self._conn() as conn:
                result = conn.execute(
                    "UPDATE tasks SET owner = '', updated_at = ? "
                    "WHERE owner = ? AND status = 'in_progress'",
                    (self._now(), agent_id),
                )
                return result.rowcount

    def get_agent_tasks(self, agent_id: str) -> list[dict]:
        """Return all tasks currently owned by an agent."""
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE owner = ? ORDER BY id", (agent_id,)
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Summary for prompt injection
    # ------------------------------------------------------------------

    def summary_for_prompt(self) -> str:
        tasks = self.list_all("all")
        active = [t for t in tasks if t["status"] in ("pending", "in_progress")]
        if not active:
            return ""
        lines = ["## Active tasks"]
        for t in active:
            blocked = t.get("blocked_by", [])
            owner = t.get("owner", "")
            active_form = t.get("active_form", "")
            parts = [f"  [{t['id']:3d}] {t['status'].upper():11s} {t['title']}"]
            if owner:
                parts.append(f"  (owner: {owner})")
            if active_form:
                parts.append(f"  [{active_form}]")
            if blocked:
                parts.append(f"  (blocked by: {blocked})")
            lines.append("".join(parts))
        return "\n".join(lines)
