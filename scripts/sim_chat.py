"""Run: python scripts/sim_chat.py <phone> "msg 1" "msg 2" ... — sends messages through the engine and prints replies."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import handle_inbound  # noqa: E402

phone, *msgs = sys.argv[1:]
PAUSE = float(os.getenv("PAUSE", "0"))  # seconds between messages (free-tier Gemini: ~25)
for i, m in enumerate(msgs):
    if i and PAUSE:
        time.sleep(PAUSE)
    r = handle_inbound(phone, m)
    d = r["debug"]
    shown = [("[reacted " + x["text"] + "]") if x["content_type"] == "reaction" else x["text"] for x in r["replies"]]
    print(f"\n👤 {m}\n🤖 " + "\n".join(shown))
    print(f"   [{d['intent']} · {d['outcome']} · {d['latency_ms']}ms · calls {d['llm_calls']} · "
          f"in {d['input_tokens']} out {d['output_tokens']} think {d['thinking_tokens']} · "
          f"tools {[t['tool'] + ':' + str(t['status']) for t in d['tools_called']]}]"
          + (f"\n   ERROR {d['error']}" if d['error'] else ""))
