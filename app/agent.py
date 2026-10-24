"""The Gemini agent: builds a small context, runs the tool loop, records every LLM call.

Token budget per call:
  - static (cacheable): instructions from prompts/*.md + tool definitions
  - dynamic (small):    a [Context] block in front of the user's message + last few messages
When every tool call succeeds, the reply is written by code (app/replies.py), so a
logging message costs ONE Gemini call. A second call happens only when Gemini has to
respond to something (an unknown exercise, an error, get_logs results, onboarding).
"""
import inspect
import json
import re
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app import replies
from app import tools as T
from app.profile import update_profile, update_targets
from app.reports import PROGRESS_METRICS, PROGRESS_PERIODS, get_progress, get_weekly_report
from app.config import (BOT_NAME, GEMINI_API_KEY, GEMINI_INPUT_USD_PER_M, GEMINI_MODEL,
                        GEMINI_OUTPUT_USD_PER_M)
from app.db import fetch_all
from app.onboarding import STEP_QUESTIONS, missing_steps, save_profile
from app.timeutil import user_now

MAX_LLM_CALLS = 5
HISTORY_MESSAGES = 6
HISTORY_BOT_CHARS = 300  # older bot replies are trimmed; the context block has the facts
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY,
                               http_options=types.HttpOptions(timeout=45_000))  # ms; fail fast instead of hanging
    return _client


def jsonable(obj):
    return json.loads(json.dumps(obj, default=lambda o: float(o) if isinstance(o, Decimal) else str(o)))


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text().replace("{{BOT_NAME}}", BOT_NAME)


# ───────────────────────── tool definitions (what Gemini sees) ─────────────────────────

NUTRITION = {k: {"type": "number"} for k in ("calories_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g")}

