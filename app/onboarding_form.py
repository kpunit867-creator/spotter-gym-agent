"""WhatsApp onboarding via an in-chat form (WhatsApp Flow).

First message from a new user → greeting + "Set up profile" form button.
Form submitted → profile saved and targets calculated in code (no Gemini call), confirmation sent.
If the user ignores the form and just types, the normal chat onboarding takes over.
"""
import json
import logging
import time

from app import whatsapp
from app.config import WHATSAPP_FLOW_MODE, WHATSAPP_ONBOARDING_FLOW_ID
from app.db import fetch_one, get_pool
from app.onboarding import save_profile
from app.tools import ToolContext, ToolError

log = logging.getLogger(__name__)

FORM_BODY = ("Hey! I'm Spotter, your gym buddy on WhatsApp. Text me your workouts, meals and weight and "
             "I'll keep track, remind you and send weekly reports.\n\nTap below to set up your profile.")
FORM_CTA = "Set up profile"
FORM_FOOTER = "Takes about 30 seconds"


_blocked_reason = None  # set when Meta refuses forms for this account (e.g. 139000 integrity), until restart


def enabled() -> bool:
    return bool(WHATSAPP_ONBOARDING_FLOW_ID) and whatsapp.configured() and _blocked_reason is None


def _save_inbound(conn, user_id, wamid, content_type, text):
    return fetch_one(conn,
        """insert into messages (user_id, direction, channel, whatsapp_message_id, content_type, message_text)
           values (%s, 'inbound', 'whatsapp', %s, %s, %s) returning message_id""",
        (user_id, wamid, content_type, text))["message_id"]


def _open_turn(conn, user_id, message_id, tool, status, started):
    return fetch_one(conn,
        """insert into agent_turns (user_id, trigger_type, message_id, detected_intent, tools_called, outcome,
               total_latency_ms, completed_at)
           values (%s, 'user_message', %s, 'onboarding', %s::jsonb, %s, %s, now()) returning agent_turn_id""",
        (user_id, message_id, json.dumps([{"tool": tool, "args": {}, "status": status}]),
         "success" if status in ("sent", "onboarding_completed") else "needs_clarification",
         int((time.monotonic() - started) * 1000)))["agent_turn_id"]


def send_form(user_id, wamid, text):
    """First contact: reply with the onboarding form. Returns False (nothing saved) if it can't be sent."""
    started = time.monotonic()
    try:
        sent_id = whatsapp.send_flow(user_id, FORM_BODY, FORM_CTA, WHATSAPP_ONBOARDING_FLOW_ID,
                                     flow_token=f"onboarding:{user_id}", screen="PROFILE",
                                     mode=WHATSAPP_FLOW_MODE, footer=FORM_FOOTER)
    except whatsapp.WhatsAppError as e:
        global _blocked_reason
        if str(e.code) in ("139000", "131051", "100"):  # account can't send Flows: stop trying until restart
            _blocked_reason = str(e)
        log.warning("Onboarding form failed to send (%s); falling back to chat onboarding", e)
        return False
    with get_pool().connection() as conn:
        mid = _save_inbound(conn, user_id, wamid, "text", text)
        conn.execute("update users set onboarding_status = 'in_progress', onboarding_step = 'name' where user_id = %s",
                     (user_id,))
        turn = _open_turn(conn, user_id, mid, "onboarding_form", "sent", started)
        conn.execute(
            """insert into messages (user_id, direction, channel, whatsapp_message_id, content_type, message_text,
                   agent_turn_id, send_mode, delivery_status)
               values (%s, 'outbound', 'whatsapp', %s, 'interactive', %s, %s, 'free_form', 'sent')""",
            (user_id, sent_id, FORM_BODY, turn))
    return True


def _num(v, cast):
    try:
        return cast(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def handle_submission(event):
    """Form submitted: save everything at once and send the targets."""
    from app import replies
    user_id, answers, started = event["from"], event.get("form") or {}, time.monotonic()
    if answers.get("flow_token") and answers["flow_token"] != f"onboarding:{user_id}":
        log.warning("Form reply with someone else's token from %s; ignored", user_id)
        return
    with get_pool().connection() as conn:
        conn.execute("""insert into users (user_id) values (%s) on conflict (user_id) do update
                        set last_inbound_message_at = now()""", (user_id,))
        conn.execute("update users set last_inbound_message_at = now() where user_id = %s", (user_id,))
        user = fetch_one(conn, "select * from users where user_id = %s", (user_id,))
        summary = ", ".join(f"{k}: {v}" for k, v in answers.items() if k != "flow_token")
        mid = _save_inbound(conn, user_id, event["wamid"], "interactive", f"[form] {summary}")
        ctx = ToolContext(conn=conn, user=user, message_id=mid)
        try:
            with conn.transaction():
                result = save_profile(
                    ctx, name=answers.get("name"), age=_num(answers.get("age"), int), gender=answers.get("gender"),
                    height_cm=_num(answers.get("height_cm"), float), weight=_num(answers.get("weight_kg"), float),
                    weight_unit="kg", goal=answers.get("goal"), experience_level=answers.get("experience_level"),
                    diet_preference=answers.get("diet_preference"),
                    workouts_per_week=_num(answers.get("workouts_per_week"), int),
                    workout_time=answers.get("workout_time"))
        except ToolError as e:
            result = {"status": "error", "not_saved": [str(e)]}
        status = result["status"]
        if status == "onboarding_completed":
            text = replies.onboarding_complete(ctx.user, result)
        else:
            problems = "; ".join(p.split(":")[0].replace("_", " ") for p in (result.get("not_saved") or [])) or "a few details"
            text = (f"Thanks! I couldn't save {problems}. Just type the right value here "
                    f"(e.g. \"age 28\" or \"height 172 cm\") and I'll finish your setup.")
        turn = _open_turn(conn, user_id, mid, "onboarding_form", status, started)
        out = fetch_one(conn,
            """insert into messages (user_id, direction, channel, content_type, message_text, agent_turn_id,
                   send_mode, delivery_status)
               values (%s, 'outbound', 'whatsapp', 'text', %s, %s, 'free_form', 'queued') returning message_id""",
            (user_id, text, turn))["message_id"]
        whatsapp.deliver(conn, out)
