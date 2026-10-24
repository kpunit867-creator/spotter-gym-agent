"""Logging tools the agent can call.

Gemini only turns the user's words into structured arguments. Everything that
must be exact — matching exercise names, saving rows, totals, PRs — happens here
in code, and the result is handed back to Gemini to phrase the reply.
"""
import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from psycopg.types.json import Jsonb

from app.db import fetch_all, fetch_one
from app.timeutil import user_today

LB_PER_KG = 2.20462

WORKOUT_NAMES = ["push", "pull", "legs", "arms", "chest", "back", "shoulders",
                 "core", "cardio", "full_body", "other"]
MEAL_TYPES = ["breakfast", "lunch", "snack", "dinner", "pre_workout", "post_workout"]
FOOD_UNITS = ["piece", "katori", "cup", "glass", "plate", "tbsp", "g", "ml"]
MUSCLE_GROUPS = ["chest", "back", "shoulders", "biceps", "triceps", "legs",
                 "glutes", "core", "full_body", "cardio"]
EXERCISE_TYPES = ["strength", "bodyweight", "cardio"]
CORRECTION_REASONS = ["wrong_parse", "wrong_estimate", "wrong_exercise",
                      "user_changed_mind", "other"]

# A workout logged within this many hours of the last one (same day) joins the same session.
SESSION_JOIN_HOURS = 4


class ToolError(Exception):
    """A problem the agent should explain to the user (bad input, not found...)."""


@dataclass
class ToolContext:
    conn: object
    user: dict
    message_id: int
    pending_action: dict | None = None      # what we were waiting on before this turn
    user_text: str | None = None            # this message + the user's previous one (for grounding checks)
    new_pending_action: dict | None = None
    logged_something: bool = False
    asked_clarification: bool = False
    notes: list = field(default_factory=list)

    @property
    def user_id(self) -> str:
        return self.user["user_id"]

    @property
    def today(self) -> date:
        return user_today(self.user["timezone"])


# ───────────────────────── small helpers ─────────────────────────

def num(v):
    """Decimal/None → float/None, rounded for readable replies."""
    if v is None:
        return None
    return round(float(v), 1)


def to_kg(weight, unit):
    if weight is None:
        return None
    weight = float(weight)
    return round(weight / LB_PER_KG, 2) if (unit or "kg").lower() in ("lb", "lbs") else weight


def parse_log_date(ctx: ToolContext, value) -> date:
    if not value:
        return ctx.today
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f"log_date must be YYYY-MM-DD, got {value!r}")
    if d > ctx.today:
        raise ToolError("Can't log something for a future date.")
    if d < ctx.today - timedelta(days=30):
        raise ToolError("Can only log up to 30 days back.")
    return d


def get_state(conn, user_id) -> dict:
    conn.execute(
        "insert into conversation_state (user_id) values (%s) on conflict do nothing",
        (user_id,))
    return fetch_one(conn, "select * from conversation_state where user_id = %s", (user_id,))