MAIN_TOOLS = {
    "log_meal": {
        "description": "Save a meal, split into items with your nutrition estimate for each.",
        "parameters": {"type": "object", "properties": {
            "meal_description": {"type": "string", "description": "The meal in the user's words."},
            "meal_type": {"type": "string", "enum": T.MEAL_TYPES},
            "log_date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "food_name": {"type": "string"},
                "quantity": {"type": "number"},
                "unit": {"type": "string", "enum": T.FOOD_UNITS},
                "estimated_grams": {"type": "number"},
                **NUTRITION},
                "required": ["food_name", "quantity", "unit", *NUTRITION]}},
        }, "required": ["meal_description", "items"]},
    },
    "log_workout": {
        "description": "Save exercises and their sets. Joins today's open session automatically.",
        "parameters": {"type": "object", "properties": {
            "workout_name": {"type": "string", "enum": T.WORKOUT_NAMES},
            "exercises": {"type": "array", "items": {"type": "object", "properties": {
                "exercise_name": {"type": "string", "description": "The user's own words."},
                "sets": {"type": "array", "items": {"type": "object", "properties": {
                    "weight": {"type": "number", "description": "Per dumbbell for dumbbells; omit for bodyweight."},
                    "weight_unit": {"type": "string", "enum": ["kg", "lb"]},
                    "reps": {"type": "integer"},
                    "duration_sec": {"type": "integer"},
                    "distance_km": {"type": "number"}}}}},
                "required": ["exercise_name", "sets"]}},
            "notes": {"type": "string"},
            "duration_min": {"type": "integer"},
            "log_date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
            "start_new_session": {"type": "boolean", "description": "Only if the user says it's a separate workout."},
        }, "required": ["workout_name", "exercises"]},
    },
    "add_exercise": {
        "description": "Add a new exercise, only after the user agreed.",
        "parameters": {"type": "object", "properties": {
            "exercise_name": {"type": "string", "description": "Clear name in Title Case, e.g. 'Svend Press'."},
            "muscle_group": {"type": "string", "enum": T.MUSCLE_GROUPS},
            "exercise_type": {"type": "string", "enum": T.EXERCISE_TYPES},
            "force_new": {"type": "boolean", "description": "Only after the user confirmed it differs from a similar existing exercise."},
        }, "required": ["exercise_name", "muscle_group", "exercise_type"]},
    },
    "log_weight": {
        "description": "Save a body-weight weigh-in.",
        "parameters": {"type": "object", "properties": {
            "weight": {"type": "number"},
            "unit": {"type": "string", "enum": ["kg", "lb"]},
            "log_date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
        }, "required": ["weight", "unit"]},
    },
    "update_entry": {
        "description": "Correct a logged entry. Include only the fields that change.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string", "enum": sorted(T.EDITABLE)},
            "record_id": {"type": "integer"},
            "entry_name": {"type": "string", "description": "What the user calls the item, e.g. 'idli', 'bench', 'rice'. Checked against the record."},
            "reason": {"type": "string", "enum": T.CORRECTION_REASONS,
                       "description": "wrong_parse: you misread; wrong_estimate: nutrition was off; user_changed_mind."},
            "changes": {"type": "object", "properties": {
                "weight": {"type": "number"}, "weight_unit": {"type": "string", "enum": ["kg", "lb"]},
                "reps": {"type": "integer"}, "duration_sec": {"type": "integer"},
                "distance_km": {"type": "number"}, "exercise_name": {"type": "string"},
                "food_name": {"type": "string"}, "quantity": {"type": "number"},
                "unit": {"type": "string", "enum": T.FOOD_UNITS}, **NUTRITION}},
        }, "required": ["table", "record_id", "entry_name", "changes", "reason"]},
    },
    "delete_entry": {
        "description": "Remove one entry (meal, food item, workout, set or weigh-in).",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string", "enum": sorted(T.ID_COLUMN)},
            "record_id": {"type": "integer"},
            "entry_name": {"type": "string", "description": "What the user calls the item, e.g. 'idli', 'bench', 'rice'. Checked against the record."},
            "reason": {"type": "string", "enum": T.CORRECTION_REASONS},
        }, "required": ["table", "record_id", "entry_name", "reason"]},
    },
    "undo_last": {
        "description": "Undo everything saved from the user's last logging message.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_logs": {
        "description": "List a day's entries with IDs. Only needed to edit something not under 'Last logged'.",
        "parameters": {"type": "object", "properties": {
            "log_date": {"type": "string", "description": "YYYY-MM-DD; omit for today."}}},
    },
    "get_weekly_report": {
        "description": "Build the user's weekly progress report (diet, sessions, volume, key lifts, weight).",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["last_7_days", "last_week"],
                       "description": "last_7_days unless they ask for last week (previous Mon–Sun)."},
            "weeks_ago": {"type": "integer", "description": "For an older Mon–Sun week: 1 = last week, 2 = two weeks back…"}}},
    },
    "update_targets": {
        "description": "Change the user's daily targets (calories, macros, fibre, workouts per week, target weight). "
                       "Pass only what they want changed; if they give macros but no calories, calories follow the macros.",
        "parameters": {"type": "object", "properties": {
            "daily_calories_kcal": {"type": "integer"}, "daily_protein_g": {"type": "integer"},
            "daily_carbs_g": {"type": "integer"}, "daily_fat_g": {"type": "integer"},
            "daily_fiber_g": {"type": "integer"}, "workouts_per_week": {"type": "integer"},
            "target_weight": {"type": "number"}, "target_weight_unit": {"type": "string", "enum": ["kg", "lb"]},
        }},
    },
    "update_profile": {
        "description": "Change goal, diet, experience level, units (kg/lb) or name. A new goal also recalculates "
                       "daily targets unless the user gave their own numbers (then recalculate_targets=false and "
                       "also call update_targets).",
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "enum": ["fat_loss", "muscle_gain", "recomposition", "strength", "general_fitness"]},
            "diet_preference": {"type": "string", "enum": ["veg", "eggetarian", "non_veg", "vegan"]},
            "experience_level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
            "unit_preference": {"type": "string", "enum": ["kg", "lb"]},
            "name": {"type": "string"},
            "recalculate_targets": {"type": "boolean"},
        }},
    },
    "get_progress": {
        "description": "Exact stats for a time range: diet days met, workout count, one lift's progress, "
                       "average calories/macros, or weight trend.",
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string", "enum": PROGRESS_METRICS},
            "period": {"type": "string", "enum": PROGRESS_PERIODS,
                       "description": "Default last_n_days. 'last month', 'last 1 month', 'past month' = last_n_days with "
                                      "days 30; 'last 2 weeks' = days 14. Use the calendar options only when the user names "
                                      "one: 'this month' = this_calendar_month, 'in September' / 'the previous month' = "
                                      "previous_calendar_month."},
            "days": {"type": "integer", "description": "For last_n_days, e.g. 14 or 30."},
            "exercise_name": {"type": "string", "description": "For lift_progress, in the user's words."},
        }, "required": ["metric"]},
    },
    "react_to_message": {
        "description": "React to the user's message with one emoji. Use sparingly.",
        "parameters": {"type": "object", "properties": {
            "emoji": {"type": "string", "enum": ["👍", "🔥", "💪", "🙏", "😂", "❤️", "😮"]}},
            "required": ["emoji"]},
    },
}

