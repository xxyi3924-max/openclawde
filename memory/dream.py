"""
autoDream — background memory consolidation.

Triggered when all three gates pass:
1. Time gate:    hours since last dream >= dream_interval_hours (default 24)
2. Session gate: sessions_since_dream >= dream_min_sessions (default 5)
3. Lock gate:    fcntl.flock(LOCK_EX | LOCK_NB) succeeds (no concurrent dream)

4-phase consolidation cycle:
  Orient → Gather Signal → Consolidate → Prune
"""

import fcntl
import json
import threading
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).parent
_MEMORY_FILE = _BASE / "MEMORY.md"
_STATE_FILE = _BASE / "dream_state.json"
_LOCK_FILE = _BASE / "dream.lock"
_NOTES_DIR = _BASE.parent / "workspace" / "notes"


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_dream_ts": None, "sessions_since_dream": 0}


def _save_state(state: dict):
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _collect_notes() -> str:
    if not _NOTES_DIR.exists():
        return ""
    notes = [p.read_text(encoding="utf-8") for p in sorted(_NOTES_DIR.glob("*.md"))]
    return "\n\n---\n\n".join(notes)


def _collect_recent_log(lines: int = 100) -> str:
    run_log = _BASE / "run.log"
    if not run_log.exists():
        return ""
    all_lines = run_log.read_text(encoding="utf-8").splitlines()
    return "\n".join(all_lines[-lines:])


