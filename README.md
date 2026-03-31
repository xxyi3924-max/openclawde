# openclawde

An autonomous AI agent that runs on your Mac and is controlled via Telegram. Built in Python, inspired by the architecture of Claude Code.

---

## What it does

You send it a task over Telegram. It thinks, calls tools, writes code, runs shell commands, searches the web, manages files, and reports back — all without you having to do anything. For complex tasks it can spin up worker agents to run in parallel and coordinate them automatically.

---

## Setup

**1. Clone and install**
```bash
git clone https://github.com/xxyi3924-max/openclawde
cd openclawde
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Create your config**
```bash
cp config.example.json config.json
```

Edit `config.json`:
```json
{
  "telegram_token": "your-telegram-bot-token",
  "telegram_chat_id": 123456789,
  "anthropic_api_key": "sk-ant-...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "auto_approve_level": "LOW"
}
```

**3. Run**
```bash
python main.py
```

---

## Config reference

| Key | Default | Description |
|---|---|---|
| `provider` | `"anthropic"` | `"anthropic"`, `"openai"`, or `"minimax"` |
| `model` | `"claude-sonnet-4-6"` | Model to use |
| `auto_approve_level` | `"LOW"` | `"LOW"` = ask for MEDIUM+HIGH ops, `"MEDIUM"` = ask for HIGH only, `"HIGH"` = never ask |
| `compaction_threshold_messages` | `40` | Summarize history after this many messages |
| `compaction_keep_recent` | `10` | How many recent messages to keep after compaction |
| `dream_enabled` | `true` | Background memory consolidation |
| `dream_interval_hours` | `24` | Min hours between dream cycles |
| `dream_min_sessions` | `5` | Min sessions before dreaming |
| `fallback_model` | `"gpt-4o-mini"` | Model to use on rate limit |
| `thinking_budget` | `0` | Extended thinking token budget (Anthropic only) |

---

## Telegram commands

| Command | Effect |
|---|---|
| `/plan` | Switch to planning mode — read-only, no writes or execution |
| `/execute` | Return to full execution mode |
| `/coord` | Switch to coordinator mode — orchestrates worker agents |
| `/agent` | Return to normal agent mode |
| `/agents` | List all running background agents |
| `/tasks` | Show all active tasks |
| `/tokens` | Token usage summary |
| `/clear` | Clear conversation history |
| `/cancel` | Cancel the current running task |
| `/<skill> [args]` | Run a skill (e.g. `/commit`, `/debug myfile.py`) |

---

## Tools (29)

| Category | Tools |
|---|---|
| Files | `write_file`, `edit_file`, `read_file`, `grep_files`, `list_files`, `list_tools` |
| Execution | `run_python`, `run_shell` |
| Web | `web_search`, `fetch_url`, `start_web_server`, `stop_web_server` |
| Memory | `write_note`, `read_notes`, `delete_note` |
| Tasks | `create_task`, `update_task`, `complete_task`, `get_task`, `claim_task`, `block_task`, `list_tasks` |
| Multi-agent | `run_agent`, `get_agent_output`, `send_to_agent`, `stop_agent`, `list_agents` |
| Skills | `invoke_skill` |
| Communication | `send_message` |

Plus any tools loaded from MCP servers (see below).

---

## Multi-agent / coordinator mode

Send `/coord` to switch into coordinator mode. The agent stops doing implementation work itself and instead:

1. Breaks the task into subtasks with `create_task()`
2. Spawns worker agents with `run_agent()` (background or blocking)
3. Sends follow-up instructions mid-task with `send_to_agent()`
4. Stops workers that go off track with `stop_agent()`
5. Synthesizes results and reports back to you

Workers run in parallel daemon threads. They get a restricted tool set — they cannot spawn their own agents, preventing infinite nesting.

---

## Skills

Skills are `.md` files that define reusable prompt templates. Invoke them in Telegram as `/skill_name [args]` or from within the agent with `invoke_skill`.

**Bundled skills:** `commit`, `debug`, `explain`, `review`

**Add your own:** drop a `.md` file in `skills/local/`:

```markdown
---
name: myskill
description: Does my thing
argument-hint: "[what to pass]"
context: inline
---

Do the following task using $ARGUMENTS.
Step 1: ...
```

`context: inline` — runs in the current conversation.
`context: fork` — spawns a sub-agent.

---

## MCP servers

openclawde supports the [Model Context Protocol](https://modelcontextprotocol.io) — connect external tool servers (GitHub, Postgres, filesystem, etc.) and their tools appear automatically in the agent's tool list as `mcp__{server}__{tool}`.

Copy `mcp.json.example` to `mcp.json` and fill in your servers:

```json
{
  "mcpServers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..." }
    }
  }
}
```

Project-level `mcp.json` takes priority over `~/.claude/mcp.json`.

---

## Memory

The agent remembers things across sessions through three layers:

- **Notes** — the agent calls `write_note()` at the end of tasks to record what it learned
- **MEMORY.md** — a consolidated summary file (≤200 lines) generated by the background DreamConsolidator and injected into every system prompt
- **Task history** — active tasks are always visible in the system prompt

**autoDream** runs in the background every N sessions and consolidates all notes into structured sections (User Prefs / Feedback / Project Context / Reference), trimming to stay under 200 lines.

---

## Architecture

```
main.py          Telegram polling, command dispatch
agent.py         Core agent class, all provider loops (Anthropic / OpenAI)
coordinator.py   Coordinator mode system prompt and tool config
mcp_manager.py   MCP client (persistent async stdio/sse sessions)
sandbox.py       Filesystem jail (all I/O confined to workspace/)
telegram.py      Telegram HTTP wrapper, inline keyboards

memory/
  history.py     ConversationHistory with compaction
  tasks.py       TaskStore (SQLite WAL, bidirectional deps, atomic claim)
  dream.py       DreamConsolidator (background memory consolidation)

tools/
  agent_registry.py   Per-agent abort events, message queues, progress tracking
  agent_tool.py       run_agent, send_to_agent, stop_agent, list_agents
  skill_tool.py       invoke_skill
  file_tools.py       File I/O (strict unique-match edit)
  exec_tools.py       Python + shell execution (sandboxed)
  ...

skills/
  examples/      Bundled skills
  local/         Your custom skills (git-ignored)
```

---

## Acknowledgement

Architecture patterns ported from [Claude Code](https://claude.ai/claude-code) (Anthropic).
