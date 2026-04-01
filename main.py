import json
import queue
import sys
import time
import threading
from pathlib import Path

import tools as tool_module
import coordinator as coord_module
import hooks as hooks_module

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
TOKEN_LOG = BASE_DIR / "memory" / "token_usage.jsonl"

_current_task: threading.Thread | None = None
_task_lock = threading.Lock()
_last_chat_id: int = 0


def _token_summary() -> str:
    if not TOKEN_LOG.exists():
        return "No token data yet. Send a message to the agent first."
    entries = []
    with open(TOKEN_LOG) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    if not entries:
        return "Token log is empty."

    last_zero = max((i for i, e in enumerate(entries) if e.get("iteration") == 0), default=0)
    session = entries[last_zero:]

    total_in = sum(e["input"] for e in session)
    total_out = sum(e["output"] for e in session)
    total_cache_read = sum(e.get("cache_read", 0) for e in session)
    total_cache_write = sum(e.get("cache_write", 0) for e in session)

    lines = [f"Last session ({len(session)} calls)\n"]
    lines.append(f"Input tokens:   {total_in:,}")
    lines.append(f"Output tokens:  {total_out:,}")
    lines.append(f"Cache reads:    {total_cache_read:,}  (saved ~{total_cache_read * 9 // 10:,})")
    lines.append(f"Cache writes:   {total_cache_write:,}")
    lines.append(f"Total:          {total_in + total_out:,}\n")
    lines.append("Per-call breakdown:")
    for e in session[-10:]:
        thinking = e.get("context_breakdown", {}).get("thinking_chars", 0)
        tool_res = e.get("context_breakdown", {}).get("tool_result_chars", 0)
        msgs = e.get("context_breakdown", {}).get("messages_in_context", 0)
        lines.append(
            f"  i={e['iteration']} in={e['input']} out={e['output']} "
            f"msgs={msgs} tool_res_chars={tool_res} thinking_chars={thinking}"
        )
    return "\n".join(lines)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _do_respond(agent, tg, text: str, chat_id: int) -> str:
    """Single agent.respond() call with typing indicator. Returns response."""
    stop_typing = threading.Event()
    threading.Thread(
        target=tg.start_typing_loop, args=(chat_id, stop_typing), daemon=True
    ).start()
    try:
        return agent.respond(text)
    except Exception as e:
        from agent import _log
        _log(f"[agent error] {e}")
        return f"Sorry, something went wrong: {e}"
    finally:
        stop_typing.set()


def run_agent(agent, tg, text: str, chat_id: int):
    """Process one message — runs in a background thread."""
    global _last_chat_id
    _last_chat_id = chat_id
    agent._current_chat_id = chat_id
    tool_module.reset_cancel()

    def send_update(msg: str):
        tg.send_chunked(chat_id, msg)

    agent.send_update = send_update

    from agent import _log

    response = _do_respond(agent, tg, text, chat_id)

    if tool_module.is_cancelled():
        _log("[Agent] Loop cancelled — dropping response.")
        return

    _log(f"[Response] {response[:300]}")
    tg.send_chunked(chat_id, response)

    agent.dream.on_task_complete()

    # Auto-continue: [[CONTINUE]] tokens + self-queued tasks both drain here
    while not tool_module.is_cancelled():
        next_step = tool_module.get_continuation()

        # Also drain self-task queue if no [[CONTINUE]] pending
        if not next_step:
            try:
                next_step = tool_module._self_task_queue.get_nowait()
                _log(f"[Self-task] {next_step[:80]}")
            except Exception:
                break

        response = _do_respond(agent, tg, next_step, chat_id)

        if tool_module.is_cancelled():
            break

        _log(f"[Agent] {response[:200]}")
        tg.send_chunked(chat_id, response)

        agent.dream.on_task_complete()


def dispatch(agent, tg, text: str, chat_id: int):
    """Cancel any running task and start a new one."""
    global _current_task

    with _task_lock:
        if _current_task and _current_task.is_alive():
            print("[Interrupt] Cancelling current task")
            tool_module.cancel()
            _current_task.join(timeout=3.0)

        _current_task = threading.Thread(
            target=run_agent,
            args=(agent, tg, text, chat_id),
            daemon=True,
        )
        _current_task.start()


