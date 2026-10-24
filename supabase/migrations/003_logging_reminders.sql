-- Meal and workout logging reminders, and their WhatsApp templates.
begin;

-- New reminder types.
alter table reminders drop constraint reminders_reminder_type_check;
alter table reminders add constraint reminders_reminder_type_check check (reminder_type in (
  'meal_log_reminder', 'workout_log_reminder',
  'workout_reminder', 'daily_recap', 'weekly_report', 'monthly_report', 'missed_workout_nudge'));

alter table agent_turns drop constraint agent_turns_trigger_type_check;
alter table agent_turns add constraint agent_turns_trigger_type_check check (trigger_type in (
  'user_message', 'meal_log_reminder', 'workout_log_reminder',
  'workout_reminder', 'daily_recap', 'weekly_report', 'monthly_report', 'missed_workout_nudge'));

-- A user can have several reminders of one type at different times (e.g. meal check at 14:30 and 21:30).
alter table reminders drop constraint reminders_user_id_reminder_type_key;
alter table reminders add constraint reminders_user_type_time_key unique (user_id, reminder_type, send_time);

-- Templates (to be submitted to Meta; used when the user is outside the 24-hour window).
insert into whatsapp_templates (template_name, template_category, language_code, body_text, variable_names)
values
  ('meal_log_reminder_v1', 'utility', 'en',
   'Hey {{1}}, you haven''t logged a meal since {{2}}. What did you eat? Just reply like: 2 roti, dal, sabzi',
   '{name,last_meal_time}'),
  ('workout_log_reminder_v1', 'utility', 'en',
   'Hey {{1}}, no workout logged today. You''re at {{2}} sessions this week. Trained? Send your sets, like: bench 60kg 3x10. Rest day? Just reply rest.',
   '{name,sessions_this_week}')
on conflict (template_name) do nothing;

commit;