ONBOARDING_TOOLS = {
    "save_profile": {
        "description": "Save any onboarding details the user gave. Include every field you can pick out.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "gender": {"type": "string", "enum": ["male", "female", "other"]},
            "height_cm": {"type": "number"},
            "weight": {"type": "number"},
            "weight_unit": {"type": "string", "enum": ["kg", "lb"]},
            "goal": {"type": "string", "enum": ["fat_loss", "muscle_gain", "recomposition", "strength", "general_fitness"]},
            "experience_level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
            "diet_preference": {"type": "string", "enum": ["veg", "eggetarian", "non_veg", "vegan"]},
            "workouts_per_week": {"type": "integer"},
            "workout_time": {"type": "string", "description": "24-hour HH:MM."},
        }},
    },
}

TOOL_FUNCTIONS = {
    "log_meal": T.log_meal, "log_workout": T.log_workout, "add_exercise": T.add_exercise,
    "log_weight": T.log_weight, "update_entry": T.update_entry, "delete_entry": T.delete_entry,
    "undo_last": T.undo_last, "get_logs": T.get_logs, "save_profile": save_profile,
    "get_weekly_report": get_weekly_report, "get_progress": get_progress,
    "update_targets": update_targets, "update_profile": update_profile,
}

INTENT_BY_TOOL = {"log_meal": "log_meal", "log_workout": "log_workout", "add_exercise": "log_workout",
                  "log_weight": "log_weight", "update_entry": "edit_log", "delete_entry": "edit_log",
                  "undo_last": "edit_log", "get_logs": "edit_log", "save_profile": "onboarding",
                  "get_weekly_report": "get_report", "get_progress": "get_report",
                  "update_targets": "update_settings", "update_profile": "update_settings"}

# The app sends these tools' output itself, then Gemini adds a short comment (one extra call).
NEEDS_COMMENT = {"get_weekly_report"}


def to_gemini_tools(declarations):
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=name, description=d["description"], parameters_json_schema=d["parameters"])
        for name, d in declarations.items()])]


# Built once: identical on every call, so Gemini's implicit cache can reuse them.
MAIN_SYSTEM = load_prompt("main")
ONBOARDING_SYSTEM = load_prompt("onboarding")
MAIN_GEMINI_TOOLS = to_gemini_tools(MAIN_TOOLS)
ONBOARDING_GEMINI_TOOLS = to_gemini_tools(ONBOARDING_TOOLS)


# ───────────────────────── the small dynamic context ─────────────────────────

