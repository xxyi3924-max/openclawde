# Fake_openclaw_3 — Architecture Note

**What it is:** A Python Telegram bot that runs as an autonomous AI agent on your Mac.
It is a from-scratch rebuild of Fake_openclaw_2, redesigned after studying the leaked Claude Code source.

---

## Directory Structure

```
Fake_openclaw_3/
│
├── main.py               Entry point. Telegram polling loop, command dispatch.
├── agent.py              The Agent class. All three provider loops live here.
├── coordinator.py        Coordinator mode: system prompt + tool config for orchestrator role.
├── mcp_manager.py        MCP (Model Context Protocol) client. Connects external tool servers.
├── sandbox.py            Filesystem jail. All agent file I/O is confined to workspace/.
├── telegram.py           Telegram HTTP wrapper. Handles polling, inline keyboards, callbacks.
│
├── memory/
│   ├── __init__.py       load_context() — single injection point into the system prompt.
│   ├── history.py        ConversationHistory. Load/save/compact (summarize old turns).
│   ├── tasks.py          TaskStore (SQLite WAL). Full task lifecycle with owner + deps.
│   ├── dream.py          DreamConsolidator. Background memory consolidation → MEMORY.md.
│   └── token_log.py      Per-turn token usage logger.
│
├── tools/
│   ├── __init__.py       Registry, execute(), filter_tools_for_agent(), _dispatch().
│   ├── _registry.py      ToolDef dataclass, RiskTier enum (LOW/MEDIUM/HIGH).
│   ├── _cancel.py        Cancel/interrupt signal.
│   ├── agent_registry.py Global registry for background agents. Message queues + abort events.
│   ├── agent_tool.py     run_agent, send_to_agent, stop_agent, list_agents.
│   ├── skill_tool.py     invoke_skill. Expands .md skill files, forks into sub-agent if needed.
│   ├── file_tools.py     write_file, edit_file (strict unique-match), read_file, grep_files, list_files.
│   ├── exec_tools.py     run_python, run_shell (sandboxed).
│   ├── web_tools.py      web_search (DuckDuckGo), fetch_url, start/stop_web_server.
│   ├── memory_tools.py   write_note, read_notes, delete_note.
│   ├── task_tools.py     create_task, update_task, complete_task, get_task, claim_task, block_task.
│   └── comms_tools.py    send_message (mid-task update to Telegram user).
│
└── skills/
    ├── __init__.py        SkillManager. Loads .md files, parses YAML frontmatter.
    ├── examples/          Bundled skills: commit, debug, explain, review.
    └── local/             User-created skills (git-ignored). Drop .md files here.
```

---

## How It Works

### Turn flow
```
Telegram message
  → main.py polls getUpdates
  → command dispatch (/plan, /coord, /tasks, /skill_name, ...)
  → agent.respond(text)
      → history.compact() if over threshold
      → _build_cached_system()  ← injects MEMORY.md + active tasks
      → _build_cached_tools()   ← filtered by mode (planning / coordinator / sub-agent)
      → Anthropic/OpenAI API loop
          → tool call → execute() → permission gate → _dispatch()
          → check abort signal (if running as sub-agent)
          → drain coordinator messages (if running as sub-agent)
      → history.save()
  → reply to Telegram
```

### Memory pipeline
```
Each session:
  write_note()       → memory/notes/*.md   (agent-writable, per-note files)
  DreamConsolidator  → memory/MEMORY.md    (background, every N sessions)
  load_context()     → injects MEMORY.md + task summary into system prompt

After enough sessions (configurable):
  Dream reads all notes + MEMORY.md
  Calls cheap model (haiku) to consolidate into 4 sections:
    User Prefs / Feedback / Project Context / Reference
  Enforces 200-line max. Old notes pruned.
```

