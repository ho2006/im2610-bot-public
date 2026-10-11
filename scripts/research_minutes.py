"""Offline 15-minute close-direction diagnosis, never a trading or P&L backtest."""
import argparse
import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MORNING = ("09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30")
AFTERNOON = ("13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00")
SESSION_BY_TIME = {**dict.fromkeys(MORNING, "morning"), **dict.fromkeys(AFTERNOON, "afternoon")}
STRATEGIES = ("always_long", "momentum", "mean_reversion")
BOOTSTRAP_REPEATS, BOOTSTRAP_SEED = 1000, 2610


@dataclass(frozen=True)
class Bar:
    stamp: datetime
    close: float


def numeric(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a market number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite market number")
    return result


def session(stamp):
    return SESSION_BY_TIME.get(stamp.strftime("%H:%M")) if stamp.second == 0 else None


def load_minutes(path):
    raw = Path(path).read_bytes()
    source = json.loads(raw)
    if (not isinstance(source, dict) or source.get("symbol") != "IM2610"
            or type(source.get("period_minutes")) is not int or source["period_minutes"] != 15
            or not isinstance(source.get("raw_data"), list)):
        raise ValueError("Expected IM2610 15-minute raw_data source")
    quality = {"raw_records": len(source["raw_data"]), "invalid_records": 0,
               "duplicate_timestamps": 0, "out_of_order_timestamps": 0,
               "outside_session_records": 0, "non_contiguous_session_intervals": 0,
               "source_sha256": hashlib.sha256(raw).hexdigest()}
    bars, seen, days, tainted = [], set(), set(), set()
    previous = None
    for row in source["raw_data"]:
        day = None
        try:
            if not isinstance(row, dict):
                raise ValueError("Not a bar")
            stamp = datetime.strptime(row["d"], "%Y-%m-%d %H:%M:%S")
            if stamp.strftime("%Y-%m-%d %H:%M:%S") != row["d"]:
                raise ValueError("Unexpected timestamp format")
            day = stamp.date().isoformat()
            days.add(day)
            o, h, l, c, v = (numeric(row[key]) for key in ("o", "h", "l", "c", "v"))
            if not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
                raise ValueError("Invalid OHLC or volume")
            if "p" in row and numeric(row["p"]) < 0:
                raise ValueError("Negative open interest")
        except (ValueError, TypeError, KeyError, OverflowError):
            quality["invalid_records"] += 1
            if day:
                tainted.add(day)
            continue
        if stamp in seen:
            quality["duplicate_timestamps"] += 1
            tainted.add(day)
            continue
        if previous is not None and stamp < previous:
            quality["out_of_order_timestamps"] += 1
            tainted.add(day)
            continue
        if session(stamp) is None:
            quality["outside_session_records"] += 1
            tainted.add(day)
            continue
        if (previous is not None and previous.date() == stamp.date()
                and session(previous) == session(stamp)
                and (stamp-previous).total_seconds() != 900):
            quality["non_contiguous_session_intervals"] += 1
        seen.add(stamp)
        previous = stamp
        bars.append(Bar(stamp, c))
    grouped = {}
    for bar in bars:
        grouped.setdefault(bar.stamp.date().isoformat(), set()).add(bar.stamp.strftime("%H:%M"))
    complete, incomplete = [], []
    expected = set(SESSION_BY_TIME)
    for day in sorted(days):
        missing = sorted(expected-grouped.get(day, set()))
        if not missing and day not in tainted:
            complete.append(day)
        else:
            incomplete.append({"day": day, "valid_bars": len(grouped.get(day, set())),
                               "missing_labels": missing, "data_fault": day in tainted})
    quality.update(valid_records=len(bars), observed_days=len(days), complete_days=complete,
                   incomplete_days=incomplete, missing_bar_slots=sum(len(d["missing_labels"]) for d in incomplete))
    return bars, quality


def samples(bars):
    """Four completed close intervals at index 4, next-close change at index 5."""
    result = []
    for index in range(len(bars)-5):
        window = bars[index:index+6]
        day, period = window[0].stamp.date(), session(window[0].stamp)
        if (period is None or any(b.stamp.date() != day or session(b.stamp) != period for b in window)
                or any((b.stamp-a.stamp).total_seconds() != 900 for a, b in zip(window, window[1:]))):
            continue
        result.append({"day": day.isoformat(), "signal_label": window[4].stamp.isoformat(sep=" "),
                       "target_label": window[5].stamp.isoformat(sep=" "),
                       "past_drift": window[4].close-window[0].close,
                       "future_change": window[5].close-window[4].close})
    return result


def summarize(rows, strategy):
    if strategy not in STRATEGIES:
        raise ValueError("Unknown direction diagnostic")
    points, by_day, signals = [], {}, 0
    for row in rows:
        direction = (1 if row["past_drift"] > 0 else -1 if row["past_drift"] < 0 else 0)
        if strategy == "always_long":
            direction = 1
        elif strategy == "mean_reversion":
            direction = -direction
        value = direction*row["future_change"]
        signals += direction != 0
        points.append(value)  # Keep abstentions as zero on the identical common sample.
        by_day.setdefault(row["day"], []).append(value)
    count = len(points)
    interval = None
    if len(by_day) >= 2:
        # Every complete date contributes six common windows, so date means have equal weight.
        means = [statistics.fmean(values) for values in by_day.values()]
        rng = random.Random(BOOTSTRAP_SEED)
        bootstrap = [statistics.fmean(rng.choices(means, k=len(means))) for _ in range(BOOTSTRAP_REPEATS)]
        quantiles = statistics.quantiles(bootstrap, n=40, method="inclusive")
        interval = [round(quantiles[0], 6), round(quantiles[-1], 6)]
    return {"strategy": strategy, "samples": count, "effective_signals": signals,
            "empty_signal_ratio": round(1-signals/count, 6) if count else None,
            "mean_points": round(statistics.fmean(points), 6) if count else None,
            "median_points": round(statistics.median(points), 6) if count else None,
            "win_rate_all_samples": round(sum(p > 0 for p in points)/count, 6) if count else None,
            "worst_points": round(min(points), 6) if count else None,
            "daily_block_bootstrap_mean_95pct_points": interval}


def study(path):
    bars, quality = load_minutes(path)
    days = quality["complete_days"]
    split = len(days)*3//5
    partitions = {"train": days[:split], "test": days[split:]}
    valid = set(days)
    observations = samples([b for b in bars if b.stamp.date().isoformat() in valid])
    results = []
    for name, selected in partitions.items():
        rows = [row for row in observations if row["day"] in selected]
        for strategy in STRATEGIES:
            results.append({"split": name, **summarize(rows, strategy)})
    return {"symbol": "IM2610", "period_minutes": 15, "source": str(path), "quality": quality,
            "method": "Direction from close[t]-close[t-4]; score direction*(close[t+1]-close[t]). Six consecutive same-date, same-session vendor labels required.",
            "units": "index points, not executed prices or P&L", "split_days": partitions,
            "common_samples": len(observations), "results": results,
            "bootstrap": {"blocks": "complete dates", "repeats": BOOTSTRAP_REPEATS, "seed": BOOTSTRAP_SEED},
            "limitations": ["Only complete 16-bar dates enter the chronological 60/40 split; missing/invalid dates are excluded, never filled.",
                            "Vendor timestamp labels are retained; bar timing convention and publication latency are not verified.",
                            "Fixed four-interval drift; no fitting, tuning or selection using test outcomes.",
                            "Every strategy uses the same samples; zero-direction abstentions count as zero points and remain in the denominator.",
                            "Bootstrap intervals describe this short sample with correlated daily blocks, not predictive probabilities or proof of future returns.",
                            "No bid/ask, passive execution, positions, transaction fees or trading P&L is simulated.",
                            "Historical OHLC does not validate the production L1 imbalance/Microprice strategy."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("artifacts/im2610-minute-source.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/strategy-minute-study.json"))
    args = parser.parse_args()
    report = study(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("Complete days:", len(report["quality"]["complete_days"]), "train/test:",
          len(report["split_days"]["train"]), len(report["split_days"]["test"]),
          "common samples:", report["common_samples"])
    for row in report["results"]:
        print(row["split"], row["strategy"], "samples/signals:", row["samples"], row["effective_signals"],
              "mean points:", row["mean_points"], "95% daily bootstrap:", row["daily_block_bootstrap_mean_95pct_points"])
    print("Saved:", args.output, "Direction diagnosis only; not trading P&L or L1 validation.")


if __name__ == "__main__":
    main()
