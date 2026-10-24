"""Channel-agnostic message handling.

The web simulator and the WhatsApp webhook both call handle_inbound().
It saves the message, runs the agent, saves the reply, records the turn and,
for WhatsApp, sends the reply through the Cloud API.
"""
import logging
import threading
import time
from collections import defaultdict

from app.agent import ModelBusy, run_agent
from app.config import DEFAULT_TIMEZONE
from app.db import fetch_one, get_pool
from app import onboarding_form, whatsapp

log = logging.getLogger(__name__)

FALLBACK_REPLY = "Sorry, something went wrong on my side 🙏 Please try again in a minute."
BUSY_REPLY = "I'm a bit swamped right now 😅 Please send that again in a minute."


def _upsert_user(conn, user_id):
    conn.execute(
        """insert into users (user_id, timezone) values (%s, %s)
           on conflict (user_id) do update set last_inbound_message_at = now(), is_active = true""",
        (user_id, DEFAULT_TIMEZONE))
    conn.execute("update users set last_inbound_message_at = now() where user_id = %s", (user_id,))
    return fetch_one(conn, "select * from users where user_id = %s", (user_id,))


def handle_inbound(user_id: str, text: str, channel: str = "web_simulator",
                   whatsapp_message_id: str | None = None) -> dict:
    started = time.monotonic()
    pool = get_pool()

    # 1. Save the inbound message and open a turn (committed even if the agent fails).
    with pool.connection() as conn:
        user = _upsert_user(conn, user_id)
        inbound = fetch_one(conn,
            """insert into messages (user_id, direction, channel, whatsapp_message_id, content_type, message_text)
               values (%s, 'inbound', %s, %s, 'text', %s) returning message_id""",
            (user_id, channel, whatsapp_message_id, text))
        turn = fetch_one(conn,
            """insert into agent_turns (user_id, trigger_type, message_id)
               values (%s, 'user_message', %s) returning agent_turn_id""",
            (user_id, inbound["message_id"]))
    turn_id = turn["agent_turn_id"]

    # 2. Run the agent; all logging it does commits together with the reply.
    stats, error, sent = {}, None, []
    try:
        with pool.connection() as conn:
            reply, reaction, stats = run_agent(conn, user, inbound["message_id"], text, turn_id)
            if reaction:
                sent.append(_save_reaction(conn, user_id, channel, reaction, inbound["message_id"], turn_id))
            if reply:
                sent.append(_save_reply(conn, user_id, channel, reply, turn_id))
    except Exception as e:  # keep the user experience alive; record what broke
        if isinstance(e, ModelBusy):
            log.warning("agent turn %s: model busy: %s", turn_id, e)
        else:
            log.exception("agent turn %s failed", turn_id)
        error = f"{type(e).__name__}: {str(e)[:500]}"
        reply = BUSY_REPLY if isinstance(e, ModelBusy) else FALLBACK_REPLY
        with pool.connection() as conn:
            sent = [_save_reply(conn, user_id, channel, reply, turn_id)]

    # 3. Close the turn with tracking numbers.
    latency_ms = int((time.monotonic() - started) * 1000)
    if channel == "whatsapp":
        with pool.connection() as conn:
            for m in sent:
                whatsapp.deliver(conn, m["message_id"])
    with pool.connection() as conn:
        conn.execute(
            """update agent_turns set detected_intent = %s, tools_called = %s::jsonb, outcome = %s,
                   error_detail = %s, llm_call_count = %s, total_input_tokens = %s,
                   total_output_tokens = %s, total_thinking_tokens = %s, estimated_cost_usd = %s,
                   total_latency_ms = %s, completed_at = now()
               where agent_turn_id = %s""",
            (stats.get("intent"), _json(stats.get("tools_called", [])),
             "error" if error else stats.get("outcome", "success"), error or stats.get("error"),
             stats.get("llm_call_count", 0), stats.get("input", 0), stats.get("output", 0),
             stats.get("thinking", 0), stats.get("cost_usd", 0), latency_ms, turn_id))

    return {
        "agent_turn_id": turn_id,
        "replies": [{"message_id": m["message_id"], "content_type": m["content_type"], "text": m["message_text"],
                     "reaction_to_message_id": m["reaction_to_message_id"],
                     "created_at": m["created_at"].isoformat()} for m in sent],
        "debug": {"intent": stats.get("intent"), "outcome": "error" if error else stats.get("outcome"),
                  "tools_called": stats.get("tools_called", []), "llm_calls": stats.get("llm_call_count", 0),
                  "input_tokens": stats.get("input", 0), "output_tokens": stats.get("output", 0),
                  "thinking_tokens": stats.get("thinking", 0), "cost_usd": stats.get("cost_usd", 0),
                  "cached_tokens": stats.get("cached", 0), "reaction": stats.get("reaction"),
                  "latency_ms": latency_ms, "error": error},
    }