def main_context(conn, user, state, pending) -> str:
    now = user_now(user["timezone"])
    today = T.user_today(user["timezone"])
    lines = [
        "[Context]",
        f"Now: {now:%A %d %b %Y, %H:%M} · today's log_date {today} · yesterday {today - timedelta(days=1)}",
        f"User: {user.get('name')}, {user.get('age')}, {user.get('gender')}, goal {user.get('goal')}, "
        f"{user.get('experience_level')}, diet {user.get('diet_preference')}, weights in {user.get('unit_preference')}",
        f"Today so far: {T.day_summary_line(conn, user['user_id'], today)}",
    ]
    last = T.recent_logged_lines(conn, user["user_id"], T.user_today(user["timezone"]))
    if last:
        lines.append("Last logged today (newest first, for corrections):")
        lines += [f"- {l}" for l in last]
    if pending:
        lines.append(f"Waiting on user: {json.dumps(jsonable(pending), ensure_ascii=False)}")
    lines.append("[/Context]")
    return "\n".join(lines)


def onboarding_context(conn, user) -> str:
    missing = missing_steps(conn, user)
    known = {k: user.get(k) for k in ("name", "age", "gender", "height_cm", "goal",
                                       "experience_level", "diet_preference") if user.get(k) is not None}
    return "\n".join([
        "[Context]",
        "First contact: yes" if not user.get("name") and user["onboarding_status"] == "not_started" else "First contact: no",
        f"Known so far: {json.dumps(jsonable(known))}",
        f"Still missing: {', '.join(missing)}",
        f"Next to ask: {STEP_QUESTIONS[missing[0]] if missing else 'nothing'}",
        "[/Context]",
    ])


def _history(conn, user_id, before_message_id):
    rows = fetch_all(conn,
        """select direction, message_text from messages
           where user_id = %s and message_id < %s and content_type = 'text' and message_text is not null
           order by message_id desc limit %s""", (user_id, before_message_id, HISTORY_MESSAGES))
    contents = []
    for r in reversed(rows):
        text = r["message_text"]
        if r["direction"] == "outbound" and len(text) > HISTORY_BOT_CHARS:
            text = text[:HISTORY_BOT_CHARS] + "…"
        contents.append(types.Content(role="user" if r["direction"] == "inbound" else "model",
                                      parts=[types.Part(text=text)]))
    while contents and contents[0].role != "user":
        contents.pop(0)
    return contents


# ───────────────────────── Gemini call + tracking ─────────────────────────

RETRY_STATS = {"count": 0}  # rate-limit retries since last reset (read by the evaluation runner)


class ModelBusy(Exception):
    """Gemini can't answer right now (rate limit or overload); the user gets a 'try again' reply."""


class RateLimited(ModelBusy):
    """Gemini quota hit and the wait is too long to retry inside one chat reply."""


SERVER_RETRY_WAITS = [2, 5]  # seconds; Gemini 5xx ("model experiencing high demand") is usually brief


def _generate(contents, config):
    """Calls Gemini. Retries short rate limits (429) and brief overloads (5xx); otherwise raises ModelBusy."""
    rate_retries = server_retries = 0
    while True:
        try:
            return client().models.generate_content(model=GEMINI_MODEL, contents=contents, config=config)
        except genai_errors.ServerError as e:
            if server_retries >= len(SERVER_RETRY_WAITS):
                raise ModelBusy(f"Gemini unavailable after retries: {e.code} {str(e)[:120]}") from e
            time.sleep(SERVER_RETRY_WAITS[server_retries])
            server_retries += 1
        except genai_errors.ClientError as e:
            if e.code != 429:
                raise
            m = re.search(r"retry in ([0-9.]+)s", str(e))
            wait = float(m.group(1)) if m else 30
            if rate_retries >= 2 or wait > 20:
                raise RateLimited(f"Gemini rate limit (retry in {wait:.0f}s)") from e
            time.sleep(wait + 0.5)
            rate_retries += 1
        RETRY_STATS["count"] += 1


def _thinking_config():
    if GEMINI_MODEL.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=512)
    return types.ThinkingConfig(thinking_level="low")


