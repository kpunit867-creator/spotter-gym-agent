"""Evaluation runner.

  .venv/bin/python -u tests/run_eval.py                 # all scenarios
  .venv/bin/python -u tests/run_eval.py --only WRK-01,FIX-01 --keep

For each scenario: create a throwaway user → send each message through the real engine →
check the result right away (DB, tools, reply, style) → save the row. After all scenarios:
LLM-judge the free-text answers, then write tests/results/eval_<run>.xlsx.
"""
import argparse
import json
import sys
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.genai import types  # noqa: E402

from app import agent as A  # noqa: E402
from app import reminders as RM  # noqa: E402
from app.config import GEMINI_MODEL  # noqa: E402
from app.db import QUERY_STATS, fetch_all, fetch_one, get_pool  # noqa: E402
from app.engine import handle_inbound  # noqa: E402
from tests import checks as C  # noqa: E402
from tests.metrics import STEP_METRICS  # noqa: E402
from tests.scenarios import SCENARIOS  # noqa: E402
from tests.seed import SETUP_HOOKS, create_user, delete_test_user, phone_for  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PROFILE_LABEL = {"new": "new user", "ready": "onboarded, no history", "history": "onboarded + 4 weeks data"}
NAMES = {"history": "Arjun", "ready": "Test User"}


class Pacer:
    """Keeps Gemini calls under the per-minute limit (Flash-Lite free tier: 15/min)."""
    def __init__(self, per_minute):
        self.per_minute, self.calls = per_minute, deque()

    def wait(self, upcoming=2):
        while True:
            now = time.time()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) + upcoming <= self.per_minute:
                return
            time.sleep(max(1, 61 - (now - self.calls[0])))

    def record(self, n):
        self.calls.extend([time.time()] * n)


def db_now(conn):
    return fetch_one(conn, "select now() as t")["t"]


def run_step(phone, step, pacer):
    """Sends one message (or runs one reminder) and returns raw results."""
    QUERY_STATS.reset()
    A.RETRY_STATS["count"] = 0
    t0 = time.perf_counter()
    out = {"reply": "", "reaction": None, "turn": None, "tools": [], "llm_calls": [], "error": None, "reminder": None}
    try:
        if "say" in step:
            pacer.wait()
            res = handle_inbound(phone, step["say"], channel="web_simulator")
            out["reply"] = "\n\n".join(r["text"] for r in res["replies"] if r["content_type"] == "text")
            out["reaction"] = next((r["text"] for r in res["replies"] if r["content_type"] == "reaction"), None)
            with get_pool().connection() as conn:
                out["turn"] = fetch_one(conn, "select * from agent_turns where agent_turn_id = %s", (res["agent_turn_id"],))
                out["llm_calls"] = fetch_all(conn, "select * from llm_calls where agent_turn_id = %s order by call_number",
                                             (res["agent_turn_id"],))
                msg = fetch_one(conn, """select send_mode from messages where agent_turn_id = %s and content_type = 'text'
                                         order by message_id desc limit 1""", (res["agent_turn_id"],))
                out["send_mode"] = msg["send_mode"] if msg else None
            out["tools"] = out["turn"]["tools_called"] or []
            out["error"] = out["turn"]["error_detail"]
            pacer.record(len(out["llm_calls"]))
        else:
            _, kind = step["action"].split()
            with get_pool().connection() as conn:
                user = fetch_one(conn, "select * from users where user_id = %s", (phone,))
                RM.ensure_default_reminders(conn, user)
                rem = fetch_one(conn, """select * from reminders where user_id = %s and reminder_type = %s
                                         order by send_time limit 1""", (phone, kind))
                out["reminder"] = RM.send_reminder(conn, rem, force=False)
            out["reply"] = out["reminder"].get("text") or ""
            out["send_mode"] = out["reminder"].get("send_mode")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()
    out["wall_ms"] = int((time.perf_counter() - t0) * 1000)
    out["db_queries"], out["db_ms"] = QUERY_STATS.count, int(QUERY_STATS.ms)
    out["retries"] = A.RETRY_STATS["count"]
    return out


