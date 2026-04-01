"""
Hooks system — external scripts react to agent events without modifying source.

Config files (merged, project overrides user):
  ~/.claude/hooks.json
  .claude/hooks.json   (project-local)

Supported events:
  SessionStart      — agent starts up
  PreToolUse        — before any tool executes (can approve / deny / rewrite inputs)
  PostToolUse       — after any tool completes (logging, notifications, CI triggers)
  PermissionRequest — when the permission gate asks user (hook can override decision)
  Notification      — when agent calls send_message()
  GoalSet           — when agent calls set_goal()

Hook definition (in hooks.json):
  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "run_shell",     // optional: only fires for this tool name
          "command": "python hooks/audit.py",
          "timeout": 5,               // seconds, default 10
          "async": false              // true = fire and forget
        }
      ]
    }
  }

Hook receives JSON on stdin:
  {
    "event": "PreToolUse",
    "tool_name": "run_shell",
    "inputs": { "command": "rm -rf /tmp/foo" },
    "agent_id": "agent_a1b2c3"
  }

Hook returns JSON on stdout (all fields optional):
  {
    "continue": true,                    // false = block the action
    "stop_reason": "Blocked by policy", // message if continue=false
    "decision": "approve",              // "approve" | "deny" — overrides permission gate
    "updated_inputs": { "command": "..." },  // rewrite the tool inputs
    "additional_context": "...",         // appended to tool result for agent to read
    "system_message": "..."              // warning shown to user
  }

Async hooks run in background threads and cannot influence the result.
A hook that exits non-zero or returns no JSON is silently skipped.
"""

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_PROJECT_HOOKS = Path(__file__).parent / ".claude" / "hooks.json"
_USER_HOOKS = Path.home() / ".claude" / "hooks.json"

_cache: Optional[dict] = None
_cache_lock = threading.Lock()


def _load() -> dict:
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        merged: dict[str, list] = {}
        for path in [_USER_HOOKS, _PROJECT_HOOKS]:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for event, hook_list in data.get("hooks", {}).items():
                        merged.setdefault(event, []).extend(hook_list)
                    print(f"[Hooks] Loaded: {path}")
                except Exception as e:
                    print(f"[Hooks] Config error {path}: {e}")
        _cache = merged
        return _cache


def reload():
    """Force reload hooks config on next call."""
    global _cache
    with _cache_lock:
        _cache = None


@dataclass
class HookResult:
    should_continue: bool = True
    stop_reason: str = ""
    decision: str = ""              # "approve" | "deny" | ""
    updated_inputs: dict = field(default_factory=dict)
    additional_context: str = ""
    system_message: str = ""

    def _apply(self, resp: dict):
        if resp.get("continue") is False:
            self.should_continue = False
            self.stop_reason = resp.get("stop_reason", "Blocked by hook.")
        decision = resp.get("decision", "")
        if decision in ("approve", "deny"):
            self.decision = decision
        if "updated_inputs" in resp and isinstance(resp["updated_inputs"], dict):
            self.updated_inputs.update(resp["updated_inputs"])
        if resp.get("additional_context"):
            self.additional_context = resp["additional_context"]
        if resp.get("system_message"):
            self.system_message = resp["system_message"]


def _run_one(hook: dict, payload: dict) -> dict:
    command = hook.get("command", "").strip()
    if not command:
        return {}
    timeout = float(hook.get("timeout", 10))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(__file__).parent),
            env={**os.environ},
        )
        if proc.returncode != 0:
            print(f"[Hooks] Non-zero exit {proc.returncode}: {proc.stderr[:200]}")
            return {}
        stdout = proc.stdout.strip()
        if stdout:
            return json.loads(stdout)
        return {}
    except subprocess.TimeoutExpired:
        print(f"[Hooks] Timeout ({timeout}s): {command[:60]}")
        return {}
    except json.JSONDecodeError:
        print(f"[Hooks] Invalid JSON from hook: {command[:60]}")
        return {}
    except Exception as e:
        print(f"[Hooks] Error ({command[:40]}): {e}")
        return {}


def fire(
    event: str,
    payload: dict,
    matcher: str = "",
) -> HookResult:
    """
    Fire all hooks registered for event.

    matcher: tool name for PreToolUse/PostToolUse filtering.
    Returns a HookResult — check .should_continue before proceeding.
    Async hooks run in background; they do not affect the result.
    """
    config = _load()
    result = HookResult()

    for hook in config.get(event, []):
        hook_matcher = hook.get("matcher", "")
        if hook_matcher and hook_matcher != matcher:
            continue

        if hook.get("async", False):
            threading.Thread(
                target=_run_one, args=(hook, payload), daemon=True
            ).start()
        else:
            resp = _run_one(hook, payload)
            result._apply(resp)
            if not result.should_continue:
                break  # First blocking hook wins; stop processing

    return result
