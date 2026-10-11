"""Deterministic checks for the offline OHLC direction diagnosis."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from scripts.research_minutes import Bar, MORNING, AFTERNOON, load_minutes, samples, study, summarize


def row(stamp, close=7000):
    return {"d": stamp.strftime("%Y-%m-%d %H:%M:%S"), "o": str(close), "h": str(close+1),
            "l": str(close-1), "c": str(close), "v": "10", "p": "100"}


def full_day(day, closes=None):
    return [row(datetime.fromisoformat(day+" "+label), closes[i] if closes else 7000+i)
            for i, label in enumerate((*MORNING, *AFTERNOON))]


def write(path, rows):
    path.write_text(json.dumps({"symbol": "IM2610", "period_minutes": 15, "raw_data": rows}), encoding="utf-8")


class MinuteResearchTests(unittest.TestCase):
    def test_causal_four_interval_drift_and_same_common_denominator(self):
        start = datetime(2026, 9, 1, 9, 45)
        bars = [Bar(start+timedelta(minutes=15*i), close) for i, close in enumerate((10, 12, 13, 12, 14, 11))]
        observed = samples(bars)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["past_drift"], 4)
        self.assertEqual(observed[0]["future_change"], -3)
        changed = samples([*bars[:-1], replace(bars[-1], close=100)])
        self.assertEqual(changed[0]["past_drift"], 4)  # Future close cannot select the signal.
        common = [observed[0], {**observed[0], "past_drift": 0, "future_change": 50}]
        momentum = summarize(common, "momentum")
        self.assertEqual(momentum["samples"], 2)
        self.assertEqual(momentum["effective_signals"], 1)
        self.assertEqual(momentum["empty_signal_ratio"], .5)
        self.assertEqual(momentum["mean_points"], -1.5)
        self.assertEqual(summarize(common, "mean_reversion")["mean_points"], 1.5)
        self.assertEqual(summarize(common, "always_long")["effective_signals"], 2)
        with self.assertRaises(ValueError):
            summarize(common, "unknown")

    def test_sessions_days_and_missing_intervals_cannot_cross(self):
        start = datetime(2026, 9, 1, 9, 45)
        bars = [Bar(start+timedelta(minutes=15*i), 7000+i) for i in range(8)]
        self.assertEqual(len(samples(bars)), 3)
        self.assertEqual(samples(bars[:4]+bars[5:]), [])
        lunch = [Bar(datetime(2026, 9, 1, 10, 45)+timedelta(minutes=15*i), 7000+i) for i in range(4)]
        lunch += [Bar(datetime(2026, 9, 1, 13, 15)+timedelta(minutes=15*i), 7004+i) for i in range(4)]
        self.assertEqual(samples(lunch), [])
        next_day = [*bars[:4], replace(bars[4], stamp=bars[4].stamp+timedelta(days=1)),
                    replace(bars[5], stamp=bars[5].stamp+timedelta(days=1))]
        self.assertEqual(samples(next_day), [])

    def test_strict_quality_counts_and_no_gap_filling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"source.json"
            rows = full_day("2026-09-01")
            del rows[12]  # Missing 14:15, retained as a gap rather than filled.
            rows.extend([dict(rows[-1]), row(datetime(2026, 9, 1, 10), 7001),
                         {**row(datetime(2026, 9, 2, 9, 45)), "c": "nan"},
                         {**row(datetime(2026, 9, 2, 10)), "v": "-1"},
                         {**row(datetime(2026, 9, 2, 10, 15)), "h": "1"},
                         row(datetime(2026, 9, 2, 12)),
                         {"d": "bad date"}])
            # The earlier timestamp must be unseen to count as reverse rather than duplicate.
            rows[16] = row(datetime(2026, 9, 1, 14, 15), 7001)
            write(path, rows)
            bars, quality = load_minutes(path)
            self.assertEqual(len(bars), 15)
            self.assertEqual(quality["duplicate_timestamps"], 1)
            self.assertEqual(quality["out_of_order_timestamps"], 1)
            self.assertEqual(quality["invalid_records"], 4)
            self.assertEqual(quality["outside_session_records"], 1)
            self.assertEqual(quality["non_contiguous_session_intervals"], 1)
            self.assertEqual(quality["complete_days"], [])
            self.assertIn("14:15", quality["incomplete_days"][0]["missing_labels"])

    def test_chronological_dates_and_shared_samples_do_not_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"source.json"
            days = ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07")
            rows = [r for day in days for r in full_day(day)]
            write(path, rows)
            original = path.read_bytes()
            before = study(path)
            self.assertEqual(before["split_days"], {"train": list(days[:3]), "test": list(days[3:])})
            self.assertEqual(before["common_samples"], 30)
            self.assertEqual({r["samples"] for r in before["results"] if r["split"] == "train"}, {18})
            self.assertEqual({r["samples"] for r in before["results"] if r["split"] == "test"}, {12})
            self.assertEqual(path.read_bytes(), original)  # Study cannot rewrite its source.
            for r in rows[48:]:
                r.update(o="8000", h="8100", l="7900", c="8000")
            write(path, rows)
            after = study(path)
            self.assertEqual([r for r in before["results"] if r["split"] == "train"],
                             [r for r in after["results"] if r["split"] == "train"])
            self.assertEqual(study(path)["results"], after["results"])  # Fixed bootstrap seed.


if __name__ == "__main__":
    unittest.main()
