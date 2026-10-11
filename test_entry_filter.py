"""Production entry-filter checks; isolated paper account, no network or credentials."""
import unittest
from dataclasses import asdict
from datetime import datetime
from unittest.mock import patch

import test_bot as fixtures
from core import Config, EntryFilter, TZ, Unsafe, make_quotes, validate_order
from engine import Engine


class EntryFilterTests(unittest.TestCase):
    def setUp(self):
        fixtures.BotTests.setUp(self)
        self.config.entry_filter = True
        self.store.put("config", asdict(self.config))
        self.seconds = 0
        test = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                now = datetime.fromtimestamp(fixtures.TS + test.seconds, TZ)
                return now if tz else now.replace(tzinfo=None)

        clock = patch("engine.datetime", Clock)
        timer = patch("time.time", side_effect=lambda: fixtures.TS + self.seconds)
        clock.start()
        timer.start()
        self.addCleanup(clock.stop)
        self.addCleanup(timer.stop)
        self.engine.config = self.config
        self.engine.entry_filter = EntryFilter()
        self.current = self.quote()
        self.engine.feed = lambda: self.current
        self.engine.account = {"equity": 1_000_000, "use_rate": 0}

    def quote(self, mid=7000.2, **changes):
        return fixtures.quote(**{"bid": round(mid-.2, 1), "ask": round(mid+.2, 1),
            "bid_size": 30, "ask_size": 30, "last": round(mid, 1), "volume": 1000+int(self.seconds),
            "timestamp": fixtures.TS+self.seconds, "received": fixtures.TS+self.seconds, **changes})

    def advance(self, seconds, mid=7000.2, **changes):
        self.seconds = seconds
        self.current = self.quote(mid, **changes)
        return self.current

    def plans(self, q=None):
        q = q or self.current
        plans, _ = self.engine.risk_and_plan(q, datetime.fromtimestamp(q.timestamp, TZ))
        for plan in plans:
            validate_order(plan, q)
        return plans

    def warm(self, mids=(7000.2,)*6):
        for i, mid in enumerate(mids):
            plans = self.plans(self.advance(i*24, mid))
        return plans

    def test_default_legacy_config_and_boolean_validation(self):
        self.assertTrue(Config.parse({}).entry_filter)
        for value in (True, False):
            self.assertIs(Config.parse({"entry_filter": value}).entry_filter, value)
        for value in (0, 1, "true", "false", None):
            with self.assertRaises(Unsafe):
                Config.parse({"entry_filter": value})
        legacy = asdict(self.config)
        legacy.pop("entry_filter")
        self.store.put("config", legacy)
        restarted = Engine(self.store, self.broker, feed=lambda: self.current)
        self.assertTrue(restarted.config.entry_filter)

    def test_warmup_repeated_sources_and_same_source_thin_book(self):
        for seconds in (0, 24, 48, 72, 96):
            q = self.advance(seconds)
            self.assertEqual(self.plans(q), [])
            count = len(self.engine.entry_filter.samples)
            for _ in range(3):
                self.assertEqual(self.plans(q), [])
            self.assertEqual(len(self.engine.entry_filter.samples), count)
        self.assertEqual([p["op"] for p in self.plans(self.advance(120))], ["0", "1"])
        count = len(self.engine.entry_filter.samples)
        self.current = self.quote(bid_size=9)
        self.assertEqual(self.plans(), [])
        self.assertEqual(len(self.engine.entry_filter.samples), count)
        self.engine.publish()
        self.assertIsInstance(self.engine.snapshot["entry_filter"], dict)
        self.assertFalse(self.engine.snapshot["entry_filter"]["allowed"])

    def test_bad_regimes_block_openings_and_preserve_passive_closes(self):
        cases = {"wide": ((7000.2,)*6, {"ask": 7001.6}),
                 "trend": ((7000.2, 7000.6, 7001, 7001.4, 7001.8, 7002.2), {}),
                 "jump": ((7000.2, 7000.2, 7005.2, 7000.2, 7000.2, 7000.2), {})}
        for name, (mids, changes) in cases.items():
            with self.subTest(regime=name):
                self.engine.entry_filter = EntryFilter()
                self.engine.fair, self.engine.fair_ts = None, 0
                self.engine.holdings = {"long": 0, "short": 0, "available_long": 0, "available_short": 0}
                self.warm(mids)
                self.current = self.quote(mids[-1], **changes)
                self.assertEqual(self.plans(), [])
                for long, short in ((1, 0), (0, 1)):
                    self.engine.holdings = {"long": long, "short": short,
                                           "available_long": long, "available_short": short}
                    result = self.plans()
                    expected = [p for p in make_quotes(self.current, long-short, long, short,
                        self.config, self.engine.fair) if p["op"] in ("2", "3")]
                    self.assertEqual(result, expected)

    def test_working_opening_cancels_once_and_waits_for_confirmation(self):
        self.engine.cycle()
        self.engine.command("start")
        for seconds in range(0, 121, 24):
            self.advance(seconds)
            self.engine.cycle()
        self.engine.sync()
        self.assertEqual(len(self.broker.orders()), 1)
        original_intent = self.store.intents()[0]["id"]
        original_cancel = self.broker.cancel
        remote_id = self.broker.orders()[0]["remote_id"]
        with patch.object(self.broker, "cancel", return_value=None) as cancel:
            self.advance(144, bid_size=9)
            self.engine.cycle()
            self.assertEqual(self.store.intents()[0]["state"], "CANCELLING")
            self.advance(160)
            self.engine.cycle()
            self.assertEqual(cancel.call_count, 1)
            self.assertEqual(len(self.broker.orders()), 1)
            self.assertEqual(len(self.store.intents()), 1)
        original_cancel(remote_id)
        self.advance(164)
        self.engine.cycle()
        self.assertEqual(next(r["state"] for r in self.store.intents()
                             if r["id"] == original_intent), "CANCELLED")
        self.assertEqual(len(self.broker.orders()), 2)

    def test_submission_rechecks_thin_book_before_creating_intent(self):
        self.engine.cycle()
        self.engine.command("start")
        self.engine.handle_commands()
        self.assertTrue(self.warm())
        good, thin = self.current, self.quote(bid_size=9)
        with patch.object(self.engine, "feed", side_effect=(good, thin)), \
                patch.object(self.broker, "submit") as submit:
            self.engine.cycle()
        self.assertFalse(submit.called)
        self.assertEqual(self.store.intents(), [])

    def test_control_and_configuration_reset_history_and_fair(self):
        self.engine.ready = True
        for action in ("start", "pause", "reconcile"):
            self.assertTrue(self.warm())
            self.engine.command(action)
            self.engine.handle_commands()
            self.assertEqual(len(self.engine.entry_filter.samples), 0)
        self.assertTrue(self.warm())
        self.engine.command("configure", asdict(self.config))
        self.engine.handle_commands()
        self.assertEqual(len(self.engine.entry_filter.samples), 0)
        self.assertIsNone(self.engine.fair)
        self.assertEqual(self.engine.fair_ts, 0)
        self.assertTrue(self.store.get("config")["entry_filter"])

    def test_thin_book_retains_existing_passive_close_order(self):
        self.broker.state.update(long=1, long_cost=7000.2, mid=7000.2)
        self.engine.cycle()
        self.engine.command("start")
        self.advance(24, bid_size=9)
        self.engine.cycle()
        self.engine.sync()
        self.assertEqual([o["op"] for o in self.broker.orders(True)], ["3"])
        self.advance(48, bid_size=9)
        self.engine.cycle()
        self.assertEqual([o["op"] for o in self.broker.orders(True)], ["3"])
        self.assertEqual(len(self.broker.orders()), 1)


if __name__ == "__main__":
    unittest.main()
