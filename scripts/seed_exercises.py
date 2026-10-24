"""Run: python scripts/seed_exercises.py — adds the built-in exercise dictionary (safe to re-run)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_pool  # noqa: E402

# (exercise_name, muscle_group, exercise_type, aliases)
EXERCISES = [
    # Chest
    ("Barbell Bench Press", "chest", "strength", ["bench", "bench press", "bp", "flat bench", "flat bench press", "barbell bench"]),
    ("Incline Barbell Bench Press", "chest", "strength", ["incline bench", "incline bp", "incline barbell"]),
    ("Decline Bench Press", "chest", "strength", ["decline bench", "decline bp"]),
    ("Dumbbell Bench Press", "chest", "strength", ["db bench", "dumbbell bench", "flat db press", "db press"]),
    ("Incline Dumbbell Press", "chest", "strength", ["incline db", "incline db press", "incline dumbbell", "incline dumbbell bench"]),
    ("Dumbbell Fly", "chest", "strength", ["db fly", "flyes", "flys", "chest fly", "dumbbell flyes"]),
    ("Cable Crossover", "chest", "strength", ["cable fly", "crossover", "cable flyes", "cable crossovers"]),
    ("Pec Deck Fly", "chest", "strength", ["pec deck", "pec fly", "machine fly", "butterfly"]),
    ("Machine Chest Press", "chest", "strength", ["chest press", "machine press"]),
    ("Push-Up", "chest", "bodyweight", ["pushup", "pushups", "push ups", "push up"]),
    ("Chest Dip", "chest", "bodyweight", ["dips", "dip", "parallel bar dips"]),
    # Back
    ("Deadlift", "back", "strength", ["dl", "deadlifts", "conventional deadlift"]),
    ("Barbell Row", "back", "strength", ["bent over row", "bb row", "barbell rows", "bent over rows"]),
    ("Dumbbell Row", "back", "strength", ["db row", "one arm row", "single arm row", "dumbbell rows"]),
    ("Lat Pulldown", "back", "strength", ["lat pull down", "pulldown", "pull down", "lat pulldowns", "lat pull",
                                          "lat pulls", "lats pulldown", "wide grip pulldown", "lat pulldown wide"]),
    ("Seated Cable Row", "back", "strength", ["cable row", "seated row", "low row"]),
    ("T-Bar Row", "back", "strength", ["t bar row", "tbar row"]),
    ("Pull-Up", "back", "bodyweight", ["pullup", "pullups", "pull ups", "pull up"]),
    ("Chin-Up", "back", "bodyweight", ["chinup", "chinups", "chin ups", "chin up"]),
    ("Straight-Arm Pulldown", "back", "strength", ["straight arm pulldown", "lat pushdown"]),
    ("Back Extension", "back", "bodyweight", ["hyperextension", "hyperextensions", "back extensions"]),
    ("Rack Pull", "back", "strength", ["rack pulls"]),
    # Shoulders
    ("Overhead Press", "shoulders", "strength", ["ohp", "military press", "barbell shoulder press", "standing press"]),
    ("Dumbbell Shoulder Press", "shoulders", "strength", ["db shoulder press", "seated db press", "shoulder press", "db ohp"]),
    ("Machine Shoulder Press", "shoulders", "strength", ["shoulder press machine"]),
    ("Lateral Raise", "shoulders", "strength", ["side raise", "lateral raises", "side laterals", "laterals", "db lateral raise"]),
    ("Cable Lateral Raise", "shoulders", "strength", ["cable laterals", "cable side raise"]),
    ("Front Raise", "shoulders", "strength", ["front raises", "db front raise"]),
    ("Rear Delt Fly", "shoulders", "strength", ["rear delt", "reverse fly", "reverse pec deck", "rear delt flyes"]),
    ("Face Pull", "shoulders", "strength", ["face pulls", "facepull"]),
    ("Arnold Press", "shoulders", "strength", ["arnold"]),
    ("Barbell Shrug", "shoulders", "strength", ["shrugs", "shrug", "db shrug", "dumbbell shrug"]),
    ("Upright Row", "shoulders", "strength", ["upright rows"]),
    # Biceps
    ("Barbell Curl", "biceps", "strength", ["bb curl", "barbell curls", "straight bar curl"]),
    ("EZ Bar Curl", "biceps", "strength", ["ez curl", "ez bar curls"]),
    ("Dumbbell Curl", "biceps", "strength", ["db curl", "bicep curl", "biceps curl", "curls", "dumbbell curls"]),
    ("Hammer Curl", "biceps", "strength", ["hammer curls", "db hammer curl"]),
    ("Preacher Curl", "biceps", "strength", ["preacher curls", "scott curl"]),
    ("Incline Dumbbell Curl", "biceps", "strength", ["incline curl", "incline db curl"]),
    ("Cable Curl", "biceps", "strength", ["cable curls", "cable bicep curl"]),
    ("Concentration Curl", "biceps", "strength", ["concentration curls"]),
    # Triceps
    ("Tricep Pushdown", "triceps", "strength", ["pushdown", "triceps pushdown", "rope pushdown", "cable pushdown", "tricep pushdowns"]),
    ("Overhead Tricep Extension", "triceps", "strength", ["overhead extension", "overhead tricep", "rope overhead extension"]),
    ("Skull Crusher", "triceps", "strength", ["skull crushers", "lying tricep extension", "ez skull crusher"]),
    ("Close-Grip Bench Press", "triceps", "strength", ["close grip bench", "cgbp", "close grip bp"]),
    ("Tricep Dip", "triceps", "bodyweight", ["bench dips", "bench dip", "tricep dips"]),
    ("Tricep Kickback", "triceps", "strength", ["kickbacks", "kickback"]),
    # Legs
    ("Barbell Back Squat", "legs", "strength", ["squat", "squats", "back squat", "bb squat"]),
    ("Front Squat", "legs", "strength", ["front squats"]),
    ("Goblet Squat", "legs", "strength", ["goblet squats"]),
    ("Leg Press", "legs", "strength", ["leg presses"]),
    ("Hack Squat", "legs", "strength", ["hack squats"]),
    ("Romanian Deadlift", "legs", "strength", ["rdl", "rdls", "romanian deadlifts"]),
    ("Leg Extension", "legs", "strength", ["leg extensions", "quad extension"]),
    ("Lying Leg Curl", "legs", "strength", ["leg curl", "hamstring curl", "leg curls"]),
    ("Seated Leg Curl", "legs", "strength", ["seated hamstring curl"]),
    ("Walking Lunge", "legs", "strength", ["lunges", "lunge", "walking lunges", "db lunges"]),
    ("Bulgarian Split Squat", "legs", "strength", ["bss", "split squat", "bulgarian split squats"]),
    ("Standing Calf Raise", "legs", "strength", ["calf raise", "calf raises", "calves"]),
    ("Seated Calf Raise", "legs", "strength", ["seated calves"]),
    # Glutes
    ("Hip Thrust", "glutes", "strength", ["hip thrusts", "barbell hip thrust", "glute bridge"]),
    ("Cable Kickback", "glutes", "strength", ["glute kickback", "cable glute kickback"]),
    ("Hip Abduction Machine", "glutes", "strength", ["abductor", "hip abduction", "abduction machine"]),
    ("Hip Adduction Machine", "legs", "strength", ["adductor", "hip adduction", "adduction machine"]),
    # Core
    ("Plank", "core", "bodyweight", ["planks", "front plank"]),
    ("Side Plank", "core", "bodyweight", ["side planks"]),
    ("Crunch", "core", "bodyweight", ["crunches", "ab crunch"]),
    ("Hanging Leg Raise", "core", "bodyweight", ["leg raise", "leg raises", "hanging leg raises"]),
    ("Cable Crunch", "core", "strength", ["cable crunches", "kneeling cable crunch"]),
    ("Russian Twist", "core", "bodyweight", ["russian twists"]),
    ("Ab Wheel Rollout", "core", "bodyweight", ["ab wheel", "ab roller", "rollout", "rollouts"]),
    ("Mountain Climber", "core", "bodyweight", ["mountain climbers"]),
    ("Bicycle Crunch", "core", "bodyweight", ["bicycle crunches", "bicycles"]),
    # Full body
    ("Kettlebell Swing", "full_body", "strength", ["kb swing", "kettlebell swings", "kb swings"]),
    ("Burpee", "full_body", "bodyweight", ["burpees"]),
    ("Clean and Press", "full_body", "strength", ["clean & press"]),
    ("Farmer's Walk", "full_body", "strength", ["farmers walk", "farmer walk", "farmers carry"]),
    # Cardio
    ("Treadmill Run", "cardio", "cardio", ["treadmill", "running", "run", "jog", "jogging"]),
    ("Treadmill Walk", "cardio", "cardio", ["walk", "walking", "incline walk", "brisk walk"]),
    ("Stationary Cycling", "cardio", "cardio", ["cycling", "cycle", "bike", "spin", "stationary bike"]),
    ("Elliptical", "cardio", "cardio", ["cross trainer", "crosstrainer"]),
    ("Rowing Machine", "cardio", "cardio", ["rower", "rowing", "erg"]),
    ("Stair Climber", "cardio", "cardio", ["stairmaster", "stair master", "stairs"]),
    ("Skipping", "cardio", "cardio", ["jump rope", "skipping rope", "rope skipping"]),
]

with get_pool().connection() as conn:
    added = 0
    for name, muscle, ex_type, aliases in EXERCISES:
        exists = conn.execute(
            "select 1 from exercises where user_id is null and lower(exercise_name) = lower(%s)", (name,)).fetchone()
        if exists:
            conn.execute("update exercises set aliases = %s, muscle_group = %s, exercise_type = %s "
                         "where user_id is null and lower(exercise_name) = lower(%s)",
                         (sorted(set(a.lower() for a in aliases)), muscle, ex_type, name))
            continue
        conn.execute(
            "insert into exercises (exercise_name, aliases, muscle_group, exercise_type) values (%s, %s, %s, %s)",
            (name, sorted(set(a.lower() for a in aliases)), muscle, ex_type))
        added += 1
    total = conn.execute("select count(*) as n from exercises where user_id is null").fetchone()["n"]

print(f"✅ Added {added} exercises · built-in dictionary now has {total}")
