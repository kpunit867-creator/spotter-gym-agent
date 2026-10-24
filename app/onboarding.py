"""Onboarding: collect the profile, then calculate daily targets in code."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import fetch_one
from app.reminders import ensure_default_reminders, next_run_at, workout_check_time
from app.tools import ToolContext, ToolError, log_weight, to_kg

STEPS = ["name", "age", "gender", "height", "weight", "goal", "experience", "diet", "schedule"]

GOALS = ["fat_loss", "muscle_gain", "recomposition", "strength", "general_fitness"]
LEVELS = ["beginner", "intermediate", "advanced"]
DIETS = ["veg", "eggetarian", "non_veg", "vegan"]
GENDERS = ["male", "female", "other"]

STEP_QUESTIONS = {
    "name": "their name",
    "age": "their age",
    "gender": "their gender (male / female / other) — needed for calorie maths",
    "height": "their height (cm, or feet and inches)",
    "weight": "their current body weight (kg or lb)",
    "goal": "their main goal: fat loss, muscle gain, recomposition, strength, or general fitness",
    "experience": "how long they've been training: beginner (<1 yr), intermediate (1–3 yrs), advanced (3+ yrs)",
    "diet": "their diet: veg, eggetarian, non-veg or vegan",
    "schedule": "how many days a week they plan to train, and roughly what time (for reminders)",
}


def missing_steps(conn, user) -> list[str]:
    has_weight = fetch_one(conn,
        "select 1 from weight_logs where user_id = %s and deleted_at is null limit 1",
        (user["user_id"],)) is not None
    targets = fetch_one(conn,
        "select workouts_per_week from user_targets where user_id = %s order by start_date desc limit 1",
        (user["user_id"],))
    filled = {
        "name": user.get("name"), "age": user.get("age"), "gender": user.get("gender"),
        "height": user.get("height_cm"), "weight": has_weight, "goal": user.get("goal"),
        "experience": user.get("experience_level"), "diet": user.get("diet_preference"),
        "schedule": targets is not None and targets["workouts_per_week"] is not None,
    }
    return [s for s in STEPS if not filled[s]]


def calculate_targets(user, weight_kg, workouts_per_week) -> dict:
    """Mifflin-St Jeor BMR × activity, adjusted for goal; protein per kg body weight."""
    w, h, a = float(weight_kg), float(user["height_cm"]), int(user["age"])
    offset = {"male": 5, "female": -161}.get(user["gender"], -78)
    bmr = 10 * w + 6.25 * h - 5 * a + offset
    # Conservative multipliers: most gym-goers have desk jobs, and standard
    # calculators tend to overestimate burn for 5–6 sessions a week.
    activity = 1.3 if workouts_per_week <= 2 else 1.4 if workouts_per_week <= 4 else 1.5
    goal_factor = {"fat_loss": 0.80, "muscle_gain": 1.10, "recomposition": 0.95,
                   "strength": 1.05, "general_fitness": 1.00}[user["goal"]]
    kcal = round(bmr * activity * goal_factor / 10) * 10
    protein_per_kg = {"fat_loss": 2.0, "muscle_gain": 1.8, "recomposition": 2.0,
                      "strength": 1.8, "general_fitness": 1.4}[user["goal"]]
    protein = round(w * protein_per_kg)
    fat = round(kcal * 0.25 / 9)
    carbs = max(0, round((kcal - protein * 4 - fat * 9) / 4))
    fiber = round(kcal / 1000 * 14)
    return {"daily_calories_kcal": kcal, "daily_protein_g": protein, "daily_carbs_g": carbs,
            "daily_fat_g": fat, "daily_fiber_g": fiber}


def _norm_choice(value, options, aliases=None):
    if value is None:
        return None
    v = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    v = (aliases or {}).get(v, v)
    if v not in options:
        raise ToolError(f"'{value}' should be one of {options}")
    return v


def save_profile(ctx: ToolContext, name=None, age=None, gender=None, height_cm=None,
                 weight=None, weight_unit="kg", goal=None, experience_level=None,
                 diet_preference=None, workouts_per_week=None, workout_time=None):
    conn, uid = ctx.conn, ctx.user_id
    # Validate each field on its own: one bad answer (e.g. age 150) must not throw away the good ones.
    fields, problems, saved_other = {}, [], []

    def take(field, fn):
        try:
            fields[field] = fn()
        except (ToolError, ValueError, TypeError) as e:
            problems.append(f"{field}: {e}")

    def in_range(value, lo, hi, label, cast):
        v = cast(value)
        if not lo <= v <= hi:
            raise ToolError(f"{label} should be between {lo} and {hi} (got {value}); ask again")
        return v

    if name:
        take("name", lambda: str(name).strip()[:60])
    if age is not None:
        take("age", lambda: in_range(age, 10, 100, "Age", int))
    if gender:
        take("gender", lambda: _norm_choice(gender, GENDERS, {"m": "male", "f": "female", "man": "male", "woman": "female"}))
    if height_cm is not None:
        take("height_cm", lambda: round(in_range(height_cm, 100, 250, "Height in cm", float), 1))
    if goal:
        take("goal", lambda: _norm_choice(goal, GOALS, {"weight_loss": "fat_loss", "lose_weight": "fat_loss",
                                                        "muscle": "muscle_gain", "bulk": "muscle_gain",
                                                        "recomp": "recomposition", "general": "general_fitness"}))
    if experience_level:
        take("experience_level", lambda: _norm_choice(experience_level, LEVELS))
    if diet_preference:
        take("diet_preference", lambda: _norm_choice(diet_preference, DIETS, {"vegetarian": "veg", "nonveg": "non_veg",
                                                                              "egg": "eggetarian"}))
    if fields:
        sets = ", ".join(f"{k} = %s" for k in fields)
        conn.execute(f"update users set {sets} where user_id = %s", (*fields.values(), uid))

    if weight is not None:
        existing = fetch_one(conn, "select weight_log_id from weight_logs where user_id = %s and deleted_at is null limit 1", (uid,))
        if not existing:
            try:
                log_weight(ctx, weight, weight_unit, input_text=f"{weight} {weight_unit} (onboarding)")
                saved_other.append("weight")
            except ToolError as e:
                problems.append(f"weight: {e}")

    if workouts_per_week is not None:
        try:
            wpw = in_range(workouts_per_week, 0, 7, "Workouts per week", int)
            conn.execute(
                """insert into user_targets (user_id, workouts_per_week, start_date)
                   values (%s, %s, %s)
                   on conflict (user_id, start_date) do update set workouts_per_week = excluded.workouts_per_week""",
                (uid, wpw, ctx.today))
            saved_other.append("workouts_per_week")
        except (ToolError, ValueError) as e:
            problems.append(f"workouts_per_week: {e}")
    if workout_time:
        try:
            t = datetime.strptime(str(workout_time).strip(), "%H:%M")
            # Check two hours after their usual training time whether a workout was logged.
            check_at = workout_check_time(t.time())
            conn.execute("delete from reminders where user_id = %s and reminder_type = 'workout_log_reminder'", (uid,))
            conn.execute(
                """insert into reminders (user_id, reminder_type, send_time, days_of_week, next_run_at)
                   values (%s, 'workout_log_reminder', %s, '{1,2,3,4,5,6,7}', %s)""",
                (uid, check_at, next_run_at(check_at, range(1, 8), ctx.user["timezone"],
                                            datetime.now(ZoneInfo("UTC")))))
            saved_other.append("workout_time")
        except ValueError:
            problems.append("workout_time: should be HH:MM in 24-hour time, e.g. 18:30")

    if problems and not fields and not saved_other:
        raise ToolError("Nothing saved. " + "; ".join(problems))

    user = fetch_one(conn, "select * from users where user_id = %s", (uid,))
    ctx.user = user
    missing = missing_steps(conn, user)
    if missing:
        conn.execute("update users set onboarding_status = 'in_progress', onboarding_step = %s where user_id = %s",
                     (missing[0], uid))
        return {"status": "saved", "saved_fields": list(fields) + saved_other,
                "not_saved": problems or None,
                "next_step": missing[0], "ask_next": STEP_QUESTIONS[missing[0]],
                "still_missing": missing,
                "instruction": "Acknowledge what was saved. If something wasn't saved, ask for it again first; "
                               "otherwise ask the next missing item. Always reply with text."}

    return complete_onboarding(ctx, user)


def complete_onboarding(ctx: ToolContext, user) -> dict:
    conn, uid = ctx.conn, ctx.user_id
    weight = fetch_one(conn,
        "select weight_kg from weight_logs where user_id = %s and deleted_at is null order by log_date desc, weight_log_id desc limit 1",
        (uid,))["weight_kg"]
    row = fetch_one(conn,
        "select user_target_id, workouts_per_week from user_targets where user_id = %s order by start_date desc limit 1",
        (uid,))
    targets = calculate_targets(user, weight, row["workouts_per_week"])
    conn.execute(
        """update user_targets set daily_calories_kcal = %s, daily_protein_g = %s, daily_carbs_g = %s,
               daily_fat_g = %s, daily_fiber_g = %s where user_target_id = %s""",
        (targets["daily_calories_kcal"], targets["daily_protein_g"], targets["daily_carbs_g"],
         targets["daily_fat_g"], targets["daily_fiber_g"], row["user_target_id"]))
    # Meal logging checks (14:30, 21:30) + workout check if no training time was given.
    ensure_default_reminders(conn, user)
    conn.execute("update users set onboarding_status = 'completed', onboarding_step = null where user_id = %s", (uid,))
    ctx.user = fetch_one(conn, "select * from users where user_id = %s", (uid,))
    return {"status": "onboarding_completed", "daily_targets": targets,
            "workouts_per_week": row["workouts_per_week"],
            "how_calculated": "Mifflin-St Jeor BMR × activity level, adjusted for goal; protein per kg body weight.",
            "next": "Tell the user their targets briefly and show 2–3 example messages they can send "
                    "(a workout, a meal, a weigh-in)."}
