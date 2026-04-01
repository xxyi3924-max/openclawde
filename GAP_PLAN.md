# openclawde → Claude Code Gap Plan

Analysis of the Claude Code source against openclawde's current implementation.
Gaps are grouped into phases by value and feasibility in Python.
Gaps that are impossible to close (tmux backends, GrowthBook flags, CCR remote agents) are listed at the bottom and explained.

---

## Current state snapshot

openclawde already has:
- Coordinator/worker multi-agent with abort + message queues
- Task system (SQLite, bidirectional deps, atomic claim, owner)
- Skills (.md files, fork/inline context)
- MCP support (stdio + sse, persistent async session)
- Permission tiers (LOW/MEDIUM/HIGH, Telegram inline keyboard)
- Planning mode (read-only tool filter)
- Context compaction (summarize old history)
- autoDream (background memory consolidation)
- Rate limit fallback

What Claude Code has that openclawde doesn't — prioritized below.

---

## Phase 1 — Agent types + ExitPlanMode tool
**Value: very high. Effort: low.**

### 1a. Named built-in agent types

Claude Code has `Explore`, `Plan`, `general-purpose`, and others. Each has its own:
- System prompt
- Tool allowlist / blocklist
- Default model

Port this as a `agents/` directory with `.md` or `.py` agent definitions.
`run_agent()` accepts an optional `agent_type` parameter.

```
agents/
  explore.md      Read-only codebase explorer. Uses haiku. Blocks write/exec tools.
  plan.md         Produces implementation plans as markdown. Blocks exec tools.
  verify.md       Runs tests, checks output, reports pass/fail.
  worker.md       Default worker in coordinator mode. Full tool set minus spawn tools.
```

Each definition specifies:
- `system_prompt` — replaces the default SYSTEM_PROMPT for that agent type
- `disallowed_tools` — names removed from the tool list before passing to API
- `model` — default model (`"haiku"`, `"sonnet"`, `"inherit"`)

In `agent_tool.py`, `run_agent()` gains `agent_type: str = "worker"` parameter.
Agent factory reads the definition and applies it before calling `respond()`.

**Why it matters:** The coordinator can now say "run a read-only Explore agent to map the codebase" instead of hoping the worker doesn't accidentally write files.

---

### 1b. ExitPlanMode tool

Currently the user must type `/execute` in Telegram to leave planning mode.
Claude Code gives the *agent* an `exit_plan_mode` tool — the agent presents its plan as text, then calls the tool, which restores the full tool set and continues execution in the same turn.

New tool: `exit_plan_mode(plan: str)`.

```python
# tools/file_tools.py or a new tools/plan_tools.py
def exit_plan_mode(plan: str) -> str:
    tool_module.set_planning_mode(False)
    # Store the plan text so it can be shown to the user
    return f"[Plan mode exited. Proceeding with execution.]\n\nPlan:\n{plan}"
```

Add to `_BUILTIN_TOOL_DEFS` with `planning_allowed=True` (it only makes sense in plan mode) and `risk=LOW`.

This closes the awkward UX where the agent finishes planning but then stops and waits for a `/execute` command before it can do anything.

---

## Phase 2 — Token-aware auto-compaction
**Value: high. Effort: medium.**

openclawde's compaction triggers on *message count* (e.g., after 40 messages).
Claude Code triggers on *actual token usage* — specifically 13,000 tokens before the model's context limit.

This is more accurate: a 40-message conversation with one-word answers is fine; a 10-message conversation with large file reads might be at 95% context.

### What to build

**`memory/token_tracker.py`** — track cumulative token usage across the conversation:

```python
class TokenTracker:
    input_tokens: int = 0    # latest value (not sum — counts cached tokens too)
    output_tokens: int = 0   # cumulative sum

    def update(self, usage): ...
    def should_compact(self, model: str) -> bool: ...
    def warning_state(self, model: str) -> dict: ...
```

**Model context windows** (hardcoded dict):
```python
CONTEXT_WINDOWS = {
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
}
AUTOCOMPACT_BUFFER = 13_000   # trigger this many tokens before limit
WARNING_BUFFER = 20_000       # warn at this many tokens before limit
OUTPUT_RESERVE = 20_000       # reserved for the compaction summary output
```

**Trigger logic:**
```python
def should_compact(self, model: str) -> bool:
    limit = CONTEXT_WINDOWS.get(model, 200_000)
    effective = limit - OUTPUT_RESERVE
    threshold = effective - AUTOCOMPACT_BUFFER
    return self.input_tokens >= threshold
```

**In `agent.py` `_anthropic_loop()`:** After each API response, call `token_tracker.update(resp.usage)`. Before the next call, check `should_compact()` — if true, run compaction.

**Warn the user:** If approaching WARNING_BUFFER, `send_update("⚠️ Context at X% — will compact soon.")`.

**Circuit breaker:** Track consecutive compaction failures. After 3, stop trying and warn user instead.

---

## Phase 3 — Hooks system
**Value: high. Effort: medium.**

Claude Code's hooks let external scripts respond to agent events. This makes openclawde programmable without modifying Python source.

### Events to implement

