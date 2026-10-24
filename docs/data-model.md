# Data Model

Database: **Postgres on Supabase**. The SQL that creates everything is in [`supabase/migrations/`](../supabase/migrations/), run in order: `001_initial_schema.sql`, `002_message_reactions.sql`, `003_logging_reminders.sql`.

## Naming rules

| Rule | Example |
|---|---|
| Table names are plural, `snake_case` | `workout_sets` |
| Every table's own ID is `<thing>_id` | `meals.meal_id` |
| **An ID has the same name in every table it appears in** | `user_id` is `user_id` everywhere, never `uid` or `phone` |
| Units are part of the column name | `weight_kg`, `protein_g`, `latency_ms` |
| `log_date` = the calendar day an entry counts for (user's timezone) | a 12:30 AM meal can count for the previous day |
| Timestamps end in `_at` | `created_at`, `last_sent_at` |
| Yes/no columns start with `is_` | `is_rest_day` |

### Columns found on most tables
| Column | Definition |
|---|---|
| `created_at` | When the row was created. |
| `updated_at` | When the row was last changed (set automatically). |
| `deleted_at` | When the row was soft-deleted (e.g. "undo"); empty means the row is live. |
| `message_id` | The chat message that created this row, so every entry can be traced back to what the user typed. |

### How the IDs connect
```
users.user_id ─┬─ user_targets, meals, meal_items, workout_sessions, workout_sets,
               │  workout_plans, workout_plan_days, workout_plan_exercises,
               │  weight_logs, messages, agent_turns, llm_calls,
               │  conversation_state, reminders, corrections
meals.meal_id ──────────────────── meal_items
workout_sessions.workout_session_id ── workout_sets
exercises.exercise_id ──────────── workout_sets, workout_plan_exercises
workout_plans.workout_plan_id ──── workout_plan_days, workout_plan_exercises
workout_plan_days.workout_plan_day_id ── workout_plan_exercises
messages.message_id ────────────── meals, meal_items, workout_sessions, workout_sets,
                                   weight_logs, agent_turns, corrections
agent_turns.agent_turn_id ──────── messages (replies), llm_calls
reminders.reminder_id ──────────── agent_turns
whatsapp_templates.whatsapp_template_id ── messages
```

---

## 1. Users

### `users` — one row per person using the bot
| Column | Definition |
|---|---|
| `user_id` | WhatsApp phone number with country code, digits only (e.g. `919876543210`). Primary key. |
| `name` | Name the user gave during onboarding. |
| `age` | Age in years at onboarding. |
| `gender` | `male`, `female` or `other`. |
| `height_cm` | Height in centimetres. |
| `goal` | `fat_loss`, `muscle_gain`, `recomposition`, `strength` or `general_fitness`. |
| `experience_level` | `beginner`, `intermediate` or `advanced`. |
| `diet_preference` | `veg`, `eggetarian`, `non_veg` or `vegan`. |
| `unit_preference` | `kg` or `lb` — the unit the bot uses when replying. |
| `timezone` | User's timezone, default `Asia/Kolkata`; decides `log_date` and reminder times. |
| `onboarding_status` | Overall progress: `not_started` → `in_progress` → `completed`. |
| `onboarding_step` | The next onboarding question to ask (`name`, `age`, `gender`, `height`, `weight`, `goal`, `experience`, `diet`, `schedule`, `plan`); empty once onboarding is completed. |
| `last_inbound_message_at` | When the user last messaged us; if under 24 hours ago we can reply freely, otherwise we must send a template. |
| `is_active` | `false` if the user has stopped the bot (e.g. sent "STOP"). |

### `user_targets` — the user's daily nutrition and body targets, with history
| Column | Definition |
|---|---|
| `user_target_id` | Primary key. |
| `user_id` | Whose targets these are. |
| `daily_calories_kcal` | Daily calorie target. |
| `daily_protein_g` | Daily protein target in grams. |
| `daily_carbs_g` | Daily carbs target in grams. |
| `daily_fat_g` | Daily fat target in grams. |
| `daily_fiber_g` | Daily fibre target in grams. |
| `target_weight_kg` | Goal body weight. |
| `workouts_per_week` | How many training days per week the user aims for. |
| `start_date` | The date these targets begin to apply. A new row is added when targets change; the row with the latest `start_date` on or before a given day is the target for that day, so old days are judged against the old target. |

---

## 2. Food

### `meals` — one row per meal the user logs
| Column | Definition |
|---|---|
| `meal_id` | Primary key. |
| `user_id` | Who ate it. |
| `log_date` | The day the meal counts for. |
| `meal_number` | Order of the meal that day: 1, 2, 3… |
| `meal_type` | `breakfast`, `lunch`, `snack`, `dinner`, `pre_workout` or `post_workout`. |
| `meal_description` | The food as the user described it, e.g. "2 roti, dal, paneer sabzi". |
| `total_calories_kcal` | Sum of calories of all items in the meal. |
| `total_protein_g` | Sum of protein. |
| `total_carbs_g` | Sum of carbs. |
| `total_fat_g` | Sum of fat. |
| `total_fiber_g` | Sum of fibre. |
| `input_type` | `text` or `photo`. |
| `message_id` | The message this meal was logged from. |

### `meal_items` — one row per food inside a meal
| Column | Definition |
|---|---|
| `meal_item_id` | Primary key. |
| `meal_id` | Which meal this item belongs to. |
| `user_id` | Who ate it (copied from the meal for easy querying). |
| `log_date` | The day it counts for (copied from the meal for easy querying). |
| `food_name` | Standard name of the food, e.g. "Chapati (roti)". |
| `quantity` | How many units, e.g. `2`. |
| `unit` | `piece`, `katori`, `cup`, `glass`, `plate`, `tbsp`, `g` or `ml`. |
| `estimated_grams` | Estimated weight of this item in grams. |
| `calories_kcal` | Calories of this item. |
| `protein_g` | Protein of this item. |
| `carbs_g` | Carbs of this item. |
| `fat_g` | Fat of this item. |
| `fiber_g` | Fibre of this item. |
| `message_id` | The message this item was logged from. |

> Nutrition values are estimated by the LLM (Indian home-cooking portions are described in its instructions). There is no food reference table for now.

---

## 3. Workouts

### `exercises` — the exercise dictionary
Every logged set must use a name from this table. If the user mentions an exercise that isn't here, the bot asks *"This looks new — should I add it to your exercise list?"*, adds it on "yes", then logs it.

| Column | Definition |
|---|---|
| `exercise_id` | Primary key. |
| `exercise_name` | The one official name used for logging and tracking, e.g. "Barbell Bench Press". |
| `aliases` | Other names users type that mean this exercise, e.g. `{bench, bp, flat bench}`. |
| `muscle_group` | Main muscle worked: `chest`, `back`, `shoulders`, `biceps`, `triceps`, `legs`, `glutes`, `core`, `full_body` or `cardio`. |
| `exercise_type` | `strength` (weight × reps), `bodyweight` (reps only) or `cardio` (time/distance). |
| `user_id` | Empty for built-in exercises; set to the user who added it for custom ones (only that user sees it). |

### `workout_sessions` — one row per workout the user actually did
Not tied to the plan: if the plan says Push on Monday but the user does Legs, the session is simply logged as Legs. Several sessions on one day are allowed (e.g. morning cardio + evening legs).

| Column | Definition |
|---|---|
| `workout_session_id` | Primary key. |
| `user_id` | Who trained. |
| `log_date` | The day the workout counts for. |
| `workout_name` | What the user did: `push`, `pull`, `legs`, `arms`, `chest`, `back`, `shoulders`, `core`, `cardio`, `full_body` or `other`. |
| `duration_min` | Workout length in minutes, if the user said. |
| `notes` | Anything extra the user said, e.g. "felt weak today". |
| `message_id` | The message that started this session. |

### `workout_sets` — one row per set
| Column | Definition |
|---|---|
| `workout_set_id` | Primary key. |
| `workout_session_id` | Which session this set belongs to. |
| `user_id` | Who did it (copied for easy querying). |
| `log_date` | The day it counts for (copied for easy querying). |
| `exercise_id` | Which exercise from the dictionary. |
| `exercise_name` | Official name of that exercise (copied so the table is readable on its own). |
| `set_number` | 1, 2, 3… within this exercise in this session. |
| `weight_kg` | Weight lifted, always stored in kg (converted if the user said lb); empty for bodyweight/cardio. |
| `reps` | Repetitions done; empty for cardio. |
| `duration_sec` | Time in seconds, for cardio or timed holds like planks. |
| `distance_km` | Distance, for cardio. |
| `is_personal_record` | `true` if this set beat the user's previous best for this exercise. |
| `message_id` | The message this set was logged from. |

---

## 4. Weekly plan

### `workout_plans` — a user's weekly training plan
| Column | Definition |
|---|---|
| `workout_plan_id` | Primary key. |
| `user_id` | Whose plan. |
| `plan_name` | Readable name, e.g. "PPL 6-day". |
| `plan_source` | `user_given` (user described their own plan) or `agent_generated` (bot built it from their goal). |
| `goal` | The goal this plan was built for (same values as `users.goal`). |
| `status` | `active` or `archived`; a user has at most one active plan. |
| `start_date` | When the user started following it. |

### `workout_plan_days` — what's planned for each day of the week
| Column | Definition |
|---|---|
| `workout_plan_day_id` | Primary key. |
| `workout_plan_id` | Which plan. |
| `user_id` | Whose plan. |
| `day_of_week` | 1 = Monday … 7 = Sunday. |
| `workout_name` | Planned workout, same values as `workout_sessions.workout_name`, or `rest`. |
| `is_rest_day` | `true` if it's a rest day. |

### `workout_plan_exercises` — the exercises planned for a day
| Column | Definition |
|---|---|
| `workout_plan_exercise_id` | Primary key. |
| `workout_plan_day_id` | Which day of the plan. |
| `workout_plan_id` | Which plan. |
| `user_id` | Whose plan. |
| `exercise_id` | Which exercise from the dictionary. |
| `exercise_name` | Official name of that exercise (copied for readability). |
| `exercise_order` | Order to do it in that day: 1, 2, 3… |
| `target_sets` | Planned number of sets. |
| `target_reps_min` | Low end of the planned rep range. |
| `target_reps_max` | High end of the planned rep range. |
| `target_weight_kg` | Planned weight, if any. |
| `notes` | Extra instructions, e.g. "slow negatives". |

---

## 5. Body weight

### `weight_logs` — one row per weigh-in
| Column | Definition |
|---|---|
| `weight_log_id` | Primary key. |
| `user_id` | Whose weight. |
| `log_date` | The day of the weigh-in. |
| `input_text` | Exactly what the user typed, e.g. "163 lbs". |
| `input_unit` | `kg` or `lb` — the unit the user used. |
| `weight_kg` | Weight in kg. |
| `weight_lb` | Weight in pounds — calculated automatically from `weight_kg`, so the two never disagree. |
| `message_id` | The message this was logged from. |

---

## 6. Conversations and agent tracking

### `messages` — every message in or out
| Column | Definition |
|---|---|
| `message_id` | Primary key. |
| `user_id` | Who the conversation is with. |
| `direction` | `inbound` (user → bot) or `outbound` (bot → user). |
| `channel` | `web_simulator` or `whatsapp`. |
| `whatsapp_message_id` | WhatsApp's own ID for the message (`wamid…`), used to match delivery updates. |
| `content_type` | `text`, `image`, `audio`, `interactive`, `template` or `reaction` (an emoji reaction on another message). |
| `message_text` | The text of the message (or caption). |
| `media_url` | Link to the photo/audio, if any. |
| `reaction_to_message_id` | For reactions: the message being reacted to. |
| `agent_turn_id` | For outbound messages: the agent turn that produced this reply. |
| `send_mode` | For outbound messages: `free_form` (inside the 24-hour window) or `template` (outside it). |
| `whatsapp_template_id` | For template messages: which template was used. |
| `delivery_status` | For outbound messages: `queued`, `sent`, `delivered`, `read` or `failed`. |
| `failure_reason` | Why sending failed, if it did. |

### `agent_turns` — one row each time the agent does a piece of work
Started either by a user message or by a scheduled job (reminder, report).

| Column | Definition |
|---|---|
| `agent_turn_id` | Primary key. |
| `user_id` | Who it was for. |
| `trigger_type` | What started it: `user_message`, `meal_log_reminder`, `workout_log_reminder` (or a reserved reminder type). |
| `message_id` | The user message that started it (empty for scheduled jobs). |
| `reminder_id` | The reminder that started it (empty for user messages). |
| `detected_intent` | What the agent understood: `onboarding`, `log_meal`, `log_workout`, `log_weight`, `edit_log`, `get_plan`, `get_report`, `question`, `small_talk` or `other`. |
| `tools_called` | List of tools the agent used, with their inputs (JSON). |
| `outcome` | `success`, `needs_clarification` or `error`. |
| `error_detail` | Error message if it failed. |
| `llm_call_count` | How many Gemini calls this turn needed. |
| `total_input_tokens` | Input tokens across all calls. |
| `total_output_tokens` | Output tokens across all calls. |
| `total_thinking_tokens` | Thinking tokens across all calls. |
| `estimated_cost_usd` | Estimated Gemini cost of this turn. |
| `total_latency_ms` | Time from receiving the trigger to sending the reply. |
| `started_at` | When the turn began. |
| `completed_at` | When the turn finished. |

### `llm_calls` — one row per Gemini API call
| Column | Definition |
|---|---|
| `llm_call_id` | Primary key. |
| `agent_turn_id` | Which turn made this call. |
| `user_id` | Who it was for. |
| `call_number` | 1, 2, 3… order within the turn. |
| `model_name` | Gemini model used, e.g. `gemini-2.5-flash`. |
| `input_tokens` | Prompt tokens. |
| `output_tokens` | Response tokens. |
| `thinking_tokens` | Thinking tokens (if the model used thinking). |
| `cached_tokens` | Input tokens served from cache. |
| `latency_ms` | How long this call took. |
| `finish_reason` | Why the model stopped, e.g. `STOP`, `MAX_TOKENS`, `SAFETY`. |

### `conversation_state` — the bot's short-term memory, one row per user
| Column | Definition |
|---|---|
| `user_id` | Whose state. Primary key. |
| `pending_action` | Something waiting on the user's yes/no, e.g. "add new exercise 'Pec Deck'?" (JSON). |
| `undo_table_name` | Always `messages` when set: "undo" removes everything that was logged from one message. |
| `undo_record_id` | The `message_id` of the user's latest logging message; "undo" soft-deletes every meal, food item, set, session and weigh-in with that `message_id`. |
| `active_workout_session_id` | The session currently being logged, so "3 more sets of curls" joins today's workout instead of starting a new one. |
| `conversation_summary` | Short running summary of older chat, given to the LLM as context. |

### `reminders` — scheduled messages per user
| Column | Definition |
|---|---|
| `reminder_id` | Primary key. |
| `user_id` | Who gets it. |
| `reminder_type` | `meal_log_reminder` (no meal logged in the last 4 hours) or `workout_log_reminder` (no workout today and weekly goal not met). Older types `workout_reminder`, `daily_recap`, `weekly_report`, `monthly_report`, `missed_workout_nudge` are reserved. A user can have several of one type at different times. |
| `send_time` | Local time of day to send, e.g. `18:00`. |
| `days_of_week` | Days to send on, 1 = Monday … 7 = Sunday. |
| `is_enabled` | `false` if the user turned it off. |
| `next_run_at` | When the scheduler should send it next. |
| `last_sent_at` | When it was last sent. |

### `whatsapp_templates` — message templates registered with Meta
| Column | Definition |
|---|---|
| `whatsapp_template_id` | Primary key. |
| `template_name` | Name exactly as registered with Meta, e.g. `weekly_report_v1`. |
| `template_category` | `utility` or `marketing` (Meta's category; affects price). |
| `language_code` | e.g. `en`, `en_IN`. |
| `body_text` | Template text with placeholders `{{1}}`, `{{2}}`… |
| `variable_names` | What each placeholder means, in order, e.g. `{name, workouts_done}`. |
| `approval_status` | `draft`, `pending`, `approved` or `rejected` at Meta. |

### `corrections` — every time the user fixes something the bot logged
Correction rate is the main accuracy measure for the project write-up.

| Column | Definition |
|---|---|
| `correction_id` | Primary key. |
| `user_id` | Who corrected it. |
| `corrected_table_name` | Which table was fixed, e.g. `workout_sets`. |
| `corrected_record_id` | ID of the row that was fixed. |
| `field_name` | Which column changed, e.g. `weight_kg`. |
| `old_value` | Value before the fix. |
| `new_value` | Value after the fix. |
| `correction_reason` | `wrong_parse` (bot misread), `wrong_estimate` (calories off), `wrong_exercise`, `user_changed_mind` or `other`. |
| `message_id` | The message in which the user asked for the fix. |

---

## Not database tables

| Item | Where it lives |
|---|---|
| Training / plan-building guidelines | Knowledge file(s) in `knowledge/training/`. |
| Daily / weekly / monthly reports | Calculated on demand from the tables above. |
