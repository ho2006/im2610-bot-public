"""Offline strategy comparisons, never account access or automatic promotion."""
import argparse
import hashlib
import json
import random
import sys
from dataclasses import asdict, replace
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from statistics import median
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config, MULTIPLIER, TZ
from scripts.backtest import (ReplayPaper, config_hash, load_quotes, out_of_sample,
                              replay, stress_quotes)
from scripts.research_policies import Policy

PROFILES = ("baseline", "selective_mm", "trend_pullback", "flat")
# The second case is a published exchange reference, NOT verified simulator fees.
# All trials start flat and last one day, so every close is a same-day close.
FEES = {"zero": (0, 0), "exchange_reference": (.23, 2.3)}  # bps of notional
MODELS = (("through", 3, 10), ("through", 10, 20), ("touch", 3, 10))
SCENARIOS = ("range", "uptrend", "downtrend", "shock", "adverse_obi",
             "illiquid", "stale", "independent_obi", "whipsaw")
SEEDS = (2610, 2611)


def trial(quotes, config, profile, *, fees="zero", priority="buy_first", **kwargs):
    """Keep production risk/execution; replace only offline quote selection and fees."""
    if config.fee_per_side_bps != 0:
        raise ValueError("Trials require zero config fees to avoid double charging")
    production = profile == "production"
    config = replace(config, entry_filter=production)
    policy, brokers = Policy("baseline" if production else profile, opening_priority=priority), []
    open_bps, close_bps = FEES[fees]
    read_delay = kwargs.get("cycle_overhead", 10)

    class MeasuredPaper(ReplayPaper):
        def __init__(self, *args, **options):
            super().__init__(*args, **options)
            self.fees_paid = self.open_lots = self.close_lots = 0
            self.exposure_seconds = self.max_holding_seconds = 0
            self.previous_time = self.holding_since = None
            self.previous_gross = 0
            self.volume_fraction = 0
            brokers.append(self)

        def submit(self, plan, quote):
            super().submit(plan, quote)
            self.state["orders"][-1]["live_after"] += read_delay

        def cancel(self, ident):
            super().cancel(ident)
            row = next(r for r in self.state["orders"] if r["remote_id"] == ident)
            row["cancel_after"] += read_delay

        def matches(self, order, quote):
            # Only volume intervals wholly after activation may fill a new order.
            active = order["live_after"]
            return (self.state["quote_ts"] >= active and quote.timestamp > active
                    and super().matches(order, quote))

        def observe(self, quote):
            now = quote.received
            if self.previous_time is not None and self.previous_gross:
                self.exposure_seconds += max(0, now - self.previous_time)
                self.max_holding_seconds = max(self.max_holding_seconds, now - self.holding_since)
            gross = self.state["long"] + self.state["short"]
            if gross and not self.previous_gross:
                self.holding_since = now
            elif not gross:
                self.holding_since = None
            self.previous_time, self.previous_gross = now, gross

        def on_quote(self, quote):
            previous = len(self.state["trades"])
            participation = self.participation
            if quote.timestamp > self.state["quote_ts"]:
                same_day = (datetime.fromtimestamp(quote.timestamp, TZ).date()
                            == datetime.fromtimestamp(self.state["quote_ts"], TZ).date())
                delta = max(0, quote.volume - self.state["volume"]) if same_day else 0
                if not same_day:
                    self.volume_fraction = 0
                if delta:
                    budget = self.volume_fraction + delta * participation
                    capacity = int(budget + 1e-9)
                    self.volume_fraction = max(0, budget - capacity)
                    # Retain fractional volume without copying Paper's fill/accounting loop.
                    # Adding half a lot makes its int(delta * participation) stable.
                    self.participation = (capacity + .5) / delta
            try:
                super().on_quote(quote)
            finally:
                self.participation = participation
            cost = 0
            for fill in self.state["trades"][previous:]:
                opening = fill["operation"] in ("买开", "卖开")
                rate = open_bps if opening else close_bps
                cost += fill["price"] * fill["qty"] * MULTIPLIER * rate / 10000
                if opening:
                    self.open_lots += fill["qty"]
                else:
                    self.close_lots += fill["qty"]
            self.fees_paid += cost
            if cost:
                self.state["cash"] -= cost
                self.save()
            self.observe(quote)

        def mark(self, quote):
            super().mark(quote)
            self.observe(quote)

    # ponytail: module patches are serial/offline only; do not run trials in threads.
    quotes_patch = nullcontext() if production and priority == "buy_first" else patch("engine.make_quotes", policy)
    with quotes_patch, patch("scripts.backtest.ReplayPaper", MeasuredPaper):
        result = replay(quotes, config, **kwargs)
    result.update(profile=profile, fee_case=fees, opening_priority=priority,
                  policy_stats={} if production else dict(policy.stats), production_entry_filter=production)
    if brokers:
        broker = brokers[0]
        result.update(fees_paid=round(broker.fees_paid, 2), opening_lots=broker.open_lots,
                      closed_lots=broker.close_lots, exposure_seconds=broker.exposure_seconds,
                      max_holding_seconds=broker.max_holding_seconds)
    return result


