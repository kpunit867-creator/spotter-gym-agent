"""Creates throwaway test users in one of three starting states (see SAMPLE_DATA in scenarios.py)."""
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.db import fetch_all, fetch_one
from app.reminders import ensure_default_reminders
from app.timeutil import user_today

TEST_PREFIX = "9199001"          # every eval user's phone starts with this
TZ = "Asia/Kolkata"
TARGETS = dict(kcal=2100, protein=160, carbs=210, fat=58, fiber=29, per_week=4)

# Weekly schedule: weekday (0=Mon) → (workout_name, [(exercise, start_kg, step_kg_per_2_weeks)])
SCHEDULE = {
    0: ("push", [("Barbell Bench Press", 70, 2.5), ("Dumbbell Bench Press", 26, 1), ("Incline Dumbbell Press", 22, 1),
                 ("Tricep Pushdown", 25, 0)]),
    1: ("pull", [("Deadlift", 120, 2.5), ("Lat Pulldown", 55, 2.5), ("Seated Cable Row", 50, 0), ("Barbell Curl", 25, 0)]),
    3: ("legs", [("Barbell Back Squat", 90, 2.5), ("Front Squat", 60, 1.25), ("Leg Press", 160, 5), ("Lying Leg Curl", 35, 0)]),
    5: ("push", [("Overhead Press", 40, 0), ("Incline Dumbbell Press", 22, 1), ("Lateral Raise", 8, 0)]),
}
REPS = [8, 8, 7]
MISSED_SESSIONS = {(3, 5)}                        # (week, weekday): week 3 skips Saturday
DIET_PATTERN = {                                  # week → 7 flags (Mon..Sun): True met, False missed, None not logged
    1: [True, True, False, True, True, False, True],      # 5 met
    2: [True, False, True, True, False, True, None],      # 4 met, Sunday not logged
    3: [True, True, True, False, True, True, True],       # 6 met
    4: [False, True, True, False, True, None, False],     # 3 met, Saturday not logged
    5: [True, False, True, True, True, True, True],       # current week (only days before today are used)
}
MET_DAY = (2000, 165)       # kcal, protein → within target (fat loss: ≤ 2,205 kcal and ≥ 144 g protein)
MISSED_DAY = (2450, 128)    # over calories and under protein


def phone_for(index: int) -> str:
    return f"{TEST_PREFIX}{index:05d}"


def delete_test_user(conn, user_id):
    assert user_id.startswith(TEST_PREFIX)
    conn.execute("delete from users where user_id = %s", (user_id,))


def _exercise_ids(conn):
    return {r["exercise_name"]: r["exercise_id"] for r in
            fetch_all(conn, "select exercise_id, exercise_name from exercises where user_id is null")}