class DreamConsolidator:
    """Background memory consolidation — runs in a daemon thread."""

    def __init__(self, config: dict, api_client):
        self.config = config
        self.client = api_client  # Anthropic or OpenAI client from Agent
        self._state = _load_state()
        self._bg_thread: threading.Thread | None = None
        self._provider = config.get("provider", "anthropic")

    def on_session_start(self):
        """Call on bot startup. Increments session counter."""
        self._state["sessions_since_dream"] = self._state.get("sessions_since_dream", 0) + 1
        _save_state(self._state)
        self._maybe_trigger()

    def on_task_complete(self):
        """Call after each agent respond() cycle."""
        self._maybe_trigger()

    def _maybe_trigger(self):
        if not self.config.get("dream_enabled", True):
            return
        if not self._check_time_gate():
            return
        if not self._check_session_gate():
            return
        self._trigger_dream()

    def _check_time_gate(self) -> bool:
        interval_h = self.config.get("dream_interval_hours", 24)
        last = self._state.get("last_dream_ts")
        if not last:
            return True
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            return elapsed >= interval_h * 3600
        except Exception:
            return True

    def _check_session_gate(self) -> bool:
        min_sessions = self.config.get("dream_min_sessions", 5)
        return self._state.get("sessions_since_dream", 0) >= min_sessions

    def _trigger_dream(self):
        if self._bg_thread and self._bg_thread.is_alive():
            return
        self._bg_thread = threading.Thread(target=self._dream_cycle, daemon=True, name="autoDream")
        self._bg_thread.start()

    def _dream_cycle(self):
        """Full consolidation cycle. Runs in a daemon thread."""
        _LOCK_FILE.parent.mkdir(exist_ok=True)
        lock_fd = None
        try:
            lock_fd = open(_LOCK_FILE, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("[Dream] Another dream is running — skipping")
            if lock_fd:
                lock_fd.close()
            return

        print("[Dream] Starting consolidation cycle...")
        try:
            # Phase 1: Orient
            current_memory = _MEMORY_FILE.read_text(encoding="utf-8") if _MEMORY_FILE.exists() else ""
            notes = _collect_notes()
            recent_log = _collect_recent_log(100)

            # Phase 2: Gather signals per category
            signals = self._gather_signal(current_memory, notes, recent_log)

            # Phase 3: Consolidate into new MEMORY.md
            new_memory = self._consolidate(signals, current_memory)

            # Phase 4: Prune to line limit
            max_lines = self.config.get("dream_memory_max_lines", 200)
            pruned = self._prune(new_memory, max_lines)

            _MEMORY_FILE.write_text(pruned, encoding="utf-8")

            # Update state
            self._state["last_dream_ts"] = datetime.now().isoformat()
            self._state["sessions_since_dream"] = 0
            _save_state(self._state)

            print(f"[Dream] Complete. MEMORY.md: {len(pruned.splitlines())} lines")

        except Exception as e:
            print(f"[Dream] Error during consolidation: {e}")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            _LOCK_FILE.unlink(missing_ok=True)

    def _call_model(self, prompt: str, max_tokens: int = 1024) -> str:
        """Make a cheap, non-streaming API call for consolidation work."""
        dream_model = self.config.get("dream_model", "claude-haiku-4-5-20251001")
        try:
            if self._provider == "anthropic":
                resp = self.client.messages.create(
                    model=dream_model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text
            else:
                resp = self.client.chat.completions.create(
                    model=self.config.get("fallback_model", "gpt-4o-mini"),
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.choices[0].message.content or ""
        except Exception as e:
            return f"[unavailable: {e}]"

    def _gather_signal(self, current_memory: str, notes: str, recent_log: str) -> dict:
        """Extract typed signals from source material."""
        sources = f"NOTES:\n{notes}\n\nRECENT LOG (last 100 lines):\n{recent_log}"

        def extract(category: str, guidance: str) -> str:
            prompt = (
                f"Extract {category} from the following agent session data.\n"
                f"{guidance}\n"
                "Be concise. Use bullet points. Max 10 items.\n\n"
                f"{sources[:3000]}"
            )
            return self._call_model(prompt, max_tokens=512)

        return {
            "user_prefs": extract(
                "User Preferences",
                "Things the user prefers, communication style, recurring requests, dislikes."
            ),
            "feedback": extract(
                "Feedback & Corrections",
                "Things the agent did wrong, corrections given, approaches to avoid or repeat."
            ),
            "project": extract(
                "Project Context",
                "Active projects, goals, deadlines, key files, current state of work."
            ),
            "reference": extract(
                "Reference & How-Tos",
                "Commands, file paths, credentials format, how-tos that should be remembered."
            ),
        }

    def _consolidate(self, signals: dict, current_memory: str) -> str:
        """Merge signals + current memory into new structured MEMORY.md."""
        prompt = (
            "You are consolidating an AI agent's memory into a structured MEMORY.md file.\n"
            "Merge the existing memory with new signals. Remove contradictions. Keep it dense and useful.\n"
            "Format:\n"
            "## User Preferences\n<bullet points>\n\n"
            "## Feedback & Corrections\n<bullet points>\n\n"
            "## Project Context\n<bullet points>\n\n"
            "## Reference\n<bullet points>\n\n"
            "Target: 100-180 lines total.\n\n"
            f"EXISTING MEMORY:\n{current_memory[:2000]}\n\n"
            f"NEW USER PREFERENCES:\n{signals['user_prefs']}\n\n"
            f"NEW FEEDBACK:\n{signals['feedback']}\n\n"
            f"NEW PROJECT CONTEXT:\n{signals['project']}\n\n"
            f"NEW REFERENCE:\n{signals['reference']}"
        )
        return self._call_model(prompt, max_tokens=2048)

    def _prune(self, text: str, max_lines: int) -> str:
        """Trim to max_lines by removing from the bottom of each section proportionally."""
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text

        # Find section boundaries
        section_starts = [i for i, l in enumerate(lines) if l.startswith("## ")]
        if not section_starts:
            return "\n".join(lines[:max_lines])

        # Build sections as slices
        section_starts.append(len(lines))
        sections = []
        for i in range(len(section_starts) - 1):
            sections.append(lines[section_starts[i]:section_starts[i + 1]])

        # Trim each section proportionally
        total = len(lines)
        excess = total - max_lines
        per_section = max(1, excess // len(sections))

        trimmed = []
        for s in sections:
            if len(s) > per_section + 1:
                trimmed.extend(s[:-per_section])
            else:
                trimmed.extend(s)

        return "\n".join(trimmed[:max_lines])
