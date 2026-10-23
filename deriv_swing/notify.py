"""WhatsApp alerts through Meta's WhatsApp Cloud API.

WhatsApp only lets a business number message you first with a pre-approved *template*. The bot uses
one generic template with a single blank, e.g.

    Deriv Swing alert: {{1}}. Sent by your trading bot.

and puts each alert into {{1}}. Template text can't contain line breaks, so multi-line alerts are
joined with " · ". Messages are sent from a background thread so a slow or failing API never delays
the trading loop.

Setup helpers:
  python -m deriv_swing.notify --hello   # Meta's built-in hello_world template: checks token, number id, recipient
  python -m deriv_swing.notify --test    # sends a test alert through YOUR template
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import threading
import urllib.error
import urllib.request

log = logging.getLogger("deriv_swing")
API = "https://graph.facebook.com/{version}/{phone_number_id}/messages"
MAX_PARAM = 1000   # template parameters are capped at 1,024 characters


def to_param(text: str) -> str:
    """Make alert text legal for a template parameter: one line, no tabs, no long space runs."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    flat = re.sub(r"[ \t]{2,}", " ", " · ".join(lines).replace("\t", " ")).rstrip(" .")  # template adds the "."
    return flat if len(flat) <= MAX_PARAM else flat[:MAX_PARAM - 1] + "…"


def _post(wcfg: dict, payload: dict, timeout: float = 15) -> dict:
    url = API.format(version=wcfg.get("api_version", "v21.0"), phone_number_id=wcfg["phone_number_id"])
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {wcfg['access_token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:   # Meta explains what's wrong in the body
        try:
            err = json.loads(exc.read()).get("error", {})
            detail = f"{err.get('message', '')} (code {err.get('code')})"
        except Exception:
            detail = ""
        raise RuntimeError(f"WhatsApp API HTTP {exc.code}: {detail}") from None


def template_payload(to: str, name: str, language: str, param: str | None) -> dict:
    template: dict = {"name": name, "language": {"code": language}}
    if param is not None:
        template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": param}]}]
    return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}


def _recipients(wcfg: dict) -> list[str]:
    to = wcfg.get("to") or []
    return [str(n).lstrip("+").replace(" ", "") for n in ([to] if isinstance(to, (str, int)) else to)]


class Notifier:
    """No-op unless whatsapp.enabled is true and token, phone number id, template and a recipient are set."""

    def __init__(self, wcfg: dict | None, sender=None):
        self.cfg = wcfg = wcfg or {}
        self.recipients = _recipients(wcfg)
        self.enabled = bool(wcfg.get("enabled") and wcfg.get("access_token") and wcfg.get("phone_number_id")
                            and wcfg.get("template") and self.recipients)
        self.notify_rejections = bool(wcfg.get("notify_rejections", False))
        self._send = sender or self._send_whatsapp
        self._q: queue.Queue[str] = queue.Queue(maxsize=200)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="whatsapp").start()

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait(to_param(text))
        except queue.Full:
            log.warning("alert queue full, dropping alert")

    def _worker(self):
        while True:
            text = self._q.get()
            try:
                self._send(text)
            except Exception as exc:
                log.warning("whatsapp send failed: %s", exc)

    def _send_whatsapp(self, text: str) -> None:
        for to in self.recipients:
            _post(self.cfg, template_payload(to, self.cfg["template"], self.cfg.get("language", "en"), text))


def main():
    from .config import load_config

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--hello", action="store_true", help="send Meta's pre-approved hello_world template")
    ap.add_argument("--test", action="store_true", help="send a test alert through your template")
    args = ap.parse_args()
    wcfg = load_config(args.config)["whatsapp"]
    missing = [k for k in ("access_token", "phone_number_id") if not wcfg.get(k)] + \
              ([] if _recipients(wcfg) else ["to"])
    if missing:
        raise SystemExit(f"Set whatsapp.{', whatsapp.'.join(missing)} in config.yaml first (see README).")
    if args.hello:
        for to in _recipients(wcfg):
            print(to, _post(wcfg, template_payload(to, "hello_world", "en_US", None)))
    elif args.test:
        if not wcfg.get("template"):
            raise SystemExit("Set whatsapp.template to your approved template's name first.")
        for to in _recipients(wcfg):
            print(to, _post(wcfg, template_payload(to, wcfg["template"], wcfg.get("language", "en"),
                                                   "✅ test alert, WhatsApp is connected")))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
