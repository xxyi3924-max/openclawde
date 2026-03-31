import json
from pathlib import Path


class ConversationHistory:
    """Manages conversation history persistence."""

    def __init__(self, path: Path, max_history: int = 100):
        self.path = path
        self.max_history = max_history
        self.history: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self.history = json.load(f)
            except Exception as e:
                print(f"[History] Load failed: {e}")
                self.history = []

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.history[-self.max_history:], f, indent=2, default=str)

    def clear(self):
        self.history = []
        self.save()

    def append(self, msg: dict):
        self.history.append(msg)

    def get_recent(self, n: int) -> list[dict]:
        return self.history[-n:] if n else self.history[:]

    def __len__(self) -> int:
        return len(self.history)

    # ------------------------------------------------------------------
    # Phase 5: Context compaction (stubs — implemented in Phase 5)
    # ------------------------------------------------------------------

    def should_compact(self, threshold: int) -> bool:
        return len(self.history) > threshold

    def compact(self, summarize_fn, keep_recent: int = 10) -> str | None:
        """
        Summarize history[:-keep_recent] into a single synthetic message.
        Returns the summary text, or None if nothing to compact.
        (Full implementation in Phase 5)
        """
        if len(self.history) <= keep_recent:
            return None
        to_summarize = self.history[:-keep_recent]
        kept = self.history[-keep_recent:]
        summary_text = summarize_fn(to_summarize)
        summary_msg = {
            "role": "user",
            "content": (
                f"[Context Summary — covers {len(to_summarize)} earlier messages]\n"
                f"{summary_text}"
            ),
        }
        self.history = [summary_msg] + kept
        self.save()
        return summary_text
