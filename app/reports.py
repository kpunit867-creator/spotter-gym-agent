"""Weekly report: every number is calculated here; Gemini only adds a 1–2 line takeaway."""
from datetime import date, timedelta

from app.db import fetch_all, fetch_one
from app.tools import ToolContext, current_targets, num
from app.timeutil import user_today

# Lifts shown in the "Key lifts" section (exact names from the exercise dictionary).
KEY_LIFTS = [
    "Barbell Bench Press",
    "Dumbbell Bench Press",
    "Incline Dumbbell Press",
    "Barbell Back Squat",
    "Front Squat",
    "Lat Pulldown",
    "Deadlift",
    "Leg Press",
]
DIET_DAYS_TO_PASS = 5  # diet counts as "met" for the week if targets were hit on at least 5 of 7 days


def _period(kind: str, today: date, weeks_ago: int | None = None):
    """last_7_days → the 7 days ending today; otherwise a Monday–Sunday week, N weeks back."""
    if kind == "last_week" and not weeks_ago:
        weeks_ago = 1
    if weeks_ago:
        start = today - timedelta(days=today.weekday() + 7 * int(weeks_ago))
    else:
        start = today - timedelta(days=6)
    return start, start + timedelta(days=6)


def _diet_day_met(kcal, protein, target, goal):
    """Calories on the right side of target for the goal, and protein at least 90% of target."""
    t_kcal, t_protein = target.get("daily_calories_kcal"), target.get("daily_protein_g")
    if not t_kcal or not t_protein:
        return None
    if goal == "fat_loss":
        kcal_ok = kcal <= t_kcal * 1.05
    elif goal in ("muscle_gain", "strength"):
        kcal_ok = kcal >= t_kcal * 0.95
    else:
        kcal_ok = abs(kcal - t_kcal) <= t_kcal * 0.10
    return kcal_ok and protein >= t_protein * 0.9


def _stats(conn, user, start: date, end: date) -> dict:
    uid = user["user_id"]
    days = fetch_all(conn,
        """select log_date, sum(total_calories_kcal) kcal, sum(total_protein_g) protein
           from meals where user_id = %s and log_date between %s and %s and deleted_at is null
           group by log_date""", (uid, start, end))
    target = current_targets(conn, uid, end) or {}
    met_days = sum(1 for d in days if _diet_day_met(float(d["kcal"]), float(d["protein"]), target, user.get("goal")))

    sessions = fetch_one(conn,
        """select count(*) as n, count(distinct log_date) as days from workout_sessions w
           where user_id = %s and log_date between %s and %s and deleted_at is null
             and exists (select 1 from workout_sets s where s.workout_session_id = w.workout_session_id
                         and s.deleted_at is null)""", (uid, start, end))
    training = fetch_one(conn,
        """select coalesce(sum(s.weight_kg * s.reps), 0) as volume, count(*) as sets,
                  count(*) filter (where s.is_personal_record) as prs
           from workout_sets s where s.user_id = %s and s.log_date between %s and %s
             and s.deleted_at is null""", (uid, start, end))
    by_muscle = fetch_all(conn,
        """select e.muscle_group, count(*) as sets from workout_sets s
           join exercises e on e.exercise_id = s.exercise_id
           where s.user_id = %s and s.log_date between %s and %s and s.deleted_at is null
           group by e.muscle_group order by count(*) desc""", (uid, start, end))
    # Best set per key lift: heaviest weight, then most reps at that weight.
    best = fetch_all(conn,
        """select distinct on (exercise_name) exercise_name, weight_kg, reps from workout_sets
           where user_id = %s and log_date between %s and %s and deleted_at is null
             and exercise_name = any(%s) and weight_kg is not null and reps is not null
           order by exercise_name, weight_kg desc, reps desc""", (uid, start, end, KEY_LIFTS))
    weight = fetch_one(conn,
        """select avg(weight_kg) as avg_kg, count(*) as n from weight_logs
           where user_id = %s and log_date between %s and %s and deleted_at is null""", (uid, start, end))
    return {
        "start": str(start), "end": str(end),
        "diet": {
            "days_logged": len(days), "days_target_met": met_days,
            "avg_calories_kcal": round(sum(float(d["kcal"]) for d in days) / len(days)) if days else None,
            "avg_protein_g": round(sum(float(d["protein"]) for d in days) / len(days)) if days else None,
            "target_calories_kcal": target.get("daily_calories_kcal"),
            "target_protein_g": target.get("daily_protein_g"),
        },
        "sessions": {"done": sessions["n"], "training_days": sessions["days"],
                     "target": target.get("workouts_per_week")},
        "training": {"volume_kg": round(float(training["volume"])), "sets": training["sets"],
                     "personal_records": training["prs"],
                     "sets_by_muscle": {m["muscle_group"]: m["sets"] for m in by_muscle}},
        "key_lifts": {b["exercise_name"]: {"weight_kg": num(b["weight_kg"]), "reps": b["reps"]} for b in best},
        "body_weight_avg_kg": num(weight["avg_kg"]) if weight["n"] else None,
    }