def create_user(conn, user_id, profile: str, name="Test User"):
    delete_test_user(conn, user_id)
    if profile == "new":
        return
    conn.execute(
        """insert into users (user_id, name, age, gender, height_cm, goal, experience_level, diet_preference,
               timezone, onboarding_status, last_inbound_message_at)
           values (%s, %s, 28, 'male', 178, 'fat_loss', 'intermediate', 'non_veg', %s, 'completed', now())""",
        (user_id, name, TZ))
    today = user_today(TZ)
    conn.execute(
        """insert into user_targets (user_id, daily_calories_kcal, daily_protein_g, daily_carbs_g, daily_fat_g,
               daily_fiber_g, workouts_per_week, start_date) values (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (user_id, TARGETS["kcal"], TARGETS["protein"], TARGETS["carbs"], TARGETS["fat"], TARGETS["fiber"],
         TARGETS["per_week"], today - timedelta(days=60)))
    user = fetch_one(conn, "select * from users where user_id = %s", (user_id,))
    ensure_default_reminders(conn, user, workout_time=dtime(19, 0))
    if profile == "ready":
        conn.execute("insert into weight_logs (user_id, log_date, input_text, input_unit, weight_kg) values (%s,%s,'seed','kg',79)",
                     (user_id, today - timedelta(days=3)))
    elif profile == "history":
        seed_history(conn, user_id, today)


def seed_history(conn, user_id, today):
    """Builds all rows in memory, then inserts them in a few batched round trips."""
    ids = _exercise_ids(conn)
    this_monday = today - timedelta(days=today.weekday())
    weight = 80.0
    sessions, sets, meals, weights = [], [], [], []
    for week in range(1, 6):                       # 1–4 full weeks, 5 = current week up to yesterday
        monday = this_monday - timedelta(days=7 * (5 - week))
        for wd in range(7):
            day = monday + timedelta(days=wd)
            if day >= today:
                break
            if wd in SCHEDULE and (week, wd) not in MISSED_SESSIONS:
                name, exercises = SCHEDULE[wd]
                sessions.append((user_id, day, name, _at(day, 19)))
                for ex, start_kg, step in exercises:
                    kg = start_kg + step * ((week - 1) // 2)
                    for n, reps in enumerate(REPS, start=1):
                        sets.append((len(sessions) - 1, day, ids[ex], ex, n, kg, reps))
            flag = DIET_PATTERN[week][wd]
            if flag is not None:
                kcal, protein = MET_DAY if flag else MISSED_DAY
                for n, (mtype, share, hour) in enumerate([("breakfast", .25, 9), ("lunch", .40, 14), ("dinner", .35, 21)], 1):
                    meals.append((user_id, day, n, mtype, kcal * share, protein * share, 220 * share, 60 * share,
                                  28 * share, _at(day, hour)))
            if wd in (0, 3):
                weights.append((user_id, day, round(weight, 1), _at(day, 8)))
                weight -= 0.2

    cur = conn.cursor()
    cur.executemany("""insert into workout_sessions (user_id, log_date, workout_name, created_at)
                       values (%s,%s,%s,%s) returning workout_session_id""", sessions, returning=True)
    session_ids = []
    while True:
        session_ids.append(cur.fetchone()["workout_session_id"])
        if not cur.nextset():
            break
    cur.executemany("""insert into workout_sets (workout_session_id, user_id, log_date, exercise_id, exercise_name,
                           set_number, weight_kg, reps, created_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(session_ids[i], user_id, d, eid, ex, n, kg, reps, _at(d, 19)) for i, d, eid, ex, n, kg, reps in sets])
    cur.executemany("""insert into meals (user_id, log_date, meal_number, meal_type, meal_description, total_calories_kcal,
                           total_protein_g, total_carbs_g, total_fat_g, total_fiber_g, created_at)
                       values (%s,%s,%s,%s,'seed',%s,%s,%s,%s,%s,%s) returning meal_id""", meals, returning=True)
    meal_ids = []
    while True:
        meal_ids.append(cur.fetchone()["meal_id"])
        if not cur.nextset():
            break
    cur.executemany("""insert into meal_items (meal_id, user_id, log_date, food_name, quantity, unit, calories_kcal,
                           protein_g, carbs_g, fat_g, fiber_g, created_at)
                       values (%s,%s,%s,'Seed meal',1,'plate',%s,%s,%s,%s,%s,%s)""",
                    [(mid, m[0], m[1], *m[4:]) for mid, m in zip(meal_ids, meals)])
    cur.executemany("""insert into weight_logs (user_id, log_date, input_text, input_unit, weight_kg, created_at)
                       values (%s,%s,'seed','kg',%s,%s)""", weights)


def _at(day, hour):
    return datetime.combine(day, dtime(hour, 0), ZoneInfo(TZ))


# ───────────── per-scenario setup hooks ─────────────

def setup_extra_sessions_this_week(conn, user_id):
    """Makes sure the current week already has 4 sessions before today (for REM-04)."""
    today = user_today(TZ)
    monday = today - timedelta(days=today.weekday())
    have = fetch_one(conn, """select count(*) n from workout_sessions where user_id = %s and log_date between %s and %s
                               and deleted_at is null""", (user_id, monday, today))["n"]
    ids = _exercise_ids(conn)
    day = monday
    while have < TARGETS["per_week"] and day < today:
        exists = fetch_one(conn, "select 1 from workout_sessions where user_id = %s and log_date = %s", (user_id, day))
        if not exists:
            sid = fetch_one(conn, """insert into workout_sessions (user_id, log_date, workout_name)
                                     values (%s,%s,'full_body') returning workout_session_id""",
                            (user_id, day))["workout_session_id"]
            conn.execute("""insert into workout_sets (workout_session_id, user_id, log_date, exercise_id, exercise_name,
                                set_number, weight_kg, reps) values (%s,%s,%s,%s,'Burpee',1,null,15)""",
                         (sid, user_id, day, ids["Burpee"]))
            have += 1
        day += timedelta(days=1)
    return have >= TARGETS["per_week"]


def setup_inbound_2_days_ago(conn, user_id):
    conn.execute("update users set last_inbound_message_at = now() - interval '2 days' where user_id = %s", (user_id,))
    return True


SETUP_HOOKS = {"extra_sessions_this_week": setup_extra_sessions_this_week,
               "inbound_2_days_ago": setup_inbound_2_days_ago}
