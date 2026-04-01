import importlib.util
import json
import logging
import queue
import subprocess
from datetime import datetime
from pathlib import Path

import sandbox
from tools._registry import ToolDef, RiskTier
from tools._cancel import _cancel_event, cancel, reset_cancel, is_cancelled
from tools import file_tools, exec_tools, web_tools, memory_tools, task_tools, comms_tools
from tools import skill_tool, agent_tool

__all__ = [
    "TOOL_DEFINITIONS", "_TOOL_REGISTRY", "execute", "reload_dynamic_tools",
    "load_notes_context", "filter_tools_for_agent",
    "cancel", "reset_cancel", "is_cancelled", "get_continuation",
    "_continuation", "_dynamic_fns", "_dynamic_defs",
    "RiskTier",
    "_planning_mode", "set_planning_mode",
    "_self_task_queue",
]

# ------------------------------------------------------------------
# Planning mode flag
# ------------------------------------------------------------------
_planning_mode: bool = False


def set_planning_mode(enabled: bool):
    global _planning_mode
    _planning_mode = enabled
    print(f"[Tools] Planning mode {'ON' if enabled else 'OFF'}")


# ------------------------------------------------------------------
# Continuation state (written by agent.py: tool_module._continuation = "...")
# ------------------------------------------------------------------
_continuation: str | None = None


def get_continuation() -> str | None:
    global _continuation
    val = _continuation
    _continuation = None
    return val


# ------------------------------------------------------------------
# Self-task queue — agent queues its own follow-up work.
# main.py drains this after each turn and auto-dispatches without
# waiting for a human message.
# ------------------------------------------------------------------
_self_task_queue: queue.Queue = queue.Queue()


# ------------------------------------------------------------------
# Dynamic tool registry
# ------------------------------------------------------------------
_dynamic_fns: dict[str, callable] = {}
_dynamic_defs: list[dict] = []


# ------------------------------------------------------------------
# Structured tool call logger
# ------------------------------------------------------------------
_LOG_FILE = Path(__file__).parent.parent / "memory" / "tool_calls.log"
_LOG_FILE.parent.mkdir(exist_ok=True)

_tool_logger = logging.getLogger("tool_calls")
_tool_logger.setLevel(logging.DEBUG)
if not _tool_logger.handlers:
    _fh = logging.FileHandler(_LOG_FILE)
    _fh.setFormatter(logging.Formatter("%(message)s"))
    _tool_logger.addHandler(_fh)


def _log_call(name: str, inputs: dict, result: str):
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": name,
        "inputs": {k: str(v)[:200] for k, v in inputs.items()},
        "result": result[:300],
    }
    _tool_logger.debug(json.dumps(entry))


# ------------------------------------------------------------------
# Built-in tool definitions
# ------------------------------------------------------------------

