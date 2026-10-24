"""WhatsApp Cloud API: sending messages and reading webhook payloads.

Sending goes through deliver(), which reads a saved outbound row from `messages`, sends it
as text / reaction / template, and writes back WhatsApp's message id (wamid) and status.
Delivery receipts (sent → delivered → read, or failed) arrive later on the webhook.
"""
import hashlib
import hmac
import json
import logging

import httpx

from app.config import (WHATSAPP_ACCESS_TOKEN, WHATSAPP_API_VERSION, WHATSAPP_APP_SECRET,
                        WHATSAPP_PHONE_NUMBER_ID)
from app.db import fetch_one

log = logging.getLogger(__name__)
_http = httpx.Client(timeout=15)

WHATSAPP_TEXT_LIMIT = 4096


def configured() -> bool:
    return bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID)


def _post(payload: dict) -> dict:
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    r = _http.post(url, json={"messaging_product": "whatsapp", **payload},
                   headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"})
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        err = body.get("error", {})
        raise WhatsAppError(err.get("code"), err.get("message") or r.text[:300],
                            (err.get("error_data") or {}).get("details"))
    return body


class WhatsAppError(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details
        super().__init__(f"{code}: {message}" + (f" ({details})" if details else ""))


# ───────────── send ─────────────

def send_text(to: str, text: str) -> str:
    body = _post({"to": to, "type": "text", "text": {"body": text[:WHATSAPP_TEXT_LIMIT], "preview_url": False}})
    return body["messages"][0]["id"]


def send_reaction(to: str, wamid: str, emoji: str) -> str:
    body = _post({"to": to, "type": "reaction", "reaction": {"message_id": wamid, "emoji": emoji}})
    return body["messages"][0]["id"]


def send_template(to: str, name: str, language: str, params: list[str]) -> str:
    components = [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}] if params else []
    body = _post({"to": to, "type": "template",
                  "template": {"name": name, "language": {"code": language}, "components": components}})
    return body["messages"][0]["id"]


def send_flow(to: str, body: str, cta: str, flow_id: str, flow_token: str, screen: str, mode: str = "draft",
              header: str | None = None, footer: str | None = None) -> str:
    interactive = {
        "type": "flow",
        "body": {"text": body},
        "action": {"name": "flow", "parameters": {
            "flow_message_version": "3", "flow_token": flow_token, "flow_id": flow_id, "flow_cta": cta[:20],
            "flow_action": "navigate", "flow_action_payload": {"screen": screen}, "mode": mode}},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    body_ = _post({"to": to, "type": "interactive", "interactive": interactive})
    return body_["messages"][0]["id"]


def mark_read(wamid: str, typing: bool = True):
    """Blue ticks on the user's message, plus a 'typing…' bubble while the agent works."""
    payload = {"status": "read", "message_id": wamid}
    if typing:
        payload["typing_indicator"] = {"type": "text"}
    try:
        _post(payload)
    except WhatsAppError as e:
        if typing:  # older API versions don't know typing_indicator; just mark read
            try:
                _post({"status": "read", "message_id": wamid})
            except WhatsAppError:
                pass
        log.info("mark_read: %s", e)


def deliver(conn, message_id: int, template_params: list[str] | None = None):
    """Sends one saved outbound message and records the result on its row."""
    m = fetch_one(conn,
        """select m.*, t.template_name, t.language_code,
                  r.whatsapp_message_id as reaction_target_wamid
           from messages m
           left join whatsapp_templates t on t.whatsapp_template_id = m.whatsapp_template_id
           left join messages r on r.message_id = m.reaction_to_message_id
           where m.message_id = %s""", (message_id,))
    if not m or m["channel"] != "whatsapp" or m["direction"] != "outbound":
        return
    try:
        if m["content_type"] == "reaction":
            if not m["reaction_target_wamid"]:
                raise WhatsAppError("local", "reaction target has no WhatsApp id")
            wamid = send_reaction(m["user_id"], m["reaction_target_wamid"], m["message_text"])
        elif m["send_mode"] == "template":
            wamid = send_template(m["user_id"], m["template_name"], m["language_code"] or "en", template_params or [])
        else:
            wamid = send_text(m["user_id"], m["message_text"])
        conn.execute("update messages set whatsapp_message_id = %s, delivery_status = 'sent' where message_id = %s",
                     (wamid, message_id))
    except (WhatsAppError, httpx.HTTPError) as e:
        log.warning("WhatsApp send failed for message %s: %s", message_id, e)
        conn.execute("update messages set delivery_status = 'failed', failure_reason = %s where message_id = %s",
                     (str(e)[:500], message_id))


# ───────────── webhook helpers ─────────────

def signature_valid(raw_body: bytes, header: str | None) -> bool:
    """Meta signs every webhook with the app secret (X-Hub-Signature-256: sha256=<hex>)."""
    if not WHATSAPP_APP_SECRET or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict):
    """Yields ('message', {...}) and ('status', {...}) events from a webhook body."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                kind = msg.get("type")
                text = None
                if kind == "text":
                    text = msg["text"]["body"]
                elif kind == "button":
                    text = msg["button"].get("text")
                elif kind == "interactive":
                    i = msg["interactive"]
                    if i.get("type") == "nfm_reply":  # a submitted WhatsApp Flow (form)
                        try:
                            answers = json.loads(i["nfm_reply"].get("response_json") or "{}")
                        except ValueError:
                            answers = {}
                        yield "message", {"from": msg["from"], "wamid": msg["id"], "type": "flow_reply",
                                          "text": None, "form": answers, "profile_name": names.get(msg["from"])}
                        continue
                    text = (i.get("button_reply") or i.get("list_reply") or {}).get("title")
                yield "message", {"from": msg["from"], "wamid": msg["id"], "type": kind, "text": text,
                                  "profile_name": names.get(msg["from"])}
            for st in value.get("statuses", []):
                errors = st.get("errors") or []
                yield "status", {"wamid": st["id"], "status": st.get("status"),
                                 "error": "; ".join(f"{e.get('code')}: {e.get('title')}" for e in errors) or None}