def _delivery_status(channel):
    # Simulator messages are "sent" instantly; WhatsApp ones start queued until Meta confirms.
    return "sent" if channel == "web_simulator" else "queued"


def _save_reply(conn, user_id, channel, text, turn_id):
    return fetch_one(conn,
        """insert into messages (user_id, direction, channel, content_type, message_text,
               agent_turn_id, send_mode, delivery_status)
           values (%s, 'outbound', %s, 'text', %s, %s, 'free_form', %s)
           returning message_id, content_type, message_text, reaction_to_message_id, created_at""",
        (user_id, channel, text, turn_id, _delivery_status(channel)))


def _save_reaction(conn, user_id, channel, emoji, inbound_message_id, turn_id):
    return fetch_one(conn,
        """insert into messages (user_id, direction, channel, content_type, message_text,
               reaction_to_message_id, agent_turn_id, send_mode, delivery_status)
           values (%s, 'outbound', %s, 'reaction', %s, %s, %s, 'free_form', %s)
           returning message_id, content_type, message_text, reaction_to_message_id, created_at""",
        (user_id, channel, emoji, inbound_message_id, turn_id, _delivery_status(channel)))


def _json(obj):
    import json
    return json.dumps(obj, default=str)


# ───────────── WhatsApp entry point ─────────────

_user_locks = defaultdict(threading.Lock)  # one turn at a time per user (messages sent in quick succession)

UNSUPPORTED_REPLY = ("I can only read text for now. Type it out, e.g. \"lunch 2 roti dal\" or "
                     "\"bench 60kg 3x10\", and I'll log it.")


def handle_whatsapp_message(event: dict):
    """Called in the background for each incoming WhatsApp message."""
    user_id, wamid = event["from"], event["wamid"]
    with get_pool().connection() as conn:
        if fetch_one(conn, "select 1 from messages where whatsapp_message_id = %s", (wamid,)):
            return  # Meta re-delivered a message we already handled
    with _user_locks[user_id]:
        whatsapp.mark_read(wamid)
        if event["type"] == "flow_reply":
            onboarding_form.handle_submission(event)
            return
        if event["text"]:
            if onboarding_form.enabled():
                with get_pool().connection() as conn:
                    user = fetch_one(conn, "select onboarding_status from users where user_id = %s", (user_id,))
                    if not user:
                        _upsert_user(conn, user_id)
                if not user or user["onboarding_status"] == "not_started":
                    if onboarding_form.send_form(user_id, wamid, event["text"].strip()):
                        return
            handle_inbound(user_id, event["text"].strip(), channel="whatsapp", whatsapp_message_id=wamid)
            return
        # Photos, voice notes, stickers…: save it and explain once.
        with get_pool().connection() as conn:
            _upsert_user(conn, user_id)
            conn.execute(
                """insert into messages (user_id, direction, channel, whatsapp_message_id, content_type, message_text)
                   values (%s, 'inbound', 'whatsapp', %s, %s, %s)""",
                (user_id, wamid, "image" if event["type"] == "image" else "audio" if event["type"] in ("audio", "voice")
                 else "text", f"[{event['type']}]"))
            if event["type"] == "reaction":
                return
            out = fetch_one(conn,
                """insert into messages (user_id, direction, channel, content_type, message_text, send_mode, delivery_status)
                   values (%s, 'outbound', 'whatsapp', 'text', %s, 'free_form', 'queued') returning message_id""",
                (user_id, UNSUPPORTED_REPLY))
            whatsapp.deliver(conn, out["message_id"])


STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}


def handle_whatsapp_status(event: dict):
    """Delivery receipts: sent → delivered → read, or failed (with the reason)."""
    status = event["status"]
    with get_pool().connection() as conn:
        if status == "failed":
            conn.execute("update messages set delivery_status = 'failed', failure_reason = %s where whatsapp_message_id = %s",
                         (event.get("error"), event["wamid"]))
        elif status in STATUS_RANK:
            conn.execute(
                """update messages set delivery_status = %s where whatsapp_message_id = %s
                   and coalesce(delivery_status, 'queued') <> 'failed'
                   and array_position(array['queued','sent','delivered','read'], coalesce(delivery_status, 'queued'))
                       < array_position(array['queued','sent','delivered','read'], %s)""",
                (status, event["wamid"], status))