_BUILTIN_TOOL_DEFS: list[ToolDef] = [
    ToolDef(
        name="write_file",
        description=(
            "Write content to a file in the workspace. "
            "To create a reusable tool, write to tools/<name>.py with TOOL_DEF + run() — "
            "it will be auto-loaded and available immediately."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace/"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
        fn=file_tools.write_file,
        risk=RiskTier.MEDIUM,
        planning_allowed=False,
    ),
    ToolDef(
        name="edit_file",
        description=(
            "Edit a file by replacing an exact string with a new string. "
            "Requires exactly ONE match — fails with an error if 0 or multiple matches. "
            "ALWAYS prefer this over write_file for modifications. "
            "Use write_file only for new files or complete rewrites."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace/"},
                "old_string": {"type": "string", "description": "Exact text to find and replace (must be unique in the file)"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        fn=file_tools.edit_file,
        risk=RiskTier.MEDIUM,
        planning_allowed=False,
    ),
    ToolDef(
        name="read_file",
        description=(
            "Read a specific line range from a file. REQUIRES both start_line and end_line — "
            "blind reads without a range are BLOCKED. Always grep_files() first to find the line numbers, "
            "then call read_file(path, start_line, end_line) for only that section."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace/"},
                "start_line": {"type": "integer", "description": "First line to read (1-indexed, required)"},
                "end_line": {"type": "integer", "description": "Last line to read (inclusive, required)"},
            },
            "required": ["path", "start_line", "end_line"],
        },
        fn=file_tools.read_file,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="grep_files",
        description=(
            "Search for a regex pattern across workspace files. Returns matching lines with context and line numbers. "
            "Supports multi-pattern via regex OR syntax: 'pattern1|pattern2|pattern3' matches any of them in one call. "
            "Always use this to locate lines before read_file. Prefer wide multi-pattern greps over multiple single calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern — use 'a|b|c' to match multiple patterns in one call"},
                "path": {"type": "string", "description": "Subdirectory or file to search in (optional, default: all workspace)"},
                "context_lines": {"type": "integer", "description": "Lines of context around each match (default 3)"},
            },
            "required": ["pattern"],
        },
        fn=file_tools.grep_files,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="list_files",
        description="List all files in the workspace or a subdirectory.",
        input_schema={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Subdirectory to list (omit for root)"},
            },
        },
        fn=file_tools.list_files,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="list_tools",
        description=(
            "List all dynamic tools currently loaded from workspace/tools/. "
            "Always check this before writing new code — the tool you need may already exist."
        ),
        input_schema={"type": "object", "properties": {}},
        fn=file_tools.list_tools,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="run_python",
        description="Execute Python code and return stdout/stderr. Always verify before reporting success.",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Max seconds (default 30)"},
            },
            "required": ["code"],
        },
        fn=exec_tools.run_python,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="run_shell",
        description="Run a shell command in the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Max seconds (default 30)"},
            },
            "required": ["command"],
        },
        fn=exec_tools.run_shell,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="web_search",
        description="Search the web with DuckDuckGo.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Number of results (default 5)"},
            },
            "required": ["query"],
        },
        fn=web_tools.web_search,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="fetch_url",
        description="Fetch and read a webpage.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            },
            "required": ["url"],
        },
        fn=web_tools.fetch_url,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="start_web_server",
        description="Start a local HTTP server. Use fetch_url to verify it loads.",
        input_schema={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Subdirectory to serve (default: root)"},
                "port": {"type": "integer", "description": "Port number (default 8080)"},
            },
        },
        fn=web_tools.start_web_server,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="stop_web_server",
        description="Stop a running local HTTP server.",
        input_schema={
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "Port to stop (default 8080)"},
            },
        },
        fn=web_tools.stop_web_server,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="write_note",
        description=(
            "Save a note to your persistent memory. Use this to record: how to do a recurring task, "
            "useful file paths, user preferences, shortcuts, or anything worth remembering next session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title for the note"},
                "content": {"type": "string", "description": "Note content (markdown ok)"},
            },
            "required": ["title", "content"],
        },
        fn=memory_tools.write_note,
        risk=RiskTier.MEDIUM,
        planning_allowed=False,
    ),
    ToolDef(
        name="read_notes",
        description="Read all your saved notes. Call this at the start of a session to recall past context.",
        input_schema={"type": "object", "properties": {}},
        fn=memory_tools.read_notes,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="delete_note",
        description="Delete a saved note by title.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the note to delete"},
            },
            "required": ["title"],
        },
        fn=memory_tools.delete_note,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="create_task",
        description=(
            "Create a tracked subtask. Use for multi-step work to record progress across iterations. "
            "Returns a task_id to reference in update_task and complete_task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title"},
                "description": {"type": "string", "description": "What needs to be done"},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Task IDs that must complete before this one can start",
                },
            },
            "required": ["title"],
        },
        fn=task_tools.create_task,
        risk=RiskTier.MEDIUM,
        planning_allowed=True,
    ),
    ToolDef(
        name="update_task",
        description="Update a task's status (pending/in_progress/failed). Use complete_task to mark done.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID from create_task"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "failed"], "description": "New status"},
                "result": {"type": "string", "description": "Notes on outcome (optional)"},
            },
            "required": ["task_id", "status"],
        },
        fn=task_tools.update_task,
        risk=RiskTier.MEDIUM,
        planning_allowed=True,
    ),
    ToolDef(
        name="list_tasks",
        description="List tasks. filter: all, pending, in_progress, completed.",
        input_schema={
            "type": "object",
            "properties": {
                "filter": {"type": "string", "enum": ["all", "pending", "in_progress", "completed"], "description": "Status filter (default: all)"},
            },
        },
        fn=task_tools.list_tasks,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="complete_task",
        description="Mark a task as completed with an optional result summary.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
                "result": {"type": "string", "description": "What was accomplished"},
            },
            "required": ["task_id"],
        },
        fn=task_tools.complete_task,
        risk=RiskTier.MEDIUM,
        planning_allowed=False,
    ),
    ToolDef(
        name="get_task",
        description="Get full details of a single task by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
        fn=task_tools.get_task,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="claim_task",
        description=(
            "Atomically mark a task as in_progress and assign it to an agent. "
            "Returns: claimed | blocked | already_claimed | agent_busy | not_found"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID to claim"},
                "agent_id": {"type": "string", "description": "Agent ID claiming the task"},
                "check_agent_busy": {"type": "boolean", "description": "Fail if agent already owns an in_progress task (default: false)"},
            },
            "required": ["task_id", "agent_id"],
        },
        fn=task_tools.claim_task,
        risk=RiskTier.MEDIUM,
        planning_allowed=False,
    ),
    ToolDef(
        name="block_task",
        description="Declare that task_id cannot start until blocked_by_id is completed. Sets up bidirectional dependency.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task that will be blocked"},
                "blocked_by_id": {"type": "integer", "description": "Task that must complete first"},
            },
            "required": ["task_id", "blocked_by_id"],
        },
        fn=task_tools.block_task,
        risk=RiskTier.MEDIUM,
        planning_allowed=True,
    ),
    ToolDef(
        name="send_message",
        description="Send a status update to the user mid-task.",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Update to send"},
            },
            "required": ["message"],
        },
        fn=comms_tools.send_message,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    # ----------------------------------------------------------------
    # Skills
    # ----------------------------------------------------------------
    ToolDef(
        name="invoke_skill",
        description="placeholder — rebuilt dynamically with loaded skill list",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name (e.g. 'commit', 'review', 'debug')"},
                "args": {"type": "string", "description": "Arguments to pass to the skill (replaces $ARGUMENTS)"},
            },
            "required": ["name"],
        },
        fn=skill_tool.invoke_skill,
        risk=RiskTier.MEDIUM,
        planning_allowed=True,
    ),
    # ----------------------------------------------------------------
    # Sub-agents
    # ----------------------------------------------------------------
    ToolDef(
        name="run_agent",
        description=(
            "Spawn a specialized sub-agent. "
            "agent_type: 'explore' (read-only, haiku), 'plan' (planning only), "
            "'verify' (runs tests), 'worker' (full tools, default). "
            "background=true returns immediately; background=false blocks for result."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "3-5 word task description"},
                "prompt": {"type": "string", "description": "Full self-contained task prompt — include all needed context"},
                "agent_type": {"type": "string", "enum": ["worker", "explore", "plan", "verify"], "description": "Agent type (default: worker)"},
                "background": {"type": "boolean", "description": "Run async in background (default: false)"},
            },
            "required": ["description", "prompt"],
        },
        fn=agent_tool.run_agent,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="get_agent_output",
        description="Read streaming output from a background sub-agent by task_id.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID returned by run_agent with background=true"},
            },
            "required": ["task_id"],
        },
        fn=agent_tool.get_agent_output,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    # ----------------------------------------------------------------
    # Coordinator tools (multi-agent orchestration)
    # ----------------------------------------------------------------
    ToolDef(
        name="send_to_agent",
        description=(
            "Send a follow-up message or new instruction to a running background agent. "
            "Use agent_id='*' to broadcast to ALL running agents. "
            "The agent will receive it between tool iterations."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID from run_agent output, or '*' for broadcast"},
                "message": {"type": "string", "description": "Message or instruction to send"},
            },
            "required": ["agent_id", "message"],
        },
        fn=agent_tool.send_to_agent,
        risk=RiskTier.MEDIUM,
        planning_allowed=True,
    ),
    ToolDef(
        name="stop_agent",
        description="Stop a running background agent by sending it an abort signal.",
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to stop"},
            },
            "required": ["agent_id"],
        },
        fn=agent_tool.stop_agent,
        risk=RiskTier.HIGH,
        planning_allowed=False,
    ),
    ToolDef(
        name="list_agents",
        description="List all registered background agents with status, tool count, and token usage.",
        input_schema={"type": "object", "properties": {}},
        fn=agent_tool.list_agents,
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    # ----------------------------------------------------------------
    # Autonomy tools
    # ----------------------------------------------------------------
    ToolDef(
        name="exit_plan_mode",
        description=(
            "Exit planning mode and immediately continue with full execution tools. "
            "Call this after presenting your plan — pass the plan text as 'plan'. "
            "Do NOT wait for human /execute — call this tool to proceed autonomously."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "The plan you are about to execute"},
            },
            "required": ["plan"],
        },
        fn=lambda plan="": _exit_plan_mode(plan),
        risk=RiskTier.LOW,
        planning_allowed=True,  # Available IN plan mode — that's the whole point
    ),
    ToolDef(
        name="queue_self_task",
        description=(
            "Schedule a follow-up task for yourself to work on after the current turn. "
            "Use this to chain work autonomously without waiting for the user. "
            "The task will be dispatched automatically in the next turn."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Full task prompt for the follow-up turn"},
                "context": {"type": "string", "description": "Additional context to prepend (optional)"},
            },
            "required": ["prompt"],
        },
        fn=lambda prompt="", context="": _queue_self_task(prompt, context),
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
    ToolDef(
        name="set_goal",
        description=(
            "Set a persistent autonomous goal. The agent will pursue this goal across sessions, "
            "automatically starting work on it without needing a human message. "
            "Goal persists until explicitly cleared with set_goal(goal='')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The goal to pursue autonomously. Empty string to clear."},
                "notes": {"type": "string", "description": "Additional context or constraints for pursuing this goal"},
            },
            "required": ["goal"],
        },
        fn=lambda goal="", notes="": _set_goal(goal, notes),
        risk=RiskTier.LOW,
        planning_allowed=True,
    ),
]


