import concurrent.futures
import json
import re
import threading
import time
from dataclasses import dataclass, replace as _dc_replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
import anthropic as anthropic_module
from openai import OpenAI, RateLimitError as OpenAIRateLimitError

import sandbox
import tools as tool_module
import agents as agent_types_module
import hooks as hooks_module
from coordinator import get_coordinator_system_prompt, is_coordinator_mode
from memory.token_log import log_tokens
from memory.dream import DreamConsolidator
from session import Session
from thinking import get_thinking_kwargs, extract_thinking, should_strip_thinking_from_history
from provider_adapters import AnthropicAdapter, OpenAIAdapter
from tools import agent_registry

MEMORY_DIR = Path(__file__).parent / "memory"
MEMORY_DIR.mkdir(exist_ok=True)
TOKEN_LOG = MEMORY_DIR / "token_usage.jsonl"
RUN_LOG = MEMORY_DIR / "run.log"


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# Claude Code uses unlimited for the main interactive loop and 200 for fork subagents.
# We mirror that: main agent = unlimited unless config sets max_iterations,
# subagents = 200 unless their agent type sets max-turns.
_SUBAGENT_DEFAULT_MAX_TURNS = 200

CONTINUE_TOKEN = "[[CONTINUE]]"
SILENT_TOKEN = "[[SILENT]]"


@dataclass(frozen=True)
class TurnState:
    """Immutable per-iteration control state for the agentic loop.

    Mirrors Claude Code's local State struct in queryLoop(). The messages
    list is kept separate (it is appended to in-place); only scalar control
    fields live here. Advance with _dc_replace(state, ...).
    """
    turn_count: int = 0
    last3_calls: tuple = ()  # last ≤3 call signatures for stuck-loop detection

