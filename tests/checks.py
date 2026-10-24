"""Checks run right after each step. Each returns (passed: bool, detail: str)."""
import re
from datetime import timedelta

from app.db import fetch_all, fetch_one
from app.reports import _diet_day_met, _period
from app.timeutil import user_today

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿]")
CODE_WRITTEN_TOOLS = {"log_workout", "log_meal", "log_weight", "update_entry", "delete_entry", "undo_last",
                      "get_weekly_report"}


class Ctx:
    def __init__(self, conn, user_id, scenario_start, step_start, reply, reaction, turn, tools, tz="Asia/Kolkata"):
        self.conn, self.user_id = conn, user_id
        self.scenario_start, self.step_start = scenario_start, step_start
        self.reply, self.reaction, self.turn, self.tools = reply or "", reaction, turn, tools
        self.today = user_today(tz)

    def q1(self, sql, *params):
        return fetch_one(self.conn, sql, params)

    def qa(self, sql, *params):
        return fetch_all(self.conn, sql, params)


def _num(x):
    return float(x) if x is not None else None


def _close(a, b, tol=0.15):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def _kv(parts):
    out = {}
    for p in parts:
        m = re.match(r"([a-z_]+)(=|≈|>=|<=)(.+)", p)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3))
    return out


def _range_ok(value, spec):
    if ".." in spec:
        lo, hi = map(float, spec.split(".."))
        return value is not None and lo <= value <= hi
    return False


# ───────────── data checks ─────────────

