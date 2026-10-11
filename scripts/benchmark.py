"""Offline repeatable SQLite/UI benchmark; no credentials or network."""
import argparse
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Store


class MeasuredStore(Store):
    changes = 0

    @contextmanager
    def connect(self):
        with super().connect() as db:
            yield db
            self.changes += db.total_changes


def run(rows=20000, repeats=15):
    with tempfile.TemporaryDirectory() as directory:
        store = MeasuredStore(Path(directory) / 'benchmark.sqlite3')
        with store.connect() as db:
            db.executemany('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ((str(i), float(i), '2026-09-30', '0', 7000, 1, 'CANCELLED', '{}', str(i), str(i), 1, float(i))
                 for i in range(rows)))
        store.history()
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            result = store.history()
            samples.append((time.perf_counter()-start)*1000)
            assert len(result['orders']) == min(100, rows)
        tracemalloc.start()
        store.history()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        store.put('unchanged', {'value': 1})
        store.changes = 0
        for _ in range(20):
            store.put('unchanged', {'value': 1})
            store.update('0', 'CANCELLED', '0', '0', 1)
        return {'journal_rows': rows, 'repeats': repeats,
                'history_median_ms': round(statistics.median(samples), 3),
                'history_peak_bytes': peak, 'unchanged_sql_row_writes': store.changes}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