# ------------------------------------------------------------------
# Autonomy tool implementations (kept here to access module-level state)
# ------------------------------------------------------------------

def _exit_plan_mode(plan: str) -> str:
    set_planning_mode(False)
    return (
        "[Plan mode exited. Full execution tools now available. Proceed with implementation.]\n\n"
        f"Plan committed:\n{plan}"
    )


def _queue_self_task(prompt: str, context: str = "") -> str:
    full = f"{context}\n\n{prompt}".strip() if context else prompt
    _self_task_queue.put(full)
    return f"[Self-task queued] Will execute after this turn: {prompt[:100]}"


_GOAL_FILE = Path(__file__).parent.parent / "memory" / "goal.json"


def _set_goal(goal: str, notes: str = "") -> str:
    if not goal:
        if _GOAL_FILE.exists():
            _GOAL_FILE.unlink()
        return "[Goal cleared. Agent will no longer auto-start work on session start.]"
    _GOAL_FILE.parent.mkdir(exist_ok=True)
    _GOAL_FILE.write_text(
        json.dumps({"goal": goal, "notes": notes, "set_at": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    return f"[Goal set] '{goal}'. Will auto-pursue on session start."


def load_goal() -> dict | None:
    """Called by main.py on startup to check for a persistent goal."""
    if _GOAL_FILE.exists():
        try:
            return json.loads(_GOAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None

_BUILTIN_NAMES: set[str] = {td.name for td in _BUILTIN_TOOL_DEFS}

# _TOOL_REGISTRY: name → ToolDef, used for permission lookups (Phase 2+)
_TOOL_REGISTRY: dict[str, ToolDef] = {td.name: td for td in _BUILTIN_TOOL_DEFS}

# TOOL_DEFINITIONS: plain-dict list in Anthropic API format — what agent loops consume
TOOL_DEFINITIONS: list[dict] = []


def _rebuild_tool_list():
    """Sync TOOL_DEFINITIONS and _TOOL_REGISTRY in-place."""
    global _TOOL_REGISTRY

    # Refresh invoke_skill description with currently loaded skills
    try:
        import skills as _skills_mod
        _skill_desc = _skills_mod.get_manager().tool_description()
        for td in _BUILTIN_TOOL_DEFS:
            if td.name == "invoke_skill":
                object.__setattr__(td, "description", _skill_desc) if hasattr(td, "__dataclass_fields__") else None
                td.__dict__["description"] = _skill_desc
                break
    except Exception:
        pass

    TOOL_DEFINITIONS.clear()
    TOOL_DEFINITIONS.extend(td.to_api_dict() for td in _BUILTIN_TOOL_DEFS)
    TOOL_DEFINITIONS.extend(_dynamic_defs)

    # Add MCP tools
    try:
        import mcp_manager as _mcp
        m = _mcp.get_manager()
        if m.tool_defs:
            TOOL_DEFINITIONS.extend(m.tool_defs)
    except Exception:
        pass

    _TOOL_REGISTRY = {td.name: td for td in _BUILTIN_TOOL_DEFS}
    for d in _dynamic_defs:
        if d["name"] not in _TOOL_REGISTRY:
            _TOOL_REGISTRY[d["name"]] = ToolDef(
                name=d["name"],
                description=d.get("description", ""),
                input_schema=d.get("input_schema", {}),
                fn=_dynamic_fns.get(d["name"], lambda **_: ""),
                risk=RiskTier.HIGH,
                planning_allowed=False,
            )


# ------------------------------------------------------------------
# Dynamic tool loader
# ------------------------------------------------------------------

def _check_requires(tool_def: dict, path: Path) -> list[str]:
    warnings = []
    requires = tool_def.get("requires", {})
    for pkg in requires.get("pip", []):
        try:
            importlib.util.find_spec(pkg.replace("-", "_"))
        except Exception:
            warnings.append(f"pip package '{pkg}' may not be installed")
    for binary in requires.get("bins", []):
        result = subprocess.run(["which", binary], capture_output=True)
        if result.returncode != 0:
            warnings.append(f"binary '{binary}' not found in PATH")
    return warnings


def reload_dynamic_tools():
    """Scan workspace/tools/*.py and (re)load all dynamic tools."""
    global _dynamic_fns, _dynamic_defs
    new_fns = {}
    new_defs = []

    tools_dir = sandbox.WORKSPACE / "tools"
    if not tools_dir.exists():
        _dynamic_fns = new_fns
        _dynamic_defs = new_defs
        _rebuild_tool_list()
        return

    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"dynamic_tool.{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            if not hasattr(mod, "TOOL_DEF") or not hasattr(mod, "run"):
                print(f"[Tools] Skipped {path.name} — missing TOOL_DEF or run()")
                continue

            tool_def = mod.TOOL_DEF
            name = tool_def.get("name")
            if not name:
                print(f"[Tools] Skipped {path.name} — TOOL_DEF missing 'name'")
                continue

            if name in _BUILTIN_NAMES:
                print(f"[Tools] Skipped {path.name} — '{name}' conflicts with a built-in tool")
                continue

            for w in _check_requires(tool_def, path):
                print(f"[Tools] Warning ({name}): {w}")

            new_defs.append(tool_def)
            new_fns[name] = mod.run
            print(f"[Tools] Loaded: {name}  ({path.name})")

        except Exception as e:
            print(f"[Tools] Failed to load {path.name}: {e}")

    _dynamic_fns = new_fns
    _dynamic_defs = new_defs
    _rebuild_tool_list()


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Tool set filtering for sub-agents vs coordinator
# ------------------------------------------------------------------

# These tools are only for the coordinator — sub-agents don't get them
_COORDINATOR_ONLY = {"run_agent", "send_to_agent", "stop_agent", "list_agents"}


def filter_tools_for_agent(is_coordinator: bool = False) -> list[dict]:
    """
    Return the appropriate tool list for an agent type.

    is_coordinator=True  → full TOOL_DEFINITIONS (coordinator can spawn + message workers)
    is_coordinator=False → TOOL_DEFINITIONS minus coordinator-only tools (no infinite nesting)

    Always includes MCP tools regardless of mode.
    """
    if is_coordinator:
        return list(TOOL_DEFINITIONS)
    return [t for t in TOOL_DEFINITIONS if t["name"] not in _COORDINATOR_ONLY]


# ------------------------------------------------------------------
# Notes context (for system prompt injection — Phase 6 upgrades this)
# ------------------------------------------------------------------

def load_notes_context() -> str:
    """Called by agent.py to inject persistent context into the system prompt."""
    from memory import load_context
    return load_context()


# ------------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------------

def _requires_confirm(risk: RiskTier, auto_approve_level: str) -> bool:
    """Return True if this risk tier requires user confirmation under the given policy."""
    order = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}
    threshold = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(auto_approve_level.upper(), 1)
    return order[risk] >= threshold


def execute(name: str, inputs: dict, send_update=None, permission_fn=None, agent_id: str = "") -> str:
    """
    Execute a tool by name.

    permission_fn(name, inputs, risk_tier) — called for ops that need user approval.
    It must return "approved" or "denied". If not provided, all ops are auto-approved.
    agent_id — passed to hooks for context.
    """
    try:
        import hooks as _hooks

        tool = _TOOL_REGISTRY.get(name)

        # Planning mode: block non-planning_allowed tools
        if _planning_mode and tool and not tool.planning_allowed:
            return (
                f"[BLOCKED — Planning Mode] '{name}' is not available while planning. "
                "Call exit_plan_mode(plan) to proceed with execution autonomously, "
                "or the user can send /execute."
            )

        # PreToolUse hook — can approve, deny, or rewrite inputs
        pre = _hooks.fire("PreToolUse", {
            "event": "PreToolUse",
            "tool_name": name,
            "inputs": inputs,
            "agent_id": agent_id,
        }, matcher=name)

        if not pre.should_continue:
            return f"[Blocked by hook] {pre.stop_reason}"

        # Hook can override permission decision
        hook_decision = pre.decision  # "approve" | "deny" | ""

        # Rewrite inputs if hook asked for it
        if pre.updated_inputs:
            inputs = {**inputs, **pre.updated_inputs}

        # Permission gate
        if hook_decision == "deny":
            return f"[Denied by hook] Tool '{name}' was denied by a PreToolUse hook."
        elif hook_decision != "approve" and tool and permission_fn:
            auto_level = getattr(permission_fn, "_auto_approve_level", "LOW")
            if _requires_confirm(tool.risk, auto_level):
                decision = permission_fn(name, inputs, tool.risk)
                if decision != "approved":
                    return f"[Denied] User did not approve '{name}'."

        result = _dispatch(name, inputs, send_update)

        # Append hook additional_context to result if provided
        if pre.additional_context:
            result = f"{result}\n\n[Hook context] {pre.additional_context}"

        _log_call(name, inputs, result)

        # PostToolUse hook (async by default — doesn't block)
        _hooks.fire("PostToolUse", {
            "event": "PostToolUse",
            "tool_name": name,
            "inputs": inputs,
            "result": result[:500],
            "agent_id": agent_id,
        }, matcher=name)

        return result
    except PermissionError as e:
        _log_call(name, inputs, f"[Permission denied] {e}")
        return f"[Permission denied] {e}"
    except Exception as e:
        _log_call(name, inputs, f"[Tool error] {e}")
        return f"[Tool error in {name}] {e}"


def _dispatch(name: str, inputs: dict, send_update=None) -> str:
    match name:
        case "write_file":
            return file_tools.write_file(inputs["path"], inputs["content"])
        case "edit_file":
            return file_tools.edit_file(inputs["path"], inputs["old_string"], inputs["new_string"])
        case "read_file":
            return file_tools.read_file(inputs["path"], inputs.get("start_line", 0), inputs.get("end_line", 0))
        case "grep_files":
            return file_tools.grep_files(inputs["pattern"], inputs.get("path", ""), inputs.get("context_lines", 2))
        case "list_files":
            return file_tools.list_files(inputs.get("directory", ""))
        case "list_tools":
            return file_tools.list_tools()
        case "run_python":
            return exec_tools.run_python(inputs["code"], inputs.get("timeout", 30))
        case "run_shell":
            return exec_tools.run_shell(inputs["command"], inputs.get("timeout", 30))
        case "web_search":
            return web_tools.web_search(inputs["query"], inputs.get("max_results", 5))
        case "fetch_url":
            return web_tools.fetch_url(inputs["url"])
        case "start_web_server":
            return web_tools.start_web_server(inputs.get("directory", ""), inputs.get("port", 8080))
        case "stop_web_server":
            return web_tools.stop_web_server(inputs.get("port", 8080))
        case "write_note":
            return memory_tools.write_note(inputs["title"], inputs["content"])
        case "read_notes":
            return memory_tools.read_notes()
        case "delete_note":
            return memory_tools.delete_note(inputs["title"])
        case "create_task":
            return task_tools.create_task(inputs["title"], inputs.get("description", ""), inputs.get("depends_on"))
        case "update_task":
            return task_tools.update_task(inputs["task_id"], inputs["status"], inputs.get("result", ""))
        case "list_tasks":
            return task_tools.list_tasks(inputs.get("filter", "all"))
        case "complete_task":
            return task_tools.complete_task(inputs["task_id"], inputs.get("result", ""))
        case "send_message":
            return comms_tools.send_message(inputs["message"], send_update)
        case "invoke_skill":
            return skill_tool.invoke_skill(inputs["name"], inputs.get("args", ""))
        case "run_agent":
            return agent_tool.run_agent(
                inputs["description"], inputs["prompt"],
                inputs.get("background", False),
                inputs.get("agent_type", "worker")
            )
        case "get_agent_output":
            return agent_tool.get_agent_output(inputs["task_id"])
        case "send_to_agent":
            return agent_tool.send_to_agent(inputs["agent_id"], inputs["message"])
        case "stop_agent":
            return agent_tool.stop_agent(inputs["agent_id"])
        case "list_agents":
            return agent_tool.list_agents()
        case "get_task":
            return task_tools.get_task(inputs["task_id"])
        case "claim_task":
            return task_tools.claim_task(
                inputs["task_id"], inputs["agent_id"], inputs.get("check_agent_busy", False)
            )
        case "block_task":
            return task_tools.block_task(inputs["task_id"], inputs["blocked_by_id"])
        case "exit_plan_mode":
            return _exit_plan_mode(inputs.get("plan", ""))
        case "queue_self_task":
            return _queue_self_task(inputs.get("prompt", ""), inputs.get("context", ""))
        case "set_goal":
            return _set_goal(inputs.get("goal", ""), inputs.get("notes", ""))
        case _:
            if name.startswith("mcp__"):
                try:
                    import mcp_manager as _mcp
                    return _mcp.get_manager().call(name, inputs)
                except Exception as e:
                    return f"[MCP] Error: {e}"
            if name in _dynamic_fns:
                return str(_dynamic_fns[name](**inputs))
            return f"Unknown tool: {name}"


# Initialize
_rebuild_tool_list()
reload_dynamic_tools()
