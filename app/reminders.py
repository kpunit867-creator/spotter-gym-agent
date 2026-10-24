"""Meal and workout logging reminders.

Each reminder has a time of day. When it's due, a check decides whether it's needed
(e.g. skip the meal reminder if a meal was logged recently). Messages come from fixed
templates, so reminders cost no Gemini calls. Outside WhatsApp's 24-hour window the
Meta-approved template is used; inside it, the same text is sent as a normal message.
"""
import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.db import fetch_all, fetch_one, get_pool
from app.timeutil import user_today

log = logging.getLogger(__name__)

MEAL_GAP_HOURS = 4          # meal reminder only if nothing was logged in the last 4 hours
DEFAULT_MEAL_TIMES = [dtime(14, 30), dtime(21, 30)]
DEFAULT_WORKOUT_CHECK = dtime(21, 0)  # used when the user didn't give a training time
WORKOUT_CHECK_AFTER_HOURS = 2         # check two hours after their usual training time

TEMPLATES = {
    "meal_log_reminder": {
        "template_name": "meal_log_reminder_v1",
        "text": "Hey {name}, you haven't logged a meal since {last_meal_time}. What did you eat? "
                "Just reply like: 2 roti, dal, sabzi",
    },
    "workout_log_reminder": {
        "template_name": "workout_log_reminder_v1",
        "text": "Hey {name}, no workout logged today. You're at {sessions_this_week} sessions this week. "
                "Trained? Send your sets, like: bench 60kg 3x10. Rest day? Just reply rest.",
    },
}


# ───────────────────────── scheduling ─────────────────────────

def next_run_at(send_time: dtime, days_of_week, tz: str, after: datetime) -> datetime:
    """The next time (after `after`) this reminder should fire, on an allowed weekday."""
    local = after.astimezone(ZoneInfo(tz))
    for i in range(8):
        day = local.date() + timedelta(days=i)
        if day.isoweekday() in days_of_week:
            candidate = datetime.combine(day, send_time, ZoneInfo(tz))
            if candidate > local:
                return candidate
    raise ValueError("reminder has no valid weekdays")


def workout_check_time(workout_time: dtime | None) -> dtime:
    if workout_time is None:
        return DEFAULT_WORKOUT_CHECK
    t = (datetime.combine(datetime.today(), workout_time) + timedelta(hours=WORKOUT_CHECK_AFTER_HOURS)).time()
    return min(t, dtime(23, 0)) if t > dtime(4, 0) else dtime(23, 0)  # don't wrap past midnight


def ensure_default_reminders(conn, user, workout_time: dtime | None = None):
    """Creates the meal and workout logging reminders for a user (safe to call again)."""
    uid, tz, now = user["user_id"], user["timezone"], datetime.now(ZoneInfo("UTC"))
    rows = [("meal_log_reminder", t) for t in DEFAULT_MEAL_TIMES]
    has_workout = fetch_one(conn,
        "select 1 from reminders where user_id = %s and reminder_type = 'workout_log_reminder'", (uid,))
    if not has_workout:
        rows.append(("workout_log_reminder", workout_check_time(workout_time)))
    for kind, t in rows:
        conn.execute(
            """insert into reminders (user_id, reminder_type, send_time, days_of_week, next_run_at)
               values (%s, %s, %s, '{1,2,3,4,5,6,7}', %s)
               on conflict (user_id, reminder_type, send_time) do nothing""",
            (uid, kind, t, next_run_at(t, range(1, 8), tz, now)))


# ───────────────────────── should we send it? ─────────────────────────

def _first_name(user):
    return (user.get("name") or "there").split()[0]


def check_meal(conn, user, now: datetime):
    uid, tz = user["user_id"], user["timezone"]
    today = user_today(tz)
    last = fetch_one(conn,
        """select created_at from meals where user_id = %s and log_date = %s and deleted_at is null
           order by created_at desc limit 1""", (uid, today))
    if last and last["created_at"] > now - timedelta(hours=MEAL_GAP_HOURS):
        return None, f"meal logged within the last {MEAL_GAP_HOURS} h"
    when = last["created_at"].astimezone(ZoneInfo(tz)).strftime("%-I:%M %p") if last else "this morning"
    return {"name": _first_name(user), "last_meal_time": when}, None


def check_workout(conn, user, now: datetime):
    uid, tz = user["user_id"], user["timezone"]
    today = user_today(tz)
    if fetch_one(conn, "select 1 from workout_sessions where user_id = %s and log_date = %s and deleted_at is null",
                 (uid, today)):
        return None, "workout already logged today"
    week_start = today - timedelta(days=today.weekday())
    done = fetch_one(conn,
        """select count(*) as n from workout_sessions
           where user_id = %s and log_date between %s and %s and deleted_at is null""",
        (uid, week_start, today))["n"]
    target = fetch_one(conn,
        """select workouts_per_week from user_targets where user_id = %s and start_date <= %s
           order by start_date desc limit 1""", (uid, today))
    goal = (target or {}).get("workouts_per_week")
    if goal is not None and done >= goal:
        return None, f"weekly goal already met ({done}/{goal})"
    return {"name": _first_name(user), "sessions_this_week": f"{done} of {goal}" if goal else str(done)}, None