def check_data(ctx: Ctx, spec: str):
    words = spec.split()
    kind = words[0].split("=")[0]          # "sessions_today=1" → kind sessions_today

    if kind == "profile":
        field, op, val = re.match(r"([a-z_]+)(=|≈)(.+)", words[1]).groups()
        actual = ctx.q1(f"select {field} from users where user_id = %s", ctx.user_id)[field]
        if val == "null":
            return actual is None, f"{field}={actual}"
        if op == "≈":
            return _close(actual, val, 1.0), f"{field}={actual}"
        return str(actual).lower() == val.lower(), f"{field}={actual}"

    if kind == "targets":
        row = ctx.q1("select * from user_targets where user_id = %s order by start_date desc limit 1", ctx.user_id)
        bad = [f"{k}={row.get(k)} (expected {v})" for k, (op, v) in _kv(words[1:]).items()
               if row is None or row.get(k) is None or abs(float(row[k]) - float(v)) > 0.5]
        return not bad, "; ".join(bad) or "targets ok"
    if kind == "targets_changed_today":
        row = ctx.q1("select * from user_targets where user_id = %s and start_date = %s", ctx.user_id, ctx.today)
        return bool(row and row["daily_calories_kcal"] and row["daily_calories_kcal"] != 2100), f"today's targets: {row and row['daily_calories_kcal']} kcal"

    if kind == "onboarding_completed":
        s = ctx.q1("select onboarding_status from users where user_id = %s", ctx.user_id)["onboarding_status"]
        return s == "completed", f"onboarding_status={s}"
    if kind == "targets_set":
        t = ctx.q1("select daily_calories_kcal, daily_protein_g from user_targets where user_id = %s order by start_date desc limit 1", ctx.user_id)
        return bool(t and t["daily_calories_kcal"] and t["daily_protein_g"]), f"targets={t}"
    if kind == "reminders_created":
        n = ctx.q1("select count(*) n from reminders where user_id = %s and reminder_type in ('meal_log_reminder','workout_log_reminder')", ctx.user_id)["n"]
        return n >= 2, f"{n} reminders"

    if kind == "weight_logged":
        row = ctx.q1("""select weight_kg from weight_logs where user_id = %s and deleted_at is null and created_at >= %s
                        order by weight_log_id desc limit 1""", ctx.user_id, ctx.scenario_start)
        actual = _num(row["weight_kg"]) if row else None
        return _close(actual, words[1], 0.1), f"latest weight={actual}"

    if kind == "nothing_logged":
        n = fetch_one(ctx.conn,
            """select (select count(*) from meal_items where user_id=%(u)s and deleted_at is null and created_at >= %(t)s)
                    + (select count(*) from workout_sets where user_id=%(u)s and deleted_at is null and created_at >= %(t)s)
                    + (select count(*) from weight_logs where user_id=%(u)s and deleted_at is null and created_at >= %(t)s) as n""",
            {"u": ctx.user_id, "t": ctx.scenario_start})["n"]
        return n == 0, f"{n} live rows logged in this scenario"

    if kind == "sets":
        # e.g. "sets Barbell Bench Press=3 weight=60 reps=10,10,8 log_date=yesterday"
        body = spec[len("sets "):]
        m = re.match(r"(.+?)=(\d+)(.*)$", body)
        name, expected_n, rest = m.group(1).strip(), int(m.group(2)), m.group(3).split()
        kv = _kv(rest)
        day = ctx.today - timedelta(days=1) if kv.get("log_date", ("", ""))[1] == "yesterday" else None
        rows = ctx.qa(f"""select set_number, weight_kg, reps, duration_sec, distance_km, log_date from workout_sets
                          where user_id = %s and exercise_name = %s and deleted_at is null and created_at >= %s
                          order by workout_set_id""", ctx.user_id, name, ctx.scenario_start)
        if day:
            rows = [r for r in rows if r["log_date"] == day]
        problems = []
        if len(rows) != expected_n:
            problems.append(f"{len(rows)} sets (expected {expected_n})")
        if "weight" in kv:
            op, v = kv["weight"]
            for r in rows:
                if v == "null":
                    if r["weight_kg"] is not None:
                        problems.append(f"weight {r['weight_kg']} (expected none)")
                elif not _close(r["weight_kg"], v, 0.1 if op == "≈" else 0.01):
                    problems.append(f"weight {r['weight_kg']} (expected {v})")
        if "reps" in kv:
            want = [int(x) for x in kv["reps"][1].split(",")]
            got = [r["reps"] for r in rows]
            if got != want:
                problems.append(f"reps {got} (expected {want})")
        if "duration" in kv:
            for r in rows:
                if r["duration_sec"] != int(kv["duration"][1]):
                    problems.append(f"duration {r['duration_sec']} (expected {kv['duration'][1]})")
        if "distance" in kv:
            for r in rows:
                if not _close(r["distance_km"], kv["distance"][1], 0.05):
                    problems.append(f"distance {r['distance_km']}")
        return not problems, "; ".join(dict.fromkeys(problems)) or f"{len(rows)} sets ok"

    if kind == "session":
        want = words[1].split("=")[1]
        row = ctx.q1("""select workout_name from workout_sessions where user_id = %s and deleted_at is null and created_at >= %s
                        order by workout_session_id desc limit 1""", ctx.user_id, ctx.scenario_start)
        got = row["workout_name"] if row else None
        return got == want, f"workout_name={got}"
    if kind == "sessions_today":
        n = ctx.q1("select count(*) n from workout_sessions where user_id = %s and log_date = %s and deleted_at is null",
                   ctx.user_id, ctx.today)["n"]
        return n == int(spec.split("=")[1]), f"{n} sessions today"
    if kind == "alias":
        m = re.match(r"alias '(.+)'→(.+)", spec)
        row = ctx.q1("select aliases from exercises where exercise_name = %s and user_id is null", m.group(2))
        ok = bool(row and m.group(1).lower() in [a.lower() for a in row["aliases"]])
        return ok, "nickname saved" if ok else "nickname not saved"
    if kind == "pr":
        name = spec[len("pr "):]
        n = ctx.q1("""select count(*) n from workout_sets where user_id = %s and exercise_name = %s and is_personal_record
                      and created_at >= %s""", ctx.user_id, name, ctx.scenario_start)["n"]
        return n > 0, f"{n} PR sets"
    if kind in ("exercise_exists", "exercise_missing"):
        name = spec.split(" ", 1)[1]
        row = ctx.q1("select 1 from exercises where lower(exercise_name) = lower(%s) and (user_id is null or user_id = %s)",
                     name, ctx.user_id)
        return (row is not None) == (kind == "exercise_exists"), "exists" if row else "missing"

    if kind == "meal":
        kv = _kv(words[1:])
        day = ctx.today - timedelta(days=1) if kv.get("log_date", ("", ""))[1] == "yesterday" else None
        meal = ctx.q1("""select * from meals where user_id = %s and deleted_at is null and created_at >= %s
                         order by meal_id desc limit 1""", ctx.user_id, ctx.scenario_start)
        if not meal:
            return False, "no meal logged"
        items = ctx.q1("select count(*) n from meal_items where meal_id = %s and deleted_at is null", meal["meal_id"])["n"]
        kcal, protein = _num(meal["total_calories_kcal"]), _num(meal["total_protein_g"])
        problems = []
        for key, (op, v) in kv.items():
            if key == "items" and ((op == "=" and items != int(v)) or (op == ">=" and items < int(v))):
                problems.append(f"{items} items (expected {op}{v})")
            if key == "type" and meal["meal_type"] != v:
                problems.append(f"type {meal['meal_type']} (expected {v})")
            if key == "kcal" and not _range_ok(kcal, v):
                problems.append(f"{kcal:.0f} kcal (expected {v})")
            if key == "protein" and ((op == ">=" and protein < float(v)) or (op == "=" and not _range_ok(protein, v))):
                problems.append(f"protein {protein:.0f} g (expected {op}{v})")
            if key == "log_date" and meal["log_date"] != day:
                problems.append(f"log_date {meal['log_date']} (expected {day})")
        detail = f"{items} items, {kcal:.0f} kcal, {protein:.0f} g protein, {meal['meal_type']}"
        return not problems, ("; ".join(problems) + f" — {detail}") if problems else detail
    if kind == "meals_today":
        n = ctx.q1("select count(*) n from meals where user_id = %s and log_date = %s and deleted_at is null",
                   ctx.user_id, ctx.today)["n"]
        return n == int(spec.split("=")[1]), f"{n} meals today"
    if kind == "meal_item":
        food, field_val = words[1], words[2]
        field, val = field_val.split("=")
        row = ctx.q1(f"""select {field} from meal_items where user_id = %s and food_name ilike %s and deleted_at is null
                         and created_at >= %s order by meal_item_id desc limit 1""",
                     ctx.user_id, f"%{food}%", ctx.scenario_start)
        got = _num(row[field]) if row else None
        return _close(got, val, 0.01), f"{food} {field}={got}"
    if kind == "meal_item_deleted":
        food = words[1]
        live = ctx.q1("""select count(*) n from meal_items where user_id = %s and food_name ilike %s and deleted_at is null
                         and created_at >= %s""", ctx.user_id, f"%{food}%", ctx.scenario_start)["n"]
        return live == 0, f"{live} live '{food}' items"
    if kind == "correction_recorded":
        n = ctx.q1("select count(*) n from corrections where user_id = %s and created_at >= %s", ctx.user_id, ctx.step_start)["n"]
        return n > 0, f"{n} corrections recorded"

    if kind == "report_numbers_match":
        arg = words[1]
        weeks_ago = int(arg.split("=")[1]) if arg.startswith("weeks_ago") else None
        start, end = _period("last_week" if arg == "last_week" else "last_7_days", ctx.today, weeks_ago)
        met, sessions = _expected_diet_and_sessions(ctx, start, end)
        want = [f"target hit on {met} of 7", f"{sessions} of 4 sessions"]
        missing = [w for w in want if w not in ctx.reply]
        return not missing, f"expected '{want[0]}' and '{want[1]}' ({start}–{end})" + (f"; missing {missing}" if missing else "")

    if kind == "stat":
        return _check_stat(ctx, words[1])

    if kind in ("reminder_sent", "reminder_skipped", "reminder_template"):
        res = ctx.turn or {}
        if kind == "reminder_sent":
            return bool(res.get("sent")), res.get("skip_reason") or "sent"
        if kind == "reminder_skipped":
            return not res.get("sent"), res.get("skip_reason") or "was sent"
        return res.get("send_mode") == "template", f"send_mode={res.get('send_mode')}"

    if kind == "reaction_only":
        return bool(ctx.reaction) and not ctx.reply.strip(), f"reaction={ctx.reaction}, text={'yes' if ctx.reply.strip() else 'no'}"
    if kind == "history_intact":
        n = ctx.q1("select count(*) n from workout_sets where user_id = %s and deleted_at is null and created_at < %s",
                   ctx.user_id, ctx.scenario_start)["n"]
        return n >= 200, f"{n} seeded sets still live"
    return False, f"unknown check '{spec}'"


