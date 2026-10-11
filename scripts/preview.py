"""Local UI smoke test. Always paper, never autostarts or reads account credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waitress import serve
from app import create_app
from core import Config
from engine import Engine, Paper, Store, process_lock

store = Store("data/preview.sqlite3")
lock = process_lock(store.path)
engine = Engine(store, Paper(store, Config()))
engine.start()
try:
    serve(create_app(engine, "local-preview-token-no-trading-account"), host="127.0.0.1", port=8000)
finally:
    engine.stop()
    lock.close()
