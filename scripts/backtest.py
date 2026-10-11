"""Offline L1 replay and explicitly synthetic stress checks; never loads credentials."""
import argparse
import csv
import hashlib
import json
import math
import random
import sqlite3
import sys
from dataclasses import asdict, fields, replace
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, time as daytime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config, Quote, TZ, Unsafe, aligned, in_session
from engine import Engine, Paper, Store


def quote_quality(quotes):
    """A coverage floor for observed L1, not evidence of profitable parameters."""
    floor = {"minimum_samples": 1000, "minimum_complete_days": 5,
             "min_session_minute_coverage": .95, "max_session_gap_seconds": 60}
    max_age = Config().max_quote_age
    fresh, stamps, source_regressions, volume_regressions, outside, stale = [], set(), 0, 0, 0, 0
    previous = 0
    previous_day, previous_volume = None, 0
    for q in quotes:
        source, received = (datetime.fromtimestamp(t, TZ) for t in (q.timestamp, q.received))
        if not -2 <= q.received-q.timestamp <= max_age:
            stale += 1
            continue
        if source.date() != received.date() or not in_session(source) or not in_session(received):
            outside += 1
            continue
        if q.timestamp < previous:
            source_regressions += 1
            continue
        if q.timestamp not in stamps:
            stamps.add(q.timestamp)
            if source.date() == previous_day and q.volume < previous_volume:
                volume_regressions += 1
                continue
            previous = q.timestamp
            previous_day, previous_volume = source.date(), q.volume
            fresh.append(q)
    grouped = {}
    for q in fresh:
        grouped.setdefault(datetime.fromtimestamp(q.timestamp, TZ).date(), []).append(q.timestamp)
    observed_days = {datetime.fromtimestamp(q.timestamp, TZ).date() for q in quotes
                     if in_session(datetime.fromtimestamp(q.timestamp, TZ).replace(hour=10, minute=0))}
    coverage = []
    if observed_days:
        day, last_day = min(observed_days), max(observed_days)
        while day <= last_day:
            if not in_session(datetime.combine(day, daytime(10), TZ)):
                day += timedelta(days=1)
                continue
            sessions = []
            for name, start, end in (("morning", daytime(9, 30), daytime(11, 30)),
                                     ("afternoon", daytime(13), daytime(15))):
                begin, finish = (datetime.combine(day, t, TZ).timestamp() for t in (start, end))
                values = [t for t in grouped.get(day, []) if begin <= t < finish]
                minutes = len({int((t-begin)//60) for t in values})
                boundaries = [begin, *values, finish]
                sessions.append({"name": name, "samples": len(values),
                    "first_source_time": datetime.fromtimestamp(values[0], TZ).isoformat() if values else None,
                    "last_source_time": datetime.fromtimestamp(values[-1], TZ).isoformat() if values else None,
                    "covered_minutes": minutes, "expected_minutes": 120,
                    "minute_coverage": round(minutes/120, 4),
                    "max_gap_seconds": round(max(b-a for a,b in zip(boundaries, boundaries[1:])), 3)})
            complete = all(s["minute_coverage"] >= floor["min_session_minute_coverage"]
                           and s["max_gap_seconds"] <= floor["max_session_gap_seconds"] for s in sessions)
            coverage.append({"day": day.isoformat(), "samples": sum(s["samples"] for s in sessions),
                             "complete": complete, "sessions": sessions})
            day += timedelta(days=1)
    days = sorted(day.isoformat() for day in grouped)
    qualified = [day["day"] for day in coverage if day["complete"]]
    reasons = []
    if len(fresh) < floor["minimum_samples"]:
        reasons.append(f"Only {len(fresh)} unique fresh in-session source samples; need 1000")
    if len(qualified) < floor["minimum_complete_days"]:
        reasons.append(f"Only {len(qualified)} complete trading days; need 5 with both sessions covered")
    if source_regressions:
        reasons.append(f"{source_regressions} fresh source timestamps regressed")
    if volume_regressions:
        reasons.append(f"{volume_regressions} unique fresh same-day cumulative volumes regressed")
    return {"fresh_in_session": len(fresh), "fresh_days": days,
            "first_fresh_source_time": datetime.fromtimestamp(fresh[0].timestamp, TZ).isoformat() if fresh else None,
            "last_fresh_source_time": datetime.fromtimestamp(fresh[-1].timestamp, TZ).isoformat() if fresh else None,
            "repeated_source_timestamps": len(quotes)-len({q.timestamp for q in quotes}),
            "stale_or_clock_skew": stale, "fresh_outside_session": outside,
            "source_time_regressions": source_regressions,
            "volume_regressions": volume_regressions,
            "max_intraday_gap_seconds": max((s["max_gap_seconds"] for d in coverage for s in d["sessions"]), default=0),
            "coverage_days": coverage, "qualified_days": qualified,
            "missing_trading_days": [d["day"] for d in coverage if not d["samples"]],
            "qualification": floor, "sufficiency_reasons": reasons,
            "sufficient_for_parameter_fit": not reasons}


def load_quotes(path):
    """Keep observation time; deduplicate exact records, never invent order-book fields."""
    path = Path(path)
    required = {field.name for field in fields(Quote)}
    # ponytail: replay keeps validated quotes in RAM; chunk by day if archives outgrow memory.
    quotes, seen = [], set()
    rows = duplicate = invalid = out_of_order = 0
    l1_digest = hashlib.sha256()
    previous = 0
    with ExitStack() as resources:
        if path.suffix == ".csv":
            records = csv.DictReader(resources.enter_context(path.open(encoding="utf-8-sig", newline="")))
        elif path.suffix in (".sqlite3", ".db"):
            db = resources.enter_context(closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)))
            records = (row[0] for row in db.execute("SELECT payload FROM ticks ORDER BY id"))
        else:
            file = resources.enter_context(path.open(encoding="utf-8"))
            records = (line for line in file if line.strip())
        for row in records:
            rows += 1
            try:
                if isinstance(row, str):
                    row = json.loads(row)
                if not isinstance(row, dict):
                    raise ValueError("L1 row must be an object")
                if row.get("symbol", "IM2610") != "IM2610" or not required <= row.keys():
                    raise ValueError("missing L1 fields or wrong contract")
                if any(isinstance(row[key],bool) for key in required):
                    raise ValueError("boolean is not a numeric market field")
                values = {key: float(row[key]) for key in required}
                if not all(math.isfinite(value) for value in values.values()):
                    raise ValueError("nonfinite")
                for key in ("bid_size", "ask_size", "volume"):
                    if not values[key].is_integer():
                        raise ValueError("noninteger volume")
                    values[key] = int(values[key])
                q = Quote(**values)
                datetime.fromtimestamp(q.timestamp,TZ)
                datetime.fromtimestamp(q.received,TZ)
                if not (0 < q.lower <= q.bid < q.ask <= q.upper and q.lower <= q.last <= q.upper
                        and q.bid_size > 0 and q.ask_size > 0 and q.volume >= 0
                        and all(aligned(p) for p in (q.bid, q.ask, q.last, q.lower, q.upper))):
                    raise ValueError("invalid book")
                l1_digest.update((json.dumps(asdict(q), sort_keys=True, separators=(",", ":"),
                                            allow_nan=False)+"\n").encode())
                if q.received < previous:
                    out_of_order += 1
                    continue
                previous = q.received
                if q in seen:
                    duplicate += 1
                    continue
                seen.add(q)
                quotes.append(q)
            except (ValueError, TypeError, KeyError, OverflowError, OSError):
                invalid += 1
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    metadata = {"rows": rows, "valid": len(quotes), "invalid": invalid,
                "exact_duplicates": duplicate, "out_of_order": out_of_order,
                "unique_source_timestamps": len({q.timestamp for q in quotes}),
                "sha256": digest,
                "l1_sha256": l1_digest.hexdigest(),
                **quote_quality(quotes)}
    for count, reason in ((invalid, "invalid L1 rows"), (out_of_order, "out-of-order received timestamps")):
        if count:
            metadata["sufficiency_reasons"].append(f"{count} {reason}")
            metadata["sufficient_for_parameter_fit"] = False
    return quotes, metadata


class ReplayPaper(Paper):
    """Snapshot sensitivity models, not claims about the competition's matching rules."""
    def __init__(self, store, config, model, delay, participation):
        super().__init__(store, config)
        self.model, self.delay, self.participation = model, delay, participation

    def submit(self, plan, quote):
        super().submit(plan, quote)
        self.state["orders"][-1]["live_after"] = self.clock + self.delay

    def cancel(self, ident):
        row = next(row for row in self.state["orders"] if row["remote_id"] == ident)
        row["cancel_after"] = self.clock + self.delay

    def matches(self, order, quote):
        if quote.received < order.get("live_after", 0):
            return False
        if self.model == "through":
            return super().matches(order, quote)
        buy = order["op"] in ("0", "2")
        return quote.last <= order["price"] if buy else quote.last >= order["price"]

    def on_quote(self, quote):
        # ponytail: discrete snapshots; allow a fill on the cancel-confirmation sample
        # to stress the race. Use actual order/fill timestamps when available.
        super().on_quote(quote, participation=self.participation)
        for row in self.state["orders"]:
            if row.get("cancel_after", float("inf")) <= self.clock and row["state"] in ("OPEN", "PARTIAL"):
                row["state"] = "CANCELLED"
        self.save()

    def orders(self, pending=False, day=None):
        for row in self.state["orders"]:
            if row.get("cancel_after", float("inf")) <= self.clock and row["state"] in ("OPEN", "PARTIAL"):
                row["state"] = "CANCELLED"
        return super().orders(pending, day)


class ReplayStore(Store):
    """Same SQL and engine, RAM storage: this test measures strategy, not disk speed."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        super().__init__(":memory:")

    @contextmanager
    def connect(self):
        with self.db:
            yield self.db


def replay(quotes, config, *, model="through", delay=3, cycle_overhead=10, participation=0.1):
    """Run the production cycle under a virtual clock, with no future observations."""
    if not quotes:
        return {"cycles": 0, "error": "No valid L1 records"}
    current = quotes[0]
    clock = current.received
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock, tz)
    with patch("engine.datetime", Clock), patch("time.time", lambda:clock):
        store = ReplayStore()
        store.put("config", asdict(config))
        store.put("zero_commission_applied", True)
        broker = ReplayPaper(store, config, model, delay, participation)
        engine = Engine(store, broker, feed=lambda:current, autostart=True)
        next_cycle = 0
        peak = minimum = 1_000_000
        drawdown = maximum_gross = cycles = 0
        for current in quotes:
            clock = broker.clock = current.received
            if in_session(datetime.fromtimestamp(clock,TZ)):
                try:
                    current.fresh(config.max_quote_age, clock)
                    broker.on_quote(current)
                except Unsafe:
                    pass
            else:
                broker.mark(current)
            if clock >= next_cycle:
                try:
                    engine.cycle()
                except Unsafe as exc:
                    engine.halt(str(exc))
                cycles += 1
                next_cycle = clock + config.poll_seconds + cycle_overhead
            equity = broker.account()["equity"]
            peak = max(peak, equity)
            minimum = min(minimum, equity)
            drawdown = max(drawdown, peak-equity)
            maximum_gross = max(maximum_gross,broker.state["long"]+broker.state["short"])
        orders, trades = broker.state["orders"], broker.state["trades"]
        result = {"cycles": cycles, "model": model, "delay_seconds": delay,
                "cycle_overhead_seconds": cycle_overhead, "participation": participation,
                "pnl_marked": round(broker.account()["equity"]-1_000_000,2),
                "pnl_realized": round(broker.state["cash"]-1_000_000,2),
                "max_drawdown": round(drawdown,2), "worst_equity": round(minimum,2),
                "orders": len(orders), "fill_lots": sum(t["qty"] for t in trades),
                "cancelled": sum(row["state"]=="CANCELLED" for row in orders),
                "ending_long": broker.state["long"], "ending_short": broker.state["short"],
                "pending": len(broker.orders(True)), "max_gross": maximum_gross,
                "latched": bool(engine.latch), "latch": engine.latch,
                "loss_hit": store.get("daily",{}).get("loss_hit",False)}
        store.db.close()
        return result


def config_hash(config):
    normalized = asdict(Config.parse(asdict(config)))
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def out_of_sample(quotes, quality, profiles):
    """Fixed profiles, chronological days; never selects parameters on test results."""
    result = {"status": "insufficient_data", "train_days": [], "test_days": [],
              "profile_hashes": {name: config_hash(config) for name, config in profiles.items()},
              "daily_reset": True, "results": [],
              "limitation": "Independent days start flat with 1,000,000 equity; residual positions are reported, not carried, closed or compounded. Snapshot fills and latency remain uncalibrated; evaluated does not mean profitable or ready to trade.",
              "split_rule": "First floor(60%) of complete chronological trading days train; remainder test. Profiles are fixed; no tuning on either split."}
    if not quality["sufficient_for_parameter_fit"]:
        result["reasons"] = quality["sufficiency_reasons"]
        return result
    days = sorted(quality["qualified_days"])
    split = len(days)*3//5
    result.update(status="evaluated", train_days=days[:split], test_days=days[split:])
    daily = {day: [] for day in days}
    train_days, max_age = set(days[:split]), Config().max_quote_age
    seen = set()
    for q in quotes:
        source, received = (datetime.fromtimestamp(t, TZ) for t in (q.timestamp, q.received))
        day = source.date().isoformat()
        closing_mark = source.time() in (daytime(11, 30), daytime(15)) and q.received >= q.timestamp
        if (day in daily and day == received.date().isoformat() and q.timestamp not in seen
                and -2 <= q.received-q.timestamp <= max_age
                and ((in_session(source) and in_session(received)) or closing_mark)):
            seen.add(q.timestamp)
            daily[day].append(q)
    for day in days:
        for name, config in profiles.items():
            for model in ("through", "touch"):
                result["results"].append({"day": day, "split": "train" if day in train_days else "test",
                                          "profile": name, **replay(daily[day], config, model=model)})
    return result


def stress_quotes(kind, seed=2610, count=432):
    """SYNTHETIC ONLY: deterministic paths, never represented as historical prices."""
    rng = random.Random(seed)
    start = datetime(2026,9,30,14,25,tzinfo=TZ)
    price, volume = 35000, 10000  # Price in integer ticks.
    closing_quote = None
    for i in range(count):
        received = (start+timedelta(seconds=i*5)).timestamp()
        if i > 420:
            yield replace(closing_quote,received=received)
            continue
        change = rng.choice((-4,-2,0,2,4))
        if kind == "downtrend": change -= 3
        if kind == "uptrend": change += 3
        if kind == "shock" and i == count//2: change -= 250
        if kind == "range": change += round((35000-price)*0.2)
        price += change
        bid_size, ask_size = (80,10) if change > 0 else (10,80)
        if kind == "adverse_obi": bid_size,ask_size = ask_size,bid_size
        if kind == "illiquid": bid_size=ask_size=1
        volume += 0 if kind == "illiquid" else rng.randint(1,30)
        stamp = received
        lag = 15 if kind == "stale" and count//3 <= i < count//2 else 0
        closing_quote = Quote(round((price-2)*.2,1),round((price+2)*.2,1),bid_size,ask_size,
                              round(price*.2,1),volume,8000,6000,stamp-lag,stamp)
        yield closing_quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/backtest-source.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/backtest.json"))
    parser.add_argument("--stress-only",action="store_true")
    args = parser.parse_args()
    baseline = Config(max_position=2,flatten_time="14:55",entry_filter=False)
    candidate = replace(baseline,max_position=1,flatten_time="14:50",entry_filter=True)
    profiles = {"baseline": baseline, "candidate": candidate}
    report = {"symbol":"IM2610", "fee_bps":0,
              "historical":None, "stress_is_synthetic":True,
              "profiles":{name:asdict(config) for name,config in profiles.items()},
              "profile_hashes":{name:config_hash(config) for name,config in profiles.items()},"stress":[]}
    if not args.stress_only:
        quotes, metadata = load_quotes(args.db)
        report["historical"] = {"quality":metadata,"replay":replay(quotes,baseline),
                                "out_of_sample":out_of_sample(quotes,metadata,profiles)}
    for kind in ("range","uptrend","downtrend","shock","adverse_obi","illiquid","stale"):
        for seed in (2610,2611):
            quotes = list(stress_quotes(kind,seed))
            for name, config in (("baseline",baseline),("candidate",candidate)):
                for model,delay,overhead in (("through",3,10),("through",10,20),("touch",3,10)):
                    result = replay(quotes,config,model=model,delay=delay,cycle_overhead=overhead)
                    report["stress"].append({"scenario":kind,"seed":seed,"profile":name,**result})
    report["decision"] = "Risk-only candidate: lower inventory and earlier reduce-only. No statistical alpha optimization without historical L1 and calibrated fills."
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    for name in report["profiles"]:
        rows = [r for r in report["stress"] if r["profile"]==name]
        print(name, "cases:",len(rows),"worst marked PnL:",min(r["pnl_marked"] for r in rows),
              "max drawdown:",max(r["max_drawdown"] for r in rows),
              "residual cases:",sum(r["ending_long"]+r["ending_short"]>0 for r in rows),
              "latched:",sum(r["latched"] for r in rows))
    print("Historical quality:", report["historical"]["quality"] if report["historical"] else "not supplied")
    print("Saved:",args.output,"(synthetic scenarios do not establish profitability)")


if __name__ == "__main__":
    main()