CHECKS = {"meal_log_reminder": check_meal, "workout_log_reminder": check_workout}


# ───────────────────────── sending ─────────────────────────

def send_reminder(conn, reminder: dict, force=False) -> dict:
    """Checks and (if needed) sends one reminder. force=True sends even if the check says skip."""
    user = fetch_one(conn, "select * from users where user_id = %s", (reminder["user_id"],))
    kind = reminder["reminder_type"]
    now = datetime.now(ZoneInfo("UTC"))
    variables, skip_reason = CHECKS[kind](conn, user, now)
    if skip_reason and force:
        # Preview with sample values so the template can still be seen.
        variables = {"name": _first_name(user), "last_meal_time": "this morning", "sessions_this_week": "0"}
    result = {"reminder_id": reminder["reminder_id"], "reminder_type": kind, "sent": False,
              "skip_reason": skip_reason}
    if variables and (force or not skip_reason):
        tpl = TEMPLATES[kind]
        text = tpl["text"].format(**{k: variables.get(k, "") for k in ("name", "last_meal_time", "sessions_this_week")})
        last = fetch_one(conn,
            """select channel from messages where user_id = %s and direction = 'inbound'
               order by message_id desc limit 1""", (user["user_id"],))
        channel = last["channel"] if last else "whatsapp"
        in_window = user["last_inbound_message_at"] and user["last_inbound_message_at"] > now - timedelta(hours=24)
        template = fetch_one(conn, "select whatsapp_template_id from whatsapp_templates where template_name = %s",
                             (tpl["template_name"],))
        turn = fetch_one(conn,
            """insert into agent_turns (user_id, trigger_type, reminder_id, detected_intent, outcome,
                   completed_at, total_latency_ms)
               values (%s, %s, %s, 'other', 'success', now(), 0) returning agent_turn_id""",
            (user["user_id"], kind, reminder["reminder_id"]))
        msg = fetch_one(conn,
            """insert into messages (user_id, direction, channel, content_type, message_text, agent_turn_id,
                   send_mode, whatsapp_template_id, delivery_status)
               values (%s, 'outbound', %s, %s, %s, %s, %s, %s, %s) returning message_id""",
            (user["user_id"], channel, "text" if in_window else "template", text, turn["agent_turn_id"],
             "free_form" if in_window else "template",
             None if in_window else template["whatsapp_template_id"],
             "sent" if channel == "web_simulator" else "queued"))
        conn.execute("update reminders set last_sent_at = now() where reminder_id = %s", (reminder["reminder_id"],))
        if channel == "whatsapp":
            from app import whatsapp
            names = fetch_one(conn, "select variable_names from whatsapp_templates where template_name = %s",
                              (tpl["template_name"],))["variable_names"]
            whatsapp.deliver(conn, msg["message_id"], template_params=[variables.get(n, "") for n in names])
        result.update(sent=True, message_id=msg["message_id"], text=text,
                      send_mode="free_form" if in_window else "template")
    if not force:
        nxt = next_run_at(reminder["send_time"], reminder["days_of_week"], user["timezone"], now)
        conn.execute("update reminders set next_run_at = %s where reminder_id = %s", (nxt, reminder["reminder_id"]))
        result["next_run_at"] = nxt.isoformat()
    return result


def run_due_reminders() -> list[dict]:
    """Sends every reminder whose time has come. Safe to run from several processes."""
    results = []
    with get_pool().connection() as conn:
        due = fetch_all(conn,
            """select r.* from reminders r join users u on u.user_id = r.user_id
               where r.is_enabled and r.next_run_at <= now() and r.reminder_type = any(%s)
                 and u.is_active and u.onboarding_status = 'completed'
               order by r.next_run_at limit 50
               for update of r skip locked""", (list(TEMPLATES),))
        for r in due:
            try:
                with conn.transaction():
                    results.append(send_reminder(conn, r))
            except Exception:
                log.exception("reminder %s failed", r["reminder_id"])
    return results


def start_scheduler(interval_sec=60):
    """Background thread that checks for due reminders every minute."""
    def loop():
        while True:
            try:
                sent = [r for r in run_due_reminders() if r["sent"]]
                if sent:
                    log.info("sent %d reminder(s)", len(sent))
            except Exception:
                log.exception("reminder scheduler error")
            time.sleep(interval_sec)
    threading.Thread(target=loop, name="reminder-scheduler", daemon=True).start()
