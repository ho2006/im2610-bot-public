"""Read-only schema check. Does not print credentials or account identifiers."""
import json
import os
import urllib.request

URL = "https://qhsim.eastmoney.com/simCustomCompeteApi/api/skills/action"


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"count": len(value), "item": shape(value[0]) if value else None}
    return type(value).__name__


if __name__ == "__main__":
    for action in ("query_futures_contracts", "query_account_detail", "query_account_holdings",
                   "query_pending_orders", "query_today_orders", "query_today_trades", "query_race_schedule"):
        request = urllib.request.Request(URL, data=json.dumps({"action": action}).encode(), headers={
            "CFAPIKEY": os.environ["CFAPIKEY"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            print(action, "status:", data.get("status"), json.dumps(shape(data), ensure_ascii=True))
            if action == "query_futures_contracts":
                print("IM2610 available:", any(x.get("futcode") == "IM2610" for x in data.get("result", [])))
                print("IM2610 mapping:", json.dumps([x for x in data.get("result", []) if x.get("futcode") == "IM2610"], ensure_ascii=True))
            if action in ("query_account_holdings", "query_today_orders"):
                rows = data.get("result", [])
                if isinstance(rows, dict):
                    rows = rows.get("futureAccountHoldings", [])
                print("contract/status examples:", json.dumps([{k: x[k] for k in ("futname", "operType", "wtzt", "mmfx") if k in x} for x in rows[:6]], ensure_ascii=True))
            if action == "query_race_schedule":
                print("schedule:", json.dumps(data.get("result"), ensure_ascii=True))
        except Exception as exc:
            print(action, type(exc).__name__)
