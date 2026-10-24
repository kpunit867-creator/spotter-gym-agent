"""Confirmation messages written by code from what was actually saved.

Gemini decides *what* to log; these messages say *what got logged*, so every number
in a confirmation comes straight from the database. At most one emoji per message.
"""
import random
from datetime import date

LB_PER_KG = 2.20462

WORKOUT_LABEL = {"push": "push", "pull": "pull", "legs": "leg", "arms": "arms", "chest": "chest",
                 "back": "back", "shoulders": "shoulder", "core": "core", "cardio": "cardio",
                 "full_body": "full-body", "other": ""}
MEAL_LABEL = {"breakfast": "Breakfast", "lunch": "Lunch", "snack": "Snack", "dinner": "Dinner",
              "pre_workout": "Pre-workout meal", "post_workout": "Post-workout meal"}


def _n(v):
    """60.0 → '60', 62.5 → '62.5'"""
    return f"{round(float(v), 1):g}"


def _weight(kg, unit):
    if kg is None:
        return None
    return f"{_n(kg * LB_PER_KG)} lb" if unit == "lb" else f"{_n(kg)} kg"


def _sets_text(sets, unit):
    """'60 kg × 10, 10, 8' — groups consecutive sets at the same weight."""
    groups, out = [], []
    for s in sets:
        key = s.get("weight_kg")
        if groups and groups[-1][0] == key:
            groups[-1][1].append(s)
        else:
            groups.append((key, [s]))
    for kg, ss in groups:
        parts = []
        for s in ss:
            if s.get("reps") is not None:
                parts.append(str(s["reps"]))
            elif s.get("duration_sec"):
                secs = s["duration_sec"]
                parts.append(f"{secs // 60} min" if secs >= 120 else f"{secs}s")
            if s.get("distance_km"):
                parts[-1:] = [" ".join(p for p in [*parts[-1:], f"{_n(s['distance_km'])} km"] if p)]
        w = _weight(kg, unit)
        has_reps = any(s.get("reps") is not None for s in ss)
        if w and has_reps:
            out.append(f"{w} × {', '.join(parts)}")
        else:
            out.append(", ".join(parts) + (" reps" if has_reps and not any(s.get("duration_sec") for s in ss) else ""))
    return " · ".join(out)


def _volume(sets):
    return sum((s.get("weight_kg") or 0) * (s.get("reps") or 0) for s in sets)


def _pretty_date(iso):
    d = date.fromisoformat(iso)
    return d.strftime("%a %d %b").replace(" 0", " ")


def workout(r: dict) -> str:
    unit = r.get("display_unit", "kg")
    label = WORKOUT_LABEL.get(r["workout_name"], "")
    session = f"{label} session".strip() if label else "workout"
    if r.get("joined_existing_session"):
        head = f"Added to today's {session}"
    else:
        head = random.choice([f"Logged your {session}", f"{session[0].upper()}{session[1:]} logged",
                              f"Got it, {session} logged"])
    lines = [head]
    notes, emoji = [], ""
    for ex in r["exercises"]:
        lines.append(f"• {ex['exercise_name']}: {_sets_text(ex['sets'], unit)}")
        pr, last = ex.get("personal_record"), ex.get("last_time")
        if pr and pr["type"] == "heaviest_weight":
            notes.append(f"New PR on {ex['exercise_name']}: *{_weight(pr['new_kg'], unit)}* "
                         f"(was {_weight(pr['previous_kg'], unit)}){'' if emoji else ' 🔥'}")
            emoji = "used"
        elif pr and pr["type"] == "most_reps":
            notes.append(f"New rep PR on {ex['exercise_name']}: *{pr['new_reps']} reps* "
                         f"(was {pr['previous_reps']}){'' if emoji else ' 🔥'}")
            emoji = "used"
        elif last and _volume(ex["sets"]) > _volume(last["sets"]) > 0:
            notes.append(f"More volume on {ex['exercise_name']} than last time ({_pretty_date(last['log_date'])})")
        elif ex.get("first_time_logged") and len(r["exercises"]) == 1:
            notes.append("First time logging this one, so this is your baseline.")
    for learned in r.get("learned_nicknames") or []:
        notes.append(f"_I'll remember \"{learned['wording']}\" means {learned['exercise_name']}._")
    msg = "\n".join(lines)
    if notes:
        msg += "\n\n" + "\n".join(notes[:3])
    return msg


