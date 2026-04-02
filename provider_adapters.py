"""
Provider adapters for the unified agent loop.

Each adapter encapsulates everything provider-specific:
  - System prompt format   (Anthropic: list with cache_control; OpenAI: first message)
  - Tool definition format (Anthropic: input_schema; OpenAI: function wrapper)
  - API call + streaming   (Anthropic: SSE blocks; OpenAI: blocking or stream)
  - Response parsing       (content blocks vs choices[0].message)
  - Assistant message      (content block list vs role+tool_calls dict)
  - Tool result messages   (Anthropic: single user msg with blocks; OpenAI: per-tool msgs)
  - Context pruning        (Anthropic: tool_result blocks; OpenAI: role=tool messages)
  - Thinking               (Anthropic: native; OpenAI: via thinking.py adapters)

The unified _agent_loop in agent.py calls only adapter methods — no provider
conditionals live in the loop itself. Mirrors Claude Code's callModel() abstraction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic as anthropic_module

from thinking import get_thinking_kwargs, extract_thinking, should_strip_thinking_from_history


# ── Shared data types ─────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    inputs: dict


@dataclass
class ParsedResponse:
    stop_reason: str           # "end_turn" | "tool_use"
    text: str                  # clean final answer (thinking stripped if needed)
    thinking: Optional[str]    # thinking content, or None
    tool_calls: list[ToolCall]
    usage: Any                 # raw SDK usage → token_tracker.update()
    raw_message: Any = None    # full SDK response object for make_assistant_message


# ── Base class ────────────────────────────────────────────────────────────────

class ProviderAdapter:
    """Interface each provider adapter must implement."""

    def build_system(self, system_text: str) -> Any:
        raise NotImplementedError

    def build_tools(self, tool_defs: list[dict]) -> list[dict]:
        raise NotImplementedError

    def build_messages_from_history(
        self, history: list[dict], ctx: int, system_text: str = ""
    ) -> list[dict]:
        raise NotImplementedError

    def call(
        self,
        client: Any,
        model: str,
        system: Any,
        tools: list[dict],
        messages: list[dict],
    ) -> ParsedResponse:
        raise NotImplementedError

    def make_assistant_message(self, parsed: ParsedResponse) -> dict:
        raise NotImplementedError

    def make_tool_result_messages(
        self, tool_calls: list[ToolCall], results: dict[str, str]
    ) -> list[dict]:
        raise NotImplementedError

    def inject_nudge(self, tool_result_messages: list[dict], nudge: str) -> list[dict]:
        """Append a system nudge to the last tool result message."""
        if not tool_result_messages:
            return tool_result_messages
        last = tool_result_messages[-1]
        updated = dict(last)
        updated["content"] = (updated.get("content") or "") + "\n\n" + nudge
        return tool_result_messages[:-1] + [updated]

    def prune_messages(self, messages: list[dict], keep_recent: int = 3) -> list[dict]:
        return messages


# ── Anthropic adapter ─────────────────────────────────────────────────────────

class AnthropicAdapter(ProviderAdapter):
    """Anthropic Messages API with streaming, prompt caching, and thinking blocks."""

    def __init__(self, config: dict, send_update: Optional[Callable] = None):
        self.config = config
        self.send_update = send_update
        self.thinking_budget = config.get("thinking_budget", 0)
        self.stream_idle_timeout = config.get("stream_idle_timeout_seconds", 90)

    def build_system(self, system_text: str) -> list[dict]:
        return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    def build_tools(self, tool_defs: list[dict]) -> list[dict]:
        if not tool_defs:
            return tool_defs
        last = {**tool_defs[-1], "cache_control": {"type": "ephemeral"}}
        return tool_defs[:-1] + [last]

    def build_messages_from_history(
        self, history: list[dict], ctx: int, system_text: str = ""
    ) -> list[dict]:
        # system_text is passed separately as system=; not injected here.
        raw = [
            {"role": m["role"], "content": m["content"]}
            for m in (history[-ctx:] if ctx else history)
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        ]
        messages: list[dict] = []
        for msg in raw:
            if messages and messages[-1]["role"] == msg["role"]:
                messages[-1]["content"] += "\n\n" + msg["content"]
            else:
                messages.append(dict(msg))
        while messages and messages[-1]["role"] == "assistant":
            messages.pop()
        return messages

    def call(self, client, model, system, tools, messages) -> ParsedResponse:
        kwargs: dict = dict(
            model=model,
            max_tokens=max(8192, self.thinking_budget + 4096) if self.thinking_budget else 8192,
            system=system,
            tools=tools,
            messages=messages,
        )
        if self.thinking_budget:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
        if self.stream_idle_timeout:
            kwargs["timeout"] = self.stream_idle_timeout

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        _cur_type: Optional[str] = None
        _cur_chunks: list[str] = []

        with client.messages.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    _cur_type = getattr(event.content_block, "type", None)
                    _cur_chunks = []
                elif etype == "content_block_delta":
                    delta = event.delta
                    dt = getattr(delta, "type", None)
                    if dt == "thinking_delta":
                        _cur_chunks.append(delta.thinking)
                    elif dt == "text_delta":
                        _cur_chunks.append(delta.text)
                elif etype == "content_block_stop":
                    body = "".join(_cur_chunks).strip()
                    if _cur_type == "thinking" and body:
                        thinking_parts.append(body)
                    elif _cur_type == "text" and body:
                        text_parts.append(body)
                        if self.send_update:
                            self.send_update(body)
            resp = stream.get_final_message()

        thinking = "\n".join(thinking_parts) or None
        if thinking and self.send_update:
            preview = thinking[:200] + ("..." if len(thinking) > 200 else "")
            self.send_update(f"💭 {preview}")

        tool_calls = [
            ToolCall(id=b.id, name=b.name, inputs=b.input)
            for b in resp.content if b.type == "tool_use"
        ]

        return ParsedResponse(
            stop_reason=resp.stop_reason or "end_turn",
            text="\n".join(text_parts).strip(),
            thinking=thinking,
            tool_calls=tool_calls,
            usage=resp.usage,
            raw_message=resp,
        )

    def make_assistant_message(self, parsed: ParsedResponse) -> dict:
        content = []
        for block in parsed.raw_message.content:
            if block.type == "thinking":
                content.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                })
            elif block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        return {"role": "assistant", "content": content}

    def make_tool_result_messages(self, tool_calls, results) -> list[dict]:
        blocks = [
            {"type": "tool_result", "tool_use_id": tc.id, "content": results[tc.id]}
            for tc in tool_calls
        ]
        return [{"role": "user", "content": blocks}]

    def inject_nudge(self, tool_result_messages, nudge) -> list[dict]:
        # For Anthropic the results are blocks inside one user message
        if not tool_result_messages:
            return tool_result_messages
        msg = tool_result_messages[0]
        blocks = list(msg.get("content", []))
        if blocks:
            last_block = dict(blocks[-1])
            existing = last_block.get("content", "") or ""
            last_block["content"] = existing + "\n\n" + nudge
            blocks = blocks[:-1] + [last_block]
        return [{"role": "user", "content": blocks}]

    def prune_messages(self, messages, keep_recent=3):
        tool_result_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        ]
        to_prune = tool_result_indices[:-keep_recent] if len(tool_result_indices) > keep_recent else []
        pruned_results = pruned_thinking = 0
        for idx in to_prune:
            new_blocks = []
            for block in messages[idx]["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > 300:
                        block = {**block, "content": text[:300] + " …[pruned]"}
                        pruned_results += 1
                new_blocks.append(block)
            messages[idx] = {**messages[idx], "content": new_blocks}
            if idx > 0 and messages[idx - 1].get("role") == "assistant":
                asst = messages[idx - 1]
                if isinstance(asst.get("content"), list):
                    stripped = [b for b in asst["content"] if not (isinstance(b, dict) and b.get("type") == "thinking")]
                    pruned_thinking += len(asst["content"]) - len(stripped)
                    messages[idx - 1] = {**asst, "content": stripped}
        if pruned_results or pruned_thinking:
            import time
            from datetime import datetime
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [Prune] tool_results={pruned_results} thinking_blocks={pruned_thinking}")
        return messages


# ── OpenAI adapter ────────────────────────────────────────────────────────────

class OpenAIAdapter(ProviderAdapter):
    """OpenAI Chat Completions adapter — also covers MiniMax and other compatible providers."""

    def __init__(
        self,
        config: dict,
        provider: str,
        model: str,
        base_url: str,
        send_update: Optional[Callable] = None,
    ):
        self.config = config
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.send_update = send_update
        self.thinking_budget = config.get("thinking_budget", 0)
        self._thinking_unsupported = False
        self._strip_thinking = should_strip_thinking_from_history(provider, model, base_url)

    def build_system(self, system_text: str) -> str:
        return system_text  # injected as first message in build_messages_from_history

    def build_tools(self, tool_defs: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tool_defs
        ]

    def build_messages_from_history(
        self, history: list[dict], ctx: int, system_text: str = ""
    ) -> list[dict]:
        msgs: list[dict] = []
        if system_text:
            msgs.append({"role": "system", "content": system_text})
        for m in (history[-ctx:] if ctx else history):
            if isinstance(m.get("content"), str):
                msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def call(self, client, model, system, tools, messages) -> ParsedResponse:
        # system was already injected into messages by build_messages_from_history
        kwargs: dict = dict(model=model, messages=messages, tools=tools)

        if not self._thinking_unsupported and self.thinking_budget:
            thinking_kwargs = get_thinking_kwargs(
                self.provider, model, self.base_url, self.thinking_budget
            )
            kwargs.update(thinking_kwargs)

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            err = str(e)
            if not self._thinking_unsupported and (
                "unknown_parameter" in err or "unknown field" in err.lower()
            ):
                self._thinking_unsupported = True
                kwargs.pop("extra_body", None)
                kwargs.pop("reasoning_effort", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise

        msg = resp.choices[0].message
        thinking_text, answer_text = extract_thinking(
            msg, self.provider, model, self.base_url
        )
        if thinking_text and self.send_update:
            preview = thinking_text[:200] + ("..." if len(thinking_text) > 200 else "")
            self.send_update(f"💭 {preview}")

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for call in msg.tool_calls:
                try:
                    inputs = json.loads(call.function.arguments)
                except Exception:
                    inputs = {}
                tool_calls.append(ToolCall(
                    id=call.id, name=call.function.name, inputs=inputs
                ))

        return ParsedResponse(
            stop_reason="tool_use" if msg.tool_calls else "end_turn",
            text=answer_text,
            thinking=thinking_text,
            tool_calls=tool_calls,
            usage=resp.usage,
            raw_message=msg,
        )

    def make_assistant_message(self, parsed: ParsedResponse) -> dict:
        msg = parsed.raw_message
        entry: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        # MiniMax: preserve reasoning_details for interleaved reasoning continuity
        rd = getattr(msg, "reasoning_details", None)
        if rd and not self._strip_thinking:
            entry["reasoning_details"] = rd
        return entry

    def make_tool_result_messages(self, tool_calls, results) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": tc.id, "content": results[tc.id]}
            for tc in tool_calls
        ]

    def prune_messages(self, messages, keep_recent=3):
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        to_prune = tool_indices[:-keep_recent] if len(tool_indices) > keep_recent else []
        pruned = 0
        for idx in to_prune:
            content = messages[idx].get("content", "")
            if isinstance(content, str) and len(content) > 300:
                messages[idx] = {**messages[idx], "content": content[:300] + " …[pruned]"}
                pruned += 1
        if pruned:
            from datetime import datetime
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [Prune/OAI] tool_results={pruned}")
        return messages
