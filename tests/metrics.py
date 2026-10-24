"""What the evaluation run records. One row per user message ("step"), plus run-level summaries."""

# (column name, what it means, where it comes from)
STEP_METRICS = [
    # Identity
    ("run_id", "ID of this test run (date-time)", "runner"),
    ("scenario_id", "Test case ID, e.g. WRK-03", "test plan"),
    ("category", "Area being tested (Onboarding, Workout logging, Reports…)", "test plan"),
    ("scenario", "Short name of the test case", "test plan"),
    ("multi_turn", "Yes if the case has more than one message", "test plan"),
    ("step", "Message number within the case (1, 2, 3…)", "runner"),
    ("test_user", "Phone number of the throwaway test user", "runner"),
    ("user_profile", "Starting state: new user / onboarded with no history / onboarded with 4 weeks of data", "test plan"),
    # Input / output
    ("input_message", "Exactly what the test user sent", "test plan"),
    ("expected_behaviour", "What a correct reply should do, in plain words", "test plan"),
    ("agent_reply", "Full text the bot sent back", "messages table"),
    ("reaction", "Emoji reaction the bot put on the message, if any", "messages table"),
    ("reply_chars", "Length of the reply in characters", "computed"),
    ("send_mode", "free_form (inside 24 h) or template", "messages table"),
    # Timing
    ("total_time_ms", "From message received to reply saved", "agent_turns.total_latency_ms"),
    ("llm_time_ms", "Time spent waiting on Gemini (all calls added up)", "llm_calls.latency_ms"),
    ("backend_time_ms", "total − LLM: our code + database queries", "computed"),
    ("db_query_count", "Number of database queries in the turn", "instrumented connection"),
    ("db_time_ms", "Time spent in database queries", "instrumented connection"),
    ("llm_call_latencies_ms", "Each Gemini call's time, e.g. 4100 + 3900", "llm_calls"),
    # LLM usage
    ("model", "Gemini model used", "llm_calls.model_name"),
    ("llm_calls", "Number of Gemini calls in the turn", "agent_turns"),
    ("input_tokens", "Prompt tokens (all calls)", "agent_turns"),
    ("output_tokens", "Response tokens (all calls)", "agent_turns"),
    ("thinking_tokens", "Thinking tokens (all calls; billed as output)", "agent_turns"),
    ("cached_tokens", "Input tokens served from Gemini's cache (billed at ~10%)", "llm_calls"),
    ("cache_hit_pct", "cached ÷ input tokens", "computed"),
    ("est_cost_usd", "Estimated Gemini cost of the turn", "agent_turns"),
    ("finish_reasons", "Why each Gemini call stopped (STOP, MAX_TOKENS, SAFETY…)", "llm_calls"),
    # Behaviour
    ("detected_intent", "What the agent decided the message was (log_meal, edit_log, question…)", "agent_turns"),
    ("expected_intent", "What it should have been", "test plan"),
    ("tools_called", "Tools in order, with their status, e.g. log_workout:logged", "agent_turns.tools_called"),
    ("expected_tools", "Tools that must be called", "test plan"),
    ("tool_args", "Arguments the agent passed (JSON)", "agent_turns.tools_called"),
    ("outcome", "success / needs_clarification / error", "agent_turns"),
    # Checks
    ("check_intent", "PASS if detected intent matches", "runner"),
    ("check_tools", "PASS if required tools ran and forbidden tools didn't", "runner"),
    ("check_data", "PASS if the database ended up as expected (rows, values, dates)", "runner"),
    ("check_reply", "PASS if the reply contains / avoids the expected content", "runner"),
    ("check_style", "PASS if WhatsApp style rules hold: ≤1 emoji outside reports, no markdown ** or #, ≤1 question", "runner"),
    ("judge_score", "1–5 quality score from an LLM judge, only for free-text answers (questions, advice)", "LLM judge"),
    ("status", "PASS if every check passed, else FAIL", "runner"),
    ("failed_checks", "Which checks failed and why", "runner"),
    # Errors
    ("backend_error", "Error name + message if the turn crashed (e.g. RateLimited: …)", "agent_turns.error_detail"),
    ("tool_errors", "Errors returned by tools (bad arguments, not found, rejected by DB)", "agent_turns.tools_called"),
    ("retries", "Gemini rate-limit retries during the turn", "runner"),
    ("timestamp", "When the step ran", "runner"),
]

RUN_SUMMARY_METRICS = [
    ("pass_rate", "Passed steps ÷ all steps, overall and per category"),
    ("scenario_pass_rate", "Cases where every step passed"),
    ("avg / p50 / p95 total_time_ms", "Response time distribution"),
    ("avg llm_time vs backend_time", "Where the time goes"),
    ("avg llm_calls per step", "1.0 is ideal for plain logging"),
    ("avg input / output / cached tokens", "Token use per message"),
    ("total and per-message cost", "Projected monthly cost at 450 messages"),
    ("error count by type", "Backend errors and tool errors grouped by name"),
    ("first-try tool accuracy", "Steps where the first tool call succeeded without a retry"),
    ("clarification rate", "Steps where the bot asked a question instead of acting"),
]
