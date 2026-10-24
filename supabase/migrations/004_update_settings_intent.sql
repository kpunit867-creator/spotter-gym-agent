-- Intent for "change my targets / goal / units" messages.
begin;
alter table agent_turns drop constraint agent_turns_detected_intent_check;
alter table agent_turns add constraint agent_turns_detected_intent_check check (detected_intent in (
  'onboarding','log_meal','log_workout','log_weight','edit_log','get_plan','get_report',
  'update_settings','question','small_talk','other'));
commit;