SYSTEM_PROMPT = """You are an autonomous AI agent running on the user's Mac, accessed via Telegram. Do the work — never ask the user to run commands or verify output yourself.

## Safety
You have no independent goals: do not pursue self-preservation, replication, resource acquisition, or power-seeking. Avoid long-term plans beyond the user's request.
Prioritize safety and human oversight over task completion. If instructions conflict with safety, pause and ask. Comply with stop/pause requests immediately and never bypass safeguards.
Do not manipulate anyone to expand your access or disable safeguards. Do not modify your own system prompt or safety rules unless explicitly requested by the user.

## Tool call style
Do not narrate routine, low-risk tool calls — just call the tool.
Narrate only when it genuinely helps: multi-step work, complex or risky actions (deletions, overwrites), or when the user explicitly asks.
Keep narration brief and value-dense. Use plain language; avoid restating obvious steps.

## Core rules
- Always start with a tool call. Never open with text.
- Call multiple tools in one response whenever they're independent (e.g. grep two patterns, read two files, run two checks). This cuts round-trips and saves tokens.
- Finish in as few iterations as possible. 10-15 is normal. 30+ means you're stuck — stop and report.
- Ask the user only for: missing credentials, genuine ambiguity blocking all paths, or after 5+ failed attempts.

## File workflow
1. grep_files(pattern, path) — always first. Use multi-pattern regex to find everything at once: "funcA|funcB|CONSTANT|error" searches all in one call. Never call grep multiple times when one pattern covers it.
2. read_file(path, start_line, end_line) — REQUIRES both line numbers. Blocked without them. Only read after grep gives you exact line numbers.
3. edit_file(path, old_string, new_string) — surgical edits. Requires exactly ONE match — will fail if 0 or multiple matches are found. Use grep_files() if it fails to locate the exact current text.
4. write_file() — only for brand-new files.

## Think first, then act
Before making any tool call, use your thinking block to build a complete plan:
1. What is the end goal?
2. What do I already know vs. what do I need to find out?
3. List every tool call needed to finish the task — group them by which can run in parallel.
4. Only then start calling tools.

After each iteration, update your plan in thinking: cross off completed steps, adjust if results were surprising. Never make a tool call you haven't already reasoned about — reactive one-off calls waste iterations.

## Parallel tool calls — always call as many as possible
Every time you make a tool call, ask: "what else can I call RIGHT NOW that doesn't need these results?" Call all of them together. Calling one tool at a time is always wrong unless the next call depends on the result.

Target: 4-6 tools per iteration during exploration, 2-4 during implementation.

## Task management
For multi-step work, use create_task() to break the goal into subtasks before starting.
Use update_task(id, "in_progress") before starting each subtask.
Use complete_task(id, result) when done. This keeps your work organized and lets you resume correctly after hitting iteration limits.
Call list_tasks() at the start of any continuation to see what's already done.

## Verification
Run tests only when a change could realistically break something. Skip syntax checks, curl, and print-debugging after routine edits. Trust your changes.

## Multi-step tasks
Use send_message() to give progress updates mid-task, then end your response with [[CONTINUE]].
Omit [[CONTINUE]] when done.

## Notes — required after every task
Call write_note() on completion. Record: what was built, how to run it, key file paths, user preferences learned.
Notes are loaded automatically next session — use them.

## Workspace
Call list_files() once at task start. All work goes in workspace/. If files already exist, continue from them — never restart.
Before writing new code, call list_tools() — a ready-made tool may exist in workspace/tools/.

## Autonomy — work independently
You are an autonomous agent. Minimize human interaction.

- **Never stop mid-task to ask for confirmation.** If you can infer what to do, do it.
- **Use [[CONTINUE]] freely** for multi-step work. Your work resumes automatically.
- **Use queue_self_task()** to schedule follow-up work for yourself. It runs automatically after the current turn — no human needed.
- **Use exit_plan_mode(plan)** to leave planning mode yourself. Do NOT wait for `/execute`.
- **Use set_goal(goal)** to set a persistent goal you'll work toward across sessions.
- After completing a task, proactively check: is there an obvious next step? If yes, queue_self_task() it.
- Only involve the human when: credentials are missing, the requirement is genuinely ambiguous, or you've hit a hard blocker after trying alternatives.

## Agent types
Spawn specialized sub-agents with run_agent(agent_type="explore"|"plan"|"verify"|"worker"):
- explore — read-only, haiku model, maps codebase fast
- plan    — produces a concrete implementation plan, no execution
- verify  — runs tests and gives PASS/FAIL verdict
- worker  — full tool set, autonomous implementation

## Skills
Skills are reusable prompt macros. Invoke with invoke_skill or /skill_name in Telegram.

## Sub-agents
Use run_agent(description, prompt, background) to delegate self-contained tasks.
Background agents run concurrently — use send_to_agent() to direct them mid-run.

## Silent replies
When you have nothing to say (e.g. a tool already sent the update, or the task is fully self-contained), respond with ONLY: [[SILENT]]
Rules:
- It must be your entire message — nothing else.
- Never append it to a real reply.
- Never use it when the user asked a question or expects a response.
"""