def meal(r: dict) -> str:
    t = r["meal_totals"]
    label = MEAL_LABEL.get(r.get("meal_type"), f"Meal {r['meal_number']}")
    lines = [f"{label} logged, about *{_n(t['calories_kcal'])} kcal*"]
    for it in r["items"]:
        lines.append(f"• {_n(it['quantity'])} {_unit(it)}{it['food_name']} ({_n(it['calories_kcal'] or 0)} kcal)")
    lines.append(f"P {_n(t['protein_g'])}g · C {_n(t['carbs_g'])}g · F {_n(t['fat_g'])}g · Fibre {_n(t['fiber_g'])}g")

    day = r["day_so_far"]
    eaten, target, remaining = day["eaten"], day["targets"], day["remaining"]
    if target.get("calories_kcal"):
        lines.append("")
        lines.append(f"Today: {_n(eaten['calories_kcal'])} / {target['calories_kcal']} kcal · "
                     f"protein {_n(eaten['protein_g'])} / {target['protein_g']} g")
        if remaining["calories_kcal"] is not None and remaining["calories_kcal"] < 0:
            lines.append(f"That's {-remaining['calories_kcal']} kcal over today's target.")
        elif remaining["protein_g"] and remaining["protein_g"] >= 25:
            lines.append(f"Still ~{remaining['protein_g']} g protein to go.")
    return "\n".join(lines)


def _unit(item):
    u = item.get("unit")
    if u in (None, "piece"):
        return ""
    if u in ("g", "ml"):
        return f"{u} "
    return f"{u}{'' if float(item.get('quantity') or 1) == 1 else 's'} "


def weight(r: dict, unit="kg") -> str:
    main = f"{_n(r['weight_lb'])} lb" if unit == "lb" else f"{_n(r['weight_kg'])} kg"
    other = f"{_n(r['weight_kg'])} kg" if unit == "lb" else f"{_n(r['weight_lb'])} lb"
    msg = f"Weight logged: *{main}* ({other})"
    week = r.get("change_vs_week_ago_kg")
    last = r.get("change_since_last_kg")
    if week is not None and abs(week) >= 0.1:
        msg += f"\n{'Down' if week < 0 else 'Up'} {_weight(abs(week), unit)} from a week ago."
    elif last is not None and abs(last) >= 0.1 and r.get("last_weigh_in_date"):
        when = ("earlier today" if r["last_weigh_in_date"] == r["log_date"]
                else _pretty_date(r["last_weigh_in_date"]))
        msg += f"\n{'Down' if last < 0 else 'Up'} {_weight(abs(last), unit)} since {when}."
    elif last is None:
        msg += "\nFirst weigh-in logged, so this is your starting point."
    return msg


FIELD_LABEL = {"weight_kg": "weight", "reps": "reps", "duration_sec": "time", "distance_km": "distance",
               "exercise_name": "exercise", "food_name": "food", "quantity": "quantity", "unit": "unit",
               "calories_kcal": "calories", "protein_g": "protein", "carbs_g": "carbs", "fat_g": "fat",
               "fiber_g": "fibre", "estimated_grams": "grams"}


MAIN_FIELDS = ["exercise_name", "weight_kg", "reps", "duration_sec", "distance_km", "food_name",
               "quantity", "unit", "estimated_grams"]


def updated(r: dict) -> str:
    before, after = r["before"], r["after"]
    changed = [k for k in after if str(before.get(k)) != str(after[k])]
    main = [k for k in MAIN_FIELDS if k in changed] or changed
    parts = []
    for k in main:
        old, new = before.get(k), after[k]
        if k == "weight_kg":
            old, new = f"{_fmt(old)} kg", f"{_fmt(new)} kg"
        label = "" if k == "quantity" else f"{FIELD_LABEL.get(k, k)} "
        parts.append(f"{label}{_fmt(old)} → {_fmt(new)}")
    if "calories_kcal" in changed and "calories_kcal" not in main:
        parts.append(f"({_fmt(before.get('calories_kcal'))} → {_fmt(after['calories_kcal'])} kcal)")
    msg = f"Fixed {r.get('label') or 'that entry'}: " + " ".join(parts) if parts else "Updated."
    day = r.get("day_so_far")
    if day and day["targets"].get("calories_kcal"):
        msg += (f"\nToday: {_n(day['eaten']['calories_kcal'])} / {day['targets']['calories_kcal']} kcal · "
                f"protein {_n(day['eaten']['protein_g'])} / {day['targets']['protein_g']} g")
    return msg