| Event | When fired | Hook can do |
|---|---|---|
| `SessionStart` | Bot starts, first message received | Run setup scripts, load env |
| `PreToolUse` | Before any tool executes | Approve/deny/modify inputs |
| `PostToolUse` | After any tool completes | Log, notify, trigger CI |
| `PermissionRequest` | When permission gate asks user | Override decision programmatically |
| `Notification` | When agent calls send_message() | Forward to Slack/email/other |

### Config format (`.claude/hooks.json`, same as Claude Code)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "run_shell",
        "command": "python hooks/audit_commands.py",
        "timeout": 5
      }
    ],
    "PostToolUse": [
      {
        "command": "python hooks/log_tool_use.py",
        "async": true
      }
    ],
    "SessionStart": [
      {
        "command": "bash hooks/load_secrets.sh"
      }
    ]
  }
}
```

### Hook payload (stdin JSON)

```json
{
  "event": "PreToolUse",
  "tool_name": "run_shell",
  "inputs": {"command": "rm -rf /tmp/foo"},
  "agent_id": "agent_a1b2c3"
}
```

### Hook response (stdout JSON)

```json
{
  "continue": true,
  "decision": "approve",
  "updated_inputs": {"command": "rm -rf /tmp/foo"},
  "additional_context": "Command approved by audit hook."
}
```

`continue: false` aborts the tool call and returns `stopReason` to the agent.
`decision: "deny"` overrides the permission gate.
`updated_inputs` lets hooks rewrite tool inputs (e.g., sanitize a shell command).

### Implementation

`hooks.py` — new top-level module:
```python
def fire(event: str, payload: dict, matcher: str = None) -> HookResult:
    # Load hooks from .claude/hooks.json
    # Filter by matcher (tool name)
    # Run matching hooks as subprocess with payload on stdin
    # Parse stdout as HookResult
    # Respect timeout
    # Return merged result
```

`tools/__init__.py` `execute()` gains two hook calls:
```python
fire("PreToolUse", {...}, matcher=name)  # before dispatch
result = _dispatch(...)
fire("PostToolUse", {..., "result": result}, matcher=name)  # after dispatch
```

---

## Phase 4 — Agent summarization
**Value: medium. Effort: medium.**

When a background agent is running, the coordinator currently has no idea what it's doing between tool iterations — just a flat output file.

Claude Code runs a parallel summarizer every 30 seconds that reads the agent's live transcript and produces a 3-5 word present-tense summary ("Reading validate.ts", "Running auth tests").

### What to build

`tools/agent_summarizer.py`:

```python
def start_summarization(agent_id: str, get_messages_fn, update_fn, client) -> Callable:
    """
    Starts a background loop that every 30s:
    1. Calls get_messages_fn() to get the agent's current conversation
    2. Asks a cheap model: "Summarize what this agent is doing in 3-5 words, present tense"
    3. Calls update_fn(agent_id, summary_text) to update agent_registry
    Returns a stop() function.
    """
```

Update `agent_registry.AgentEntry` with a `summary: str` field.

Update `list_agents()` to show the summary alongside status.

The key design note from Claude Code: the summarizer uses `canUseTool = deny` (no tool calls) but passes the same `system_prompt + tools` to the API as the main agent — this keeps the prompt cache valid so the summarization call is cheap (hits the cache, only pays for output tokens).

---

## Phase 5 — Git worktree isolation
**Value: medium. Effort: medium.**

Currently multiple background agents share the same `workspace/` directory. Concurrent file edits race. Claude Code solves this by giving each agent its own git worktree.

### What to build

`worktree.py` — new module:

```python
def create_worktree(slug: str, base_branch: str = None) -> str:
    """
    Creates a git worktree at workspace/.worktrees/{slug}.
    Returns the path to the worktree directory.
    Uses: git worktree add .worktrees/{slug} -b wt/{slug}
    """

def remove_worktree(slug: str, force: bool = False) -> str:
    """
    Validates no uncommitted changes (unless force=True), then:
    git worktree remove .worktrees/{slug}
    git branch -d wt/{slug}
    """

def list_worktrees() -> list[dict]:
    """git worktree list --porcelain → parsed list"""
```

`run_agent()` gains `isolation: str = None` parameter.
When `isolation="worktree"`, agent_tool:
1. Creates a worktree named `wt_{agent_id}`
2. Passes the worktree path to the sub-agent as its `sandbox.BASE_DIR`
3. On completion, reports `worktree_path` + `worktree_branch` in the result
4. Coordinator decides whether to merge or discard

`sandbox.py` needs to support per-agent base directories (currently uses a module-level constant). Change to a thread-local or pass `base_dir` through the tool calls.

---

## Phase 6 — Rule-based permission system
**Value: medium. Effort: high.**

openclawde has a single global `auto_approve_level` setting (LOW/MEDIUM/HIGH).
Claude Code has a full rule system where permissions can be allowed/denied for:
- Specific tool names
- Specific inputs (e.g., domain names for web_fetch)
- Specific risk levels

Rules come from multiple sources with a priority order:
`policy > userSettings > projectSettings > localSettings > session`

### What to build

`permissions.py` — new module:

```python
@dataclass
class PermissionRule:
    tool_name: str          # exact name or "*" for all
    rule_content: str       # e.g., "domain:github.com", "" for wildcard
    behavior: str           # "allow" | "deny" | "ask"
    source: str             # "user" | "project" | "session"

