"""Validate the live schema without submitting or cancelling anything."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import load_env
from core import Competition, fetch_quote

load_env()
quote = fetch_quote()
print("Sina IM2610 L1 parsed:", quote.bid, quote.ask, quote.bid_size, quote.ask_size)
print("Quote source timestamp:", quote.timestamp)
broker = Competition(os.environ.get("CFAPIKEY", ""))
broker.prepare()
broker.account()
broker.holdings()
broker.orders()
broker.orders(True)
broker.trades()
print("Competition read-only checks passed. No order or cancel was sent.")
