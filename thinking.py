"""
Provider-aware thinking/reasoning support for OpenAI-compatible APIs.

Each provider exposes thinking differently. This module normalises:
  - request parameterisation (what to add to the API call)
  - response extraction   (where thinking lives in the response)
  - multi-turn rules      (whether to strip reasoning from history)

Supported providers / patterns
────────────────────────────────────────────────────────────────────────────────
Provider        Detection                   API param               Response field
────────────────────────────────────────────────────────────────────────────────
MiniMax         provider=="minimax"         extra_body reasoning_split=True
                                                                    msg.reasoning_details[]
DeepSeek-R1     "deepseek-reasoner" in      (always on)             msg.reasoning_content
                model
DeepSeek-chat   "deepseek" in base_url      extra_body thinking      msg.reasoning_content
Fireworks       "fireworks" in base_url     extra_body thinking      msg.reasoning_content
OpenRouter      "openrouter" in base_url    extra_body reasoning     msg.reasoning (string)
xAI Grok-3-mini "grok-3-mini" in model     reasoning_effort kwarg   msg.reasoning_content
Together AI     "together" in base_url      (always on for R1)      <think> tags in content
Generic         fallback                    extra_body thinking      msg.reasoning_content
                                            (ignored if unsupported)
────────────────────────────────────────────────────────────────────────────────
"""

import re
from typing import Optional


# ── Detection helpers ──────────────────────────────────────────────────────────

def _m(model: str) -> str:
    return model.lower()

def _b(base_url: str) -> str:
    return (base_url or "").lower()


def is_thinking_capable(provider: str, model: str, base_url: str) -> bool:
    """True if this provider/model can produce thinking output at all."""
    m, b = _m(model), _b(base_url)
    if provider == "minimax":
        return True
    if "deepseek" in m or "deepseek" in b:
        return True
    if "fireworks" in b:
        return True
    if "openrouter" in b:
        return True
    if "grok-3-mini" in m:
        return True
    if "together" in b:
        return True
    # Generic OpenAI-compatible: may work, worth trying
    return True


def get_thinking_kwargs(
    provider: str,
    model: str,
    base_url: str,
    budget_tokens: int,
) -> dict:
    """
    Return kwargs to merge into client.chat.completions.create() to enable
    thinking for this provider/model.  Returns {} when not applicable.

    budget_tokens=0 → thinking disabled, always returns {}.
    """
    if not budget_tokens:
        return {}

    m, b = _m(model), _b(base_url)

    # MiniMax: reasoning_split flag; no token budget exposed
    if provider == "minimax":
        return {"extra_body": {"reasoning_split": True}}

    # DeepSeek-reasoner: thinking is always on — model name IS the trigger
    if "deepseek-reasoner" in m:
        return {}

    # DeepSeek-chat / V3.2 via DeepSeek API
    if "deepseek" in b or ("deepseek" in m and "reasoner" not in m):
        return {"extra_body": {"thinking": {"type": "enabled"}}}

    # Fireworks AI: Anthropic-compatible thinking param with budget
    if "fireworks" in b:
        return {"extra_body": {"thinking": {
            "type": "enabled",
            "budget_tokens": max(budget_tokens, 1024),  # minimum enforced by Fireworks
        }}}

    # OpenRouter: unified reasoning param
    if "openrouter" in b:
        return {"extra_body": {"reasoning": {"max_tokens": budget_tokens}}}

    # xAI Grok-3-mini: native top-level kwarg (not extra_body)
    if "grok-3-mini" in m:
        effort = "high" if budget_tokens >= 8000 else "low"
        return {"reasoning_effort": effort}

    # Together AI (DeepSeek-R1): thinking always embedded in content — no param
    if "together" in b:
        return {}

    # Generic / unknown OpenAI-compatible: try Anthropic-style thinking.
    # Many vLLM / local servers silently ignore unknown fields; real OpenAI
    # returns a 400 which the loop catches and retries without thinking.
    return {"extra_body": {"thinking": {
        "type": "enabled",
        "budget_tokens": budget_tokens,
    }}}


def extract_thinking(
    msg,
    provider: str,
    model: str,
    base_url: str,
) -> tuple[Optional[str], str]:
    """
    Return (thinking_text, answer_text) from a chat completion message.
    thinking_text is None when no thinking is present.
    answer_text is always the clean final answer (no <think> tags).
    """
    m, b = _m(model), _b(base_url)
    content = msg.content or ""

    # MiniMax: reasoning_details array  →  [{type: "reasoning.text", text: "..."}]
    if provider == "minimax":
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            parts = []
            for item in rd:
                text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
                if text:
                    parts.append(text)
            thinking = "\n".join(parts) or None
            return (thinking, content)
        return (None, content)

    # DeepSeek / Fireworks / xAI Grok-3-mini: reasoning_content field
    rc = getattr(msg, "reasoning_content", None)
    if rc:
        return (rc, content)

    # OpenRouter: reasoning string alias
    reasoning = getattr(msg, "reasoning", None)
    if reasoning:
        return (reasoning, content)

    # Together AI / any provider that embeds <think> tags in content
    if "<think>" in content and "</think>" in content:
        match = re.search(r"<think>(.*?)</think>(.*)", content, re.DOTALL)
        if match:
            thinking = match.group(1).strip() or None
            answer = match.group(2).strip()
            return (thinking, answer)

    return (None, content)


def should_strip_thinking_from_history(
    provider: str,
    model: str,
    base_url: str,
) -> bool:
    """
    Whether to strip reasoning_content from assistant messages before passing
    them back in multi-turn.

    DeepSeek: MUST strip — passing reasoning_content back causes HTTP 400.
    MiniMax: MUST NOT strip — reasoning_details must be preserved verbatim.
    Others: safe either way; strip for cleanliness.
    """
    m, b = _m(model), _b(base_url)
    if "deepseek" in b or "deepseek" in m:
        return True
    if provider == "minimax":
        return False  # preserve for interleaved reasoning continuity
    return True  # strip by default to keep history lean
