"""Offline archive/fault checks: python -m unittest -q test_collector."""
import argparse
import signal
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core import Quote, TZ, Unsafe
from engine import Store
from scripts.collect_l1 import Collector, health, interval_arg, main

TS = datetime(2026, 9, 30, 10, 5, tzinfo=TZ).timestamp()
QUOTE = Quote(7000, 7000.6, 30, 10, 7000.2, 1000, 7700, 6300, TS, TS)


class CollectorTests(unittest.TestCase):
    def test_fault_backoff_recovery_and_restart_deduplication(self):
        with tempfile.TemporaryDirectory() as directory, patch("time.time", return_value=TS):
            path = Path(directory) / "l1.sqlite3"
            store = Store(path, tick_retention=None)
            collector = Collector(store)
            self.assertEqual(collector.step(TS, lambda: QUOTE), 5)
            collector.step(TS, lambda: QUOTE)
            collector.step(TS, lambda: replace(QUOTE, timestamp=TS-1))
            self.assertEqual(collector.state["accepted"], 1)
            self.assertEqual(collector.state["duplicates"], 1)
            self.assertEqual(collector.state["source_reversed"], 1)
            def offline():
                raise Unsafe("arbitrary upstream failure must not be printed")
            for expected in (10, 20, 40, 60, 60):
                self.assertEqual(collector.step(TS, offline), expected)
            collector.step(TS+1, lambda: replace(QUOTE, timestamp=TS+1, received=TS+1))
            self.assertEqual(collector.state["consecutive_failures"], 0)
            self.assertEqual(collector.state["accepted"], 2)
            collector.step(TS+2, lambda: replace(QUOTE, timestamp=TS+2, received=TS+2, volume=999))
            self.assertEqual(collector.state["accepted"], 2)
            restarted = Collector(Store(path, tick_retention=None))
            restarted.step(TS+1, lambda: replace(QUOTE, timestamp=TS+1, received=TS+1))
            self.assertEqual(restarted.state["accepted"], 2)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0], 2)
            # More than the UI's rolling retention remains available in this archive.
            with store.connect() as db:
                payload = db.execute("SELECT payload FROM ticks LIMIT 1").fetchone()[0]
                db.executemany("INSERT INTO ticks(ts,payload) VALUES (?,?)", [(TS, payload)] * 10000)
            collector.step(TS+2, lambda: replace(QUOTE, timestamp=TS+2, received=TS+2))
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0], 10003)

    def test_only_fresh_session_records_and_read_only_health(self):
        with tempfile.TemporaryDirectory() as directory, patch("time.time", return_value=TS):
            path = Path(directory) / "l1.sqlite3"
            collector = Collector(Store(path, tick_retention=None))
            collector.step(TS+9, lambda: QUOTE)
            self.assertEqual(collector.state["accepted"], 0)
            self.assertFalse(health(path, TS+9)["healthy"])
            collector.step(TS, lambda: QUOTE)
            self.assertTrue(health(path, TS)["healthy"])
            self.assertFalse(health(path, TS+91)["healthy"])
            close = datetime(2026, 9, 30, 15, 0, tzinfo=TZ).timestamp()
            with patch("scripts.collect_l1.fetch_quote") as fetch:
                collector.step(close)
                self.assertFalse(fetch.called)
            self.assertEqual(collector.state["phase"], "waiting_session")
            self.assertTrue(health(path, close)["healthy"])
            collector.stop()
            self.assertFalse(health(path, TS)["healthy"])
            self.assertFalse(health(Path(directory) / "missing.sqlite3", TS)["healthy"])

    def test_interval_boundary(self):
        self.assertEqual(interval_arg("2"), 2)
        self.assertEqual(interval_arg("60"), 60)
        for value in ("1", "61", "nan", "inf"):
            with self.assertRaises(argparse.ArgumentTypeError):
                interval_arg(value)

    def test_sigterm_interrupts_wait_and_marks_stopped(self):
        handlers = {}
        original = Collector.step
        def step_and_stop(collector):
            delay = original(collector, TS, lambda: QUOTE)
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return delay
        with tempfile.TemporaryDirectory() as directory, patch("time.time", return_value=TS):
            path = Path(directory) / "l1.sqlite3"
            with patch("sys.argv", ["collect_l1.py", "--db", str(path)]), \
                    patch("signal.signal", side_effect=lambda kind, handler: handlers.update({kind: handler})), \
                    patch.object(Collector, "step", step_and_stop):
                self.assertEqual(main(), 0)
            report = health(path, TS)
            self.assertEqual(report["phase"], "stopped")
            self.assertEqual(report["accepted"], 1)
            self.assertFalse(report["healthy"])


if __name__ == "__main__":
    unittest.main()
