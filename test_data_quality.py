"""Coverage gates only; generated fixtures are not market evidence."""
import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from core import Config, Quote, TZ
from engine import Store
from scripts.backtest import config_hash, load_quotes, out_of_sample, quote_quality


def quote(stamp, lag=0):
    return Quote(7000, 7000.6, 30, 10, 7000.2, 1000, 7700, 6300,
                 stamp.timestamp(), stamp.timestamp()+lag)


def complete_days():
    for day in (18, 21, 22, 23, 24):
        for hour, minute in ((9, 30), (13, 0)):
            start = datetime(2026, 9, day, hour, minute, tzinfo=TZ)
            for seconds in range(0, 7200, 30):
                yield quote(start+timedelta(seconds=seconds))


class DataQualityTests(unittest.TestCase):
    def test_complete_days_require_continuity_and_both_sessions(self):
        quotes = list(complete_days())
        quality = quote_quality(quotes)
        self.assertTrue(quality["sufficient_for_parameter_fit"])
        self.assertEqual(len(quality["qualified_days"]), 5)
        self.assertEqual(quality["max_intraday_gap_seconds"], 30)
        partial = [q for q in quotes if not (datetime.fromtimestamp(q.timestamp, TZ).day == 22
                   and 10 <= datetime.fromtimestamp(q.timestamp, TZ).hour < 11)]
        quality = quote_quality(partial)
        self.assertFalse(quality["sufficient_for_parameter_fit"])
        self.assertEqual(quality["max_intraday_gap_seconds"], 3630)
        quality = quote_quality([q for q in quotes if datetime.fromtimestamp(q.timestamp, TZ).day != 22])
        self.assertEqual(quality["missing_trading_days"], ["2026-09-22"])

    def test_duplicate_holiday_stale_boundary_and_source_regression(self):
        start = datetime(2026, 9, 30, 10, tzinfo=TZ)
        rows = [quote(start), quote(start, 1), quote(start+timedelta(seconds=2)),
                quote(start+timedelta(seconds=1), 2), quote(start+timedelta(seconds=3), 9),
                quote(datetime(2026, 9, 30, 11, 29, 59, tzinfo=TZ), 1),
                quote(datetime(2026, 9, 30, 11, 30, tzinfo=TZ), -1),
                quote(datetime(2026, 9, 25, 10, tzinfo=TZ)),
                quote(datetime(2026, 10, 1, 10, tzinfo=TZ)),
                quote(datetime(2026, 10, 2, 10, tzinfo=TZ))]
        quality = quote_quality(rows)
        self.assertEqual(quality["fresh_in_session"], 2)
        self.assertEqual(quality["source_time_regressions"], 1)
        self.assertEqual(quality["repeated_source_timestamps"], 1)
        self.assertEqual(quality["stale_or_clock_skew"], 1)
        self.assertEqual(quality["fresh_outside_session"], 5)
        self.assertEqual(quality["fresh_days"], ["2026-09-30"])
        self.assertEqual(quality["max_intraday_gap_seconds"], 7200)

    def test_load_quality_rejects_invalid_and_reordered_rows(self):
        rows = [asdict(q) for q in complete_days()]
        rows.extend([dict(rows[-1], bid=0), dict(rows[0], received=rows[0]["received"]-1)])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"quotes.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows)+"\ninvalid JSON\n[]", encoding="utf-8")
            _, quality = load_quotes(path)
        self.assertEqual(quality["invalid"], 3)
        self.assertEqual(quality["out_of_order"], 1)
        self.assertFalse(quality["sufficient_for_parameter_fit"])
        self.assertEqual(len(quality["qualified_days"]), 5)

    def test_out_of_sample_split_fixed_profiles_and_no_repeated_source(self):
        quotes = [row for q in complete_days() for row in (q, replace(q, received=q.received+1))]
        profiles = {"baseline": Config(max_position=2, flatten_time="14:55"), "candidate": Config()}
        quality = quote_quality(quotes)
        def fake_replay(rows, config, *, model):
            self.assertEqual(len({q.timestamp for q in rows}), len(rows))
            self.assertEqual(len({datetime.fromtimestamp(q.timestamp, TZ).date() for q in rows}), 1)
            return {"model": model, "pnl_marked": -1, "ending_long": 1}
        with patch("scripts.backtest.replay", side_effect=fake_replay) as run:
            result = out_of_sample(quotes, quality, profiles)
        self.assertEqual(run.call_count, 20)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["train_days"], ["2026-09-18", "2026-09-21", "2026-09-22"])
        self.assertEqual(result["test_days"], ["2026-09-23", "2026-09-24"])
        self.assertTrue(result["daily_reset"])
        self.assertEqual({row["model"] for row in result["results"]}, {"through", "touch"})
        self.assertEqual(sum(row["split"] == "test" for row in result["results"]), 8)
        self.assertNotEqual(result["profile_hashes"]["candidate"], result["profile_hashes"]["baseline"])
        self.assertEqual(config_hash(Config()), config_hash(Config.parse(asdict(Config()))))
        with patch("scripts.backtest.replay") as run:
            result = out_of_sample(quotes, quote_quality(quotes[:100]), profiles)
        self.assertFalse(run.called)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertTrue(result["reasons"])

    def test_formats_stream_records_hash_and_close_sqlite(self):
        records = [asdict(q) for q in list(complete_days())[:2]]
        connections = []
        original_connect = sqlite3.connect
        class TrackedConnection(sqlite3.Connection):
            closed = False
            def close(self):
                self.closed = True
                super().close()
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=TrackedConnection)
            connections.append(connection)
            return connection
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/("l1"+suffix) for suffix in (".jsonl", ".csv", ".sqlite3")]
            paths[0].write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
            with paths[1].open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=records[0].keys())
                writer.writeheader()
                writer.writerows(records)
            with closing(original_connect(paths[2])) as db, db:
                db.execute("CREATE TABLE ticks (id INTEGER PRIMARY KEY, payload TEXT)")
                db.executemany("INSERT INTO ticks(payload) VALUES (?)", [(json.dumps(row),) for row in records])
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            l1_hashes = []
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must stream hash")), \
                    patch.object(Path, "read_text", side_effect=AssertionError("must stream records")), \
                    patch("scripts.backtest.sqlite3.connect", side_effect=connect):
                for path, digest in zip(paths, hashes):
                    quotes, quality = load_quotes(path)
                    self.assertEqual(len(quotes), 2)
                    self.assertEqual(quality["rows"], 2)
                    self.assertEqual(quality["sha256"], digest)
                    l1_hashes.append(quality["l1_sha256"])
            self.assertEqual(len(connections), 1)
            self.assertTrue(connections[0].closed)
            self.assertEqual(len(set(l1_hashes)), 1)

    def test_l1_hash_sees_wal_and_ignores_settings_and_matches_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path, backup = Path(folder)/"live.sqlite3", Path(folder)/"snapshot.sqlite3"
            store = Store(path)
            with closing(sqlite3.connect(path)) as hold:
                first_quote = quote(datetime(2026, 9, 30, 10, tzinfo=TZ))
                self.assertTrue(store.tick(first_quote, 7000, 1_000_000, 0))
                hold.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                first = load_quotes(path)[1]
                second_quote = replace(first_quote, timestamp=first_quote.timestamp+1, received=first_quote.received+1)
                self.assertTrue(store.tick(second_quote, 7000, 1_000_000, 0))
                second = load_quotes(path)[1]
                self.assertEqual(first["sha256"], second["sha256"])
                self.assertNotEqual(first["l1_sha256"], second["l1_sha256"])
                self.assertEqual(second["rows"], 2)
                hold.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                before_heartbeat = load_quotes(path)[1]
                store.put("collector_status", {"at": 1})
                hold.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                heartbeat = load_quotes(path)[1]
                self.assertNotEqual(before_heartbeat["sha256"], heartbeat["sha256"])
                self.assertEqual(second["l1_sha256"], heartbeat["l1_sha256"])
                with closing(sqlite3.connect(backup)) as destination:
                    hold.backup(destination)
                self.assertEqual(load_quotes(backup)[1]["l1_sha256"], heartbeat["l1_sha256"])

    def test_fresh_volume_regression_is_unique_and_resets_across_days(self):
        first = quote(datetime(2026, 9, 30, 10, tzinfo=TZ))
        bad = replace(first, timestamp=first.timestamp+1, received=first.received+1, volume=999)
        later = replace(first, timestamp=first.timestamp+2, received=first.received+2, volume=1001)
        next_day = replace(quote(datetime(2026, 10, 8, 10, tzinfo=TZ)), volume=5)
        quality = quote_quality([first, bad, replace(bad, received=bad.received+.5, volume=998), later, next_day])
        self.assertEqual(quality["volume_regressions"], 1)
        self.assertEqual(quality["fresh_in_session"], 3)
        self.assertEqual(quality["source_time_regressions"], 0)
        self.assertFalse(quality["sufficient_for_parameter_fit"])
        self.assertTrue(any("cumulative volumes regressed" in reason for reason in quality["sufficiency_reasons"]))


if __name__ == "__main__":
    unittest.main()
