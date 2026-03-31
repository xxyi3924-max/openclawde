import json
from datetime import datetime
from pathlib import Path


def log_tokens(token_log_path: Path, iteration: int, label: str, usage, messages: list):
    """Log a detailed token breakdown entry to token_usage.jsonl."""
    input_tok = getattr(usage, "input_tokens", 0) or 0
    output_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    history_chars = sum(
        len(str(m.get("content", ""))) for m in messages
        if isinstance(m.get("content"), str)
    )
    tool_result_chars = sum(
        len(str(b.get("content", "")))
        for m in messages if isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    thinking_chars = sum(
        len(str(b.get("thinking", "")))
        for m in messages if isinstance(m.get("content"), list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "thinking"
    )

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "iteration": iteration,
        "label": label,
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "billable_input": input_tok - cache_read + cache_write,
        "context_breakdown": {
            "history_chars": history_chars,
            "tool_result_chars": tool_result_chars,
            "thinking_chars": thinking_chars,
            "messages_in_context": len(messages),
        },
    }
    token_log_path.parent.mkdir(exist_ok=True)
    with open(token_log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    print(
        f"[Tokens i={iteration}] in={input_tok} out={output_tok} "
        f"cache_read={cache_read} cache_write={cache_write} | "
        f"ctx_msgs={len(messages)} tool_res_chars={tool_result_chars} thinking_chars={thinking_chars}"
    )