def _record_llm_call(conn, agent_turn_id, user_id, call_number, resp, latency_ms):
    u = resp.usage_metadata
    usage = {"input": (u.prompt_token_count or 0) if u else 0,
             "output": (u.candidates_token_count or 0) if u else 0,
             "thinking": (u.thoughts_token_count or 0) if u else 0,
             "cached": (u.cached_content_token_count or 0) if u else 0}
    finish = None
    if resp.candidates and resp.candidates[0].finish_reason:
        fr = resp.candidates[0].finish_reason
        finish = str(getattr(fr, "value", fr))
    conn.execute(
        """insert into llm_calls (agent_turn_id, user_id, call_number, model_name, input_tokens,
               output_tokens, thinking_tokens, cached_tokens, latency_ms, finish_reason)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (agent_turn_id, user_id, call_number, GEMINI_MODEL, usage["input"], usage["output"],
         usage["thinking"], usage["cached"], latency_ms, finish))
    return usage


def _text_parts(resp):
    if not resp.candidates or not resp.candidates[0].content or not resp.candidates[0].content.parts:
        return []
    return [p.text.strip() for p in resp.candidates[0].content.parts
            if getattr(p, "text", None) and not getattr(p, "thought", False) and p.text.strip()]


def _clean_keys(value):
    """Smaller models occasionally emit keys like '"workout_name' — strip stray quotes/spaces."""
    if isinstance(value, dict):
        return {str(k).strip().strip('"\'').strip(): _clean_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_keys(v) for v in value]
    return value


def _run_tool(conn, ctx, name, args, allowed):
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None or name not in allowed:
        return {"status": "error", "error": f"Unknown tool {name}"}
    accepted = inspect.signature(fn).parameters
    args = _clean_keys(args)
    args = {k: v for k, v in args.items() if k in accepted}  # models sometimes add extra fields
    try:
        with conn.transaction():  # savepoint: one failed tool doesn't undo the others
            return fn(ctx, **args)
    except T.ToolError as e:
        return {"status": "error", "error": str(e)}
    except TypeError as e:
        return {"status": "error", "error": f"Bad arguments: {e}"}
    except psycopg.Error as e:
        return {"status": "error", "error": f"Database rejected it: {str(e).splitlines()[0]}"}


# ───────────────────────── the loop ─────────────────────────

def run_agent(conn, user, inbound_message_id, text, agent_turn_id):
    """Returns (reply_text or None, reaction_emoji or None, stats). The caller owns the transaction."""
    onboarding = user["onboarding_status"] != "completed"
    state = T.get_state(conn, user["user_id"])
    pending = state.get("pending_action")

    if onboarding:
        system, gemini_tools, allowed = ONBOARDING_SYSTEM, ONBOARDING_GEMINI_TOOLS, ONBOARDING_TOOLS
        context = onboarding_context(conn, user)
    else:
        system, gemini_tools, allowed = MAIN_SYSTEM, MAIN_GEMINI_TOOLS, MAIN_TOOLS
        context = main_context(conn, user, state, pending)

    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=gemini_tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=_thinking_config(),
    )
    contents = _history(conn, user["user_id"], inbound_message_id)
    contents.append(types.Content(role="user", parts=[types.Part(text=context), types.Part(text=text)]))

    prev = fetch_all(conn, """select message_text from messages where user_id = %s and direction = 'inbound'
                              and message_id < %s order by message_id desc limit 2""",
                     (user["user_id"], inbound_message_id))
    ctx = T.ToolContext(conn=conn, user=user, message_id=inbound_message_id, pending_action=pending,
                        user_text=" ".join([text, *[p["message_text"] or "" for p in prev]]))
    stats = {"llm_call_count": 0, "input": 0, "output": 0, "thinking": 0, "cached": 0, "tools_called": []}
    confirmations, model_texts, reaction = [], [], None
    finished, awaiting_reply = False, onboarding
    asks_question = "?" in text

    for call_number in range(1, MAX_LLM_CALLS + 1):
        t0 = time.monotonic()
        resp = _generate(contents, config)
        usage = _record_llm_call(conn, agent_turn_id, user["user_id"], call_number, resp,
                                 int((time.monotonic() - t0) * 1000))
        stats["llm_call_count"] += 1
        for k in ("input", "output", "thinking", "cached"):
            stats[k] += usage[k]

        texts = _text_parts(resp)
        model_texts += texts
        calls = resp.function_calls or []
        if not calls:
            # A reaction alone is fine only for a plain "thanks"/"ok". If a tool result is waiting for a
            # reply (or there's nothing at all to send), ask for the text once more.
            if not texts and (awaiting_reply or not reaction) and call_number < MAX_LLM_CALLS:
                if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                    contents.append(resp.candidates[0].content)
                contents.append(types.Content(role="user", parts=[types.Part(
                    text="[App] Your reply was empty. Write the text message for the user now.")]))
                stats["empty_reply_retries"] = stats.get("empty_reply_retries", 0) + 1
                continue
            finished = True
            break

        contents.append(resp.candidates[0].content)
        response_parts, gemini_must_reply = [], False
        for fc in calls:
            args = dict(fc.args or {})
            if fc.name == "react_to_message":
                reaction = args.get("emoji")
                result = {"status": "reacted"}
            else:
                result = jsonable(_run_tool(conn, ctx, fc.name, args, allowed))
                confirmation = None if onboarding else replies.for_tool(fc.name, result, ctx.user)
                if confirmation:
                    confirmations.append(confirmation)
                if not confirmation or fc.name in NEEDS_COMMENT or result.get("needs_reply"):
                    gemini_must_reply = True
            stats["tools_called"].append({"tool": fc.name, "args": jsonable(args), "status": result.get("status")})
            response_parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
        only_reacted = all(fc.name == "react_to_message" for fc in calls)
        if awaiting_reply and only_reacted and not texts:
            # A reaction can't stand in for the reply we're still waiting on.
            gemini_must_reply = True
            response_parts.append(types.Part(text="[App] Reaction added. Now write the text reply for the user."))
        if not gemini_must_reply and asks_question and not texts and confirmations:
            # The log is confirmed by code, but the user also asked something: get the answer.
            gemini_must_reply = True
            response_parts.append(types.Part(text="[App] The confirmation above was already sent to the user. "
                                                   "Now answer only their question, in 1–3 short lines."))
        contents.append(types.Content(role="user", parts=response_parts))
        awaiting_reply = gemini_must_reply

        if not gemini_must_reply:
            finished = True  # every tool succeeded: code writes the reply, no second call
            break

    reply_parts = confirmations + model_texts
    if not finished:
        stats["error"] = "no final reply within call limit"
        reply_parts.append("Sorry, I got a bit stuck on that one. Could you say it again, a little differently?")
    reply = "\n\n".join(reply_parts).strip() or None
    if not reply and not reaction:
        reply = "Sorry, I didn't catch that. Could you say it again?"

    # Pending questions live for one turn: keep a new one if a tool set it, otherwise clear.
    T.update_state(conn, user["user_id"], pending_action=ctx.new_pending_action)

    names = [t["tool"] for t in stats["tools_called"] if t["tool"] != "react_to_message"]
    stats["intent"] = ("onboarding" if onboarding else INTENT_BY_TOOL.get(names[0], "other") if names
                       else "small_talk" if reaction and not model_texts else "question")
    stats["outcome"] = ("error" if stats.get("error") else
                        "needs_clarification" if ctx.asked_clarification else "success")
    billable_input = stats["input"] - stats["cached"] + stats["cached"] * 0.1  # cached input is billed at ~10%
    stats["cost_usd"] = round((billable_input * GEMINI_INPUT_USD_PER_M
                               + (stats["output"] + stats["thinking"]) * GEMINI_OUTPUT_USD_PER_M) / 1_000_000, 6)
    stats["reaction"] = reaction
    return reply, reaction, stats