def update_state(conn, user_id, **fields):
    if not fields:
        return
    get_state(conn, user_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = [Jsonb(v) if k == "pending_action" and v is not None else v for k, v in fields.items()]
    conn.execute(
        f"update conversation_state set {sets}, updated_at = now() where user_id = %s",
        (*values, user_id))


def remember_for_undo(ctx: ToolContext):
    """'undo' removes everything logged from the latest logging message."""
    ctx.logged_something = True
    update_state(ctx.conn, ctx.user_id, undo_table_name="messages", undo_record_id=ctx.message_id)


def record_correction(ctx, table, record_id, field_name, old, new, reason):
    if reason not in CORRECTION_REASONS:
        reason = "other"
    ctx.conn.execute(
        """insert into corrections (user_id, corrected_table_name, corrected_record_id,
               field_name, old_value, new_value, correction_reason, message_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (ctx.user_id, table, record_id, field_name,
         None if old is None else str(old), None if new is None else str(new),
         reason, ctx.message_id))


# ───────────────────────── nutrition ─────────────────────────

def current_targets(conn, user_id, on_date: date):
    return fetch_one(conn,
        """select * from user_targets where user_id = %s and start_date <= %s
           order by start_date desc limit 1""", (user_id, on_date))


def day_nutrition(conn, user_id, on_date: date) -> dict:
    row = fetch_one(conn,
        """select count(*) as meals, coalesce(sum(total_calories_kcal),0) kcal,
                  coalesce(sum(total_protein_g),0) protein, coalesce(sum(total_carbs_g),0) carbs,
                  coalesce(sum(total_fat_g),0) fat, coalesce(sum(total_fiber_g),0) fiber
           from meals where user_id = %s and log_date = %s and deleted_at is null""",
        (user_id, on_date))
    t = current_targets(conn, user_id, on_date) or {}
    return {
        "date": str(on_date),
        "meals_logged": row["meals"],
        "eaten": {"calories_kcal": num(row["kcal"]), "protein_g": num(row["protein"]),
                  "carbs_g": num(row["carbs"]), "fat_g": num(row["fat"]),
                  "fiber_g": num(row["fiber"])},
        "targets": {"calories_kcal": t.get("daily_calories_kcal"),
                    "protein_g": t.get("daily_protein_g"), "carbs_g": t.get("daily_carbs_g"),
                    "fat_g": t.get("daily_fat_g"), "fiber_g": t.get("daily_fiber_g")},
        "remaining": {
            "calories_kcal": None if t.get("daily_calories_kcal") is None
            else round(t["daily_calories_kcal"] - float(row["kcal"])),
            "protein_g": None if t.get("daily_protein_g") is None
            else round(t["daily_protein_g"] - float(row["protein"])),
        },
    }


def recompute_meal_totals(conn, meal_id):
    conn.execute(
        """update meals m set
             total_calories_kcal = coalesce(s.kcal, 0), total_protein_g = coalesce(s.p, 0),
             total_carbs_g = coalesce(s.c, 0), total_fat_g = coalesce(s.f, 0),
             total_fiber_g = coalesce(s.fi, 0)
           from (select sum(calories_kcal) kcal, sum(protein_g) p, sum(carbs_g) c,
                        sum(fat_g) f, sum(fiber_g) fi
                 from meal_items where meal_id = %s and deleted_at is null) s
           where m.meal_id = %s""", (meal_id, meal_id))


VAGUE_FOODS = {"something", "something light", "light snack", "snack", "food", "meal", "lunch", "dinner",
               "breakfast", "stuff", "light meal", "light food", "a meal", "some food", "light"}


def log_meal(ctx: ToolContext, meal_description, items, meal_type=None, log_date=None):
    if not items:
        raise ToolError("A meal needs at least one food item.")
    vague = [it["food_name"] for it in items if _norm(it.get("food_name", "")) in VAGUE_FOODS]
    if vague:
        raise ToolError(f"'{vague[0]}' isn't a specific food. Nothing saved; ask what they actually ate.")
    d = parse_log_date(ctx, log_date)
    if meal_type not in MEAL_TYPES:
        meal_type = None
    conn = ctx.conn
    meal_number = fetch_one(conn,
        "select count(*) + 1 as n from meals where user_id = %s and log_date = %s and deleted_at is null",
        (ctx.user_id, d))["n"]
    meal = fetch_one(conn,
        """insert into meals (user_id, log_date, meal_number, meal_type, meal_description, message_id)
           values (%s, %s, %s, %s, %s, %s) returning meal_id""",
        (ctx.user_id, d, meal_number, meal_type, meal_description, ctx.message_id))
    saved = []
    for it in items:
        unit = it.get("unit") if it.get("unit") in FOOD_UNITS else "piece"
        row = fetch_one(conn,
            """insert into meal_items (meal_id, user_id, log_date, food_name, quantity, unit,
                   estimated_grams, calories_kcal, protein_g, carbs_g, fat_g, fiber_g, message_id)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning meal_item_id""",
            (meal["meal_id"], ctx.user_id, d, it["food_name"], it.get("quantity") or 1, unit,
             it.get("estimated_grams"), it.get("calories_kcal") or 0, it.get("protein_g") or 0,
             it.get("carbs_g") or 0, it.get("fat_g") or 0, it.get("fiber_g") or 0,
             ctx.message_id))
        saved.append({"meal_item_id": row["meal_item_id"], "food_name": it["food_name"],
                      "quantity": it.get("quantity") or 1, "unit": unit,
                      "calories_kcal": it.get("calories_kcal")})
    recompute_meal_totals(conn, meal["meal_id"])
    totals = fetch_one(conn, "select * from meals where meal_id = %s", (meal["meal_id"],))
    remember_for_undo(ctx)
    return {
        "status": "logged",
        "meal_id": meal["meal_id"],
        "meal_number": meal_number,
        "meal_type": meal_type,
        "log_date": str(d),
        "items": saved,
        "meal_totals": {"calories_kcal": num(totals["total_calories_kcal"]),
                        "protein_g": num(totals["total_protein_g"]),
                        "carbs_g": num(totals["total_carbs_g"]),
                        "fat_g": num(totals["total_fat_g"]),
                        "fiber_g": num(totals["total_fiber_g"])},
        "day_so_far": day_nutrition(conn, ctx.user_id, d),
    }


# ───────────────────────── exercises ─────────────────────────

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}


def decimals_in(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]


def numbers_in(text: str) -> set[int]:
    nums = {int(float(n)) for n in re.findall(r"\d+(?:\.\d+)?", text or "")}
    nums |= {NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", (text or "").lower()) if w in NUMBER_WORDS}
    return nums


def _norm(s: str) -> str:
    return " ".join(str(s).lower().replace("-", " ").replace("_", " ").split())


_BUILTIN_CACHE = {"at": 0.0, "rows": []}
BUILTIN_CACHE_SECONDS = 300


def load_exercises(conn, user_id):
    """Built-in dictionary (cached 5 min) + this user's own exercises."""
    import time
    if time.monotonic() - _BUILTIN_CACHE["at"] > BUILTIN_CACHE_SECONDS:
        _BUILTIN_CACHE["rows"] = fetch_all(conn,
            """select exercise_id, exercise_name, aliases, muscle_group, exercise_type
               from exercises where user_id is null order by exercise_name""")
        _BUILTIN_CACHE["at"] = time.monotonic()
    own = fetch_all(conn,
        """select exercise_id, exercise_name, aliases, muscle_group, exercise_type
           from exercises where user_id = %s order by exercise_name""", (user_id,))
    return [dict(r) for r in _BUILTIN_CACHE["rows"]] + own


AUTO_MATCH_RATIO = 0.88  # "pec dek" → "pec deck" is accepted; anything less certain is asked


def resolve_exercise(exercises, name, allow_fuzzy=False):
    """Exact match on name or nickname; optionally accept a near-identical spelling."""
    n = _norm(name)
    for e in exercises:
        if _norm(e["exercise_name"]) == n or n in (_norm(a) for a in e["aliases"]):
            return e
    if not allow_fuzzy:
        return None
    best, best_ratio = None, 0.0
    for e in exercises:
        for key in [e["exercise_name"], *e["aliases"]]:
            r = difflib.SequenceMatcher(None, n, _norm(key)).ratio()
            if r > best_ratio:
                best, best_ratio = e, r
    return best if best_ratio >= AUTO_MATCH_RATIO else None


def learn_alias(conn, exercise, wording):
    """Remember the user's wording as a nickname so we never ask about it again."""
    w = _norm(wording)
    if not w or w == _norm(exercise["exercise_name"]) or w in (_norm(a) for a in exercise["aliases"]):
        return False
    conn.execute("update exercises set aliases = array_append(aliases, %s) where exercise_id = %s",
                 (w, exercise["exercise_id"]))
    _BUILTIN_CACHE["at"] = 0.0  # reload the dictionary next time
    exercise["aliases"] = [*exercise["aliases"], w]
    return True


GENERIC_WORDS = {"press", "curl", "curls", "row", "rows", "raise", "raises", "fly", "flys", "flyes", "extension",
                 "extensions", "squat", "squats", "pull", "pulls", "push", "pushdown", "machine", "cable", "dumbbell",
                 "db", "barbell", "bb", "kettlebell", "kb", "seated", "standing", "lying", "incline", "decline",
                 "crunch", "crunches", "lunge", "lunges", "dip", "dips", "walk", "run", "free", "weight", "weighted"}


def _distinctive(text: str) -> str:
    """'svend press' → 'svend': drop words most exercises share, so 'press' alone can't make two names look alike."""
    words = [w for w in _norm(text).split() if w not in GENERIC_WORDS]
    return " ".join(words) or _norm(text)


def _singular(tokens):
    return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens}


def suggest_exercises(exercises, name, limit=3):
    """Close matches to offer as 'did you mean…?'.

    1. Exercises whose name contains every word the user typed ("leg raise" → Hanging/Lying Leg Raise).
    2. Otherwise fuzzy match on the distinctive words only, so a shared "press" or "crunch" can't
       make unrelated exercises look alike.
    """
    words = _singular(_norm(name).split())
    distinct = _distinctive(name)
    has_distinct = distinct != _norm(name) or not (set(_norm(name).split()) <= GENERIC_WORDS)
    scored = {}
    for e in exercises:
        for key in [e["exercise_name"], *e["aliases"]]:
            key_words = _singular(_norm(key).split())
            if words and words <= key_words:
                score = 1.0 + 1 / len(key_words)          # shorter names that contain all words rank first
            elif has_distinct and set(distinct.split()) - GENERIC_WORDS:
                score = difflib.SequenceMatcher(None, distinct, _distinctive(key)).ratio()
                score = score if score >= 0.75 else 0
            else:
                score = difflib.SequenceMatcher(None, _norm(name), _norm(key)).ratio()
                score = score if score >= 0.85 else 0
            if score > scored.get(e["exercise_name"], 0):
                scored[e["exercise_name"]] = score
    ranked = sorted(((v, k) for k, v in scored.items() if v > 0), reverse=True)
    if ranked and ranked[0][0] > 1:           # exact-word matches exist: don't pad with weak fuzzy ones
        ranked = [r for r in ranked if r[0] > 1]
    return [k for _, k in ranked[:limit]]

def _same_wording(a: str, b: str) -> bool:
    x, y = _norm(a).rstrip("s"), _norm(b).rstrip("s")
    return x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.6


def similar_exercise(exercises, name):
    """An existing exercise that's really the same as `name` (close spelling, or name contains it)."""
    n = _norm(name)
    words = set(n.split())
    for e in exercises:
        existing = _norm(e["exercise_name"])
        if difflib.SequenceMatcher(None, n, existing).ratio() >= 0.8:
            return e
        if len(existing.split()) >= 2 and set(existing.split()) <= words:
            return e
    return None


def add_exercise(ctx: ToolContext, exercise_name, muscle_group, exercise_type, aliases=None, force_new=False):
    if muscle_group not in MUSCLE_GROUPS:
        raise ToolError(f"muscle_group must be one of {MUSCLE_GROUPS}")
    if exercise_type not in EXERCISE_TYPES:
        raise ToolError(f"exercise_type must be one of {EXERCISE_TYPES}")
    asked = [u["name_given"] for u in (ctx.pending_action or {}).get("unknown_exercises", [])]
    if not any(_same_wording(exercise_name, a) for a in asked):
        raise ToolError(f"The user hasn't agreed to add '{exercise_name}'. Nothing added. Call log_workout with "
                        "their words first; if it's unknown, ask them whether to add it.")
    catalog = load_exercises(ctx.conn, ctx.user_id)
    existing = resolve_exercise(catalog, exercise_name)
    if existing:
        return {"status": "already_exists", "exercise_name": existing["exercise_name"],
                "instruction": "Use this existing name in log_workout."}
    close = None if force_new else similar_exercise(catalog, exercise_name)
    offered = {n for u in (ctx.pending_action or {}).get("unknown_exercises", []) for n in u.get("did_you_mean", [])}
    if close and close["exercise_name"] not in offered:
        close = None  # the user was asked about their own name and said add it: trust that
    if close:
        return {"status": "similar_exists", "existing_exercise": close["exercise_name"],
                "instruction": f"Not added. '{close['exercise_name']}' already exists. If the user meant it, log with "
                               "that name. Only if they clearly say it's a different exercise, call add_exercise "
                               "again with force_new=true."}
    wordings = list(aliases or [])
    if ctx.pending_action and ctx.pending_action.get("type") == "confirm_new_exercises":
        wordings += [u["name_given"] for u in ctx.pending_action.get("unknown_exercises", [])
                     if _norm(u["name_given"]) in _norm(exercise_name) or _norm(exercise_name) in _norm(u["name_given"])]
    clean_aliases = sorted({_norm(a) for a in wordings if a and _norm(a) != _norm(exercise_name)})
    row = fetch_one(ctx.conn,
        """insert into exercises (exercise_name, aliases, muscle_group, exercise_type, user_id)
           values (%s, %s, %s, %s, %s) returning exercise_id""",
        (exercise_name.strip(), clean_aliases, muscle_group, exercise_type, ctx.user_id))
    return {"status": "added", "exercise_id": row["exercise_id"], "exercise_name": exercise_name.strip(),
            "aliases": clean_aliases,
            "next_step": "Now call log_workout with this exact exercise_name to log the sets."}


# ───────────────────────── workouts ─────────────────────────

def _find_joinable_session(ctx, d, start_new_session, workout_name):
    """Join the open session only if it's the same day, recent, and the same kind of workout."""
    if start_new_session:
        return None
    state = get_state(ctx.conn, ctx.user_id)
    sid = state.get("active_workout_session_id")
    if not sid:
        return None
    return fetch_one(ctx.conn,
        """select * from workout_sessions
           where workout_session_id = %s and user_id = %s and log_date = %s and workout_name = %s
             and deleted_at is null and updated_at > now() - make_interval(hours => %s)""",
        (sid, ctx.user_id, d, workout_name if workout_name in WORKOUT_NAMES else "other", SESSION_JOIN_HOURS))


def _previous_best(conn, user_id, exercise_id):
    return fetch_one(conn,
        """select max(weight_kg) as best_weight_kg, max(reps) as best_reps
           from workout_sets where user_id = %s and exercise_id = %s and deleted_at is null""",
        (user_id, exercise_id))


def _last_session_sets(conn, user_id, exercise_id, before_session_id):
    last = fetch_one(conn,
        """select workout_session_id, log_date from workout_sets
           where user_id = %s and exercise_id = %s and deleted_at is null
             and workout_session_id <> %s
           order by log_date desc, workout_set_id desc limit 1""",
        (user_id, exercise_id, before_session_id))
    if not last:
        return None
    sets = fetch_all(conn,
        """select set_number, weight_kg, reps, duration_sec, distance_km from workout_sets
           where workout_session_id = %s and exercise_id = %s and deleted_at is null
           order by set_number""", (last["workout_session_id"], exercise_id))
    return {"log_date": str(last["log_date"]),
            "sets": [{"weight_kg": num(s["weight_kg"]), "reps": s["reps"],
                      "duration_sec": s["duration_sec"], "distance_km": num(s["distance_km"])}
                     for s in sets]}


DUPLICATE_WINDOW_MINUTES = 60


def _already_logged(ctx, match, sets, d):
    """Same exercise with the same weights and reps, saved from an earlier message in the last hour?"""
    rows = fetch_all(ctx.conn,
        """select message_id, weight_kg, reps, duration_sec from workout_sets
           where user_id = %s and exercise_id = %s and log_date = %s and deleted_at is null
             and message_id is distinct from %s and created_at > now() - make_interval(mins => %s)
           order by workout_set_id""",
        (ctx.user_id, match["exercise_id"], d, ctx.message_id, DUPLICATE_WINDOW_MINUTES))
    wanted = [(round(to_kg(s.get("weight"), s.get("weight_unit")) or 0, 1), s.get("reps"), s.get("duration_sec")) for s in sets]
    by_msg = {}
    for r in rows:
        by_msg.setdefault(r["message_id"], []).append((round(float(r["weight_kg"] or 0), 1), r["reps"], r["duration_sec"]))
    return any(v == wanted for v in by_msg.values())


def log_workout(ctx: ToolContext, workout_name, exercises, notes=None, duration_min=None,
                log_date=None, start_new_session=False, confirm_duplicate=False):
    if not exercises:
        raise ToolError("Need at least one exercise with sets.")
    d = parse_log_date(ctx, log_date)
    conn = ctx.conn
    catalog = load_exercises(conn, ctx.user_id)

    # 1. Every exercise must exist in the dictionary before anything is saved.
    resolved, unknown, learned = [], [], []
    for ex in exercises:
        match = resolve_exercise(catalog, ex["exercise_name"], allow_fuzzy=True)
        if match:
            if _norm(ex["exercise_name"]) not in [_norm(match["exercise_name"]), *map(_norm, match["aliases"])]:
                if learn_alias(conn, match, ex["exercise_name"]):
                    learned.append({"wording": ex["exercise_name"], "exercise_name": match["exercise_name"]})
            resolved.append((match, ex.get("sets") or []))
        else:
            unknown.append({"name_given": ex["exercise_name"],
                            "did_you_mean": suggest_exercises(catalog, ex["exercise_name"])})
    unknown_instruction = None
    if unknown:
        unknown_names = {_norm(u["name_given"]) for u in unknown}
        ctx.new_pending_action = {"type": "confirm_new_exercises",
                                  "unknown_exercises": unknown,
                                  "workout_still_to_log": {
                                      "workout_name": workout_name, "log_date": str(d),
                                      "exercises": [e for e in exercises if _norm(e["exercise_name"]) in unknown_names]}}
        ctx.asked_clarification = True
        unknown_instruction = (
            "Ask ONE short question covering ALL unknown exercises together (never one at a time). For each: if "
            "did_you_mean has names, 'did you mean X, or add <name> as new?'; if empty, 'add <name> as a new "
            "<muscle> exercise?'. Example: 'Should I add Free Weight Crunch and Oblique Cable Crunch as new core "
            "exercises?' "
            "After they answer, call add_exercise if needed, then log_workout with ONLY the exercises in "
            "workout_still_to_log (the others are already saved).")
        if not resolved:
            return {"status": "not_logged", "reason": "unknown_exercises", "unknown": unknown,
                    "instruction": "Nothing was saved. " + unknown_instruction}

    # The user confirmed a "did you mean" from last turn → save their original wording as a nickname.
    if ctx.pending_action and ctx.pending_action.get("type") == "confirm_new_exercises":
        logged_names = {m["exercise_name"] for m, _ in resolved}
        for u in ctx.pending_action.get("unknown_exercises", []):
            for suggestion in u.get("did_you_mean", []):
                if suggestion in logged_names:
                    match = next(m for m, _ in resolved if m["exercise_name"] == suggestion)
                    if learn_alias(conn, match, u["name_given"]):
                        learned.append({"wording": u["name_given"], "exercise_name": suggestion})
                    break

    # Grounding: every rep count must come from what the user wrote (stops invented "× 10").
    if ctx.user_text is not None:
        said = numbers_in(ctx.user_text)
        for match, sets in resolved:
            made_up = sorted({s["reps"] for s in sets if s.get("reps") is not None and s["reps"] not in said})
            if made_up:
                raise ToolError(f"The user didn't say {made_up} reps for {match['exercise_name']}. Nothing saved; "
                                "ask how many sets and reps they did.")

    # The same sets already saved from an earlier message (e.g. the user just said "yes lat pulldown")?
    if not confirm_duplicate:
        dupes = [m["exercise_name"] for m, sets in resolved if sets and _already_logged(ctx, m, sets, d)]
        if dupes:
            return {"status": "not_logged", "reason": "already_logged", "exercises": dupes,
                    "instruction": "These exact sets were already saved from an earlier message, so nothing was "
                                   "saved again. Tell the user they're already logged. Only if they clearly say "
                                   "they did them again, call log_workout with confirm_duplicate=true."}

    for match, sets in resolved:
        if not sets:
            raise ToolError(f"No sets given for {match['exercise_name']}.")
        for s in sets:
            if s.get("reps") is None and s.get("duration_sec") is None and s.get("distance_km") is None:
                raise ToolError(f"Each set of {match['exercise_name']} needs reps, duration or distance.")

    # 2. Join today's open session or start a new one.
    session = _find_joinable_session(ctx, d, start_new_session, workout_name)
    joined = session is not None
    if not session:
        session = fetch_one(conn,
            """insert into workout_sessions (user_id, log_date, workout_name, duration_min, notes, message_id)
               values (%s, %s, %s, %s, %s, %s) returning *""",
            (ctx.user_id, d, workout_name if workout_name in WORKOUT_NAMES else "other",
             duration_min, notes, ctx.message_id))
    else:
        conn.execute(
            """update workout_sessions set
                 duration_min = coalesce(%s, duration_min),
                 notes = case when %s::text is null then notes
                              else concat_ws('; ', notes, %s::text) end
               where workout_session_id = %s""",
            (duration_min, notes, notes, session["workout_session_id"]))
    sid = session["workout_session_id"]
    update_state(conn, ctx.user_id, active_workout_session_id=sid)

    # 3. Save sets, numbering after any sets already in this session; check PRs.
    logged = []
    for match, sets in resolved:
        best_before = _previous_best(conn, ctx.user_id, match["exercise_id"])
        had_history = best_before["best_weight_kg"] is not None or best_before["best_reps"] is not None
        last_time = _last_session_sets(conn, ctx.user_id, match["exercise_id"], sid)
        start_no = fetch_one(conn,
            """select count(*) as n from workout_sets
               where workout_session_id = %s and exercise_id = %s and deleted_at is null""",
            (sid, match["exercise_id"]))["n"]
        rows = []
        for i, s in enumerate(sets, start=1):
            weight_kg = to_kg(s.get("weight"), s.get("weight_unit"))
            if match["exercise_type"] != "strength" and not s.get("weight"):
                weight_kg = None
            row = fetch_one(conn,
                """insert into workout_sets (workout_session_id, user_id, log_date, exercise_id,
                       exercise_name, set_number, weight_kg, reps, duration_sec, distance_km, message_id)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
                (sid, ctx.user_id, d, match["exercise_id"], match["exercise_name"], start_no + i,
                 weight_kg, s.get("reps"), s.get("duration_sec"), s.get("distance_km"), ctx.message_id))
            rows.append(row)

        pr = None
        if had_history:
            if match["exercise_type"] == "strength":
                top = max(rows, key=lambda r: (r["weight_kg"] or 0, r["reps"] or 0))
                prev = best_before["best_weight_kg"] or Decimal(0)
                if top["weight_kg"] is not None and top["weight_kg"] > prev:
                    pr = {"type": "heaviest_weight", "new_kg": num(top["weight_kg"]),
                          "previous_kg": num(prev), "workout_set_id": top["workout_set_id"]}
            elif match["exercise_type"] == "bodyweight":
                top = max(rows, key=lambda r: r["reps"] or 0)
                prev = best_before["best_reps"] or 0
                if top["reps"] is not None and top["reps"] > prev:
                    pr = {"type": "most_reps", "new_reps": top["reps"], "previous_reps": prev,
                          "workout_set_id": top["workout_set_id"]}
            if pr:
                conn.execute("update workout_sets set is_personal_record = true where workout_set_id = %s",
                             (pr["workout_set_id"],))

        logged.append({
            "exercise_name": match["exercise_name"],
            "sets": [{"workout_set_id": r["workout_set_id"], "set_number": r["set_number"],
                      "weight_kg": num(r["weight_kg"]), "reps": r["reps"],
                      "duration_sec": r["duration_sec"], "distance_km": num(r["distance_km"])}
                     for r in rows],
            "personal_record": pr,
            "first_time_logged": not had_history,
            "last_time": last_time,
        })

    remember_for_undo(ctx)
    result = {"status": "logged", "workout_session_id": sid,
              "workout_name": session["workout_name"], "log_date": str(d),
              "joined_existing_session": joined, "exercises": logged, "learned_nicknames": learned,
              "display_unit": ctx.user.get("unit_preference") or "kg"}
    if unknown:
        # Save what we recognised; ask only about the rest.
        result.update(status="partially_logged", unknown=unknown, needs_reply=True,
                      instruction="The app has already confirmed the saved exercises to the user. Don't repeat "
                                  "them. " + unknown_instruction)
    return result


# ───────────────────────── body weight ─────────────────────────

def log_weight(ctx: ToolContext, weight, unit="kg", log_date=None, input_text=None):
    d = parse_log_date(ctx, log_date)
    # Grounding: the weight must be a number the user actually wrote (stops "7.4" quietly becoming 74).
    if ctx.user_text is not None and not any(abs(float(weight) - n) < 0.01 for n in decimals_in(ctx.user_text)):
        raise ToolError(f"The user didn't write {weight}. Nothing saved; ask them to confirm their weight.")
    unit = "lb" if str(unit).lower() in ("lb", "lbs", "pound", "pounds") else "kg"
    kg = to_kg(weight, unit)
    if not 20 <= kg <= 300:
        raise ToolError(f"{weight} {unit} doesn't look like a body weight.")
    row = fetch_one(ctx.conn,
        """insert into weight_logs (user_id, log_date, input_text, input_unit, weight_kg, message_id)
           values (%s, %s, %s, %s, %s, %s) returning weight_log_id, weight_kg, weight_lb""",
        (ctx.user_id, d, input_text or f"{weight} {unit}", unit, kg, ctx.message_id))
    prev = fetch_one(ctx.conn,
        """select log_date, weight_kg from weight_logs
           where user_id = %s and deleted_at is null and weight_log_id <> %s and log_date <= %s
           order by log_date desc, weight_log_id desc limit 1""",
        (ctx.user_id, row["weight_log_id"], d))
    week_ago = fetch_one(ctx.conn,
        """select log_date, weight_kg from weight_logs
           where user_id = %s and deleted_at is null and log_date <= %s
           order by log_date desc limit 1""",
        (ctx.user_id, d - timedelta(days=7)))
    remember_for_undo(ctx)
    return {
        "status": "logged", "weight_log_id": row["weight_log_id"], "log_date": str(d),
        "weight_kg": num(row["weight_kg"]), "weight_lb": num(row["weight_lb"]),
        "change_since_last_kg": None if not prev else num(row["weight_kg"] - prev["weight_kg"]),
        "last_weigh_in_date": None if not prev else str(prev["log_date"]),
        "change_vs_week_ago_kg": None if not week_ago else num(row["weight_kg"] - week_ago["weight_kg"]),
    }


# ───────────────────────── edit / delete / undo ─────────────────────────

EDITABLE = {
    "workout_sets": {"weight", "weight_unit", "reps", "duration_sec", "distance_km", "exercise_name"},
    "meal_items": {"food_name", "quantity", "unit", "estimated_grams", "calories_kcal",
                   "protein_g", "carbs_g", "fat_g", "fiber_g"},
    "weight_logs": {"weight", "weight_unit"},
}
ID_COLUMN = {"meals": "meal_id", "meal_items": "meal_item_id", "workout_sessions": "workout_session_id",
             "workout_sets": "workout_set_id", "weight_logs": "weight_log_id"}


def _get_owned(ctx, table, record_id):
    if table not in ID_COLUMN:
        raise ToolError(f"Unknown table {table}")
    row = fetch_one(ctx.conn,
        f"select * from {table} where {ID_COLUMN[table]} = %s and user_id = %s and deleted_at is null",
        (record_id, ctx.user_id))
    if not row:
        raise ToolError(f"No {table} entry with id {record_id} for this user.")
    return row


STOP_WORDS = {"the", "my", "a", "an", "of", "that", "this", "last", "set", "sets", "first", "second", "third"}


def _tokens(text):
    return {t for t in _norm(text).split() if t not in STOP_WORDS and len(t) >= 2}


def _check_entry_name(ctx, table, row, entry_name):
    """Refuse to edit a row that isn't the thing the user named (protects against guessed IDs)."""
    if table == "weight_logs" or not entry_name:
        return
    if table == "workout_sets":
        ex = fetch_one(ctx.conn, "select exercise_name, aliases from exercises where exercise_id = %s", (row["exercise_id"],))
        names = [ex["exercise_name"], *ex["aliases"]] if ex else [row["exercise_name"]]
        label = row["exercise_name"]
    elif table == "meal_items":
        names, label = [row["food_name"]], row["food_name"]
    elif table == "meals":
        names, label = [row["meal_description"], row.get("meal_type") or ""], row["meal_description"]
    else:  # workout_sessions
        names, label = [row["workout_name"]], row["workout_name"]
    wanted = _tokens(entry_name)
    for n in names:
        have = _tokens(n)
        if not wanted or wanted & have or _norm(entry_name) in _norm(n) or _norm(n) in _norm(entry_name):
            return
        if any(difflib.SequenceMatcher(None, w, h).ratio() >= 0.8 for w in wanted for h in have):
            return
    raise ToolError(f"{table} {row[ID_COLUMN[table]]} is '{label}', not '{entry_name}'. Nothing changed. "
                    "Call get_logs (with the right log_date) to find the correct ID.")


def _same_value(a, b) -> bool:
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a) == str(b)


def update_entry(ctx: ToolContext, table, record_id, changes, reason="other", entry_name=None):
    if table not in EDITABLE:
        raise ToolError(f"Can only edit {sorted(EDITABLE)}")
    changes = {k: v for k, v in (changes or {}).items() if k in EDITABLE[table] and v is not None}
    if not changes:
        raise ToolError("No valid changes given.")
    row = _get_owned(ctx, table, record_id)
    _check_entry_name(ctx, table, row, entry_name)
    updates = {}
    if table in ("workout_sets", "weight_logs") and "weight" in changes:
        unit = changes.get("weight_unit") or "kg"
        updates["weight_kg"] = to_kg(changes["weight"], unit)
        if table == "weight_logs":
            updates["input_unit"] = "lb" if unit.lower().startswith("lb") else "kg"
    if table == "workout_sets":
        for k in ("reps", "duration_sec", "distance_km"):
            if k in changes:
                updates[k] = changes[k]
        if "exercise_name" in changes:
            match = resolve_exercise(load_exercises(ctx.conn, ctx.user_id), changes["exercise_name"])
            if not match:
                raise ToolError(f"'{changes['exercise_name']}' isn't in the exercise list. Ask to add it first.")
            updates["exercise_id"] = match["exercise_id"]
            updates["exercise_name"] = match["exercise_name"]
    if table == "meal_items":
        for k in EDITABLE["meal_items"]:
            if k in changes:
                updates[k] = changes[k]
        if updates.get("unit") and updates["unit"] not in FOOD_UNITS:
            updates["unit"] = "piece"
        # "make it 10 idli": scale calories, macros and grams with the quantity unless new values were given.
        nutrition = ("calories_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "estimated_grams")
        old_q, new_q = row.get("quantity"), updates.get("quantity")
        if new_q is not None and old_q and float(old_q) > 0 and float(new_q) != float(old_q) \
                and not any(k in changes for k in nutrition[:5]):
            factor = float(new_q) / float(old_q)
            for k in nutrition:
                if row.get(k) is not None:
                    updates[k] = round(float(row[k]) * factor, 1)
    # Drop "changes" that don't change anything (e.g. quantity 10 → 10).
    updates = {k: v for k, v in updates.items()
               if not _same_value(row.get(k), v)}
    if not updates:
        return {"status": "updated", "table": table, "record_id": record_id, "label": row.get("food_name")
                or row.get("exercise_name") or "entry", "before": {}, "after": {}, "unchanged": True}

    ids = [record_id]
    if table == "workout_sets" and "exercise_id" in updates:
        # "that was incline, not bench" is about the exercise, so fix every set of it logged together.
        ids = [r["workout_set_id"] for r in fetch_all(ctx.conn,
            """select workout_set_id from workout_sets
               where workout_session_id = %s and exercise_id = %s and message_id is not distinct from %s
                 and user_id = %s and deleted_at is null""",
            (row["workout_session_id"], row["exercise_id"], row["message_id"], ctx.user_id))] or [record_id]
    sets = ", ".join(f"{k} = %s" for k in updates)
    ctx.conn.execute(f"update {table} set {sets} where {ID_COLUMN[table]} = any(%s)",
                     (*updates.values(), ids))
    for rid in ids:
        for k, v in updates.items():
            if str(row.get(k)) != str(v):
                record_correction(ctx, table, rid, k, row.get(k), v, reason)
    label = {"workout_sets": (f"{row.get('exercise_name')} ({len(ids)} sets)" if len(ids) > 1
                              else f"{row.get('exercise_name')} set {row.get('set_number')}"),
             "meal_items": row.get("food_name"), "weight_logs": "weigh-in"}[table]
    result = {"status": "updated", "table": table, "record_id": record_id, "label": label,
              "before": {k: (num(row[k]) if isinstance(row.get(k), Decimal) else row.get(k)) for k in updates},
              "after": updates}
    if table == "meal_items":
        recompute_meal_totals(ctx.conn, row["meal_id"])
        result["day_so_far"] = day_nutrition(ctx.conn, ctx.user_id, row["log_date"])
    return result


def delete_entry(ctx: ToolContext, table, record_id, reason="other", entry_name=None):
    row = _get_owned(ctx, table, record_id)
    _check_entry_name(ctx, table, row, entry_name)
    conn = ctx.conn
    conn.execute(f"update {table} set deleted_at = now() where {ID_COLUMN[table]} = %s", (record_id,))
    if table == "meals":
        conn.execute("update meal_items set deleted_at = now() where meal_id = %s and deleted_at is null",
                     (record_id,))
    if table == "workout_sessions":
        conn.execute("update workout_sets set deleted_at = now() where workout_session_id = %s and deleted_at is null",
                     (record_id,))
    if table == "meal_items":
        recompute_meal_totals(conn, row["meal_id"])
    record_correction(ctx, table, record_id, "row_deleted", "present", "deleted", reason)
    return {"status": "deleted", "table": table, "record_id": record_id}


def undo_last(ctx: ToolContext):
    state = get_state(ctx.conn, ctx.user_id)
    if state.get("undo_table_name") != "messages" or not state.get("undo_record_id"):
        return {"status": "nothing_to_undo"}
    mid = state["undo_record_id"]
    conn = ctx.conn
    removed = {}
    for table in ("meal_items", "meals", "workout_sets", "workout_sessions", "weight_logs"):
        rows = fetch_all(conn,
            f"""update {table} set deleted_at = now()
                where user_id = %s and message_id = %s and deleted_at is null
                returning {ID_COLUMN[table]} as id""", (ctx.user_id, mid))
        if rows:
            removed[table] = len(rows)
            for r in rows:
                record_correction(ctx, table, r["id"], "row_deleted", "present", "deleted", "user_changed_mind")
    # Sessions removed → remove their remaining sets too; meals touched → refresh totals.
    conn.execute(
        """update workout_sets s set deleted_at = now() from workout_sessions w
           where s.workout_session_id = w.workout_session_id and w.message_id = %s
             and w.user_id = %s and s.deleted_at is null""", (mid, ctx.user_id))
    for m in fetch_all(conn, "select distinct meal_id from meal_items where message_id = %s and user_id = %s",
                       (mid, ctx.user_id)):
        recompute_meal_totals(conn, m["meal_id"])
    update_state(conn, ctx.user_id, undo_table_name=None, undo_record_id=None)
    return {"status": "undone" if removed else "nothing_to_undo", "removed_counts": removed}


# ───────────────────────── snapshot for context ─────────────────────────

def today_snapshot(conn, user_id, on_date: date) -> dict:
    """Compact view of the day's logs, with IDs, so the agent can edit them."""
    meals = fetch_all(conn,
        """select meal_id, meal_number, meal_type, meal_description, total_calories_kcal
           from meals where user_id = %s and log_date = %s and deleted_at is null order by meal_number""",
        (user_id, on_date))
    items = fetch_all(conn,
        """select meal_item_id, meal_id, food_name, quantity, unit, calories_kcal, protein_g
           from meal_items where user_id = %s and log_date = %s and deleted_at is null order by meal_item_id""",
        (user_id, on_date))
    sessions = fetch_all(conn,
        """select workout_session_id, workout_name from workout_sessions
           where user_id = %s and log_date = %s and deleted_at is null order by workout_session_id""",
        (user_id, on_date))
    sets = fetch_all(conn,
        """select workout_set_id, workout_session_id, exercise_name, set_number, weight_kg, reps,
                  duration_sec, distance_km
           from workout_sets where user_id = %s and log_date = %s and deleted_at is null
           order by workout_set_id""", (user_id, on_date))
    weights = fetch_all(conn,
        """select weight_log_id, weight_kg from weight_logs
           where user_id = %s and log_date = %s and deleted_at is null""", (user_id, on_date))
    return {
        "meals": [{"meal_id": m["meal_id"], "meal_number": m["meal_number"], "meal_type": m["meal_type"],
                   "description": m["meal_description"], "kcal": num(m["total_calories_kcal"]),
                   "items": [{"meal_item_id": i["meal_item_id"], "food_name": i["food_name"],
                              "quantity": num(i["quantity"]), "unit": i["unit"],
                              "kcal": num(i["calories_kcal"]), "protein_g": num(i["protein_g"])}
                             for i in items if i["meal_id"] == m["meal_id"]]} for m in meals],
        "workout_sessions": [{"workout_session_id": s["workout_session_id"], "workout_name": s["workout_name"],
                              "sets": [{"workout_set_id": x["workout_set_id"], "exercise_name": x["exercise_name"],
                                        "set_number": x["set_number"], "weight_kg": num(x["weight_kg"]),
                                        "reps": x["reps"], "duration_sec": x["duration_sec"],
                                        "distance_km": num(x["distance_km"])}
                                       for x in sets if x["workout_session_id"] == s["workout_session_id"]]}
                             for s in sessions],
        "weight_logs": [{"weight_log_id": w["weight_log_id"], "weight_kg": num(w["weight_kg"])} for w in weights],
        "nutrition": day_nutrition(conn, user_id, on_date),
    }


def get_logs(ctx: ToolContext, log_date=None):
    """Full list of a day's entries with IDs — used before editing older entries."""
    d = parse_log_date(ctx, log_date)
    return {"status": "ok", "log_date": str(d), **today_snapshot(ctx.conn, ctx.user_id, d)}


def _fmt_w(kg):
    return "" if kg is None else f"{num(kg):g}kg"


def last_logged_lines(conn, user_id, message_id) -> list[str]:
    """One short line per entry saved from the given message, with table + ID for corrections."""
    if not message_id:
        return []
    lines = []
    for r in fetch_all(conn,
            """select meal_item_id, food_name, quantity, unit, calories_kcal from meal_items
               where user_id = %s and message_id = %s and deleted_at is null order by meal_item_id""",
            (user_id, message_id)):
        lines.append(f"meal_items {r['meal_item_id']}: {num(r['quantity']):g} {r['unit']} {r['food_name']} ({num(r['calories_kcal']):g} kcal)")
    for r in fetch_all(conn,
            """select workout_set_id, exercise_name, set_number, weight_kg, reps, duration_sec, distance_km
               from workout_sets where user_id = %s and message_id = %s and deleted_at is null
               order by workout_set_id""", (user_id, message_id)):
        detail = " ".join(x for x in [_fmt_w(r["weight_kg"]),
                                       f"x{r['reps']}" if r["reps"] is not None else "",
                                       f"{r['duration_sec']}s" if r["duration_sec"] else "",
                                       f"{num(r['distance_km']):g}km" if r["distance_km"] else ""] if x)
        lines.append(f"workout_sets {r['workout_set_id']}: {r['exercise_name']} set {r['set_number']} {detail}")
    for r in fetch_all(conn,
            "select weight_log_id, weight_kg from weight_logs where user_id = %s and message_id = %s and deleted_at is null",
            (user_id, message_id)):
        lines.append(f"weight_logs {r['weight_log_id']}: {_fmt_w(r['weight_kg'])}")
    return lines[:15]


def day_summary_line(conn, user_id, on_date) -> str:
    n = day_nutrition(conn, user_id, on_date)
    e, t = n["eaten"], n["targets"]
    sessions = fetch_all(conn,
        """select w.workout_name, count(s.*) as sets from workout_sessions w
           left join workout_sets s on s.workout_session_id = w.workout_session_id and s.deleted_at is null
           where w.user_id = %s and w.log_date = %s and w.deleted_at is null group by w.workout_session_id""",
        (user_id, on_date))
    weight = fetch_one(conn,
        "select weight_kg from weight_logs where user_id = %s and log_date = %s and deleted_at is null order by weight_log_id desc limit 1",
        (user_id, on_date))
    workout = ", ".join(f"{s['workout_name']} ({s['sets']} sets)" for s in sessions) or "none"
    return (f"{n['meals_logged']} meals, {e['calories_kcal']:g}/{t['calories_kcal'] or '?'} kcal, "
            f"protein {e['protein_g']:g}/{t['protein_g'] or '?'} g · workout: {workout} · "
            f"weight: {_fmt_w(weight['weight_kg']) if weight else 'not logged'}")


def recent_logged_lines(conn, user_id, on_date, messages=3, max_lines=15) -> list[str]:
    """Entries from the user's last few logging messages today, newest first."""
    ids = fetch_all(conn,
        """select message_id from (
             select message_id, max(created_at) as at from (
               select message_id, created_at from meal_items where user_id = %(u)s and log_date = %(d)s and deleted_at is null
               union all select message_id, created_at from workout_sets where user_id = %(u)s and log_date = %(d)s and deleted_at is null
               union all select message_id, created_at from weight_logs where user_id = %(u)s and log_date = %(d)s and deleted_at is null
             ) x where message_id is not null group by message_id) y
           order by at desc limit %(n)s""", {"u": user_id, "d": on_date, "n": messages})
    lines = []
    for r in ids:
        lines += last_logged_lines(conn, user_id, r["message_id"])
    return lines[:max_lines]
