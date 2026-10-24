"""Changing targets and profile after onboarding."""
from app.db import fetch_one
from app.onboarding import DIETS, GOALS, LEVELS, _norm_choice, calculate_targets
from app.tools import ToolContext, ToolError, current_targets, to_kg

LIMITS = {"daily_calories_kcal": (1000, 6000), "daily_protein_g": (20, 400), "daily_carbs_g": (0, 900),
          "daily_fat_g": (10, 350), "daily_fiber_g": (0, 100), "workouts_per_week": (0, 7),
          "target_weight_kg": (30, 250)}


def _save_targets(ctx, new: dict) -> dict:
    """Targets keep history: today's row is replaced, earlier days keep the targets they had."""
    conn, uid = ctx.conn, ctx.user_id
    conn.execute(
        """insert into user_targets (user_id, daily_calories_kcal, daily_protein_g, daily_carbs_g, daily_fat_g,
               daily_fiber_g, workouts_per_week, target_weight_kg, start_date)
           values (%(u)s, %(daily_calories_kcal)s, %(daily_protein_g)s, %(daily_carbs_g)s, %(daily_fat_g)s,
                   %(daily_fiber_g)s, %(workouts_per_week)s, %(target_weight_kg)s, %(d)s)
           on conflict (user_id, start_date) do update set
               daily_calories_kcal = excluded.daily_calories_kcal, daily_protein_g = excluded.daily_protein_g,
               daily_carbs_g = excluded.daily_carbs_g, daily_fat_g = excluded.daily_fat_g,
               daily_fiber_g = excluded.daily_fiber_g, workouts_per_week = excluded.workouts_per_week,
               target_weight_kg = excluded.target_weight_kg""",
        {**new, "u": uid, "d": ctx.today})
    return new


def update_targets(ctx: ToolContext, daily_calories_kcal=None, daily_protein_g=None, daily_carbs_g=None,
                   daily_fat_g=None, daily_fiber_g=None, workouts_per_week=None, target_weight=None,
                   target_weight_unit="kg"):
    old = current_targets(ctx.conn, ctx.user_id, ctx.today) or {}
    keys = ["daily_calories_kcal", "daily_protein_g", "daily_carbs_g", "daily_fat_g", "daily_fiber_g",
            "workouts_per_week", "target_weight_kg"]
    given = {"daily_calories_kcal": daily_calories_kcal, "daily_protein_g": daily_protein_g,
             "daily_carbs_g": daily_carbs_g, "daily_fat_g": daily_fat_g, "daily_fiber_g": daily_fiber_g,
             "workouts_per_week": workouts_per_week,
             "target_weight_kg": to_kg(target_weight, target_weight_unit) if target_weight is not None else None}
    given = {k: v for k, v in given.items() if v is not None}
    if not given:
        raise ToolError("No target values given.")
    for k, v in given.items():
        lo, hi = LIMITS[k]
        if not lo <= float(v) <= hi:
            raise ToolError(f"{k} = {v} is outside {lo}–{hi}. Nothing changed.")
    new = {k: given.get(k, old.get(k)) for k in keys}
    # If macros changed but calories weren't stated, calories follow the macros (4/4/9 kcal per gram).
    macros_changed = any(k in given for k in ("daily_protein_g", "daily_carbs_g", "daily_fat_g"))
    note = None
    if macros_changed and "daily_calories_kcal" not in given and all(new.get(k) is not None for k in
                                                                     ("daily_protein_g", "daily_carbs_g", "daily_fat_g")):
        new["daily_calories_kcal"] = round(new["daily_protein_g"] * 4 + new["daily_carbs_g"] * 4 + new["daily_fat_g"] * 9)
        note = "calories recalculated from the new macros"
    _save_targets(ctx, new)
    return {"status": "targets_updated", "before": {k: old.get(k) for k in keys}, "after": new, "note": note}


def update_profile(ctx: ToolContext, goal=None, diet_preference=None, experience_level=None, unit_preference=None,
                   name=None, recalculate_targets=None):
    fields = {}
    if goal:
        fields["goal"] = _norm_choice(goal, GOALS)
    if diet_preference:
        fields["diet_preference"] = _norm_choice(diet_preference, DIETS)
    if experience_level:
        fields["experience_level"] = _norm_choice(experience_level, LEVELS)
    if unit_preference:
        fields["unit_preference"] = _norm_choice(unit_preference, ["kg", "lb"])
    if name:
        fields["name"] = str(name).strip()[:60]
    if not fields:
        raise ToolError("Nothing to change.")
    before = {k: ctx.user.get(k) for k in fields}
    sets = ", ".join(f"{k} = %s" for k in fields)
    ctx.conn.execute(f"update users set {sets} where user_id = %s", (*fields.values(), ctx.user_id))
    ctx.user = fetch_one(ctx.conn, "select * from users where user_id = %s", (ctx.user_id,))
    result = {"status": "profile_updated", "before": before, "after": fields}

    # A new goal changes the right calories/protein — recalculate unless told not to.
    if "goal" in fields and recalculate_targets is not False:
        weight = fetch_one(ctx.conn, """select weight_kg from weight_logs where user_id = %s and deleted_at is null
                                        order by log_date desc, weight_log_id desc limit 1""", (ctx.user_id,))
        old = current_targets(ctx.conn, ctx.user_id, ctx.today) or {}
        if weight and ctx.user.get("height_cm") and ctx.user.get("age"):
            calc = calculate_targets(ctx.user, weight["weight_kg"], old.get("workouts_per_week") or 3)
            new = {**{k: old.get(k) for k in LIMITS}, **calc}
            _save_targets(ctx, new)
            result["targets_before"] = {k: old.get(k) for k in calc}
            result["targets_after"] = calc
    return result