def _fmt(v):
    if isinstance(v, (int, float)):
        return _n(v)
    return str(v)


TABLE_LABEL = {"meals": "meal", "meal_items": "food item", "workout_sessions": "workout",
               "workout_sets": "set", "weight_logs": "weigh-in"}


def deleted(r: dict) -> str:
    return f"Removed that {TABLE_LABEL.get(r['table'], 'entry')}."


def undone(r: dict) -> str:
    if r["status"] == "nothing_to_undo":
        return "There's nothing recent to undo."
    c = r["removed_counts"]
    bits = []
    if c.get("meals"):
        bits.append(f"{c['meals']} meal{'s' * (c['meals'] > 1)}")
    elif c.get("meal_items"):
        bits.append(f"{c['meal_items']} food item{'s' * (c['meal_items'] > 1)}")
    if c.get("workout_sets"):
        bits.append(f"{c['workout_sets']} set{'s' * (c['workout_sets'] > 1)}")
    if c.get("weight_logs"):
        bits.append("the weigh-in")
    if not bits:
        return "Undone."
    listed = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
    return f"Undone. Removed {listed}."


def for_tool(name: str, result: dict, user: dict) -> str | None:
    """The confirmation for one finished tool, or None if Gemini needs to respond instead."""
    status = result.get("status")
    unit = user.get("unit_preference") or "kg"
    if name == "log_workout" and status in ("logged", "partially_logged"):
        return workout(result)
    if name == "log_meal" and status == "logged":
        return meal(result)
    if name == "log_weight" and status == "logged":
        return weight(result, unit)
    if name == "update_entry" and status == "updated":
        return updated(result)
    if name == "delete_entry" and status == "deleted":
        return deleted(result)
    if name == "undo_last" and status in ("undone", "nothing_to_undo"):
        return undone(result)
    if name == "update_targets" and status == "targets_updated":
        return targets_updated(result)
    if name == "update_profile" and status == "profile_updated":
        return profile_updated(result)
    if name == "get_weekly_report" and status == "report_ready":
        return weekly_report(result)
    return None


# ───────────────────────── weekly report ─────────────────────────

MUSCLE_LABEL = {"full_body": "full body"}


def _short_date(iso):
    return date.fromisoformat(iso).strftime("%d %b").lstrip("0")


def _lift(b, unit):
    return f"{_weight(b['weight_kg'], unit)} × {b['reps']}"


def weekly_report(r: dict) -> str:
    unit = r.get("display_unit", "kg")
    title = ("Last week" if r["period"] == "last_week" else
             f"Week of {_short_date(r['start'])} ({r['weeks_ago']} weeks ago)" if r.get("weeks_ago") else
             "Your last 7 days")
    lines = [f"*{title}* · {_short_date(r['start'])} – {_short_date(r['end'])}", ""]

    # Targets
    lines.append("*Targets*")
    d = r["diet"]
    if d["days_logged"] == 0:
        lines.append("➖ Diet: no meals logged")
    else:
        mark = "✅" if d["goal_met"] else "❌"
        lines.append(f"{mark} Diet: target hit on {d['days_target_met']} of 7 days")
        if d["target_calories_kcal"]:
            lines.append(f"     Avg {d['avg_calories_kcal']:,} kcal (target {d['target_calories_kcal']:,}) · "
                         f"protein {d['avg_protein_g']} g (target {d['target_protein_g']})")
        if d["days_logged"] < 7:
            lines.append(f"     Meals logged on {d['days_logged']} of 7 days")
    s = r["sessions"]
    if s["target"]:
        mark = "✅" if s["goal_met"] else "❌"
        lines.append(f"{mark} Workouts: {s['done']} of {s['target']} sessions")
    else:
        lines.append(f"➖ Workouts: {s['done']} sessions")

    # Training
    v = r["volume"]
    lines += ["", "*Training*"]
    if v["this_week_kg"]:
        change = ""
        if v["change_pct"] is not None:
            arrow = "▲" if v["change_pct"] > 0 else "▼" if v["change_pct"] < 0 else "="
            change = f" ({arrow} {abs(v['change_pct'])}% vs previous week)"
        elif not v["last_week_kg"]:
            change = " (no data for previous week)"
        vol = v["this_week_kg"] * LB_PER_KG if unit == "lb" else v["this_week_kg"]
        lines.append(f"Total volume (weight × reps): {round(vol):,} {unit}{change}")
    else:
        lines.append("No weight training logged.")
    if r["sets_by_muscle"]:
        lines.append("Sets: " + " · ".join(f"{MUSCLE_LABEL.get(m, m)} {n}" for m, n in r["sets_by_muscle"].items()))
    if r["personal_records"]:
        lines.append(f"PRs this week: {r['personal_records']}")

    # Key lifts
    if r["key_lifts"]:
        lines += ["", "*Key lifts* (best set)"]
        for l in r["key_lifts"]:
            name, now, before = l["exercise_name"], l["this_week"], l["last_week"]
            if l["trend"] == "not_done":
                lines.append(f"• {name}: not done (prev {_lift(before, unit)})")
            elif l["trend"] == "new":
                lines.append(f"• {name}: {_lift(now, unit)} (new)")
            else:
                arrow = {"up": "▲", "down": "▼", "same": "="}[l["trend"]]
                lines.append(f"• {name}: {_lift(now, unit)} {arrow} (prev {_lift(before, unit)})")

    # Body weight
    bw = r["body_weight"]
    if bw["avg_kg"]:
        lines += ["", f"*Body weight*: avg {_weight(bw['avg_kg'], unit)}"
                      + (f" ({'▼' if bw['change_kg'] < 0 else '▲' if bw['change_kg'] > 0 else '='} "
                         f"{_weight(abs(bw['change_kg']), unit)} vs previous week)" if bw["change_kg"] is not None else "")]
    return "\n".join(lines)