def get_weekly_report(ctx: ToolContext, period="last_7_days", weeks_ago=None):
    today = user_today(ctx.user["timezone"])
    if weeks_ago is not None and int(weeks_ago) < 0:
        weeks_ago = None
    if weeks_ago:
        period = "last_week" if int(weeks_ago) == 1 else "weeks_ago"
    start, end = _period(period, today, weeks_ago)
    this = _stats(ctx.conn, ctx.user, start, end)
    prev = _stats(ctx.conn, ctx.user, start - timedelta(days=7), end - timedelta(days=7))

    lifts = []
    for name in KEY_LIFTS:
        now, before = this["key_lifts"].get(name), prev["key_lifts"].get(name)
        if not now and not before:
            continue
        if now and before:
            if now["weight_kg"] != before["weight_kg"]:
                trend = "up" if now["weight_kg"] > before["weight_kg"] else "down"
            else:
                trend = "up" if now["reps"] > before["reps"] else "down" if now["reps"] < before["reps"] else "same"
        else:
            trend = "new" if now else "not_done"
        lifts.append({"exercise_name": name, "this_week": now, "last_week": before, "trend": trend})

    vol_now, vol_prev = this["training"]["volume_kg"], prev["training"]["volume_kg"]
    w_now, w_prev = this["body_weight_avg_kg"], prev["body_weight_avg_kg"]
    diet, sessions = this["diet"], this["sessions"]
    return {
        "status": "report_ready",
        "period": period, "weeks_ago": int(weeks_ago) if weeks_ago else None,
        "start": this["start"], "end": this["end"],
        "diet": {**diet, "goal_met": diet["days_target_met"] >= DIET_DAYS_TO_PASS,
                 "days_needed": DIET_DAYS_TO_PASS},
        "sessions": {**sessions, "goal_met": sessions["target"] is not None and sessions["done"] >= sessions["target"],
                     "last_week": prev["sessions"]["done"]},
        "volume": {"this_week_kg": vol_now, "last_week_kg": vol_prev,
                   "change_pct": round((vol_now - vol_prev) / vol_prev * 100) if vol_prev else None},
        "sets_by_muscle": this["training"]["sets_by_muscle"],
        "personal_records": this["training"]["personal_records"],
        "key_lifts": lifts,
        "body_weight": {"avg_kg": w_now, "last_week_avg_kg": w_prev,
                        "change_kg": round(w_now - w_prev, 1) if w_now and w_prev else None},
        "display_unit": ctx.user.get("unit_preference") or "kg",
        "goal": ctx.user.get("goal"),
        "instruction": "The app sends the report itself. Reply with only a 1–2 line takeaway: one thing "
                       "going well and one thing to focus on next week, using these numbers.",
    }


# ───────────────────────── period stats (get_progress) ─────────────────────────

PROGRESS_METRICS = ["diet_days_met", "workout_sessions", "lift_progress", "avg_nutrition", "weight_trend"]
PROGRESS_PERIODS = ["last_n_days", "this_week", "this_calendar_month", "previous_calendar_month"]


