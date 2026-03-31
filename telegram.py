import threading
import time
import requests


class TelegramHandler:
    """Poll Telegram Bot API for messages and send replies."""

    def __init__(self, token: str, allowed_chat_ids: list[int] = None, proxy: str = None):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.allowed_chat_ids = set(allowed_chat_ids) if allowed_chat_ids else set()
        self.offset = 0
        self._last_chat_id: int | None = None

        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            self.session.headers["Connection"] = "close"
            print(f"[Telegram] Using proxy: {proxy}")

        self._init()

    def _init(self):
        try:
            resp = self.session.get(f"{self.base}/getMe", timeout=10)
            info = resp.json()
            if info.get("ok"):
                name = info["result"].get("username", "?")
                print(f"[Telegram] Bot ready: @{name}")
                if self.allowed_chat_ids:
                    print(f"[Telegram] Accepting chat IDs: {self.allowed_chat_ids}")
                else:
                    print("[Telegram] Accepting messages from anyone")
            else:
                print(f"[Telegram] Token error: {info}")
        except Exception as e:
            print(f"[Telegram init error] {e}")

    def poll_all(self) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
        """
        Fetch all pending updates.
        Returns:
          messages:  list of (text, chat_id)
          callbacks: list of (callback_data, callback_query_id)
        """
        try:
            resp = self.session.get(
                f"{self.base}/getUpdates",
                params={"offset": self.offset, "timeout": 0},
                timeout=10,
            )
            updates = resp.json().get("result", [])
        except Exception as e:
            print(f"[Telegram poll error] {e}")
            return [], []

        messages = []
        callbacks = []

        for u in updates:
            self.offset = u["update_id"] + 1

            # Inline keyboard callback
            if "callback_query" in u:
                cq = u["callback_query"]
                data = cq.get("data", "")
                qid = cq.get("id", "")
                callbacks.append((data, qid))
                continue

            # Regular message
            msg = u.get("message") or u.get("edited_message", {})
            text = msg.get("text", "").strip()
            chat_id = msg.get("chat", {}).get("id")
            if not text or not chat_id:
                continue
            print(f"[Telegram] Incoming chat_id={chat_id} text={text[:60]!r}")
            if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
                print(f"[Telegram] Blocked — chat_id {chat_id} not in allow list")
                continue
            self._last_chat_id = chat_id
            messages.append((text, chat_id))

        return messages, callbacks

    def poll(self) -> list[tuple[str, int]]:
        """Backward-compat wrapper — returns messages only."""
        messages, _ = self.poll_all()
        return messages

    def send_typing(self, chat_id: int):
        try:
            self.session.post(
                f"{self.base}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5,
            )
        except Exception:
            pass

    def start_typing_loop(self, chat_id: int, stop_event: threading.Event):
        while not stop_event.is_set():
            self.send_typing(chat_id)
            stop_event.wait(4)

    def send(self, chat_id: int, text: str):
        try:
            self.session.post(
                f"{self.base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as e:
            print(f"[Telegram send error] {e}")

    def send_chunked(self, chat_id: int, text: str, max_len: int = 4000):
        text = text.strip()
        if not text:
            return
        if len(text) <= max_len:
            self.send(chat_id, text)
            return
        chunks = [text[i: i + max_len] for i in range(0, len(text), max_len)]
        for i, chunk in enumerate(chunks):
            label = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
            self.send(chat_id, label + chunk)
            time.sleep(0.3)

    # ------------------------------------------------------------------
    # Phase 2: Inline keyboard for permission confirmations (stubs)
    # ------------------------------------------------------------------

    def send_inline_keyboard(
        self,
        chat_id: int,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> int:
        """Send a message with an inline keyboard. Returns the message_id."""
        keyboard = {
            "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
        }
        try:
            resp = self.session.post(
                f"{self.base}/sendMessage",
                json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
                timeout=10,
            )
            result = resp.json().get("result", {})
            return result.get("message_id", 0)
        except Exception as e:
            print(f"[Telegram keyboard error] {e}")
            return 0

    def answer_callback(self, callback_query_id: str):
        """Acknowledge a callback query (removes loading spinner on button)."""
        try:
            self.session.post(
                f"{self.base}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
                timeout=5,
            )
        except Exception:
            pass