def scenario_quotes(kind, seed):
    """Synthetic mechanisms only; independent OBI and reversals weaken easy toy alpha."""
    underlying = "range" if kind in ("independent_obi", "whipsaw") else kind
    rng = random.Random(seed + 90210)
    previous = None
    for i, q in enumerate(stress_quotes(underlying, seed)):
        if previous is not None and q.timestamp == previous.timestamp:
            yield replace(previous, received=q.received)
            continue
        if kind == "independent_obi":
            bid_size, ask_size = rng.choice(((10, 80), (80, 10), (40, 40)))
            q = replace(q, bid_size=bid_size, ask_size=ask_size)
        elif kind == "whipsaw":
            step = min(i, 420) % 120
            offset = 3 * min(step, 120 - step) * .2
            q = replace(q, bid=round(q.bid + offset, 1), ask=round(q.ask + offset, 1),
                        last=round(q.last + offset, 1), bid_size=40, ask_size=40)
        previous = q
        yield q


def summarize(rows):
    """Case medians are descriptive, not a probability-weighted expected return."""
    return {"cases": len(rows), "median_marked_pnl": round(median(r["pnl_marked"] for r in rows), 2),
            "median_realized_pnl": round(median(r["pnl_realized"] for r in rows), 2),
            "worst_realized_pnl": min(r["pnl_realized"] for r in rows),
            "worst_marked_pnl": min(r["pnl_marked"] for r in rows),
            "max_drawdown": max(r["max_drawdown"] for r in rows),
            "profitable_cases": sum(r["pnl_marked"] > 0 for r in rows),
            "losing_cases": sum(r["pnl_marked"] < 0 for r in rows),
            "traded_cases": sum(r["fill_lots"] > 0 for r in rows),
            "residual_cases": sum(r["ending_long"] + r["ending_short"] > 0 for r in rows),
            "median_orders": median(r["orders"] for r in rows),
            "median_exposure_seconds": median(r["exposure_seconds"] for r in rows),
            "max_holding_seconds": max(r["max_holding_seconds"] for r in rows),
            "max_gross": max(r["max_gross"] for r in rows),
            "pending_cases": sum(r["pending"] > 0 for r in rows),
            "latched_cases": sum(r["latched"] for r in rows)}