def evaluate(scenario, step, raw, phone, scenario_start, step_start):
    results = {}
    turn = raw["turn"] or {}
    if step.get("intent"):
        got = turn.get("detected_intent")
        results["intent"] = (got == step["intent"], f"got {got}")
    if step.get("tools") or step.get("no_tools"):
        results["tools"] = C.check_tools(step, raw["tools"])
    if step.get("data"):
        with get_pool().connection() as conn:
            ctx = C.Ctx(conn, phone, scenario_start, step_start, raw["reply"], raw["reaction"],
                        raw["reminder"] if raw["reminder"] else turn, raw["tools"])
            outcomes = []
            for spec in step["data"]:
                try:
                    outcomes.append((spec, *C.check_data(ctx, spec)))
                except Exception as e:
                    outcomes.append((spec, False, f"check crashed: {type(e).__name__}: {e}"))
        bad = [f"{s} → {d}" for s, ok, d in outcomes if not ok]
        results["data"] = (not bad, "; ".join(bad) or "; ".join(d for _, _, d in outcomes))
    if step.get("reply_has") or step.get("reply_not"):
        results["reply"] = C.check_reply(step, raw["reply"])
    if "say" in step:
        results["style"] = C.check_style(raw["reply"], raw["tools"])
    results["no_crash"] = (not raw["error"] and turn.get("outcome") != "error",
                           raw["error"] or "ok")
    return results


def build_row(run_id, scenario, n, step, raw, checks, phone):
    turn = raw["turn"] or {}
    calls = raw["llm_calls"]
    llm_ms = sum(c["latency_ms"] or 0 for c in calls)
    total_ms = turn.get("total_latency_ms") or raw["wall_ms"]
    inp = sum(c["input_tokens"] for c in calls)
    cached = sum(c["cached_tokens"] for c in calls)
    tools = raw["tools"]
    tool_errors = [f"{t['tool']}: {t.get('status')}" for t in tools if t.get("status") == "error"]
    failed = [f"{k}: {d}" for k, (ok, d) in checks.items() if not ok]
    expected_tools = ", ".join(step.get("tools", [])) + (f" (not: {', '.join(step['no_tools'])})" if step.get("no_tools") else "")
    return {
        "run_id": run_id, "scenario_id": scenario["id"], "category": scenario["category"], "scenario": scenario["name"],
        "multi_turn": "Yes" if len(scenario["steps"]) > 1 else "No", "step": n, "test_user": phone,
        "user_profile": PROFILE_LABEL[scenario["profile"]],
        "input_message": step.get("say") or f"[system] {step['action']}", "expected_behaviour": step.get("expect", ""),
        "agent_reply": raw["reply"], "reaction": raw["reaction"] or "", "reply_chars": len(raw["reply"] or ""),
        "send_mode": raw.get("send_mode") or "",
        "total_time_ms": total_ms, "llm_time_ms": llm_ms, "backend_time_ms": max(0, total_ms - llm_ms),
        "db_query_count": raw["db_queries"], "db_time_ms": raw["db_ms"],
        "llm_call_latencies_ms": " + ".join(str(c["latency_ms"]) for c in calls),
        "model": calls[0]["model_name"] if calls else "", "llm_calls": len(calls),
        "input_tokens": inp, "output_tokens": sum(c["output_tokens"] for c in calls),
        "thinking_tokens": sum(c["thinking_tokens"] for c in calls), "cached_tokens": cached,
        "cache_hit_pct": round(cached / inp, 3) if inp else 0,
        "est_cost_usd": float(turn.get("estimated_cost_usd") or 0),
        "finish_reasons": ", ".join(str(c["finish_reason"]) for c in calls),
        "detected_intent": turn.get("detected_intent") or "", "expected_intent": step.get("intent", ""),
        "tools_called": ", ".join(f"{t['tool']}:{t.get('status')}" for t in tools),
        "expected_tools": expected_tools,
        "tool_args": json.dumps([{t["tool"]: t.get("args")} for t in tools], ensure_ascii=False)[:3000],
        "outcome": turn.get("outcome") or ("sent" if (raw["reminder"] or {}).get("sent") else "skipped" if raw["reminder"] else ""),
        **{f"check_{k}": ("PASS" if checks[k][0] else "FAIL") if k in checks else "n/a"
           for k in ("intent", "tools", "data", "reply", "style")},
        "judge_score": "", "status": "PASS" if not failed else "FAIL", "failed_checks": "\n".join(failed),
        "backend_error": raw["error"] or "", "tool_errors": "; ".join(tool_errors), "retries": raw["retries"],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "_judge": bool(step.get("judge")), "_calls": calls,
    }


JUDGE_PROMPT = """You are grading a WhatsApp gym and nutrition assistant for Indian gym-goers.
User message: {msg}
What a good reply should do: {expect}
Assistant reply: {reply}

Score 1–5: 5 = fully does what's expected, accurate, safe, brief and natural for WhatsApp; 4 = good with a minor issue;
3 = partly right or awkward; 2 = mostly wrong or unhelpful; 1 = wrong, unsafe or ignores the message.
Return JSON only: {{"score": <1-5>, "reason": "<one sentence>"}}"""