### Multi-agent flow
```
/coord  →  coordinator mode ON
Agent receives coordinator system prompt (orchestrator role, no direct implementation)

run_agent("description", "prompt", background=True)
  → assigns agent_id (agent_xxxxxxxx)
  → registers in agent_registry with abort_event + message_queue
  → spawns daemon thread
  → sub-agent gets RESTRICTED tool set (no run_agent/send_to_agent/stop_agent)
  → sub-agent checks abort_event and drains message_queue between tool iterations

send_to_agent("agent_id", "new instruction")
  → puts message in agent's queue
  → sub-agent receives it at next iteration boundary, injects as user message

stop_agent("agent_id")
  → sets abort_event → sub-agent returns "[Agent stopped by coordinator.]"
  → releases owned tasks back to unowned

list_agents()  →  status table: agent_id, status, tools used, tokens, recent activity
```

### Task system
```
Tasks stored in SQLite (WAL mode, threading.Lock).

Schema: id, title, description, active_form, status, owner,
        blocks (JSON), blocked_by (JSON), metadata (JSON), result

Lifecycle:
  create_task()   →  pending, no owner
  claim_task()    →  atomic: sets in_progress + owner, checks blocked_by first
  block_task()    →  bidirectional: A.blocked_by += [B], B.blocks += [A]
  complete_task() →  status=completed, owner cleared
  unassign()      →  called on agent shutdown, releases in_progress tasks

Dependency check: cannot claim a task if any blocked_by task is not completed.
```

### Permission system
```
Each tool has a RiskTier: LOW | MEDIUM | HIGH

Config: auto_approve_level = "LOW" | "MEDIUM" | "HIGH"

Rule: if tool.risk >= auto_approve_level → ask user via Telegram inline keyboard
      "Approve" / "Deny" buttons, 120s timeout, timeout = deny

Planning mode (/plan):
  Only planning_allowed=True tools exposed to API.
  Any non-allowed tool call is blocked with an explanation.
  /execute to restore full tool set.
```

### Skills
```
Skills are .md files with YAML frontmatter:
  name, description, when-to-use, argument-hint, allowed-tools, context (inline|fork)

User invokes: /commit "fix auth bug"  (Telegram slash command)
  → main.py expands to skill prompt with $ARGUMENTS substituted
  → dispatches to agent as a normal message

Agent invokes: invoke_skill("commit", "fix auth bug")
  → inline: expanded prompt returned, agent executes it in current context
  → fork: spawns sub-agent with expanded prompt

Skill locations loaded in order (later overrides earlier):
  skills/examples/   bundled (commit, debug, explain, review)
  ~/.claude/skills/  user-global
  skills/local/      project-local
```

### MCP (Model Context Protocol)
```
Config: mcp.json in project root or ~/.claude/mcp.json

Supported transports: stdio (subprocess), sse (HTTP server)

At startup: load_mcp() connects each server, calls list_tools(),
            registers tools as mcp__{server}__{tool} in TOOL_DEFINITIONS.

Persistent connection: one async event loop on a daemon thread.
                       All MCP calls use run_coroutine_threadsafe() to bridge sync → async.
```

---

## Telegram Commands

| Command | Effect |
|---|---|
| `/plan` | Planning mode ON (read-only tools only) |
| `/execute` | Planning mode OFF (full tool set) |
| `/coord` | Coordinator mode ON (orchestrator system prompt) |
| `/agent` | Coordinator mode OFF |
| `/agents` | List all running background agents |
| `/tasks` | List all tasks |
| `/tokens` | Token usage summary |
| `/clear` | Clear conversation history |
| `/cancel` | Cancel current running task |
| `/<skill> [args]` | Expand and run a skill (e.g. `/commit`, `/debug`) |

---

## 29 Tools

| Category | Tools |
|---|---|
| Files | write_file, edit_file, read_file, grep_files, list_files, list_tools |
| Execution | run_python, run_shell |
| Web | web_search, fetch_url, start_web_server, stop_web_server |
| Memory | write_note, read_notes, delete_note |
| Tasks | create_task, update_task, complete_task, get_task, claim_task, block_task, list_tasks |
| Communication | send_message |
| Skills | invoke_skill |
| Multi-agent | run_agent, get_agent_output, send_to_agent, stop_agent, list_agents |
| + MCP tools | dynamically added at startup from mcp.json |

---

## Gap: Fake_openclaw_3 vs Real Claude Code

### What is faithfully ported