def historical_comparison(path, config):
    quotes, quality = load_quotes(path)
    comparisons = {}
    for profile in PROFILES:
        # out_of_sample supplies one day per call: new policy, flat inventory, no carry.
        def run(rows, cfg, **kwargs):
            return trial(rows, cfg, profile, **kwargs)
        with patch("scripts.backtest.replay", run):
            comparisons[profile] = out_of_sample(quotes, quality, {profile: config})
    return {"quality": quality, "comparisons": comparisons,
            "fees": "zero", "opening_priority": "buy_first"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=Path("data/backtest-source.sqlite3"))
    parser.add_argument("--stress-only", action="store_true")
    parser.add_argument("--production-check", action="store_true",
                        help="Include the integrated engine filter, compared against offline policies")
    parser.add_argument("--output", type=Path, default=Path("artifacts/strategy-research.json"))
    args = parser.parse_args()
    config = Config(entry_filter=False)
    profiles = (*PROFILES, "production") if args.production_check else PROFILES
    root = Path(__file__).resolve().parents[1]
    sources = ("core.py", "engine.py", "scripts/backtest.py", "scripts/research_policies.py",
               "scripts/research_strategies.py")
    report = {"symbol": "IM2610", "config": asdict(config), "config_hash": config_hash(config),
              "code_hashes": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
              "synthetic": True, "seeds": SEEDS, "scenarios": SCENARIOS,
              "production_filter_config": asdict(replace(config, entry_filter=True)) if args.production_check else None,
              "fee_assumptions_bps": FEES, "historical": None, "stress": [], "summary": {},
              "limitations": ["Synthetic scenarios are not historical performance or expected returns.",
                              "Snapshot fills, queue priority, fees and delays need venue calibration.",
                              "Each case starts flat with 1,000,000; no overnight carry or forced close.",
                              "Fixed strategies: no parameter search or selection on test data.",
                              "Exchange-reference fees are a sensitivity case, not verified simulator charges.",
                              "buy_first reproduces production ordering; sell_first is a bias check only.",
                              "Research matching adds read budget to activation/cancellation delays, skips activation-crossing volume and carries fractional capacity.",
                              "Read timing is a conservative approximation, not a measured request-by-request event simulation.",
                              "Passive exits cannot enforce a maximum loss or guarantee flat inventory.",
                              "Cash control is not eligible for contest ranking without a trade."],
              "promote_to_live": False}
    if not args.stress_only:
        report["historical"] = historical_comparison(args.history, config)
    for kind in SCENARIOS:
        for seed in SEEDS:
            quotes = list(scenario_quotes(kind, seed))
            for fees in FEES:
                for priority in ("buy_first", "sell_first"):
                    for profile in profiles:
                        for model, delay, overhead in MODELS:
                            result = trial(quotes, config, profile, fees=fees, priority=priority,
                                           model=model, delay=delay, cycle_overhead=overhead)
                            report["stress"].append({"scenario": kind, "seed": seed, **result})
        print("Completed synthetic scenario:", kind, flush=True)
    for fees in FEES:
        for priority in ("buy_first", "sell_first"):
            for profile in profiles:
                key = "/".join((fees, priority, profile))
                rows = [r for r in report["stress"] if (r["fee_case"], r["opening_priority"], r["profile"])
                        == (fees, priority, profile)]
                report["summary"][key] = summarize(rows)
    if args.production_check:
        keys = ("scenario", "seed", "fee_case", "opening_priority", "model", "delay_seconds",
                "cycle_overhead_seconds")
        selective = {tuple(r[k] for k in keys): r for r in report["stress"] if r["profile"] == "selective_mm"}
        compare = ("pnl_marked", "pnl_realized", "max_drawdown", "orders", "fill_lots", "pending",
                   "max_gross", "ending_long", "ending_short", "latched", "fees_paid", "exposure_seconds")
        production_rows = [r for r in report["stress"] if r["profile"] == "production"]
        differences = [dict(zip(keys, (r[k] for k in keys))) for r in production_rows
                       if any(r[k] != selective[tuple(r[x] for x in keys)][k] for k in compare)]
        report["production_parity"] = {"cases": len(production_rows), "matches": len(production_rows)-len(differences),
                                       "differences": differences, "compared_metrics": compare}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=True, indent=2))
    print("Saved", args.output, "No account access or strategy activation.")


if __name__ == "__main__":
    main()