def judge(rows, pacer):
    for r in rows:
        if not r["_judge"]:
            continue
        pacer.wait(upcoming=1)
        try:
            resp = A.client().models.generate_content(
                model=GEMINI_MODEL,
                contents=JUDGE_PROMPT.format(msg=r["input_message"], expect=r["expected_behaviour"], reply=r["agent_reply"]),
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   thinking_config=types.ThinkingConfig(thinking_level="low")))
            pacer.record(1)
            verdict = json.loads(resp.text)
            r["judge_score"] = int(verdict["score"])
            if r["judge_score"] < 4:
                r["status"] = "FAIL"
                r["failed_checks"] = (r["failed_checks"] + "\n" if r["failed_checks"] else "") + \
                    f"judge: {r['judge_score']}/5 — {verdict.get('reason', '')}"
            else:
                r["failed_checks"] = (r["failed_checks"] + "\n" if r["failed_checks"] else "") + \
                    f"(judge {r['judge_score']}/5: {verdict.get('reason', '')})"
        except Exception as e:
            r["judge_score"] = "error"
            r["failed_checks"] += f"\njudge error: {type(e).__name__}: {e}"


def snapshot_aliases():
    with get_pool().connection() as conn:
        return {r["exercise_id"]: r["aliases"] for r in
                fetch_all(conn, "select exercise_id, aliases from exercises where user_id is null")}


def restore_aliases(snapshot):
    """Test runs teach nicknames to built-in exercises; put them back as they were."""
    with get_pool().connection() as conn:
        for eid, aliases in snapshot.items():
            conn.execute("update exercises set aliases = %s where exercise_id = %s and aliases <> %s", (aliases, eid, aliases))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated scenario IDs")
    ap.add_argument("--keep", action="store_true", help="keep test users afterwards (for inspecting in the simulator)")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--per-minute", type=int, default=12, help="max Gemini calls per minute")
    args = ap.parse_args()

    chosen = [s for s in SCENARIOS if not args.only or s["id"] in args.only.split(",")]
    run_id = datetime.now().strftime("%Y%m%d-%H%M")
    RESULTS_DIR.mkdir(exist_ok=True)
    jsonl = RESULTS_DIR / f"eval_{run_id}.jsonl"
    pacer = Pacer(args.per_minute)
    aliases = snapshot_aliases()
    rows, users = [], []
    print(f"Run {run_id}: {len(chosen)} scenarios, {sum(len(s['steps']) for s in chosen)} steps, model {GEMINI_MODEL}")

    try:
        for s in chosen:
            phone = phone_for(SCENARIOS.index(s) + 1)
            users.append(phone)
            with get_pool().connection() as conn:
                create_user(conn, phone, s["profile"], NAMES.get(s["profile"], "Test User"))
                hook_ok = SETUP_HOOKS[s["setup_hook"]](conn, phone) if s.get("setup_hook") else True
            # New transaction: now() inside the seeding transaction would equal the seed rows' created_at.
            with get_pool().connection() as conn:
                scenario_start = db_now(conn)
            if not hook_ok:
                print(f"  {s['id']}: setup not possible today, skipped")
                continue
            for n, step in enumerate(s["steps"], start=1):
                with get_pool().connection() as conn:
                    step_start = db_now(conn)
                raw = run_step(phone, step, pacer)
                checks = evaluate(s, step, raw, phone, scenario_start, step_start)
                row = build_row(run_id, s, n, step, raw, checks, phone)
                rows.append(row)
                with jsonl.open("a") as f:
                    f.write(json.dumps({k: v for k, v in row.items() if k != "_calls"}, default=str, ensure_ascii=False) + "\n")
                mark = "✅" if row["status"] == "PASS" else "❌"
                print(f"{mark} {s['id']} step {n}: {row['total_time_ms']} ms, {row['llm_calls']} calls"
                      + (f" — {row['failed_checks'].splitlines()[0][:140]}" if row["status"] == "FAIL" else ""), flush=True)
        if not args.no_judge:
            print("Judging free-text answers…")
            judge(rows, pacer)
    finally:
        restore_aliases(aliases)
        if not args.keep:
            with get_pool().connection() as conn:
                for u in users:
                    delete_test_user(conn, u)
        from tests.report_xlsx import write_results
        path = write_results(rows, run_id)
        passed = sum(r["status"] == "PASS" for r in rows)
        print(f"\nDone: {passed}/{len(rows)} steps passed → {path}")


if __name__ == "__main__":
    main()