| Feature | Status |
|---|---|
| Tool risk tiers + permission gates | ✅ Same logic (LOW/MEDIUM/HIGH, configurable threshold) |
| Planning mode | ✅ Same pattern (filtered tool list, planning_allowed flag) |
| Strict edit_file (unique match) | ✅ Identical behavior |
| Context compaction (summarize old history) | ✅ Same pattern (cheap model, keep N recent) |
| Task schema (owner, blocks, blocked_by, claim) | ✅ Full port |
| Coordinator system prompt | ✅ Direct port from coordinatorMode.ts |
| Agent registry (abort + message queue) | ✅ Full port |
| Tool filtering for sub-agents | ✅ Coordinator tools blocked for workers |
| Skills system (.md frontmatter, $ARGUMENTS, fork/inline) | ✅ Full port |
| MCP support (stdio + sse, persistent session) | ✅ Full port |
| autoDream memory consolidation | ✅ Full port (3-gate trigger, 4-phase cycle) |

### Genuine gaps — things Claude Code has that Fake_openclaw_3 does not

**1. Multiple agent backends**
Claude Code spawns workers as separate tmux panes / iTerm2 splits / remote CCR environments.
Fake_openclaw_3 only has in-process daemon threads. Workers share the same Python process and memory space. There is no real isolation — a crashed worker can affect the main agent.

**2. Git worktree isolation**
Claude Code's `isolation: "worktree"` spawns an agent in a fresh git worktree (separate branch + directory). Workers can make changes without touching the main working tree until merged.
Fake_openclaw_3 has no worktree support. All workers operate on the same workspace/ directory, so concurrent file edits from multiple workers will race.

**3. Prompt caching awareness**
Claude Code manages Anthropic prompt caching (cache_control breakpoints) carefully across the multi-agent tree to minimize cost.
Fake_openclaw_3 caches system + tools with `cache_control: ephemeral` but does not coordinate cache breakpoints across sub-agents.

**4. Plan approval flow**
Claude Code has a structured `plan_approval_response` message type: coordinator presents plan, waits for user or another agent to approve/reject before proceeding.
Fake_openclaw_3 has planning mode (read-only) but no approval handshake protocol.

**5. Cross-session message routing**
Claude Code teammates can communicate across tmux sessions via Unix Domain Sockets (UDS) or a bridge process.
Fake_openclaw_3 message queues live only in memory — if the process restarts, all queued messages are lost.

**6. Agent summarization**
Claude Code runs a background summarizer alongside long-running agents, producing a live summary as they work (for the coordinator to read without waiting).
Fake_openclaw_3 only streams output to a flat text file.

**7. UI integration**
Claude Code has a rich TUI (React/Ink) showing each agent's panel, status, recent tool activity, token burn rate in real time.
Fake_openclaw_3's "UI" is a Telegram message thread and a Flask web panel (inherited from v2). No live multi-pane view.

**8. Permission modes beyond approve/deny**
Claude Code has `acceptEdits`, `auto`, `bypassPermissions`, `plan` modes per-agent.
Fake_openclaw_3 has a single global `auto_approve_level` (LOW/MEDIUM/HIGH) + planning mode. No per-agent permission scope.

**9. Built-in agent types**
Claude Code has named built-in agent types (e.g. `statusline-setup`, `Explore`, `Plan`) with their own system prompts and tool restrictions.
Fake_openclaw_3 has no named agent types — all sub-agents use the same base Agent class.

**10. Token budget enforcement**
Claude Code can set per-agent token budgets and terminate agents that exceed them.
Fake_openclaw_3 tracks token usage for display but does not enforce any budget.

---

## What Makes Fake_openclaw_3 Non-Trivial

Despite the gaps, Fake_openclaw_3 has features that most hobby agents don't:

- A real coordinator/worker architecture where the coordinator's system prompt instructs it not to implement anything itself — just like Claude Code's `coordinatorMode.ts`.
- Workers that genuinely cannot spawn further agents (tool filtering at dispatch time).
- Tasks with bidirectional dependency tracking and atomic claim — not just a to-do list.
- Message injection between tool iterations, not just on next `respond()` call.
- autoDream: background memory consolidation that actually runs on a timer, not just "write a note".
- MCP support so any third-party tool server (GitHub, Postgres, filesystem) works out of the box.
- A persistent async event loop for MCP so stdio server processes stay alive between calls.