def _range(period, days, today):
    if period == "this_week":
        return today - timedelta(days=today.weekday()), today
    if period == "this_calendar_month":
        return today.replace(day=1), today
    if period == "previous_calendar_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    n = max(1, min(int(days or 30), 365))
    return today - timedelta(days=n - 1), today


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def get_progress(ctx: ToolContext, metric, period="last_n_days", days=None, exercise_name=None):
    """Exact numbers for a time range. Gemini phrases the answer."""
    from app.tools import ToolError, load_exercises, resolve_exercise
    if metric not in PROGRESS_METRICS:
        raise ToolError(f"metric must be one of {PROGRESS_METRICS}")
    conn, user, uid = ctx.conn, ctx.user, ctx.user["user_id"]
    today = user_today(user["timezone"])
    start, end = _range(period if period in PROGRESS_PERIODS else "last_n_days", days, today)
    out = {"status": "ok", "metric": metric, "start": str(start), "end": str(end),
           "days_in_range": (end - start).days + 1}
    target = current_targets(conn, uid, end) or {}

    if metric in ("diet_days_met", "avg_nutrition"):
        rows = fetch_all(conn,
            """select log_date, sum(total_calories_kcal) kcal, sum(total_protein_g) protein,
                      sum(total_carbs_g) carbs, sum(total_fat_g) fat, sum(total_fiber_g) fiber
               from meals where user_id = %s and log_date between %s and %s and deleted_at is null
               group by log_date order by log_date""", (uid, start, end))
        if metric == "diet_days_met":
            met = [str(r["log_date"]) for r in rows
                   if _diet_day_met(float(r["kcal"]), float(r["protein"]), target, user.get("goal"))]
            weekly = {}
            for r in rows:
                w = str(_week_start(r["log_date"]))
                weekly.setdefault(w, {"logged": 0, "met": 0})
                weekly[w]["logged"] += 1
                weekly[w]["met"] += str(r["log_date"]) in met
            out.update(days_logged=len(rows), days_met=len(met), by_week_starting=weekly,
                       rule="calories on the right side of target for the goal and protein ≥ 90% of target",
                       target_calories_kcal=target.get("daily_calories_kcal"),
                       target_protein_g=target.get("daily_protein_g"))
        else:
            n = len(rows) or 1
            out.update(days_logged=len(rows), **{f"avg_{k}": round(sum(float(r[k]) for r in rows) / n, 1) if rows else None
                                                 for k in ("kcal", "protein", "carbs", "fat", "fiber")},
                       target_calories_kcal=target.get("daily_calories_kcal"),
                       target_protein_g=target.get("daily_protein_g"))

    elif metric == "workout_sessions":
        rows = fetch_all(conn,
            """select log_date, workout_name from workout_sessions w
               where user_id = %s and log_date between %s and %s and deleted_at is null
                 and exists (select 1 from workout_sets s where s.workout_session_id = w.workout_session_id
                             and s.deleted_at is null) order by log_date""", (uid, start, end))
        weekly = {}
        for r in rows:
            w = str(_week_start(r["log_date"]))
            weekly[w] = weekly.get(w, 0) + 1
        out.update(sessions=len(rows), training_days=len({r["log_date"] for r in rows}),
                   by_week_starting=weekly, by_type={k: sum(1 for r in rows if r["workout_name"] == k)
                                                      for k in {r["workout_name"] for r in rows}},
                   target_per_week=target.get("workouts_per_week"))

    elif metric == "lift_progress":
        if not exercise_name:
            raise ToolError("exercise_name is needed for lift_progress")
        ex = resolve_exercise(load_exercises(conn, uid), exercise_name, allow_fuzzy=True)
        if not ex:
            raise ToolError(f"'{exercise_name}' isn't in the exercise list")
        rows = fetch_all(conn,
            """select distinct on (date_trunc('week', log_date)) date_trunc('week', log_date)::date as week,
                      weight_kg, reps from workout_sets
               where user_id = %s and exercise_id = %s and log_date between %s and %s
                 and deleted_at is null and reps is not null
               order by date_trunc('week', log_date), weight_kg desc nulls last, reps desc""",
            (uid, ex["exercise_id"], start, end))
        best = [{"week_starting": str(r["week"]), "weight_kg": num(r["weight_kg"]), "reps": r["reps"]} for r in rows]
        out.update(exercise_name=ex["exercise_name"], best_set_by_week=best,
                   change_kg=round(best[-1]["weight_kg"] - best[0]["weight_kg"], 1)
                   if len(best) > 1 and best[0]["weight_kg"] is not None else None)

    elif metric == "weight_trend":
        rows = fetch_all(conn,
            """select log_date, weight_kg from weight_logs
               where user_id = %s and log_date between %s and %s and deleted_at is null
               order by log_date, weight_log_id""", (uid, start, end))
        if rows:
            w = [float(r["weight_kg"]) for r in rows]
            out.update(weigh_ins=len(rows), first_kg=num(w[0]), last_kg=num(w[-1]),
                       change_kg=round(w[-1] - w[0], 1), lowest_kg=num(min(w)), highest_kg=num(max(w)))
        else:
            out.update(weigh_ins=0)
    out["instruction"] = "Answer in 1–4 short lines using only these numbers. Mention the date range briefly."
    return out