class PermissionSystem:
    def decide(self, tool_name: str, inputs: dict) -> str:
        # 1. Check deny rules first (deny wins)
        # 2. Check allow rules
        # 3. Default to ask
        ...

    def add_rule(self, rule: PermissionRule): ...
    def load_from_config(self, config: dict): ...
```

Config format (in `config.json`):
```json
{
  "permissions": {
    "allow": ["read_file", "grep_files", "web_search"],
    "deny": ["run_shell:rm -rf"],
    "ask": ["run_python", "run_shell"]
  }
}
```

This is a significant rewrite of the permission gate in `tools/__init__.py execute()`.
The current tier system can coexist as a default fallback.

---

## Phase 7 — Web fetch improvements
**Value: low-medium. Effort: low.**

Claude Code's `WebFetchTool`:
1. Converts HTML to markdown before returning (using an HTML parser + markdownify)
2. Has a preapproved domain list that skips permission checks (github.com, docs.anthropic.com, etc.)
3. Detects cross-host redirects and asks permission before following
4. Persists binary content (PDFs, images) to disk and returns the path

openclawde's `fetch_url` just returns raw response text.

### What to build

Update `tools/web_tools.py`:

```python
PREAPPROVED_DOMAINS = {
    "github.com", "docs.anthropic.com", "stackoverflow.com",
    "wikipedia.org", "pypi.org", "docs.python.org",
}

def fetch_url(url: str, prompt: str = "") -> str:
    response = requests.get(url, allow_redirects=False, timeout=15)

    # Redirect detection
    if response.is_redirect:
        redirect_url = response.headers.get("Location", "")
        if urlparse(redirect_url).netloc != urlparse(url).netloc:
            return f"REDIRECT to different host: {redirect_url}\nFetch again with the new URL to follow."

    content_type = response.headers.get("Content-Type", "")

    # HTML → markdown conversion
    if "text/html" in content_type:
        from bs4 import BeautifulSoup
        import markdownify
        soup = BeautifulSoup(response.text, "html.parser")
        # Remove scripts, nav, footer
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        content = markdownify.markdownify(str(soup.body or soup), heading_style="ATX")
    else:
        content = response.text

    # If prompt provided, summarize with cheap model
    if prompt and len(content) > 4000:
        content = summarize_with_haiku(prompt, content)

    return content[:8000]
```

Add `markdownify` to `requirements.txt`.

---

## What cannot be ported (and why)

### tmux / iTerm2 worker backends
Claude Code spawns workers as separate terminal panes so you can see them running live in splits. This requires tmux or iTerm2 to be running, plus complex session/pane management. openclawde uses Telegram as the UI — there is no terminal to split. The daemon thread model is the right approach here.

### Mailbox system (Unix Domain Sockets)
Claude Code teammates in separate processes communicate via UDS mailboxes and bridge processes. openclawde workers all run in the same Python process, so direct in-memory message queues are equivalent and simpler.

### CCR (Claude Code Remote) / remote agents
Ant-internal infrastructure for running agents in cloud environments. Not available externally.

### GrowthBook feature flags
Internal A/B testing system. All Claude Code features gated on feature flags can be treated as unconditionally on or off in openclawde.

### 1-hour prompt cache TTL
This is an Anthropic API feature you can add by passing `"ttl": "1h"` in `cache_control`. It requires an eligible account tier. Easy to add when available — just update `_build_cached_system()` and `_build_cached_tools()` to include the TTL field.

### Reactive compaction (streaming 413 recovery)
Claude Code has a mode that handles `prompt_too_long` API errors by immediately streaming a compaction in place. This requires catching specific streaming errors and re-routing mid-stream. Low value for openclawde since the token-aware proactive compaction (Phase 2) prevents ever hitting the limit.

### Transcript Classifier / auto permission mode
An ML classifier that decides whether to ask for permission based on the content of the tool call. Too complex and requires a separate model call per tool use. The rule-based system (Phase 6) covers 95% of the value.

---

## Implementation order recommendation

```
Phase 1a  Named agent types          (1-2 days, high value, foundation for everything else)
Phase 1b  ExitPlanMode tool          (half day, closes obvious UX gap)
Phase 2   Token-aware compaction     (1-2 days, prevents silent context overflow)
Phase 3   Hooks system               (2-3 days, makes the agent programmable externally)
Phase 4   Agent summarization        (1 day, nice-to-have for coordinator UX)
Phase 5   Worktree isolation         (2-3 days, required for safe parallel agents)
Phase 6   Rule-based permissions     (3-4 days, high effort, low urgency)
Phase 7   Web fetch improvements     (half day, easy win)
```

Phases 1-3 close the most impactful gaps. Phases 4-5 are important for heavy multi-agent use.
Phase 6 is a large refactor with moderate payoff — do it last or skip if the tier system is sufficient.
