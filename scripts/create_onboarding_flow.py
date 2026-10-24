"""Run: python scripts/create_onboarding_flow.py — creates (or updates) the onboarding form on Meta as a draft.

Writes WHATSAPP_BUSINESS_ACCOUNT_ID / WHATSAPP_ONBOARDING_FLOW_ID to .env.
Publish later with: python scripts/create_onboarding_flow.py --publish
"""
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C  # noqa: E402

WABA_ID = C.os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")  # Meta → WhatsApp → API Setup
if not WABA_ID:
    sys.exit("Set WHATSAPP_BUSINESS_ACCOUNT_ID in .env first.")
FLOW_FILE = ROOT / "app" / "flows" / "onboarding_flow.json"
API = f"https://graph.facebook.com/{C.WHATSAPP_API_VERSION}"
H = {"Authorization": f"Bearer {C.WHATSAPP_ACCESS_TOKEN}"}


def set_env(key, value):
    env = ROOT / ".env"
    lines = [l for l in env.read_text().splitlines() if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env.write_text("\n".join(lines) + "\n")


flow_id = C.os.getenv("WHATSAPP_ONBOARDING_FLOW_ID")
if "--publish" in sys.argv:
    print(httpx.post(f"{API}/{flow_id}/publish", headers=H, timeout=30).json())
    sys.exit()

if not flow_id:
    r = httpx.post(f"{API}/{WABA_ID}/flows", headers=H, timeout=30,
                   json={"name": "spotter_onboarding", "categories": ["SIGN_UP"]}).json()
    if "id" not in r:
        sys.exit(f"Create failed: {r}")
    flow_id = r["id"]
    set_env("WHATSAPP_ONBOARDING_FLOW_ID", flow_id)
    set_env("WHATSAPP_BUSINESS_ACCOUNT_ID", WABA_ID)
    print("Created flow", flow_id)

up = httpx.post(f"{API}/{flow_id}/assets", headers=H, timeout=30,
                data={"name": "flow.json", "asset_type": "FLOW_JSON"},
                files={"file": ("flow.json", FLOW_FILE.read_bytes(), "application/json")}).json()
errors = up.get("validation_errors") or []
print("Upload:", "ok" if up.get("success") and not errors else up)
for e in errors:
    print(" -", e.get("error"), e.get("message"), e.get("line_start"))
info = httpx.get(f"{API}/{flow_id}", headers=H, params={"fields": "name,status,validation_errors"}, timeout=30).json()
print("Flow:", json.dumps(info))