# ───────────────────────── targets / profile changes ─────────────────────────

TARGET_LABEL = {"daily_calories_kcal": ("Calories", "kcal"), "daily_protein_g": ("Protein", "g"),
                "daily_carbs_g": ("Carbs", "g"), "daily_fat_g": ("Fat", "g"), "daily_fiber_g": ("Fibre", "g"),
                "workouts_per_week": ("Workouts", "/week"), "target_weight_kg": ("Target weight", "kg")}
PROFILE_LABEL = {"goal": "Goal", "diet_preference": "Diet", "experience_level": "Experience",
                 "unit_preference": "Units", "name": "Name"}


def _target_lines(before, after):
    lines = []
    for k, (label, unit) in TARGET_LABEL.items():
        if k in after and after[k] is not None:
            old = before.get(k)
            changed = old is not None and str(old) != str(after[k]) and float(old) != float(after[k])
            val = f"{round(float(after[k])):,}" if k == "daily_calories_kcal" else _n(after[k])
            lines.append(f"• {label}: {val}{'' if unit.startswith('/') else ' '}{unit}" + (f" (was {_n(old)})" if changed else ""))
    return lines


def targets_updated(r):
    lines = ["Targets updated. From today:", *_target_lines(r["before"], r["after"])]
    if r.get("note"):
        lines.append(f"_{r['note'][0].upper()}{r['note'][1:]}._")
    return "\n".join(lines)


def profile_updated(r):
    nice = lambda v: str(v).replace("_", " ")  # noqa: E731
    lines = [f"{PROFILE_LABEL.get(k, k)} changed: {nice(r['before'].get(k))} → *{nice(v)}*" for k, v in r["after"].items()]
    if r.get("targets_after"):
        lines += ["", "New daily targets for that goal:", *_target_lines(r["targets_before"], r["targets_after"])]
    return "\n".join(lines)


def onboarding_complete(user: dict, result: dict) -> str:
    t = result["daily_targets"]
    name = (user.get("name") or "").split()[0] if user.get("name") else "there"
    return "\n".join([
        f"You're all set, {name}! 🎉 Your daily targets:",
        f"• Calories: {t['daily_calories_kcal']:,} kcal",
        f"• Protein: {t['daily_protein_g']} g",
        f"• Carbs: {t['daily_carbs_g']} g · Fat: {t['daily_fat_g']} g",
        "",
        "These are starting estimates. Change them anytime, e.g. \"set my protein to 140\".",
        "",
        "Now just text me what you do, like:",
        "• bench 60kg 3x10",
        "• lunch 2 roti, dal, paneer sabzi",
        "• weight 72.5",
    ])
