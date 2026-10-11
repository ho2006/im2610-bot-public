"""Read-only smoke check for the local Docker console. Never prints secrets."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import load_env
import os

load_env()
base = "http://127.0.0.1:8000"
with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
    assert json.load(response)["ok"]
try:
    urllib.request.urlopen(base + "/api/state", timeout=5)
except urllib.error.HTTPError as exc:
    assert exc.code == 401
else:
    raise AssertionError("State API must require authentication")
request = urllib.request.Request(base + "/api/state", headers={
    "Authorization": "Bearer " + os.environ["BOT_TOKEN"]})
with urllib.request.urlopen(request, timeout=5) as response:
    state = json.load(response)
assert state["symbol"] == "IM2610"
assert state["mode"] == "paper"
assert state["config"]["fee_per_side_bps"] == 0
assert state["ready"] and not state["latched"]
assert state["quote"] is not None
assert state["ticks"]
print(json.dumps({"mode": state["mode"], "commission_bps": 0,
    "healthy": True, "authenticated_api": True, "ready": state["ready"],
    "quote": {k: state["quote"][k] for k in ("bid", "ask", "bid_size", "ask_size")},
    "persisted_ticks": len(state["ticks"]), "enabled": state["enabled"]}, ensure_ascii=True))
