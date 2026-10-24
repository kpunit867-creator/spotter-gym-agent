"""FastAPI app: health check, web simulator API, and (later) the WhatsApp webhook."""
import re
from pathlib import Path

import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import reminders
from app import tools as T
from app import whatsapp
from app.config import CRON_SECRET, SCHEDULER_ENABLED, SIMULATOR_ENABLED, WHATSAPP_VERIFY_TOKEN
from app.db import check_schema, fetch_all, fetch_one, get_pool
from app.engine import handle_inbound, handle_whatsapp_message, handle_whatsapp_status, recover_unanswered

log = logging.getLogger(__name__)

app = FastAPI(title="Gym WhatsApp Agent")


@app.on_event("startup")
def _start_scheduler():
    if SCHEDULER_ENABLED:
        reminders.start_scheduler()
    try:
        n = recover_unanswered()
        if n:
            log.warning("Recovered %d WhatsApp message(s) interrupted by the last restart", n)
    except Exception:
        log.exception("Couldn't check for interrupted messages")
STATIC = Path(__file__).resolve().parent / "static"
PHONE_RE = re.compile(r"^[0-9]{8,15}$")


@app.middleware("http")
async def _simulator_switch(request: Request, call_next):
    if not SIMULATOR_ENABLED and (request.url.path == "/" or request.url.path.startswith("/api/sim")):
        return PlainTextResponse("Not found", status_code=404)
    return await call_next(request)


def _check_phone(user_id: str):
    if not PHONE_RE.match(user_id):
        raise HTTPException(400, "Phone number must be 8–15 digits with country code, e.g. 919876543210")


@app.get("/health")
def health():
    return {"status": "ok", "database": check_schema()}


# ───────────────────────── WhatsApp webhook ─────────────────────────

@app.get("/webhook/whatsapp")
def whatsapp_verify(mode: str = Query(None, alias="hub.mode"), token: str = Query(None, alias="hub.verify_token"),
                    challenge: str = Query(None, alias="hub.challenge")):
    """Meta calls this once when you save the webhook URL; we echo the challenge if the token matches."""
    if mode == "subscribe" and WHATSAPP_VERIFY_TOKEN and token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, background: BackgroundTasks,
                           x_hub_signature_256: str | None = Header(None)):
    """Incoming messages and delivery receipts. Answer 200 fast; the agent runs in the background."""
    raw = await request.body()
    if not whatsapp.signature_valid(raw, x_hub_signature_256):
        log.warning("Rejected WhatsApp webhook with a bad signature")
        raise HTTPException(403, "Bad signature")
    for kind, event in whatsapp.parse_webhook(json.loads(raw or b"{}")):
        background.add_task(handle_whatsapp_message if kind == "message" else handle_whatsapp_status, event)
    return {"status": "ok"}


# ───────────────────────── scheduled jobs ─────────────────────────

@app.post("/cron/run-reminders")
def cron_run_reminders(x_cron_secret: str | None = Header(None)):
    """Sends due reminders. Called every few minutes by Supabase pg_cron in production."""
    if not CRON_SECRET or x_cron_secret != CRON_SECRET:
        raise HTTPException(403, "Bad cron secret")
    results = reminders.run_due_reminders()
    return {"checked": len(results), "sent": sum(r["sent"] for r in results)}


# ───────────────────────── web simulator ─────────────────────────

@app.get("/")
def simulator_page():
    return FileResponse(STATIC / "simulator.html")


class SimMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.get("/api/sim/users")
def sim_users():
    with get_pool().connection() as conn:
        return fetch_all(conn,
            """select u.user_id, u.name, u.onboarding_status,
                      max(m.created_at) as last_message_at
               from users u join messages m on m.user_id = u.user_id and m.channel = 'web_simulator'
               group by u.user_id order by last_message_at desc nulls last""")