def _expected_diet_and_sessions(ctx, start, end):
    t = ctx.q1("select daily_calories_kcal, daily_protein_g from user_targets where user_id = %s order by start_date desc limit 1", ctx.user_id)
    goal = ctx.q1("select goal from users where user_id = %s", ctx.user_id)["goal"]
    days = ctx.qa("""select log_date, sum(total_calories_kcal) k, sum(total_protein_g) p from meals where user_id = %s
                     and log_date between %s and %s and deleted_at is null group by log_date""", ctx.user_id, start, end)
    met = sum(1 for d in days if _diet_day_met(float(d["k"]), float(d["p"]), t, goal))
    sessions = ctx.q1("""select count(*) n from workout_sessions where user_id = %s and log_date between %s and %s
                         and deleted_at is null""", ctx.user_id, start, end)["n"]
    return met, sessions


def _check_stat(ctx, name):
    today = ctx.today
    if name == "diet_days_met_30d":
        met, _ = _expected_diet_and_sessions(ctx, today - timedelta(days=29), today)
        return bool(re.search(rf"\b{met}\b", ctx.reply)), f"expected {met} days in reply"
    if name == "sessions_this_month":
        _, n = _expected_diet_and_sessions(ctx, today.replace(day=1), today)
        return bool(re.search(rf"\b{n}\b", ctx.reply)), f"expected {n} sessions in reply"
    if name == "bench_trend":
        best = ctx.q1("""select max(weight_kg) w from workout_sets where user_id = %s and exercise_name = 'Barbell Bench Press'
                         and log_date >= %s and deleted_at is null""", ctx.user_id, today - timedelta(days=29))["w"]
        w = f"{float(best):g}"
        return w in ctx.reply, f"expected best {w} kg in reply"
    if name == "avg_protein_14d":
        rows = ctx.qa("""select sum(total_protein_g) p from meals where user_id = %s and log_date between %s and %s
                         and deleted_at is null group by log_date""", ctx.user_id, today - timedelta(days=13), today)
        avg = sum(float(r["p"]) for r in rows) / len(rows) if rows else 0
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", ctx.reply)]
        ok = any(abs(n - avg) <= 2 for n in nums)
        return ok, f"expected ~{avg:.0f} g in reply"
    return False, f"unknown stat {name}"


