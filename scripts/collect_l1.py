"""Read-only IM2610 L1 archive. No credentials and no trading account access."""
import argparse
import json
import math
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import TZ, Unsafe, fetch_quote, in_session
from engine import Store, process_lock

STATUS_KEY = "collector"
COUNTERS = ("accepted", "duplicates", "source_reversed", "failures")


def interval_arg(value):
    value = float(value)
    if not math.isfinite(value) or not 2 <= value <= 60:
        raise argparse.ArgumentTypeError("interval must be between 2 and 60 seconds")
    return value


class Collector:
    def __init__(self, store, interval=5):
        self.store, self.interval = store, interval_arg(interval)
        previous = store.get(STATUS_KEY, {})
        self.state = {key: previous.get(key, 0) for key in COUNTERS}
        with store.connect() as db:
            row = db.execute("SELECT ts,payload FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
            self.state["accepted"] = db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        self.state.update(phase="starting", started=time.time(), heartbeat=time.time(),
                          interval=self.interval, consecutive_failures=0,
                          last_source=json.loads(row[1])["timestamp"] if row else 0,
                          last_volume=json.loads(row[1])["volume"] if row else 0,
                          last_write=row[0] if row else 0)
        self.store.put(STATUS_KEY, self.state)

    def step(self, now=None, feed=None):
        current = time.time() if now is None else now
        delay = self.interval
        if not in_session(datetime.fromtimestamp(current, TZ)):
            self.state.update(phase="waiting_session", consecutive_failures=0)
        else:
            try:
                quote = (feed or fetch_quote)()
                current = time.time() if now is None else now
                quote.fresh(8, current)
                if not in_session(datetime.fromtimestamp(quote.timestamp, TZ)):
                    raise Unsafe("source is outside the trading session")
                if not in_session(datetime.fromtimestamp(current, TZ)):
                    self.state["phase"] = "waiting_session"
                elif quote.timestamp <= self.state["last_source"]:
                    key = "duplicates" if quote.timestamp == self.state["last_source"] else "source_reversed"
                    self.state[key] += 1
                    self.state["phase"] = key
                else:
                    same_day = datetime.fromtimestamp(quote.timestamp, TZ).date() == datetime.fromtimestamp(self.state["last_source"], TZ).date()
                    if same_day and quote.volume < self.state["last_volume"]:
                        raise Unsafe("cumulative volume regressed")
                    self.store.tick(quote, None, None, None)
                    self.state["accepted"] += 1
                    self.state.update(last_source=quote.timestamp, last_volume=quote.volume, last_write=current, phase="fresh")
                self.state["consecutive_failures"] = 0
            except (Unsafe, OSError):
                self.state["failures"] += 1
                self.state["consecutive_failures"] += 1
                self.state["phase"] = "unavailable"
                delay = min(60, self.interval * 2 ** min(6, self.state["consecutive_failures"]))
        self.state["heartbeat"] = current
        self.store.put(STATUS_KEY, self.state)
        return delay

    def stop(self):
        self.state.update(phase="stopped", heartbeat=time.time())
        self.store.put(STATUS_KEY, self.state)


def health(path, now=None):
    """Read-only check: process heartbeat, plus fresh stored L1 during a session."""
    current = time.time() if now is None else now
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (STATUS_KEY,)).fetchone()
        state = json.loads(row[0]) if row else {}
        heartbeat_age = current - state["heartbeat"]
        data_age = current - state["last_write"] if state["last_write"] else None
        live = 0 <= heartbeat_age <= 90 and state["phase"] != "stopped"
        session = in_session(datetime.fromtimestamp(current, TZ))
        fresh = data_age is not None and 0 <= data_age <= max(30, state["interval"] * 4)
        # Print only known collector fields, never arbitrary SQLite settings.
        report = {key: state[key] for key in (*COUNTERS, "phase", "interval", "heartbeat", "last_source", "last_write")}
        return {**report, "healthy": live and (not session or fresh),
                "in_session": session, "heartbeat_age": heartbeat_age, "data_age": data_age}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return {"healthy": False, "phase": "status_unavailable"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/l1.sqlite3"))
    parser.add_argument("--interval", type=interval_arg, default=5)
    parser.add_argument("--once", action="store_true", help="perform one read-only observation and exit")
    parser.add_argument("--health", action="store_true", help="check persisted collector status without fetching")
    args = parser.parse_args()
    if args.health:
        report = health(args.db)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["healthy"] else 1
    args.db.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    try:
        with process_lock(args.db):
            collector = Collector(Store(args.db, tick_retention=None), args.interval)
            try:
                while not stop.is_set():
                    delay = collector.step()
                    if args.once:
                        print(json.dumps(collector.state, ensure_ascii=False))
                        break
                    stop.wait(delay)
            finally:
                collector.stop()
        return 1 if args.once and collector.state["consecutive_failures"] else 0
    except (OSError, sqlite3.Error, Unsafe, ValueError, KeyError, TypeError):
        print("Collector could not safely use its archive; check database permissions, integrity and process lock.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