@app.post("/api/sim/users/{user_id}/messages")
def sim_send(user_id: str, body: SimMessage):
    _check_phone(user_id)
    with get_pool().connection() as conn:
        on_whatsapp = fetch_one(conn,
            "select 1 from messages where user_id = %s and channel = 'whatsapp' limit 1", (user_id,))
    if on_whatsapp:
        raise HTTPException(409, "This number is a real WhatsApp user; use a test number in the simulator.")
    return handle_inbound(user_id, body.text.strip(), channel="web_simulator")


@app.get("/api/sim/users/{user_id}/messages")
def sim_history(user_id: str):
    _check_phone(user_id)
    with get_pool().connection() as conn:
        rows = fetch_all(conn,
            """select m.message_id, m.direction, m.message_text as text, m.created_at, m.delivery_status,
                      m.content_type, m.reaction_to_message_id, t.agent_turn_id,
                      t.detected_intent as intent, t.outcome, t.tools_called, t.llm_call_count as llm_calls,
                      t.total_input_tokens as input_tokens, t.total_output_tokens as output_tokens,
                      t.total_thinking_tokens as thinking_tokens, t.estimated_cost_usd as cost_usd,
                      t.total_latency_ms as latency_ms, t.error_detail as error,
                      (select coalesce(sum(c.cached_tokens), 0) from llm_calls c
                        where c.agent_turn_id = t.agent_turn_id) as cached_tokens
               from messages m
               left join agent_turns t on t.agent_turn_id = m.agent_turn_id
                  or (m.direction = 'inbound' and t.message_id = m.message_id)
               where m.user_id = %s order by m.message_id""", (user_id,))
    return rows


@app.get("/api/sim/users/{user_id}/today")
def sim_today(user_id: str):
    _check_phone(user_id)
    with get_pool().connection() as conn:
        user = fetch_one(conn, "select * from users where user_id = %s", (user_id,))
        if not user:
            raise HTTPException(404, "No such user yet")
        today = T.user_today(user["timezone"])
        return {"profile": user, "log_date": str(today),
                "today": T.today_snapshot(conn, user_id, today),
                "targets": T.current_targets(conn, user_id, today)}


@app.delete("/api/sim/users/{user_id}")
def sim_reset(user_id: str):
    """Deletes a simulator test user and all their data (never a real WhatsApp user)."""
    _check_phone(user_id)
    with get_pool().connection() as conn:
        if fetch_one(conn, "select 1 from messages where user_id = %s and channel = 'whatsapp' limit 1", (user_id,)):
            raise HTTPException(409, "Refusing to delete a real WhatsApp user.")
        conn.execute("delete from users where user_id = %s", (user_id,))
    return {"status": "deleted", "user_id": user_id}


@app.get("/api/sim/users/{user_id}/reminders")
def sim_reminders(user_id: str):
    _check_phone(user_id)
    with get_pool().connection() as conn:
        return fetch_all(conn,
            """select reminder_id, reminder_type, send_time, is_enabled, next_run_at, last_sent_at
               from reminders where user_id = %s and reminder_type = any(%s) order by send_time""",
            (user_id, list(reminders.TEMPLATES)))


@app.post("/api/sim/users/{user_id}/reminders/{reminder_type}/send")
def sim_send_reminder(user_id: str, reminder_type: str):
    """Testing aid: sends this reminder now, even if its check would skip it (the reason is returned)."""
    _check_phone(user_id)
    if reminder_type not in reminders.TEMPLATES:
        raise HTTPException(404, "Unknown reminder type")
    with get_pool().connection() as conn:
        user = fetch_one(conn, "select * from users where user_id = %s", (user_id,))
        if not user or user["onboarding_status"] != "completed":
            raise HTTPException(409, "Finish onboarding first")
        reminders.ensure_default_reminders(conn, user)
        r = fetch_one(conn, "select * from reminders where user_id = %s and reminder_type = %s order by send_time limit 1",
                      (user_id, reminder_type))
        return reminders.send_reminder(conn, r, force=True)