def main():
    config = load_config()
    token = config.get("telegram_token", "")

    if not token or "YOUR_TOKEN" in token:
        print("ERROR: Set your Telegram bot token in config.json under 'telegram_token'.")
        sys.exit(1)

    poll_interval = config.get("poll_interval", 1)
    allowed_chat_ids = config.get("allow_from_chat_ids", [])

    from telegram import TelegramHandler
    from agent import Agent, RUN_LOG, _log

    tg = TelegramHandler(token, allowed_chat_ids, proxy=config.get("telegram_proxy"))
    agent = Agent(config)

    # Wire up Telegram + callback queue to agent for permission prompts (Phase 2)
    callback_queue: queue.Queue[str] = queue.Queue()
    agent._tg = tg
    agent._callback_queue = callback_queue

    agent.dream.on_session_start()

    # Fire SessionStart hooks
    hooks_module.fire("SessionStart", {"event": "SessionStart"})

    with open(RUN_LOG, "a", encoding="utf-8") as f:
        from datetime import datetime
        f.write(f"\n{'='*60}\n")
        f.write(f"SESSION START {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"  Provider : {config.get('provider')} / {config.get('model')}\n")
        f.write(f"  Autonomous: {config.get('autonomous', False)}\n")
        f.write(f"{'='*60}\n")

    print(f"[openclawde] Running")
    print(f"  Provider  : {config.get('provider')} / {config.get('model')}")
    print(f"  Workspace : {BASE_DIR / 'workspace'}")
    print(f"  Autonomous: {config.get('autonomous', False)}")

    # Auto-wake: load persistent goal and prime the self-task queue
    goal_data = tool_module.load_goal()
    if goal_data:
        goal_txt = goal_data["goal"]
        notes_txt = goal_data.get("notes", "")
        print(f"  Goal      : {goal_txt[:80]}")
        wake_prompt = (
            f"Autonomous session start. Your persistent goal:\n\n{goal_txt}"
            + (f"\n\nContext: {notes_txt}" if notes_txt else "")
            + "\n\nCheck your task list, assess progress, and continue working toward the goal."
        )
        tool_module._self_task_queue.put(wake_prompt)

    if config.get("autonomous", False):
        print("  [AUTONOMOUS MODE] Agent will work without human confirmation.")
    print("Send a message to your bot on Telegram to start. Ctrl+C to stop.\n")

    while True:
        try:
            messages, callbacks = tg.poll_all()

            # Route inline keyboard callbacks to the waiting agent thread
            for cb_data, cb_query_id in callbacks:
                tg.answer_callback(cb_query_id)
                try:
                    callback_queue.put_nowait(cb_data)
                except Exception:
                    pass

            for text, chat_id in messages:
                _log(f"[User] {text}")

                if text.lower() in ("/clear", "clear history"):
                    tool_module.cancel()
                    agent.clear_history()
                    tg.send(chat_id, "History cleared.")
                    continue

                if text.lower() in ("/stop", "stop"):
                    tool_module.cancel()
                    tg.send(chat_id, "Stopped.")
                    continue

                if text.lower() == "/tokens":
                    tg.send_chunked(chat_id, _token_summary())
                    continue

                if text.lower() == "/tasks":
                    tg.send_chunked(chat_id, tool_module.execute("list_tasks", {}))
                    continue

                # Phase 2: planning mode toggle
                if text.lower() == "/plan":
                    tool_module.set_planning_mode(True)
                    tg.send(chat_id, (
                        "Planning mode ON. I'll read and research only — no writes or execution.\n"
                        "Describe your task and I'll draft a plan.\n"
                        "Send /execute when you're ready to run it."
                    ))
                    continue

                if text.lower() == "/execute":
                    tool_module.set_planning_mode(False)
                    tg.send(chat_id, "Execution mode ON. Full tool set active.")
                    continue

                if text.lower() == "/coord":
                    coord_module.set_coordinator_mode(True)
                    tg.send(chat_id, (
                        "Coordinator mode ON.\n"
                        "I'll orchestrate worker agents for complex tasks.\n"
                        "Workers handle implementation; I handle planning and synthesis.\n"
                        "Send /agent to return to normal mode."
                    ))
                    continue

                if text.lower() == "/agent":
                    coord_module.set_coordinator_mode(False)
                    tg.send(chat_id, "Normal agent mode ON. Coordinator mode OFF.")
                    continue

                if text.lower() == "/agents":
                    result = tool_module.execute("list_agents", {})
                    tg.send_chunked(chat_id, result)
                    continue

                # Skill shorthand: /skill_name [args] — expand and dispatch to agent
                if text.startswith("/") and not text.startswith("//"):
                    parts = text[1:].split(None, 1)
                    skill_name = parts[0].lower()
                    skill_args = parts[1] if len(parts) > 1 else ""
                    try:
                        import skills as _skills_mod
                        skill = _skills_mod.get_manager().get(skill_name)
                        if skill:
                            expanded = skill.expand(skill_args)
                            dispatch(agent, tg, expanded, chat_id)
                            continue
                    except Exception:
                        pass
                    # Unknown slash command — fall through to agent
                    tg.send(chat_id, f"Unknown command: /{skill_name}")
                    continue

                dispatch(agent, tg, text, chat_id)

        except KeyboardInterrupt:
            print("\n[Fake_openclaw_3] Stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[main error] {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
