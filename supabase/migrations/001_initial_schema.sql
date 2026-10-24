-- Gym WhatsApp agent: initial schema
-- Column-by-column definitions: docs/data-model.md
-- Run once in Supabase → SQL Editor.

begin;

-- Keeps updated_at current on every update.
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ───────────────────────── 1. Users ─────────────────────────

create table users (
  user_id                  text primary key check (user_id ~ '^[0-9]{8,15}$'),
  name                     text,
  age                      int check (age between 10 and 100),
  gender                   text check (gender in ('male','female','other')),
  height_cm                numeric(5,1) check (height_cm between 100 and 250),
  goal                     text check (goal in ('fat_loss','muscle_gain','recomposition','strength','general_fitness')),
  experience_level         text check (experience_level in ('beginner','intermediate','advanced')),
  diet_preference          text check (diet_preference in ('veg','eggetarian','non_veg','vegan')),
  unit_preference          text not null default 'kg' check (unit_preference in ('kg','lb')),
  timezone                 text not null default 'Asia/Kolkata',
  onboarding_status        text not null default 'not_started' check (onboarding_status in ('not_started','in_progress','completed')),
  onboarding_step          text check (onboarding_step in ('name','age','gender','height','weight','goal','experience','diet','schedule','plan')),
  last_inbound_message_at  timestamptz,
  is_active                boolean not null default true,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create table user_targets (
  user_target_id       bigint generated always as identity primary key,
  user_id              text not null references users(user_id) on delete cascade,
  daily_calories_kcal  int,
  daily_protein_g      int,
  daily_carbs_g        int,
  daily_fat_g          int,
  daily_fiber_g        int,
  target_weight_kg     numeric(5,1),
  workouts_per_week    int check (workouts_per_week between 0 and 7),
  start_date           date not null default current_date,
  created_at           timestamptz not null default now(),
  unique (user_id, start_date)
);

-- ─────────────── WhatsApp templates + messages ───────────────

create table whatsapp_templates (
  whatsapp_template_id  bigint generated always as identity primary key,
  template_name         text not null unique,
  template_category     text not null check (template_category in ('utility','marketing')),
  language_code         text not null default 'en',
  body_text             text not null,
  variable_names        text[] not null default '{}',
  approval_status       text not null default 'draft' check (approval_status in ('draft','pending','approved','rejected')),
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create table messages (
  message_id            bigint generated always as identity primary key,
  user_id               text not null references users(user_id) on delete cascade,
  direction             text not null check (direction in ('inbound','outbound')),
  channel               text not null check (channel in ('web_simulator','whatsapp')),
  whatsapp_message_id   text unique,
  content_type          text not null default 'text' check (content_type in ('text','image','audio','interactive','template')),
  message_text          text,
  media_url             text,
  agent_turn_id         bigint,  -- foreign key added after agent_turns exists
  send_mode             text check (send_mode in ('free_form','template')),
  whatsapp_template_id  bigint references whatsapp_templates(whatsapp_template_id),
  delivery_status       text check (delivery_status in ('queued','sent','delivered','read','failed')),
  failure_reason        text,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  -- outbound messages must say how they were sent; inbound must not
  check ((direction = 'outbound') = (send_mode is not null)),
  check (send_mode is distinct from 'template' or whatsapp_template_id is not null)
);
create index messages_user_time_idx on messages (user_id, created_at desc);

-- ───────────────────────── 2. Food ─────────────────────────

create table meals (
  meal_id              bigint generated always as identity primary key,
  user_id              text not null references users(user_id) on delete cascade,
  log_date             date not null,
  meal_number          int not null check (meal_number >= 1),
  meal_type            text check (meal_type in ('breakfast','lunch','snack','dinner','pre_workout','post_workout')),
  meal_description     text not null,
  total_calories_kcal  numeric(7,1) not null default 0,
  total_protein_g      numeric(6,1) not null default 0,
  total_carbs_g        numeric(6,1) not null default 0,
  total_fat_g          numeric(6,1) not null default 0,
  total_fiber_g        numeric(6,1) not null default 0,
  input_type           text not null default 'text' check (input_type in ('text','photo')),
  message_id           bigint references messages(message_id) on delete set null,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  deleted_at           timestamptz
);
create index meals_user_date_idx on meals (user_id, log_date) where deleted_at is null;

create table meal_items (
  meal_item_id     bigint generated always as identity primary key,
  meal_id          bigint not null references meals(meal_id) on delete cascade,
  user_id          text not null references users(user_id) on delete cascade,
  log_date         date not null,
  food_name        text not null,
  quantity         numeric(6,2) not null,
  unit             text not null check (unit in ('piece','katori','cup','glass','plate','tbsp','g','ml')),
  estimated_grams  numeric(7,1),
  calories_kcal    numeric(7,1) not null,
  protein_g        numeric(6,1) not null,
  carbs_g          numeric(6,1) not null,
  fat_g            numeric(6,1) not null,
  fiber_g          numeric(6,1) not null default 0,
  message_id       bigint references messages(message_id) on delete set null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  deleted_at       timestamptz
);
create index meal_items_meal_idx on meal_items (meal_id);
create index meal_items_user_date_idx on meal_items (user_id, log_date) where deleted_at is null;

-- ───────────────────────── 3. Workouts ─────────────────────────

create table exercises (
  exercise_id    bigint generated always as identity primary key,
  exercise_name  text not null,
  aliases        text[] not null default '{}',
  muscle_group   text not null check (muscle_group in ('chest','back','shoulders','biceps','triceps','legs','glutes','core','full_body','cardio')),
  exercise_type  text not null check (exercise_type in ('strength','bodyweight','cardio')),
  user_id        text references users(user_id) on delete cascade,  -- empty = built-in
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
-- No two exercises with the same name for the same user (built-ins count for everyone).
create unique index exercises_name_unique_idx on exercises (lower(exercise_name), coalesce(user_id, ''));
create index exercises_aliases_idx on exercises using gin (aliases);

create table workout_sessions (
  workout_session_id  bigint generated always as identity primary key,
  user_id             text not null references users(user_id) on delete cascade,
  log_date            date not null,
  workout_name        text not null check (workout_name in ('push','pull','legs','arms','chest','back','shoulders','core','cardio','full_body','other')),
  duration_min        int check (duration_min > 0),
  notes               text,
  message_id          bigint references messages(message_id) on delete set null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  deleted_at          timestamptz
);
create index workout_sessions_user_date_idx on workout_sessions (user_id, log_date) where deleted_at is null;

create table workout_sets (
  workout_set_id      bigint generated always as identity primary key,
  workout_session_id  bigint not null references workout_sessions(workout_session_id) on delete cascade,
  user_id             text not null references users(user_id) on delete cascade,
  log_date            date not null,
  exercise_id         bigint not null references exercises(exercise_id),
  exercise_name       text not null,
  set_number          int not null check (set_number >= 1),
  weight_kg           numeric(6,2) check (weight_kg >= 0),
  reps                int check (reps >= 0),
  duration_sec        int check (duration_sec >= 0),
  distance_km         numeric(6,2) check (distance_km >= 0),
  is_personal_record  boolean not null default false,
  message_id          bigint references messages(message_id) on delete set null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  deleted_at          timestamptz,
  check (reps is not null or duration_sec is not null or distance_km is not null)
);
create index workout_sets_session_idx on workout_sets (workout_session_id);
create index workout_sets_user_exercise_idx on workout_sets (user_id, exercise_id, log_date) where deleted_at is null;

-- ───────────────────────── 4. Weekly plan ─────────────────────────

create table workout_plans (
  workout_plan_id  bigint generated always as identity primary key,
  user_id          text not null references users(user_id) on delete cascade,
  plan_name        text not null,
  plan_source      text not null check (plan_source in ('user_given','agent_generated')),
  goal             text check (goal in ('fat_loss','muscle_gain','recomposition','strength','general_fitness')),
  status           text not null default 'active' check (status in ('active','archived')),
  start_date       date not null default current_date,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
-- At most one active plan per user.
create unique index workout_plans_one_active_idx on workout_plans (user_id) where status = 'active';

create table workout_plan_days (
  workout_plan_day_id  bigint generated always as identity primary key,
  workout_plan_id      bigint not null references workout_plans(workout_plan_id) on delete cascade,
  user_id              text not null references users(user_id) on delete cascade,
  day_of_week          int not null check (day_of_week between 1 and 7),
  workout_name         text not null check (workout_name in ('push','pull','legs','arms','chest','back','shoulders','core','cardio','full_body','other','rest')),
  is_rest_day          boolean not null default false,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  unique (workout_plan_id, day_of_week)
);

create table workout_plan_exercises (
  workout_plan_exercise_id  bigint generated always as identity primary key,
  workout_plan_day_id       bigint not null references workout_plan_days(workout_plan_day_id) on delete cascade,
  workout_plan_id           bigint not null references workout_plans(workout_plan_id) on delete cascade,
  user_id                   text not null references users(user_id) on delete cascade,
  exercise_id               bigint not null references exercises(exercise_id),
  exercise_name             text not null,
  exercise_order            int not null check (exercise_order >= 1),
  target_sets               int check (target_sets >= 1),
  target_reps_min           int check (target_reps_min >= 1),
  target_reps_max           int check (target_reps_max >= target_reps_min),
  target_weight_kg          numeric(6,2),
  notes                     text,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);

-- ───────────────────────── 5. Body weight ─────────────────────────

create table weight_logs (
  weight_log_id  bigint generated always as identity primary key,
  user_id        text not null references users(user_id) on delete cascade,
  log_date       date not null,
  input_text     text,
  input_unit     text not null check (input_unit in ('kg','lb')),
  weight_kg      numeric(5,2) not null check (weight_kg between 20 and 300),
  weight_lb      numeric(6,1) generated always as (round(weight_kg * 2.20462, 1)) stored,
  message_id     bigint references messages(message_id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  deleted_at     timestamptz
);
create index weight_logs_user_date_idx on weight_logs (user_id, log_date) where deleted_at is null;

-- ─────────────────── 6. Agent tracking + scheduling ───────────────────

create table reminders (
  reminder_id    bigint generated always as identity primary key,
  user_id        text not null references users(user_id) on delete cascade,
  reminder_type  text not null check (reminder_type in ('workout_reminder','daily_recap','weekly_report','monthly_report','missed_workout_nudge')),
  send_time      time not null,
  days_of_week   int[] not null default '{1,2,3,4,5,6,7}',
  is_enabled     boolean not null default true,
  next_run_at    timestamptz,
  last_sent_at   timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (user_id, reminder_type)
);
create index reminders_due_idx on reminders (next_run_at) where is_enabled;

create table agent_turns (
  agent_turn_id          bigint generated always as identity primary key,
  user_id                text not null references users(user_id) on delete cascade,
  trigger_type           text not null check (trigger_type in ('user_message','workout_reminder','daily_recap','weekly_report','monthly_report','missed_workout_nudge')),
  message_id             bigint references messages(message_id) on delete set null,
  reminder_id            bigint references reminders(reminder_id) on delete set null,
  detected_intent        text check (detected_intent in ('onboarding','log_meal','log_workout','log_weight','edit_log','get_plan','get_report','question','small_talk','other')),
  tools_called           jsonb not null default '[]',
  outcome                text check (outcome in ('success','needs_clarification','error')),
  error_detail           text,
  llm_call_count         int not null default 0,
  total_input_tokens     int not null default 0,
  total_output_tokens    int not null default 0,
  total_thinking_tokens  int not null default 0,
  estimated_cost_usd     numeric(10,6) not null default 0,
  total_latency_ms       int,
  started_at             timestamptz not null default now(),
  completed_at           timestamptz
);
create index agent_turns_user_time_idx on agent_turns (user_id, started_at desc);

alter table messages
  add constraint messages_agent_turn_id_fkey
  foreign key (agent_turn_id) references agent_turns(agent_turn_id) on delete set null;

create table llm_calls (
  llm_call_id      bigint generated always as identity primary key,
  agent_turn_id    bigint not null references agent_turns(agent_turn_id) on delete cascade,
  user_id          text not null references users(user_id) on delete cascade,
  call_number      int not null check (call_number >= 1),
  model_name       text not null,
  input_tokens     int not null default 0,
  output_tokens    int not null default 0,
  thinking_tokens  int not null default 0,
  cached_tokens    int not null default 0,
  latency_ms       int,
  finish_reason    text,
  created_at       timestamptz not null default now()
);
create index llm_calls_turn_idx on llm_calls (agent_turn_id);

create table conversation_state (
  user_id                    text primary key references users(user_id) on delete cascade,
  pending_action             jsonb,
  undo_table_name            text,
  undo_record_id             bigint,
  active_workout_session_id  bigint references workout_sessions(workout_session_id) on delete set null,
  conversation_summary       text,
  updated_at                 timestamptz not null default now()
);

create table corrections (
  correction_id         bigint generated always as identity primary key,
  user_id               text not null references users(user_id) on delete cascade,
  corrected_table_name  text not null,
  corrected_record_id   bigint not null,
  field_name            text not null,
  old_value             text,
  new_value             text,
  correction_reason     text check (correction_reason in ('wrong_parse','wrong_estimate','wrong_exercise','user_changed_mind','other')),
  message_id            bigint references messages(message_id) on delete set null,
  created_at            timestamptz not null default now()
);

-- ─────────────────── updated_at triggers ───────────────────

do $$
declare t text;
begin
  for t in
    select c.table_name from information_schema.columns c
    where c.table_schema = 'public' and c.column_name = 'updated_at'
  loop
    execute format(
      'create trigger %I before update on %I for each row execute function set_updated_at()',
      t || '_set_updated_at', t);
  end loop;
end $$;

-- ─────────────────── Lock tables from public API ───────────────────
-- Row Level Security on with no policies = nobody can read/write through
-- Supabase's public API keys. Our backend connects directly to Postgres,
-- which is not affected.

do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

commit;
