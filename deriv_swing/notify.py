"""Telegram alerts. Messages are sent from a background thread so a slow or failing Telegram
never delays the trading loop.

Setup helpers:
  python -m deriv_swing.notify --find-chat   # after messaging your bot, prints your chat id
  python -m deriv_swing.notify --test        # sends a test alert using config.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import urllib.parse
import urllib.request

log = logging.getLogger("deriv_swing")
API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, params: dict | None = None, timeout: float = 15) -> dict:
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(API.format(token=token, method=method), data=data, timeout=timeout) as resp:
        return json.loads(resp.read())


class Notifier:
    """No-op unless telegram.enabled is true with a bot token and chat id."""

    def __init__(self, tcfg: dict | None, sender=None):
        tcfg = tcfg or {}
        self.enabled = bool(tcfg.get("enabled") and tcfg.get("bot_token") and tcfg.get("chat_id"))
        self.token = tcfg.get("bot_token", "")
        self.chat_id = str(tcfg.get("chat_id", ""))
        self.notify_rejections = bool(tcfg.get("notify_rejections", False))
        self._send = sender or self._send_telegram
        self._q: queue.Queue[str] = queue.Queue(maxsize=200)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="telegram").start()

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait(text)
        except queue.Full:
            log.warning("telegram queue full, dropping alert")

    def _worker(self):
        while True:
            text = self._q.get()
            try:
                self._send(text)
            except Exception as exc:
                log.warning("telegram send failed: %s", exc)

    def _send_telegram(self, text: str) -> None:
        _call(self.token, "sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                          "disable_web_page_preview": "true"})


def main():
    from .config import load_config

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--find-chat", action="store_true", help="list chats that have messaged the bot")
    ap.add_argument("--test", action="store_true", help="send a test alert")
    args = ap.parse_args()
    tcfg = load_config(args.config)["telegram"]
    if not tcfg.get("bot_token"):
        raise SystemExit("Set telegram.bot_token in config.yaml first (get one from @BotFather).")
    if args.find_chat:
        updates = _call(tcfg["bot_token"], "getUpdates").get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"] for u in updates if "message" in u}
        if not chats:
            print("No messages yet. Send any message to your bot in Telegram, then run this again.")
        for cid, chat in chats.items():
            print(f"chat_id: {cid}   ({chat.get('first_name') or chat.get('title', '')})")
    elif args.test:
        _call(tcfg["bot_token"], "sendMessage",
              {"chat_id": tcfg["chat_id"], "text": "✅ Deriv Swing test alert - Telegram is connected."})
        print("Sent.")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
