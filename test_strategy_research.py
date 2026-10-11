"""Deterministic offline policy checks; no credentials, network or service changes."""
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from core import Config, Quote, TZ, make_quotes, validate_order
from scripts.research_policies import Policy
from scripts import backtest
from scripts.research_strategies import scenario_quotes, trial

TS = datetime(2026, 9, 30, 10, tzinfo=TZ).timestamp()
CONFIG = Config(max_position=2)


def quote(mid=7000, seconds=0, **changes):
    return replace(Quote(round(mid-.2, 1), round(mid+.2, 1), 30, 30,
                         round(mid, 1), 1000+int(seconds), 7700, 6300,
                         TS+seconds, TS+seconds), **changes)


def plans(policy, q, inventory=0, long=0, short=0, reduce_only=False):
    result = policy(q, inventory, long, short, CONFIG, reduce_only=reduce_only)
    for plan in result:
        validate_order(plan, q)
    return result


class ResearchPolicyTests(unittest.TestCase):
    def test_warmup_unique_source_and_counters(self):
        policy = Policy("selective_mm")
        for seconds in (0, 20, 40, 60, 80, 100):
            self.assertEqual(plans(policy, quote(seconds=seconds)), [])
        q = quote(seconds=120)
        self.assertEqual([p["op"] for p in plans(policy, q)], ["0", "1"])
        saved = dict(policy.stats)
        for _ in range(5):
            self.assertEqual([p["op"] for p in plans(policy, q)], ["0", "1"])
        self.assertEqual(policy.stats, saved)
        self.assertEqual(len(policy.samples), 7)
        self.assertEqual(policy.stats, {"observed": 14, "allowed": 2, "blocked": 12})
        # A new eligible side on the same source is counted once, closing calls are not openings.
        one_side = Policy("baseline")
        plans(one_side, q, inventory=1, long=1)
        self.assertEqual(one_side.stats["observed"], 1)
        plans(one_side, q)
        plans(one_side, q)
        self.assertEqual(one_side.stats["observed"], 2)

    def test_reset_and_source_window(self):
        for next_seconds in (60, 5500, 86400):
            policy = Policy("selective_mm")
            for seconds in range(0, 141, 20):
                plans(policy, quote(seconds=seconds))
            self.assertTrue(policy.opening_ops)
            self.assertEqual(plans(policy, quote(seconds=next_seconds)), [])
            self.assertEqual(len(policy.samples), 1)
        policy = Policy("selective_mm")
        for seconds in range(0, 501, 20):
            plans(policy, quote(seconds=seconds))
        self.assertEqual(policy.samples[0][0], TS+200)
        self.assertEqual(len(policy.samples), 16)

    def test_filter_never_changes_or_removes_passive_closes(self):
        q = quote()
        for name in ("selective_mm", "trend_pullback", "flat"):
            for inventory, long, short in ((1, 1, 0), (-1, 0, 1), (0, 1, 1)):
                expected = [p for p in make_quotes(q, inventory, long, short, CONFIG)
                            if p["op"] in ("2", "3")]
                self.assertEqual(plans(Policy(name), q, inventory, long, short), expected)
                expected = make_quotes(q, inventory, long, short, CONFIG, reduce_only=True)
                self.assertEqual(plans(Policy(name), q, inventory, long, short, True), expected)

    def test_selective_filter_range_depth_spread_jump_and_trend(self):
        def warm(mids, **changes):
            policy = Policy("selective_mm")
            for i, mid in enumerate(mids):
                result = plans(policy, quote(mid, i*24, **(changes if i == len(mids)-1 else {})))
            return result
        mids = (7000, 7000.4, 6999.6, 7000.4, 6999.6, 7000)
        self.assertEqual(len(warm(mids)), 2)
        self.assertEqual(warm(mids, bid_size=9), [])
        self.assertEqual(warm(mids, ask=7001.4), [])
        self.assertEqual(warm((7000, 7000, 7005, 7000, 7000, 7000)), [])
        self.assertEqual(warm((7000, 7002, 7004, 7006, 7008, 7010)), [])

    def test_trend_pullback_is_symmetric_and_nonempty(self):
        for direction, operation in ((1, "0"), (-1, "1")):
            policy = Policy("trend_pullback")
            offsets = (0, 2, 4, 6, 8, 7.8)
            for i, offset in enumerate(offsets):
                result = plans(policy, quote(7000+direction*offset, i*24))
            self.assertEqual([p["op"] for p in result], [operation])
            self.assertGreater(policy.stats["allowed"], 0)
            self.assertEqual(plans(policy, quote(7000+direction*9, 144)), [])
            self.assertEqual(plans(policy, quote(7000+direction*7, 168)), [])

    def test_baseline_and_opening_priority_preserve_closing_slots(self):
        q = quote()
        for inventory, long, short in ((0, 0, 0), (1, 1, 0), (-1, 0, 1), (0, 1, 1)):
            original = make_quotes(q, inventory, long, short, CONFIG)
            self.assertEqual(plans(Policy("baseline"), q, inventory, long, short), original)
            changed = plans(Policy("baseline", "sell_first"), q, inventory, long, short)
            for index, plan in enumerate(original):
                if plan["op"] in ("2", "3"):
                    self.assertEqual(changed[index], plan)
        self.assertEqual([p["op"] for p in plans(Policy("baseline", "sell_first"), q)], ["1", "0"])
        for args in (("unknown",), ("baseline", "random")):
            with self.assertRaises(ValueError):
                Policy(*args)


