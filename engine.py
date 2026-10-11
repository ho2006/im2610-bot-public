"""Single-account worker with a durable write-ahead order journal."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from core import (Config, EntryFilter, EXPIRY, MULTIPLIER, OPERATIONS, SYMBOL, TICK, TZ, Unsafe, QueryUnavailable, NotSent,
                  Uncertain, fetch_quote, in_session, make_quotes, order_key, order_matches, validate_order)

ACTIVE = ("PREPARED", "UNKNOWN", "ACK", "OPEN", "PARTIAL", "CANCELLING", "UNKNOWN_CANCEL")
TERMINAL = ("FILLED", "CANCELLED", "REJECTED", "NOT_SENT")


class SnapshotConflict(Unsafe):
    """Separated read endpoints may observe one legitimate state transition."""


class Store:
    def __init__(self, path, tick_retention=10000):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.tick_retention = tick_retention
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, day TEXT NOT NULL,
                    op TEXT NOT NULL, price REAL NOT NULL, qty INTEGER NOT NULL,
                    state TEXT NOT NULL, baseline TEXT NOT NULL, remote_key TEXT,
                    remote_id TEXT, remaining INTEGER NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS ticks (id INTEGER PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS intents_created ON intents(created DESC);
                CREATE INDEX IF NOT EXISTS intents_active ON intents(created DESC)
                    WHERE state NOT IN ('FILLED','CANCELLED','REJECTED','NOT_SENT');
                CREATE TABLE IF NOT EXISTS intent_observations (
                    intent_id TEXT NOT NULL, state TEXT NOT NULL, remaining INTEGER NOT NULL,
                    observed REAL NOT NULL, PRIMARY KEY(intent_id,state));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db:
            db.execute("""INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE
                SET value=excluded.value WHERE settings.value != excluded.value""",
                (key, json.dumps(value, ensure_ascii=False)))

    def event(self, message, level="info"):
        with self.connect() as db:
            db.execute("INSERT INTO events(ts,level,message) VALUES (?,?,?)", (time.time(), level, message))
            db.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0)-5000 FROM events)")

    def add_intent(self, plan, orders):
        now = time.time()
        ident = uuid.uuid4().hex
        counts = dict(Counter(order_key(row) for row in orders))
        with self.connect() as db:
            db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                ident, now, datetime.fromtimestamp(now, TZ).date().isoformat(), plan["op"], plan["price"], plan["qty"],
                "PREPARED", json.dumps(counts), None, None, plan["qty"], now))
        return ident

    def update(self, ident, state, remote_key=None, remote_id=None, remaining=None):
        with self.connect() as db:
            changed = db.execute("""UPDATE intents SET state=?,updated=?,remote_key=COALESCE(?,remote_key),
                remote_id=COALESCE(?,remote_id),remaining=COALESCE(?,remaining) WHERE id=?
                AND (state IS NOT ? OR remote_key IS NOT COALESCE(?,remote_key)
                     OR remote_id IS NOT COALESCE(?,remote_id) OR remaining IS NOT COALESCE(?,remaining))""",
                (state, time.time(), remote_key, remote_id, remaining, ident, state, remote_key, remote_id, remaining)).rowcount
            if changed:
                db.execute("""INSERT OR IGNORE INTO intent_observations SELECT id,state,remaining,updated
                    FROM intents WHERE id=?""", (ident,))

    def intents(self, active=False):
        with self.connect() as db:
            sql = "SELECT * FROM intents"
            if active:
                sql += " WHERE state NOT IN ('FILLED','CANCELLED','REJECTED','NOT_SENT')"
            sql += " ORDER BY created DESC"
            return [dict(row) for row in db.execute(sql)]

    def tick(self, q, fair, equity, inventory):
        payload = {**q.public(), "fair": fair, "equity": equity, "inventory": inventory}
        with self.connect() as db:
            previous = db.execute("SELECT payload FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
            if previous and q.timestamp <= json.loads(previous[0])["timestamp"]:
                return False  # Repeated closed-market snapshots must not evict useful L1 history.
            db.execute("INSERT INTO ticks(ts,payload) VALUES (?,?)", (time.time(), json.dumps(payload)))
            if self.tick_retention is not None:
                db.execute("DELETE FROM ticks WHERE id <= (SELECT COALESCE(MAX(id),0)-? FROM ticks)", (self.tick_retention,))
        return True

    def history(self):
        with self.connect() as db:
            ticks = [json.loads(r[0]) for r in db.execute("SELECT payload FROM ticks ORDER BY id DESC LIMIT 300")]
            events = [dict(r) for r in db.execute("SELECT ts,level,message FROM events ORDER BY id DESC LIMIT 80")]
            orders = [dict(r) for r in db.execute("""SELECT id,created,op,price,qty,state,remaining
                FROM intents ORDER BY created DESC LIMIT 100""")]
        return {"ticks": list(reversed(ticks)), "events": events, "orders": orders}


class Paper:
    """Conservative trade-through illustration, NOT a queue-position/backtest model."""
    mode = "paper"
    identity = "local-paper-v1"

    def __init__(self, store, config):
        self.store, self.config = store, config
        self.state = store.get("paper", {"cash": 1_000_000, "long": 0, "short": 0,
            "long_cost": 0, "short_cost": 0, "orders": [], "trades": [], "quote_ts": 0, "volume": 0, "mid": 0})

    def save(self):
        self.store.put("paper", self.state)

    def prepare(self):
        pass

    def race_active(self, now):
        return now.date() <= EXPIRY

    def mark(self, quote):
        if quote.timestamp >= self.state["quote_ts"] and quote.mid != self.state["mid"]:
            self.state["mid"] = quote.mid
            self.save()

    def matches(self, order, quote):
        buy = order["op"] in ("0", "2")
        return (quote.last < order["price"] and quote.ask <= order["price"] if buy
                else quote.last > order["price"] and quote.bid >= order["price"])

    def on_quote(self, quote, participation=1.0):
        state = self.state
        if quote.timestamp <= state["quote_ts"]:
            return
        same_day = datetime.fromtimestamp(quote.timestamp, TZ).date() == datetime.fromtimestamp(state["quote_ts"], TZ).date()
        capacity = int(max(0, quote.volume - state["volume"]) * participation) if same_day else 0
        for order in state["orders"]:
            if order["state"] not in ("OPEN", "PARTIAL") or capacity <= 0:
                continue
            if not self.matches(order, quote):
                continue
            qty = min(order["remaining"], capacity)
            op, price = order["op"], order["price"]
            side = "long" if op in ("0", "3") else "short"
            if op in ("0", "1"):
                state[side + "_cost"] = (state[side + "_cost"] * state[side] + price * qty) / (state[side] + qty)
                state[side] += qty
            else:
                if state[side] < qty:
                    raise Unsafe("本地撮合可平数量不足")
                sign = 1 if side == "long" else -1
                state["cash"] += sign * (price - state[side + "_cost"]) * qty * MULTIPLIER
                state[side] -= qty
            state["cash"] -= price * qty * MULTIPLIER * self.config.fee_per_side_bps / 10000
            order["remaining"] -= qty
            order["state"] = "FILLED" if order["remaining"] == 0 else "PARTIAL"
            state["trades"].append({"time": datetime.fromtimestamp(quote.timestamp, TZ).isoformat(),
                "operation": OPERATIONS[op], "price": price, "qty": qty})
            capacity -= qty
        state.update(quote_ts=quote.timestamp, volume=quote.volume, mid=quote.mid)
        self.save()

    def holdings(self):
        s = self.state
        pending = self.orders(True)
        return {"long": s["long"], "short": s["short"],
            "available_long": s["long"] - sum(x["remaining"] for x in pending if x["op"] == "3"),
            "available_short": s["short"] - sum(x["remaining"] for x in pending if x["op"] == "2")}

    def account(self):
        s = self.state
        equity = s["cash"] + MULTIPLIER * ((s["mid"] - s["long_cost"]) * s["long"] + (s["short_cost"] - s["mid"]) * s["short"])
        margin = (s["long"] + s["short"]) * s["mid"] * MULTIPLIER * self.config.margin_rate
        return {"equity": equity, "use_rate": margin / equity if equity > 0 else 1}

    def orders(self, pending=False, day=None):
        day = day or datetime.now(TZ).date().isoformat()
        return [{**r, "day": r["time"][:10]} for r in self.state["orders"] if
                (r["state"] in ("OPEN", "PARTIAL") if pending else r["time"].startswith(day))]

    def trades(self):
        today = datetime.now(TZ).date().isoformat()
        return [r for r in self.state["trades"] if r["time"].startswith(today)][-100:]

    def submit(self, plan, quote):
        validate_order(plan, quote)
        self.state["orders"].append({**plan, "remaining": plan["qty"], "state": "OPEN",
            "time": datetime.now(TZ).isoformat(), "remote_id": uuid.uuid4().hex})
        self.save()

    def cancel(self, remote_id):
        for row in self.state["orders"]:
            if row["remote_id"] == remote_id and row["state"] in ("OPEN", "PARTIAL"):
                row["state"] = "CANCELLED"
                self.save()
                return
        raise Unsafe("本地委托已不在可撤状态")


class Engine:
    def __init__(self, store, broker, feed=fetch_quote, autostart=False):
        self.store, self.broker, self.feed = store, broker, feed
        identity = [broker.mode, broker.identity]
        if store.get("identity", identity) != identity:
            raise Unsafe("数据库绑定了其他账户或模式，请使用独立数据库")
        self.config = Config.parse(store.get("config", {}))
        if not store.get("zero_commission_applied", False):
            self.config.fee_per_side_bps = 0
            store.put("config", asdict(self.config))
            store.put("zero_commission_applied", True)
        if isinstance(broker, Paper):
            broker.config = self.config
        broker.max_quote_age = self.config.max_quote_age
        broker.before_submit = self.before_submit
        store.put("identity", identity)
        self.enabled = autostart and store.get("enabled", True)
        self.latch = store.get("latch", "")
        self.flatten = store.get("flatten", False)
        self.ready = False
        self.account = {}
        self.holdings = {"long": 0, "short": 0, "available_long": 0, "available_short": 0}
        self.pending, self.orders, self.trades = [], [], []
        self.quote = None
        self.fair = None
        self.fair_ts = 0
        self.entry_filter = EntryFilter()
        self.note = "启动对账中"
        self.last_sync = 0
        self.last_trade_sync = 0
        self.query_failures = 0
        self.cycle_seconds = 0
        self.last_cycle = time.time()
        self.commands = queue.Queue(maxsize=20)
        self.wake = threading.Event()
        self.exit = threading.Event()
        self.thread = None
        self.snapshot = {}
        self.publish()

    def alerts(self):
        alerts = []
        gross = self.holdings["long"] + self.holdings["short"]
        now = datetime.now(TZ)
        if gross and now.strftime("%H:%M") >= self.config.flatten_time:
            alerts.append(f"收盘减仓仍有 {gross} 手持仓；被动平仓不能保证清仓，请核对平台")
        if gross > self.config.max_position:
            alerts.append("持仓超过上限，限制新开仓")
        if self.account.get("use_rate", 0) >= self.config.max_use_rate:
            alerts.append("保证金使用率达到限制，限制新开仓")
        return alerts

    def before_submit(self, plan, quote):
        if not self.enabled or self.latch or not self.commands.empty() or self.exit.is_set():
            raise Unsafe("提交前收到停止或控制命令")
        candidates, _ = self.risk_and_plan(quote, datetime.now(TZ))
        if not any(p["op"] == plan["op"] and p["qty"] >= plan["qty"] for p in candidates):
            raise Unsafe("提交前风险状态变化，已停止发送")
        validate_order(plan, quote)

    def read_quote(self):
        quote = self.feed()
        if self.quote:
            if quote.timestamp < self.quote.timestamp:
                raise Unsafe("行情源时间回退，停止报价")
            same_day = datetime.fromtimestamp(quote.timestamp, TZ).date() == datetime.fromtimestamp(self.quote.timestamp, TZ).date()
            if same_day and quote.volume < self.quote.volume:
                raise Unsafe("行情累计成交量回退，停止报价")
        self.quote = quote
        return quote

    def publish(self):
        self.snapshot = {"symbol": SYMBOL, "mode": self.broker.mode, "enabled": self.enabled,
            "ready": self.ready, "latched": bool(self.latch), "latch": self.latch,
            "flatten": self.flatten, "note": self.note, "account": dict(self.account),
            "holdings": dict(self.holdings), "quote": self.quote.public() if self.quote else None,
            "fair": self.fair, "last_sync": self.last_sync, "last_cycle": self.last_cycle,
            "last_trade_sync": self.last_trade_sync, "cycle_seconds": self.cycle_seconds,
            "query_failures": self.query_failures, "active_orders": len(self.store.intents(active=True)),
            "alerts": self.alerts(),
            "daily": self.store.get("daily", {}), "config": asdict(self.config),
            "entry_filter": dict(self.entry_filter.status),
            "plans": getattr(self, "plans", []), "trades": self.trades[-100:],
            "commands_pending": self.commands.qsize()}

    def command(self, action, config=None):
        if action not in ("start", "pause", "flatten", "reconcile", "configure"):
            raise Unsafe("未知控制命令")
        if action == "configure":
            config = asdict(Config.parse(config))
        try:
            self.commands.put_nowait((action, config))
        except queue.Full:
            raise Unsafe("控制命令队列已满") from None
        self.wake.set()

    def halt(self, message):
        if self.latch != message:
            self.store.event(message, "error")
        self.latch = message
        self.enabled = False
        self.store.put("latch", message)
        self.store.put("enabled", False)
        self.note = message

    def sync(self, force_trades=False):
        day = datetime.now(TZ).date().isoformat()
        old_days = {r["day"] for r in self.store.intents(active=True)} - {day}
        for attempt in range(2):
            self.orders = self.broker.orders()
            for old_day in old_days:
                self.orders.extend(self.broker.orders(day=old_day))
            self.pending = self.broker.orders(True)
            try:
                self.reconcile()
                break
            except SnapshotConflict:
                if attempt:
                    raise
                # Only reads are repeated. No mutation may be resent to resolve a race.
        self.holdings = self.broker.holdings()
        self.account = self.broker.account()
        if (force_trades or isinstance(self.broker, Paper) or not self.last_trade_sync
                or time.time() - self.last_trade_sync >= 30 or getattr(self, "trade_day", None) != day):
            self.trades = self.broker.trades()
            self.store.put("trades:" + day, self.trades)
            self.last_trade_sync = time.time()
            self.trade_day = day
        self.last_sync = time.time()
        daily = self.store.get("daily", {})
        if daily.get("day") != day:
            daily = {"day": day, "start_equity": self.account["equity"], "pnl": 0, "loss_hit": False}
        daily["pnl"] = self.account["equity"] - daily["start_equity"]
        daily["loss_hit"] = daily["loss_hit"] or daily["pnl"] <= -self.config.max_daily_loss
        self.store.put("daily", daily)

    def reconcile(self):
        keys = Counter(order_key(row) for row in self.orders)
        order_rows, pending_rows = {}, {}
        for row in self.orders:
            order_rows.setdefault(order_key(row), []).append(row)
        for row in self.pending:
            pending_rows.setdefault(order_key(row), []).append(row)
        for intent in self.store.intents(active=True):
            key = intent["remote_key"]
            if key:
                matches = order_rows.get(key, [])
            else:
                baseline = json.loads(intent["baseline"])
                matches = [row for row in self.orders if order_matches(row, intent)
                           and row.get("day", intent["day"]) == intent["day"]
                           and keys[order_key(row)] > baseline.get(order_key(row), 0)]
            if len(matches) != 1:
                if intent["state"] == "PREPARED":
                    self.store.update(intent["id"], "UNKNOWN")
                raise Unsafe("委托无法唯一对账；禁止重复提交，需核查恢复")
            row = matches[0]
            key = order_key(row)
            if keys[key] != 1:
                raise Unsafe("平台返回同时间同价格的重复委托，无法唯一识别")
            pend = pending_rows.get(key, [])
            if len(pend) > 1:
                raise Unsafe("挂单无法唯一识别")
            if row["state"] in ("OPEN", "PARTIAL") and len(pend) != 1:
                raise SnapshotConflict("委托与挂单状态不一致，等待人工核查")
            if row["state"] in TERMINAL and pend:
                raise SnapshotConflict("终态委托仍在挂单列表中")
            if pend and (pend[0]["remaining"] > row["remaining"]
                         or (row["state"] == "PARTIAL" and pend[0]["state"] == "OPEN")):
                raise SnapshotConflict("挂单数量或状态发生回退，等待核查")
            state = row["state"]
            if intent["state"] in ("CANCELLING", "UNKNOWN_CANCEL") and state in ("OPEN", "PARTIAL"):
                state = intent["state"]
                if time.time() - intent["updated"] > 30:
                    raise Unsafe("撤单未确认，保持锁定并禁止重发")
                # Preserve the original cancellation deadline while waiting.
                continue
            self.store.update(intent["id"], state, key, pend[0]["remote_id"] if pend else None,
                              pend[0]["remaining"] if pend else row["remaining"])
        owned = {r["remote_key"] for r in self.store.intents(active=True) if r["remote_key"]}
        if any(order_key(row) not in owned for row in self.pending):
            raise SnapshotConflict("发现非本程序或已终结的 IM2610 挂单，请在平台处理后核查恢复")

    def handle_commands(self):
        while not self.commands.empty():
            action, config = self.commands.get_nowait()
            try:
                if action == "configure":
                    if self.enabled or self.store.intents(active=True):
                        raise Unsafe("请先暂停并等待挂单撤销后修改参数")
                    self.config = Config.parse(config)
                    self.fair, self.fair_ts = None, 0
                    if isinstance(self.broker, Paper):
                        self.broker.config = self.config
                    self.broker.max_quote_age = self.config.max_quote_age
                    self.store.put("config", asdict(self.config))
                elif action == "pause":
                    self.enabled = False
                    self.flatten = False
                elif action == "reconcile":
                    self.broker.prepare()
                    self.sync(force_trades=True)
                    if any(r["state"] in ("UNKNOWN", "PREPARED", "ACK", "CANCELLING", "UNKNOWN_CANCEL") for r in self.store.intents(active=True)):
                        raise Unsafe("仍有不明确委托，不能解除锁定")
                    self.latch = ""
                    self.store.put("latch", "")
                    self.ready = True
                    self.enabled = False
                else:
                    if self.latch or not self.ready:
                        raise Unsafe("请先核查恢复，解除锁定后再启动")
                    self.sync()
                    self.enabled = True
                    self.flatten = action == "flatten"
                self.entry_filter = EntryFilter()
                self.store.put("enabled", self.enabled)
                self.store.put("flatten", self.flatten)
                self.store.event({"start": "全自动策略已启动", "pause": "已暂停，正在撤销本程序挂单",
                    "flatten": "已开启被动减仓", "reconcile": "对账通过，等待启动", "configure": "策略参数已保存"}[action])
            except Unsafe as exc:
                self.store.event(str(exc), "error")
                self.note = str(exc)

    def cancel_one(self, intent):
        if intent["state"] not in ("OPEN", "PARTIAL") or not intent["remote_id"]:
            return
        # Persist BEFORE the side effect: a process crash cannot cause a duplicate cancel.
        self.store.update(intent["id"], "CANCELLING")
        try:
            self.broker.cancel(intent["remote_id"])
        except Unsafe:
            self.store.update(intent["id"], "UNKNOWN_CANCEL")
            raise Uncertain("撤单回执不明确；不重发，等待对账") from None
        self.store.event("已发送撤单，等待平台确认")

    def cancel_all_step(self):
        for intent in self.store.intents(active=True):
            if intent["state"] in ("OPEN", "PARTIAL"):
                self.cancel_one(intent)
                break

    def risk_and_plan(self, q, now):
        q.fresh(self.config.max_quote_age)
        if not in_session(now) or not self.broker.race_active(now):
            raise Unsafe("当前不在 IM2610 或比赛交易时段")
        if (q.ask - q.bid) / TICK > self.config.max_spread_ticks + 1e-6:
            raise Unsafe("买卖价差超过阈值")
        holdings = self.holdings
        gross = holdings["long"] + holdings["short"]
        inventory = holdings["long"] - holdings["short"]
        day = self.store.get("daily", {})
        reduce = (self.flatten or day.get("loss_hit") or now.strftime("%H:%M") >= self.config.flatten_time
                  or gross >= self.config.max_position or self.account["use_rate"] >= self.config.max_use_rate)
        if q.timestamp != self.fair_ts:
            previous = self.fair if self.fair is not None and q.timestamp - self.fair_ts < 60 else q.microprice
            self.fair = self.config.ema_alpha * q.microprice + (1 - self.config.ema_alpha) * previous
            self.fair_ts = q.timestamp
        # Use total holdings to retain already-working close quotes. Available quantity is rechecked on submission.
        plans = make_quotes(q, inventory, holdings["long"], holdings["short"], self.config, self.fair, reduce)
        if in_session(datetime.fromtimestamp(q.timestamp, TZ)):
            status = self.entry_filter.observe(q)
        else:
            self.entry_filter = EntryFilter()
            status = self.entry_filter.status
        if self.config.entry_filter and not status["allowed"]:
            plans = [p for p in plans if p["op"] in ("2", "3")]
        opening_count = sum(r["op"] in ("0", "1") and r.get("day", now.date().isoformat()) == now.date().isoformat() for r in self.orders)
        reserved = sum(r["remaining"] for r in self.pending if r["op"] in ("0", "1"))
        margin_reserved = reserved * q.mid * MULTIPLIER * self.config.margin_rate
        allowed = []
        for p in plans:
            if p["op"] in ("0", "1"):
                if opening_count >= self.config.max_orders_per_day:
                    continue
                # Existing same-side orders already consume this reservation; don't double-count them.
                same = sum(r["remaining"] for r in self.pending if r["op"] == p["op"])
                extra = max(0, p["qty"] - same)
                cost = margin_reserved + extra * q.mid * MULTIPLIER * self.config.margin_rate
                if gross + reserved + extra > self.config.max_position:
                    continue
                if self.account["use_rate"] + cost / self.account["equity"] > self.config.max_use_rate:
                    continue
            allowed.append(p)
        return allowed, reduce

    def trade_step(self, q, now):
        try:
            plans, reduce = self.risk_and_plan(q, now)
        except Unsafe as exc:
            self.note = str(exc)
            self.plans = []
            self.cancel_all_step()
            return
        self.plans = plans
        self.note = ("只减仓" if reduce else "暂停新开仓：" + self.entry_filter.status["reason"]
                     if self.config.entry_filter and not self.entry_filter.status["allowed"] else "被动报价")
        working = self.store.intents(active=True)
        if any(r["state"] not in ("OPEN", "PARTIAL") for r in working):
            self.note = "等待委托 / 撤单确认"
            return
        by_side = {"buy": None, "sell": None}
        for intent in working:
            side = "buy" if intent["op"] in ("0", "2") else "sell"
            if by_side[side]:
                raise Unsafe("同侧存在多笔委托，停止新增")
            by_side[side] = intent
            plan = next((p for p in plans if p["side"] == side), None)
            crossed = intent["price"] > q.bid if side == "buy" else intent["price"] < q.ask
            changed = not plan or plan["op"] != intent["op"] or abs(plan["price"] - intent["price"]) >= self.config.reprice_ticks * TICK - 1e-7
            if crossed or changed or time.time() - intent["created"] >= self.config.order_lifetime:
                self.cancel_one(intent)
                return  # One mutation per cycle; never replace before confirmed cancellation.
        if reduce and not (self.holdings["long"] + self.holdings["short"]) and not working:
            if self.flatten:
                self.enabled = self.flatten = False
                self.store.put("enabled", False)
                self.store.put("flatten", False)
            self.note = "已空仓，等待下一个可开仓时段"
            return
        for plan in plans:
            if by_side[plan["side"]]:
                continue
            # Closing quantity and account limits must be refreshed immediately before order submission.
            self.sync()
            fresh = self.read_quote()
            fresh.fresh(self.config.max_quote_age)
            candidates, _ = self.risk_and_plan(fresh, datetime.now(TZ))
            plan = next((p for p in candidates if p["op"] == plan["op"]), None)
            if plan is None:
                return
            if plan["op"] in ("2", "3"):
                available = self.holdings["available_short" if plan["op"] == "2" else "available_long"]
                plan["qty"] = min(plan["qty"], available)
                if plan["qty"] <= 0:
                    return
            validate_order(plan, fresh)
            if not self.commands.empty() or self.exit.is_set():
                return
            ident = self.store.add_intent(plan, self.orders)
            try:
                # Re-check freshness after the durable write, before crossing the trust boundary.
                fresh.fresh(self.config.max_quote_age)
            except Unsafe:
                self.store.update(ident, "NOT_SENT")
                return
            try:
                self.broker.submit(plan, fresh)
            except NotSent as exc:
                self.store.update(ident, "NOT_SENT")
                self.note = str(exc)
                return
            except Uncertain:
                self.store.update(ident, "UNKNOWN")
                raise
            except Unsafe:
                self.store.update(ident, "REJECTED")
                raise
            self.store.update(ident, "ACK")
            self.store.event(f"{OPERATIONS[plan['op']]} {plan['qty']} 手，限价 {plan['price']:.1f}，等待对账")
            return

    def cycle(self):
        self.last_cycle = time.time()
        initial_sync = not self.ready
        if not self.ready:
            self.broker.prepare()
            self.sync()
            self.ready = True
        self.handle_commands()
        if not initial_sync:
            self.sync()
        now = datetime.now(TZ)
        try:
            self.read_quote()
            if in_session(now):
                self.quote.fresh(self.config.max_quote_age)
        except Unsafe as exc:
            self.note = str(exc)
            self.plans = []
            self.entry_filter = EntryFilter()
            self.cancel_all_step()
            return
        if isinstance(self.broker, Paper):
            if in_session(now):
                self.broker.on_quote(self.quote)
            else:
                self.broker.mark(self.quote)  # Value residual positions at the close; never match outside session.
            self.sync()
        inventory = self.holdings["long"] - self.holdings["short"]
        if not self.enabled or self.latch:
            self.plans = []
            self.note = self.latch or "已暂停；仅采集行情和账户"
            self.cancel_all_step()
        elif not in_session(now) or not self.broker.race_active(now):
            self.plans = []
            self.note = "休市 / 比赛时段外；撤销本程序挂单"
            self.cancel_all_step()
        else:
            self.trade_step(self.quote, now)
        self.store.tick(self.quote, self.fair, self.account.get("equity"), inventory)

    def run(self):
        while not self.exit.is_set():
            self.wake.clear()
            started = time.monotonic()
            try:
                self.cycle()
                self.query_failures = 0
            except QueryUnavailable as exc:
                self.query_failures += 1
                self.note = "比赛查询暂不可用，停止报价并退避重连"
                self.plans = []
                if self.query_failures == 1:
                    self.store.event(str(exc), "warning")
                try:
                    self.cancel_all_step()
                except Unsafe as cancel_error:
                    self.halt(str(cancel_error))
                self.handle_commands()
            except Unsafe as exc:
                self.halt(str(exc))
                try:
                    self.cancel_all_step()
                except Unsafe as cancel_error:
                    self.halt(str(cancel_error))
                # Reconciliation must remain possible even if initial prepare/sync failed.
                self.handle_commands()
            except Exception as exc:
                self.halt(f"内部异常 {type(exc).__name__}，已停止交易")
                try:
                    self.cancel_all_step()
                except Unsafe as cancel_error:
                    self.halt(str(cancel_error))
            self.cycle_seconds = round(time.monotonic() - started, 3)
            self.last_cycle = time.time()
            if self.alerts():
                key = datetime.now(TZ).date().isoformat() + ":" + "|".join(self.alerts())
                if self.store.get("last_alert") != key:
                    self.store.event("；".join(self.alerts()), "warning")
                    self.store.put("last_alert", key)
            self.publish()
            delay = min(60, self.config.poll_seconds * 2 ** min(self.query_failures, 4)) if self.query_failures else self.config.poll_seconds
            self.wake.wait(delay)
        self.enabled = False
        # Best effort cancel on shutdown. Unconfirmed requests remain durable and block restart.
        try:
            self.sync()
            for intent in self.store.intents(active=True):
                self.cancel_one(intent)
        except Exception:
            self.store.event("退出时未能确认全部撤单；请检查平台挂单", "error")
        self.publish()

    def start(self):
        self.thread = threading.Thread(target=self.run, name="im2610-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self.exit.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=45)


def process_lock(path):
    """Lock held until process exit. A second process must never submit to this journal."""
    lock = open(str(path) + ".lock", "a+b")
    if os.name == "nt":
        import msvcrt
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return lock