# ───────────── reply, tools, intent, style ─────────────

def check_tools(step, tools_called):
    names = [t["tool"] for t in tools_called]
    problems = [f"missing {t}" for t in step.get("tools", []) if t not in names]
    problems += [f"called {t}" for t in step.get("no_tools", []) if t in names]
    return not problems, "; ".join(problems) or ", ".join(names) or "no tools"


def check_reply(step, reply):
    problems = [f"no match for /{r}/" for r in step.get("reply_has", []) if not re.search(r, reply or "", re.I)]
    problems += [f"matched forbidden /{r}/" for r in step.get("reply_not", []) if re.search(r, reply or "", re.I)]
    return not problems, "; ".join(problems) or "ok"


def check_style(reply, tools_called):
    reply = reply or ""
    problems = []
    if "**" in reply:
        problems.append("markdown ** used")
    if re.search(r"^#+ ", reply, re.M):
        problems.append("markdown heading used")
    code_written = any(t["tool"] in CODE_WRITTEN_TOOLS and t["status"] in
                       ("logged", "updated", "deleted", "undone", "report_ready") for t in tools_called)
    if not code_written:
        n = len(EMOJI_RE.findall(reply))
        if n > 1:
            problems.append(f"{n} emojis")
        if reply.count("?") > 1:
            problems.append(f"{reply.count('?')} questions")
    return not problems, "; ".join(problems) or "ok"
