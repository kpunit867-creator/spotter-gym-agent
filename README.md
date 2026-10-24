# Spotter — a gym & nutrition buddy that lives in WhatsApp

> Work in progress. This README will become the full case study (problem, decisions, what worked, what didn't).

Log workouts, meals and body weight by just texting — no app, no login. Built for Indian gym-goers
(Indian food portions, veg/eggetarian diets).

## How it works
```
Web simulator ─┐
               ├─► engine.handle_inbound() ─► Gemini (tool calling) ─► logging tools ─► Postgres (Supabase)
WhatsApp API ──┘     saves message, turn,         understands words      exact maths:
  (next phase)       tokens, latency, cost                               totals, PRs, sets
```
- **Gemini understands; code confirms.** Gemini turns a message into a tool call. Code saves it, does the maths
  (totals, PRs, set numbers, unit conversion) and writes the confirmation, so a logging message costs one Gemini call
  and every number shown comes from the database.
- **Small context.** Instructions live in [`prompts/`](prompts/) and stay identical across calls (cache-friendly);
  each message only adds a short `[Context]` block (time, today's totals, last logged entries, pending question).
- **Every message is traceable:** each logged row stores the `message_id` it came from; every Gemini call is
  recorded in `llm_calls`; every user correction goes into `corrections`.
- Data model: [docs/data-model.md](docs/data-model.md)

## Run it locally
1. Python 3.12+: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in `DATABASE_URL` (Supabase → Connect → Session pooler) and `GEMINI_API_KEY`.
3. Create tables: run the files in `supabase/migrations/` in order (Supabase SQL Editor, or `scripts/run_migration.py`).
4. Seed exercises: `.venv/bin/python scripts/seed_exercises.py`
5. Check: `.venv/bin/python scripts/check_db.py`
6. Start: `.venv/bin/uvicorn app.main:app --port 8000` and open http://localhost:8000

## Project layout
| Path | What's there |
|---|---|
| `app/engine.py` | Channel-agnostic message handling (simulator now, WhatsApp later) |
| `prompts/` | The instructions given to Gemini (plain text, edit freely) |
| `app/agent.py` | Tool definitions, context block, Gemini loop, token/latency tracking |
| `app/replies.py` | Confirmation messages written by code |
| `app/tools.py` | Logging tools: meals, workouts, weight, edit, delete, undo |
| `app/onboarding.py` | Profile questions and target calculation |
| `app/static/simulator.html` | WhatsApp-style test UI with a debug panel |
| `knowledge/training/` | (Next phase) plan-building guidelines |
| `scripts/` | DB check, exercise seed, scripted chat test |