class ResearchReplayTests(unittest.TestCase):
    def test_fee_cash_and_activation_source_intervals(self):
        def exercise(rows, config, **options):
            store = backtest.ReplayStore()
            broker = backtest.ReplayPaper(store, config, "through", 3, 1)
            broker.clock = TS
            broker.on_quote(quote(7000.4))
            broker.submit({"op": "0", "side": "buy", "price": 7000, "qty": 1}, quote(7000.4))
            # Received after activation, but source precedes it: cannot fill.
            broker.clock = TS+18
            broker.on_quote(quote(6999.6, 10, received=TS+18))
            self.assertEqual(len(broker.state["trades"]), 0)
            # First interval straddles activation; only establish its volume baseline.
            broker.clock = TS+19
            broker.on_quote(quote(6999.6, 15, received=TS+19))
            self.assertEqual(len(broker.state["trades"]), 0)
            broker.clock = TS+20
            broker.on_quote(quote(6999.6, 20))
            self.assertEqual(broker.state["long"], 1)
            broker.submit({"op": "3", "side": "sell", "price": 7000, "qty": 1}, quote(6999.6, 20))
            for seconds in (35, 40):
                broker.clock = TS+seconds
                broker.on_quote(quote(7000.4, seconds))
            self.assertEqual(broker.state["long"], 0)
            cash = broker.state["cash"]
            broker.on_quote(quote(7000.4, 40, received=TS+41))
            self.assertEqual(broker.state["cash"], cash)
            store.db.close()
            return {"pnl_realized": cash-1_000_000}
        for fees, expected in (("zero", 0), ("exchange_reference", 354.2)):
            with patch("scripts.research_strategies.replay", side_effect=exercise):
                result = trial([quote()], Config(), "baseline", fees=fees, cycle_overhead=10)
            self.assertAlmostEqual(result["pnl_realized"], -expected)
            self.assertAlmostEqual(result["fees_paid"], expected)
            self.assertEqual((result["opening_lots"], result["closed_lots"]), (1, 1))

    def test_fractional_volume_is_not_lost_when_samples_are_split(self):
        def exercise(rows, config, **options):
            store = backtest.ReplayStore()
            broker = backtest.ReplayPaper(store, config, "through", 0, .1)
            broker.clock = TS
            broker.on_quote(quote(7000.4))
            broker.submit({"op": "0", "side": "buy", "price": 7000, "qty": 1}, quote(7000.4))
            for q in rows:
                broker.clock = q.received
                broker.on_quote(q)
            self.assertEqual(broker.participation, .1)
            self.assertEqual(broker.state["long"], 1)
            store.db.close()
            return {}
        for seconds in ((10,), range(1, 11)):
            rows = [quote(6999.6, s) for s in seconds]
            with patch("scripts.research_strategies.replay", side_effect=exercise):
                result = trial(rows, Config(), "baseline", cycle_overhead=0)
            self.assertEqual(result["opening_lots"], 1)

    def test_generated_frozen_sources_and_offline_patch_restoration(self):
        for scenario in ("independent_obi", "whipsaw"):
            rows = list(scenario_quotes(scenario, 2610))
            frozen = rows[420]
            for q in rows[421:]:
                self.assertEqual(replace(q, received=frozen.received), frozen)
        import engine
        original_quotes, original_paper = engine.make_quotes, backtest.ReplayPaper
        result = trial([quote(), quote(seconds=5)], Config(), "flat")
        self.assertEqual(result["fill_lots"], 0)
        self.assertIs(engine.make_quotes, original_quotes)
        self.assertIs(backtest.ReplayPaper, original_paper)


if __name__ == "__main__":
    unittest.main()