class Agent:
    def __init__(self, config: dict, send_update=None):
        self.config = config
        self.send_update = send_update
        self.provider = config.get("provider", "anthropic")
        self.model = config.get("model", "claude-sonnet-4-6")

        # Session holds history, token tracker, and task store as one unit.
        # Rebuild this object on /clear to get a fresh session ID + clean state.
        self.session = Session.new(config)
        self._apply_persisted_grants()

        # Phase 2: set by main.py — Telegram handler and callback queue for permission prompts
        self._tg = None
        self._callback_queue = None
        self._current_chat_id: int = 0

        # Multi-agent: set when this Agent is running as a sub-agent/worker
        self._agent_id: str | None = None
        # When set, overrides TOOL_DEFINITIONS for this agent instance
        self._sub_agent_tools: list[dict] | None = None

        # Autonomous mode: skip all permission confirmation, auto-approve everything
        self._autonomous = config.get("autonomous", False)

        # Agent type (set when spawned as explore/plan/verify/worker)
        self._agent_type_name: str | None = None
        self._agent_type_system_prompt: str | None = None

        # Per-instance turn limit (set by agent type; None = use config default)
        self._max_turns: Optional[int] = None

        # Query source tag — used in logs and compaction to distinguish call origins
        self.query_source: str = "user"

        # Fallback model state
        self._active_model = self.model
        self._using_fallback = False
        self._recovery_timer: threading.Timer | None = None

        if self.provider == "anthropic":
            api_key = config.get("anthropic_api_key") or None
            self.client = Anthropic(api_key=api_key) if api_key else Anthropic()
            self._base_url = ""
        elif self.provider == "minimax":
            api_key = config.get("minimax_api_key") or None
            self._base_url = config.get("openai_base_url", "https://api.minimax.io/v1")
            self.client = OpenAI(api_key=api_key, base_url=self._base_url)
        else:
            api_key = config.get("openai_api_key") or None
            self._base_url = config.get("openai_base_url", "")
            self.client = OpenAI(api_key=api_key, base_url=self._base_url or None) if api_key else OpenAI()

        # Phase 6: autoDream memory consolidation (needs self.client)
        self.dream = DreamConsolidator(config, self.client)

        # Wire sub-agent factory (skills + agent tool need a way to spawn fresh agents)
        from tools.skill_tool import set_skill_runner
        from tools.agent_tool import set_agent_factory
        set_agent_factory(lambda: Agent(self.config), output_dir=None)
        set_skill_runner(lambda prompt: Agent(self.config).respond(prompt))

        # Load MCP servers (no-op if mcp package not installed or no mcp.json)
        try:
            from mcp_manager import load_mcp
            load_mcp()
            tool_module.reload_dynamic_tools()  # rebuild TOOL_DEFINITIONS to include MCP tools
        except Exception as e:
            _log(f"[MCP] Skipped: {e}")

        _log(f"[Agent] session={self.session.session_id[:8]} loaded {len(self.history)} history entries")

    # ------------------------------------------------------------------
    # Session property shims — keep all code using self.history etc. working
    # ------------------------------------------------------------------

    @property
    def history(self):
        return self.session.history

    @property
    def token_tracker(self):
        return self.session.token_tracker

    @property
    def task_store(self):
        return self.session.task_store

    # ------------------------------------------------------------------
    # History helpers
    # ------------------------------------------------------------------

    def _apply_persisted_grants(self):
        for msg in self.history.history:
            content = msg.get("content", "")
            if isinstance(content, str):
                m = re.search(r"you may access ([^\.\n,]+)", content, re.IGNORECASE)
                if m:
                    sandbox.grant_access(m.group(1).strip())

    def clear_history(self):
        self.session = Session.new(self.config)
        _log(f"[Agent] /clear — new session={self.session.session_id[:8]}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def respond(self, user_message: str) -> str:
        m = re.search(r"you may access ([^\.\n,]+)", user_message, re.IGNORECASE)
        if m:
            sandbox.grant_access(m.group(1).strip())

        # Safety gate 1: session wall-clock timeout (default 6h for autonomous phone use).
        # Claude Code's bridge uses 24h; 6h is tighter for unattended Telegram sessions.
        timeout_hours = self.config.get("session_timeout_hours", 6)
        if timeout_hours:
            elapsed = (time.time() - self.session.started_at) / 3600
            if elapsed > timeout_hours:
                _log(f"[Session] Wall-clock timeout after {elapsed:.1f}h (limit {timeout_hours}h)")
                return (
                    f"⏱ Session expired after {timeout_hours}h of continuous use. "
                    f"Send /clear to start a fresh session."
                )

        # Safety gate 2: blocking limit — context window nearly full.
        # TokenTracker.is_blocking() is defined but was never wired here; now it is.
        if self.token_tracker.is_blocking(self._active_model):
            _log(f"[Blocking] {self.token_tracker.status_line(self._active_model)}")
            return (
                "⚠️ Context window is nearly full and cannot accept more input. "
                "Send /compact to summarise history, or /clear to start fresh."
            )

        self.history.append({"role": "user", "content": user_message})

        # Build permission_fn for this turn (requires chat_id from respond() caller)
        _permission_fn = self._make_permission_fn(getattr(self, "_current_chat_id", 0))

        max_retries = 5
        raw: tuple[str, str] = ("", "error")
        for attempt in range(max_retries):
            try:
                if self.provider == "anthropic":
                    raw = self._anthropic_loop(_permission_fn)
                elif self.provider == "minimax":
                    raw = self._openai_loop(_permission_fn)
                elif self.config.get("use_responses_api"):
                    raw = self._openai_responses_loop(_permission_fn)
                else:
                    raw = self._openai_loop(_permission_fn)
                break
            except (OpenAIRateLimitError, anthropic_module.RateLimitError) as e:
                raw = self._handle_rate_limit(str(e))
                _log(f"[Rate limit] {e}")
                break
            except (anthropic_module.APIConnectionError, anthropic_module.APITimeoutError) as e:
                if attempt < max_retries - 1:
                    wait = min(2 ** attempt, 16)
                    _log(f"[Connection error] attempt {attempt+1}/{max_retries}, retrying in {wait}s: {e}")
                    time.sleep(wait)
                    continue
                _log(f"[Agent error] {e}")
                raw = (f"Connection error after {max_retries} attempts — network may be unstable.", "error")
                break
            except Exception as e:
                err = str(e)
                _log(f"[Agent error] {err}")
                if "400" in err and "flagged" in err.lower():
                    self.history.history.pop()
                    raw = (
                        "Your message was flagged by the content filter. "
                        "If this keeps happening, send /clear to reset the conversation history.",
                        "error",
                    )
                else:
                    raw = (f"Error: {e}", "error")
                break

        response, exit_reason = raw

        # Agent embedded [[CONTINUE]] in its reply (multi-step continuation)
        if CONTINUE_TOKEN in response:
            response = response.replace(CONTINUE_TOKEN, "").strip()
            tool_module._continuation = (
                "Continue the task from where you left off. "
                "Check workspace/ files to see what was already done and pick up from the next step."
            )
        # Loop hit its turn limit — set continuation for automatic resume
        elif exit_reason == "max_turns":
            tool_module._continuation = (
                "You hit the iteration limit mid-task. Check workspace/ files to see what was "
                "already completed, then continue from where you left off. Do not redo finished work."
            )

        if response.strip() == SILENT_TOKEN:
            response = ""

        self.history.append({"role": "assistant", "content": response})
        self.history.save()
        return response

    # ------------------------------------------------------------------
    # Rate limit handling
    # ------------------------------------------------------------------

    def _handle_rate_limit(self, error_detail: str) -> tuple[str, str]:
        fallback_model = self.config.get("fallback_model", "gpt-4o-mini")
        cooldown = self.config.get("fallback_cooldown_seconds", 60)

        if not self._using_fallback:
            self._switch_to_fallback(fallback_model, cooldown)
            notice = f"Rate limit hit on {self.model}. Switched to {fallback_model} for now — will retry primary in {cooldown}s."
            _log(f"[Fallback] {notice}")
            try:
                if self.provider == "anthropic":
                    return self._anthropic_loop()
                elif self.provider == "minimax":
                    return self._openai_loop()
                elif self.config.get("use_responses_api"):
                    return self._openai_responses_loop()
                else:
                    return self._openai_loop()
            except Exception as e:
                return (f"{notice}\n(Fallback also failed: {e})", "error")
        else:
            return (f"Still rate limited on primary. Using {self._active_model}. Primary retries in {cooldown}s.", "error")

    def _switch_to_fallback(self, fallback_model: str, cooldown: int):
        self._active_model = fallback_model
        self._using_fallback = True
        if self._recovery_timer:
            self._recovery_timer.cancel()
        self._recovery_timer = threading.Timer(cooldown, self._recover_primary)
        self._recovery_timer.daemon = True
        self._recovery_timer.start()

    def _recover_primary(self):
        self._active_model = self.model
        self._using_fallback = False
        self._recovery_timer = None
        _log(f"[Fallback] Recovered — back to primary model: {self.model}")

    # ------------------------------------------------------------------
    # Anthropic autonomous loop
    # ------------------------------------------------------------------

    def _build_cached_system(self) -> list[dict]:
        # Priority: agent type > coordinator mode > default
        if self._agent_type_system_prompt:
            base = self._agent_type_system_prompt
        elif is_coordinator_mode():
            base = get_coordinator_system_prompt()
        else:
            base = SYSTEM_PROMPT

        parts = [base]

        if tool_module._planning_mode:
            parts.append(
                "\n## PLANNING MODE ACTIVE\n"
                "You are in read-only planning mode. Write/execute tools are not available.\n"
                "Your job: research the task using read tools, then present a clear step-by-step plan as text.\n"
                "Do NOT attempt to execute anything. End with: 'Send /execute when ready to proceed.'"
            )

        from memory import load_context
        context = load_context(task_store=self.task_store)
        if context:
            parts.append(f"\n## Persistent memory from previous sessions\n{context}")

        return [{"type": "text", "text": "\n".join(parts), "cache_control": {"type": "ephemeral"}}]

    def _build_cached_tools(self) -> list[dict]:
        # Sub-agent tool override (set by agent_tool.py to restrict what workers can do)
        if self._sub_agent_tools is not None:
            tools = list(self._sub_agent_tools)
        elif tool_module._planning_mode:
            # In planning mode only expose tools where planning_allowed=True
            tools = [
                td.to_api_dict()
                for td in tool_module._BUILTIN_TOOL_DEFS
                if td.planning_allowed
            ]
        else:
            tools = list(tool_module.TOOL_DEFINITIONS)

        if not tools:
            return tools
        last = dict(tools[-1])
        last["cache_control"] = {"type": "ephemeral"}
        return tools[:-1] + [last]

    def _build_system_text(self) -> str:
        """Plain-text version of _build_cached_system() for OpenAI-format loops.

        Produces the same content as _build_cached_system() (agent type, coordinator,
        planning mode, persistent memory) but as a single string — no cache_control
        markers, compatible with OpenAI's role:system message format.
        """
        if self._agent_type_system_prompt:
            base = self._agent_type_system_prompt
        elif is_coordinator_mode():
            base = get_coordinator_system_prompt()
        else:
            base = SYSTEM_PROMPT

        parts = [base]

        if tool_module._planning_mode:
            parts.append(
                "\n## PLANNING MODE ACTIVE\n"
                "You are in read-only planning mode. Write/execute tools are not available.\n"
                "Your job: research the task using read tools, then present a clear step-by-step plan as text.\n"
                "Do NOT attempt to execute anything. End with: 'Send /execute when ready to proceed.'"
            )

        from memory import load_context
        context = load_context(task_store=self.task_store)
        if context:
            parts.append(f"\n## Persistent memory from previous sessions\n{context}")

        return "\n".join(parts)

    def _get_tool_defs(self) -> list[dict]:
        """Return raw tool definitions without provider-specific formatting.

        Adapters call build_tools() on these to add cache_control (Anthropic)
        or wrap in the "function" schema (OpenAI).
        """
        if self._sub_agent_tools is not None:
            return list(self._sub_agent_tools)
        elif tool_module._planning_mode:
            return [td.to_api_dict() for td in tool_module._BUILTIN_TOOL_DEFS if td.planning_allowed]
        return list(tool_module.TOOL_DEFINITIONS)

    def _agent_loop(self, adapter, permission_fn=None) -> tuple[str, str]:
        """Unified agentic loop — provider-agnostic.

        All provider-specific work (API call, message format, tool format,
        streaming, thinking extraction, pruning) is delegated to `adapter`.
        This loop body is the same for both Anthropic and OpenAI providers,
        mirroring Claude Code's single queryLoop().
        """
        ctx = self.config.get("max_context_messages", 20)
        system_text = self._build_system_text()
        system = adapter.build_system(system_text)
        tools = adapter.build_tools(self._get_tool_defs())
        messages = adapter.build_messages_from_history(self.history.history, ctx, system_text)
        initial_msg_count = len(messages)
        max_loop_pairs = self.config.get("max_loop_pairs", 12)

        if self._agent_id is None:
            max_turns = self.config.get("max_iterations")  # None = unlimited (main agent)
        else:
            max_turns = self._max_turns or self.config.get("max_iterations", _SUBAGENT_DEFAULT_MAX_TURNS)

        state = TurnState()

        while max_turns is None or state.turn_count < max_turns:
            if tool_module.is_cancelled():
                return ("Interrupted.", "interrupted")

            # Context sliding: keep in-loop pairs bounded so messages don't grow unboundedly
            loop_msgs = messages[initial_msg_count:]
            if len(loop_msgs) > max_loop_pairs * 2:
                excess = len(loop_msgs) - max_loop_pairs * 2
                trim = excess + (excess % 2)
                messages = messages[:initial_msg_count] + loop_msgs[trim:]

            messages = adapter.prune_messages(messages)

            try:
                parsed = adapter.call(self.client, self._active_model, system, tools, messages)
            except Exception as e:
                err = str(e)
                status = getattr(e, "status_code", 0)
                is_too_long = (
                    "prompt is too long" in err.lower()
                    or "context_length_exceeded" in err
                    or "maximum context length" in err.lower()
                    or "413" in err
                    or status == 413
                )
                if is_too_long:
                    _log(f"[ReactiveCompact] Context too long, compacting and retrying: {err[:120]}")
                    keep_recent = self.config.get("compaction_keep_recent", 10)
                    summary = self.history.compact(self._summarize_history, keep_recent)
                    if summary:
                        messages = adapter.build_messages_from_history(
                            self.history.history, ctx, self._build_system_text()
                        )
                        initial_msg_count = len(messages)
                        parsed = adapter.call(self.client, self._active_model, system, tools, messages)
                    else:
                        raise
                else:
                    raise

            log_tokens(TOKEN_LOG, state.turn_count, parsed.stop_reason, parsed.usage, messages)
            self.token_tracker.update(parsed.usage)

            if self._agent_id and parsed.usage:
                inp = getattr(parsed.usage, "input_tokens", None) or getattr(parsed.usage, "prompt_tokens", 0) or 0
                out = getattr(parsed.usage, "output_tokens", None) or getattr(parsed.usage, "completion_tokens", 0) or 0
                agent_registry.record_tokens(self._agent_id, input_tokens=inp, output_tokens=out)

            if parsed.thinking:
                _log(f"[Thinking] {parsed.thinking[:120]}...")

            if self.token_tracker.should_warn(self._active_model):
                self.token_tracker._warned = True
                warn_msg = f"⚠️ {self.token_tracker.status_line(self._active_model)} — will auto-compact soon."
                _log(f"[Context] {warn_msg}")
                if self.send_update:
                    self.send_update(warn_msg)

            if self.token_tracker.should_compact(self._active_model):
                _log(f"[Compact] Token threshold reached ({self.token_tracker.status_line(self._active_model)}). Auto-compacting.")
                _prev_source = self.query_source
                try:
                    self.query_source = "compact"
                    keep_recent = self.config.get("compaction_keep_recent", 10)
                    summary = self.history.compact(self._summarize_history, keep_recent)
                    self.query_source = _prev_source
                    if summary:
                        _log(f"[Compact] Done. Summary: {len(summary)} chars.")
                        self.token_tracker.reset()
                        messages = adapter.build_messages_from_history(
                            self.history.history, ctx, self._build_system_text()
                        )
                        initial_msg_count = len(messages)
                except Exception as e:
                    self.query_source = _prev_source
                    _log(f"[Compact] Failed: {e}")
                    self.token_tracker.record_failure()
                    if self.token_tracker.circuit_open:
                        _log("[Compact] Circuit open — compaction disabled for this session.")

            if parsed.stop_reason == "end_turn":
                return (parsed.text, "end_turn")

            if parsed.stop_reason != "tool_use" or not parsed.tool_calls:
                return (f"Unexpected stop reason: {parsed.stop_reason}", "error")

            new_turn = state.turn_count + 1
            _aid = self._agent_id or ""
            _max_str = "∞" if max_turns is None else max_turns

            def _exec_call(tc, _permission_fn=permission_fn, _aid=_aid):
                _log(f"[Tool {new_turn}/{_max_str}|{self.query_source}] {tc.name}({json.dumps(tc.inputs)[:120]})")
                result = self._cap_tool_result(
                    str(tool_module.execute(tc.name, tc.inputs, self.send_update, _permission_fn, _aid))
                )
                _log(f"[Result] {result[:200]}")
                if self._agent_id:
                    agent_registry.record_tool(self._agent_id, tc.name)
                return tc.id, result

            if len(parsed.tool_calls) > 1:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    futures = {executor.submit(_exec_call, tc): tc for tc in parsed.tool_calls}
                    results_map = {tc_id: res for future in futures for tc_id, res in [future.result()]}
            else:
                tc_id, res = _exec_call(parsed.tool_calls[0])
                results_map = {tc_id: res}

            messages.append(adapter.make_assistant_message(parsed))
            tool_result_msgs = adapter.make_tool_result_messages(parsed.tool_calls, results_map)

            nudges = []
            if len(parsed.tool_calls) == 1:
                nudges.append("[SYSTEM] You called 1 tool. What else could you call in parallel right now? Aim for 3+ tools per iteration.")
            remaining = None if max_turns is None else max_turns - new_turn
            if remaining is not None and remaining <= 10:
                nudges.append(
                    f"[SYSTEM] {new_turn}/{max_turns} iterations used, {remaining} remaining. "
                    f"Batch all remaining tool calls — no single-tool responses."
                )
            if nudges:
                tool_result_msgs = adapter.inject_nudge(tool_result_msgs, "\n".join(nudges))

            messages.extend(tool_result_msgs)

            if self._agent_id and agent_registry.is_aborted(self._agent_id):
                return ("[Agent stopped by coordinator.]", "aborted")
            if self._agent_id:
                for coord_msg in agent_registry.drain_messages(self._agent_id):
                    _log(f"[Agent] Received coordinator message: {coord_msg[:80]}")
                    messages.append({"role": "user", "content": f"[Message from coordinator]: {coord_msg}"})

            call_sig = "|".join(
                f"{tc.name}:{json.dumps(tc.inputs, sort_keys=True)}" for tc in parsed.tool_calls
            )
            new_last3 = (state.last3_calls + (call_sig,))[-3:]
            state = _dc_replace(state, turn_count=new_turn, last3_calls=new_last3)
            if len(state.last3_calls) == 3 and len(set(state.last3_calls)) == 1:
                return (
                    "Stuck in a loop — same tool call repeated 3 times. Stop and tell the user what you tried and what failed.",
                    "stuck",
                )

        return ("Still working — hit iteration limit, resuming automatically...", "max_turns")

    def _cap_tool_result(self, result: str) -> str:
        """Truncate oversized tool results to prevent context explosion.

        Matches Claude Code's per-tool size limits (toolLimits.ts). Default 20,000
        chars; configurable via max_tool_result_chars in config.
        """
        max_chars = self.config.get("max_tool_result_chars", 20_000)
        if len(result) > max_chars:
            trimmed = len(result) - max_chars
            return result[:max_chars] + f"\n…[{trimmed:,} chars truncated — result too large]"
        return result

    def _make_permission_fn(self, chat_id: int):
        """
        Returns a permission_fn closure that asks the user via Telegram inline keyboard.
        In autonomous mode, always approves without asking.
        """
        # Autonomous mode or sub-agents always auto-approve
        if self._autonomous or self._agent_id is not None:
            def _auto(name, inputs, risk):
                return "approved"
            _auto._auto_approve_level = "HIGH"
            return _auto

        auto_level = self.config.get("auto_approve_level", "LOW")
        tg = getattr(self, "_tg", None)
        callback_queue = getattr(self, "_callback_queue", None)

        def permission_fn(name: str, inputs: dict, risk) -> str:
            if not tg or not callback_queue:
                return "approved"  # no UI available, auto-approve

            # Format a readable summary of the inputs
            try:
                import json as _json
                args_str = _json.dumps(inputs, ensure_ascii=False)[:300]
            except Exception:
                args_str = str(inputs)[:300]

            msg = (
                f"[{risk.value} risk] Tool: {name}\n"
                f"Args: {args_str}\n\n"
                f"Approve?"
            )
            tg.send_inline_keyboard(chat_id, msg, [("Approve", f"approve:{name}"), ("Deny", f"deny:{name}")])

            import queue as _queue
            try:
                data = callback_queue.get(timeout=120)
                return "approved" if data.startswith("approve:") else "denied"
            except _queue.Empty:
                return "denied"  # timeout = deny

        permission_fn._auto_approve_level = auto_level
        return permission_fn

    def _anthropic_loop(self, permission_fn=None) -> tuple[str, str]:
        return self._agent_loop(
            AnthropicAdapter(self.config, send_update=self.send_update), permission_fn
        )

    # ------------------------------------------------------------------
    # OpenAI Chat Completions loop
    # ------------------------------------------------------------------

    def _openai_loop(self, permission_fn=None) -> tuple[str, str]:
        return self._agent_loop(
            OpenAIAdapter(self.config, self.provider, self._active_model, self._base_url, send_update=self.send_update),
            permission_fn,
        )

    # ------------------------------------------------------------------
    # OpenAI Responses API loop
    # ------------------------------------------------------------------

    def _openai_responses_loop(self, permission_fn=None) -> tuple[str, str]:
        ctx = self.config.get("max_context_messages", 20)
        input_messages = []
        for m in self.history.get_recent(ctx):
            if isinstance(m.get("content"), str):
                input_messages.append({"role": m["role"], "content": m["content"]})

        responses_tools = [
            {
                "type": "function",
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            }
            for t in self._build_cached_tools()
        ]

        iterations = 0
        if self._agent_id is None:
            max_turns = self.config.get("max_iterations")
        else:
            max_turns = self._max_turns or self.config.get("max_iterations", _SUBAGENT_DEFAULT_MAX_TURNS)

        while max_turns is None or iterations < max_turns:
            if tool_module.is_cancelled():
                return ("Interrupted.", "interrupted")

            resp = self.client.responses.create(
                model=self._active_model,
                instructions=SYSTEM_PROMPT,
                input=input_messages,
                tools=responses_tools,
            )

            text_parts = []
            tool_calls = []
            for item in resp.output:
                if item.type == "message":
                    for block in item.content:
                        if hasattr(block, "text"):
                            text_parts.append(block.text)
                elif item.type == "function_call":
                    tool_calls.append(item)

            if not tool_calls:
                return ("\n".join(text_parts).strip(), "end_turn")

            iterations += 1
            for item in resp.output:
                input_messages.append(item)

            for call in tool_calls:
                try:
                    inputs = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
                except Exception:
                    inputs = {}
                _log(f"[Tool {iterations}/{'∞' if max_turns is None else max_turns}|{self.query_source}] {call.name}({str(inputs)[:120]})")
                result = str(tool_module.execute(call.name, inputs, self.send_update, permission_fn))
                _log(f"[Result] {result[:200]}")
                input_messages.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result,
                })

        return ("Still working — hit iteration limit, resuming automatically...", "max_turns")

    # ------------------------------------------------------------------
    # Phase 5: Context compaction helpers (stubs — activated in Phase 5)
    # ------------------------------------------------------------------

    def _summarize_history(self, messages: list[dict]) -> str:
        """Summarize old history messages into a compact block via a cheap API call."""
        lines = []
        for m in messages:
            role = m.get("role", "?").upper()
            content = m.get("content", "")
            if isinstance(content, str):
                lines.append(f"{role}: {content[:300]}")
        prompt = (
            "Summarize this conversation history concisely. Preserve: "
            "completed tasks and their outcomes, important file paths, "
            "user preferences learned, key decisions made, and any unresolved issues. "
            "Be dense — this replaces the full history.\n\n"
            + "\n".join(lines)
        )
        try:
            resp = self.client.messages.create(
                model=self.config.get("compaction_model", "claude-haiku-4-5-20251001"),
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            return f"[Summary unavailable: {e}]"
