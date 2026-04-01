"""
Token-aware compaction tracker.

Tracks input token usage per session and determines when the context window
is approaching capacity. Mirrors Claude Code's autoCompact.ts logic.

Design:
  input_tokens  = LATEST value from API (already includes cache reads in the count)
  output_tokens = CUMULATIVE sum (each response adds to the total)

Thresholds (matching Claude Code defaults):
  AUTOCOMPACT_BUFFER = 13,000   trigger compaction this many tokens before limit
  WARNING_BUFFER     = 20,000   warn user at this threshold
  BLOCKING_BUFFER    =  3,000   refuse new messages if within this of limit
  OUTPUT_RESERVE     = 20,000   headroom reserved for compaction summary output
"""

from dataclasses import dataclass, field

CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000

OUTPUT_RESERVE = 20_000
AUTOCOMPACT_BUFFER = 13_000
WARNING_BUFFER = 20_000
BLOCKING_BUFFER = 3_000
MAX_CONSECUTIVE_FAILURES = 3


def _get_window(model: str) -> int:
    """Return raw context window for this model (before reserve deduction)."""
    normalized = model.lower()
    for key, size in CONTEXT_WINDOWS.items():
        if key in normalized or normalized in key:
            return size
    return DEFAULT_CONTEXT_WINDOW


def _effective_window(model: str) -> int:
    """Usable context = full window minus output reserve."""
    return _get_window(model) - OUTPUT_RESERVE


@dataclass
class TokenTracker:
    input_tokens: int = 0     # latest from API (not cumulative)
    output_tokens: int = 0    # cumulative sum
    compaction_failures: int = 0
    _warned: bool = field(default=False, repr=False)

    def update(self, usage) -> None:
        """Call after every API response with the usage object."""
        if usage is None:
            return
        if hasattr(usage, "input_tokens") and usage.input_tokens:
            self.input_tokens = usage.input_tokens
        if hasattr(usage, "output_tokens") and usage.output_tokens:
            self.output_tokens += usage.output_tokens

    def reset(self) -> None:
        """Call after successful compaction to reset counters."""
        self.input_tokens = 0
        self.output_tokens = 0
        self.compaction_failures = 0
        self._warned = False

    def record_failure(self) -> None:
        self.compaction_failures += 1

    @property
    def circuit_open(self) -> bool:
        """True when compaction has failed too many times — stop retrying."""
        return self.compaction_failures >= MAX_CONSECUTIVE_FAILURES

    def should_compact(self, model: str) -> bool:
        if self.circuit_open:
            return False
        limit = _effective_window(model)
        return self.input_tokens > 0 and self.input_tokens >= (limit - AUTOCOMPACT_BUFFER)

    def should_warn(self, model: str) -> bool:
        if self._warned:
            return False
        limit = _effective_window(model)
        return self.input_tokens > 0 and self.input_tokens >= (limit - WARNING_BUFFER)

    def is_blocking(self, model: str) -> bool:
        limit = _effective_window(model)
        return self.input_tokens > 0 and self.input_tokens >= (limit - BLOCKING_BUFFER)

    def pct_used(self, model: str) -> float:
        limit = _effective_window(model)
        if limit <= 0:
            return 0.0
        return round(self.input_tokens / limit * 100, 1)

    def status_line(self, model: str) -> str:
        pct = self.pct_used(model)
        left = _effective_window(model) - self.input_tokens
        circuit = " [CIRCUIT OPEN — compaction disabled]" if self.circuit_open else ""
        return f"Context: {pct}% used ({self.input_tokens:,} / {_effective_window(model):,} tokens, {left:,} remaining){circuit}"
