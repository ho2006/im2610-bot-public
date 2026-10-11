"""IM2610 market data, strategy and the competition's HTTP adapter."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time as daytime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

SYMBOL = "IM2610"
TICK = 0.2
MULTIPLIER = 200
TZ = timezone(timedelta(hours=8))
EXPIRY = date(2026, 10, 16)
SINA_URL = "https://hq.sinajs.cn/list=nf_IM2610"
TRADE_URL = "https://qhsim.eastmoney.com/simCustomCompeteApi/api/skills/action"
OPERATIONS = {"0": "买开", "1": "卖开", "2": "买平", "3": "卖平"}
STATES = {"0": "FILLED", "1": "PARTIAL", "2": "OPEN", "3": "CANCELLED"}
# CFFEX notice [2025]55: weekends remain closed even on civil workdays.
HOLIDAYS = (("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
            ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
            ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"),
            ("2026-10-01", "2026-10-07"))


class Unsafe(RuntimeError):
    """A condition that must prevent trading."""


class Uncertain(Unsafe):
    """A mutation may have reached the exchange; never retry it."""


class QueryUnavailable(Unsafe):
    """A read-only transport failure; retry reads after stopping known quotes."""


class NotSent(Unsafe):
    """A submission guard stopped the request before any network side effect."""


def number(value, *, integer=False):
    if isinstance(value, bool):
        raise Unsafe("数值字段不能为布尔值")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise Unsafe("接口数值字段缺失或格式不符") from None
    if not math.isfinite(result) or (integer and not result.is_integer()):
        raise Unsafe("接口数值字段非有限数或非整数")
    return int(result) if integer else result


def tick_price(value, up=False):
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return float((Decimal(str(value)) / Decimal("0.2")).to_integral_value(rounding=rounding) * Decimal("0.2"))


def aligned(value):
    return abs(value / TICK - round(value / TICK)) < 1e-7


def in_session(now):
    now = now.astimezone(TZ)
    t = now.time().replace(tzinfo=None)
    day = now.date().isoformat()
    return (now.year == 2026 and now.weekday() < 5 and now.date() <= EXPIRY
            and not any(start <= day <= end for start, end in HOLIDAYS)
            and (daytime(9, 30) <= t < daytime(11, 30) or daytime(13) <= t < daytime(15)))


@dataclass
class Config:
    poll_seconds: float = 10
    max_quote_age: float = 8
    order_lifetime: float = 40
    reprice_ticks: int = 3
    order_size: int = 1
    max_position: int = 1
    max_daily_loss: float = 3000
    max_use_rate: float = 0.65
    margin_rate: float = 0.2
    half_spread_ticks: float = 2
    edge_ticks: float = 1
    fee_per_side_bps: float = 0
    inventory_skew_ticks: float = 3
    imbalance_cutoff: float = 0.65
    ema_alpha: float = 0.5
    max_spread_ticks: int = 30
    max_orders_per_day: int = 100
    flatten_time: str = "14:50"
    entry_filter: bool = True

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise Unsafe("未知策略参数")
        config = cls(**raw)
        if type(config.entry_filter) is not bool:
            raise Unsafe("entry_filter 必须为布尔值")
        bounds = {
            "poll_seconds": (5, 300), "max_quote_age": (1, 30), "order_lifetime": (10, 600),
            "reprice_ticks": (1, 100), "order_size": (1, 10), "max_position": (1, 20),
            "max_daily_loss": (1, 100000), "max_use_rate": (0.01, 0.8), "margin_rate": (0.08, 1),
            "half_spread_ticks": (1, 100), "edge_ticks": (0, 100), "fee_per_side_bps": (0, 100),
            "inventory_skew_ticks": (0, 100), "imbalance_cutoff": (0.05, 1), "ema_alpha": (0.01, 1),
            "max_spread_ticks": (1, 200), "max_orders_per_day": (1, 1000),
        }
        ints = {"reprice_ticks", "order_size", "max_position", "max_spread_ticks", "max_orders_per_day"}
        for key, (low, high) in bounds.items():
            val = number(getattr(config, key), integer=key in ints)
            if not low <= val <= high:
                raise Unsafe(f"参数 {key} 超出范围 [{low}, {high}]")
            setattr(config, key, val)
        if config.order_size > config.max_position:
            raise Unsafe("单笔手数不能超过库存上限")
        if not isinstance(config.flatten_time, str) or not re.fullmatch(r"14:[0-5][0-9]", config.flatten_time):
            raise Unsafe("收盘减仓时间须在 14:00–14:59")
        return config


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    last: float
    volume: int
    upper: float
    lower: float
    timestamp: float
    received: float

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def imbalance(self):
        return (self.bid_size - self.ask_size) / (self.bid_size + self.ask_size)

    @property
    def microprice(self):
        return (self.ask * self.bid_size + self.bid * self.ask_size) / (self.bid_size + self.ask_size)

    def fresh(self, max_age, now=None):
        now = time.time() if now is None else now
        age = now - self.timestamp
        if age < -2 or age > max_age or not -2 <= now - self.received <= max_age:
            raise Unsafe("行情过期或时钟偏差：暂停报价")

    def public(self):
        return {**asdict(self), "mid": self.mid, "imbalance": self.imbalance, "microprice": self.microprice}


def parse_sina(text, received=None):
    # CFFEX's 50-field format is different from Sina commodity futures.
    match = re.fullmatch(r'\s*var hq_str_nf_IM2610="([^"\r\n]+)";\s*', text)
    if not match:
        raise Unsafe("新浪未返回 IM2610 行情")
    values = match.group(1).split(",")
    if len(values) != 50:
        raise Unsafe("新浪盘口格式变更：拒绝猜测字段")
    try:
        stamp = datetime.strptime(values[36] + " " + values[37], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
    except ValueError:
        raise Unsafe("新浪行情时间格式不符") from None
    q = Quote(number(values[16]), number(values[26]), number(values[17], integer=True),
              number(values[27], integer=True), number(values[3]), number(values[4], integer=True),
              number(values[9]), number(values[10]), stamp, received or time.time())
    if not (0 < q.lower <= q.bid < q.ask <= q.upper and q.lower <= q.last <= q.upper
            and q.bid_size > 0 and q.ask_size > 0 and q.volume >= 0
            and all(aligned(x) for x in (q.bid, q.ask, q.last, q.lower, q.upper))):
        raise Unsafe("盘口为空、交叉、越界或价格不符合最小变动单位")
    return q


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the account key to another host.


def fetch_quote():
    request = urllib.request.Request(SINA_URL, headers={
        "Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            text = response.read(65536).decode("gb18030")
    except (OSError, http.client.HTTPException, UnicodeError):
        raise Unsafe("新浪行情连接失败") from None
    return parse_sina(text)


def make_quotes(q, inventory, available_long, available_short, config, fair=None, reduce_only=False):
    """OBI gates the adverse side; microprice estimates fair, not a second OBI alpha."""
    fair = q.microprice if fair is None else fair
    fair -= inventory * config.inventory_skew_ticks * TICK
    half = max(config.half_spread_ticks * TICK,
               q.mid * config.fee_per_side_bps / 10000 + config.edge_ticks * TICK)
    buy = max(q.lower, tick_price(min(q.bid, fair - half)))
    sell = min(q.upper, tick_price(max(q.ask, fair + half), up=True))
    plans = []
    if available_short > 0:
        plans.append({"op": "2", "price": q.bid if reduce_only else buy,
                      "qty": min(config.order_size, available_short), "side": "buy"})
    elif not reduce_only and inventory < config.max_position and q.imbalance >= -config.imbalance_cutoff:
        plans.append({"op": "0", "price": buy, "qty": min(config.order_size, config.max_position - max(0, inventory)), "side": "buy"})
    if available_long > 0:
        plans.append({"op": "3", "price": q.ask if reduce_only else sell,
                      "qty": min(config.order_size, available_long), "side": "sell"})
    elif not reduce_only and inventory > -config.max_position and q.imbalance <= config.imbalance_cutoff:
        plans.append({"op": "1", "price": sell, "qty": min(config.order_size, config.max_position + min(0, inventory)), "side": "sell"})
    return plans


class EntryFilter:
    """Fixed research thresholds; bounds entry exposure, never selects exit quotes."""
    WINDOW_SECONDS = 300
    WARMUP_SECONDS = 120
    MIN_OBSERVATIONS = 6
    RESET_GAP_SECONDS = 90
    MAX_SPREAD_TICKS = 6
    MAX_EFFICIENCY = .30
    MAX_JUMP_TICKS = 20
    MIN_DEPTH = 10

    def __init__(self):
        self.samples = deque()
        self.last_source = self.last_day = None
        self.status = {"allowed": False, "reason": "等待新鲜交易时段采样", "samples": 0,
                       "span_seconds": 0, "efficiency": None, "max_jump_ticks": None,
                       "spread_ticks": None, "min_depth": None, "source_time": None}

    def observe(self, q):
        """Caller validates freshness/session; repeats cannot advance the price window."""
        day = datetime.fromtimestamp(q.timestamp, TZ).date()
        if self.last_source is not None and (q.timestamp < self.last_source
                or q.timestamp-self.last_source > self.RESET_GAP_SECONDS or day != self.last_day):
            self.samples.clear()
        if q.timestamp != self.last_source:
            self.samples.append((q.timestamp, q.mid))
        else:
            # Same-second book changes still recheck price, spread and depth, without extra samples.
            self.samples[-1] = (q.timestamp, q.mid)
        while self.samples and self.samples[0][0] < q.timestamp-self.WINDOW_SECONDS:
            self.samples.popleft()
        self.last_source, self.last_day = q.timestamp, day
        mids = [mid for _, mid in self.samples]
        moves = [b-a for a, b in zip(mids, mids[1:])]
        variation = sum(abs(step) for step in moves)
        efficiency = abs(mids[-1]-mids[0])/variation if variation else 0
        jump = max((abs(step)/TICK for step in moves), default=0)
        span, spread, depth = (q.timestamp-self.samples[0][0], (q.ask-q.bid)/TICK,
                               min(q.bid_size, q.ask_size))
        allowed = False
        if len(self.samples) < self.MIN_OBSERVATIONS or span < self.WARMUP_SECONDS:
            reason = "采样不足：至少 6 个独立源时间并覆盖 120 秒"
        elif spread > self.MAX_SPREAD_TICKS+1e-8:
            reason = "盘口价差过宽"
        elif depth < self.MIN_DEPTH:
            reason = "买卖一档数量不足"
        elif jump > self.MAX_JUMP_TICKS+1e-8:
            reason = "近期价格跳变"
        elif efficiency > self.MAX_EFFICIENCY+1e-8:
            reason = "近期单边趋势明显"
        else:
            allowed = True
            reason = "环境通过，继续检查报价与账户限制"
        self.status = {"allowed": allowed, "reason": reason,
                       "samples": len(self.samples), "span_seconds": span,
                       "efficiency": efficiency, "max_jump_ticks": jump, "spread_ticks": spread,
                       "min_depth": depth, "source_time": q.timestamp}
        return self.status


def order_key(row):
    return json.dumps([row.get("day", datetime.now(TZ).date().isoformat()), row["time"], row["op"], row["price"], row["qty"]], separators=(",", ":"))


def order_matches(row, intent):
    return all(row[k] == intent[k] for k in ("op", "price", "qty"))


class Competition:
    mode = "competition"

    def __init__(self, key):
        if not key:
            raise Unsafe("未配置比赛账户密钥")
        self.key = key
        self.identity = hashlib.sha256(key.encode()).hexdigest()
        self.names = {SYMBOL}
        self.known_names = set()
        self.last_request = 0.0
        self.opener = urllib.request.build_opener(NoRedirect)
        self.schedule = {}

    def call(self, action, _before_send=None, **params):
        mutation = action in ("order_submission", "cancel_order")
        # ponytail: one serialized account, at most one request/second; no distributed workers.
        time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = urllib.request.Request(TRADE_URL,
            data=json.dumps({"action": action, **params}).encode("utf-8"),
            headers={"CFAPIKEY": self.key, "Content-Type": "application/json"})
        if _before_send:
            _before_send()  # A rate-limit wait must not let a quote expire before sending.
        try:
            with self.opener.open(request, timeout=6) as response:
                body = json.loads(response.read(2_000_000).decode("utf-8"))
            if not isinstance(body, dict) or type(body.get("status")) is not int:
                raise ValueError("missing status")
        except urllib.error.HTTPError as exc:
            error = Uncertain if mutation else (QueryUnavailable if exc.code in (408, 429) or exc.code >= 500 else Unsafe)
            raise error("交易请求回执不明确，已禁止重发" if mutation else f"比赛查询 HTTP {exc.code}") from None
        except (OSError, http.client.HTTPException):
            error = Uncertain if mutation else QueryUnavailable
            raise error("交易请求回执不明确，已禁止重发" if mutation else "比赛查询失败") from None
        except (ValueError, UnicodeError):
            error = Uncertain if mutation else Unsafe
            raise error("交易请求回执不明确，已禁止重发" if mutation else "比赛回执格式不符") from None
        if body["status"] != 0:
            # Don't echo arbitrary upstream text: it may contain account identifiers.
            raise Unsafe(f"比赛接口拒绝请求（状态 {body['status']}）")
        if not mutation and "result" not in body:
            raise Unsafe("比赛查询缺少结果字段")
        return body.get("result")

    def prepare(self):
        contracts = self.call("query_futures_contracts")
        if not isinstance(contracts, list) or not all(isinstance(r, dict) for r in contracts):
            raise Unsafe("可交易合约格式不符")
        found = [r for r in contracts if r.get("futcode") == SYMBOL and str(r.get("isOption")) == "0"]
        if len(found) != 1:
            raise Unsafe("IM2610 未在可交易合约中，禁止换月")
        name = found[0].get("futname")
        if not isinstance(name, str) or not name or sum(r.get("futname") == name for r in contracts) != 1:
            raise Unsafe("合约名称无法唯一对应 IM2610")
        self.names.add(name)
        self.known_names = {r.get("futname") for r in contracts} | {r.get("futcode") for r in contracts}
        self.schedule = self.call("query_race_schedule")

    def race_active(self, now):
        for start, end in (("preStartTime", "preEndTime"), ("startTime", "endTime")):
            try:
                a, b = (datetime.strptime(self.schedule[k], "%Y%m%d %H:%M").replace(tzinfo=TZ) for k in (start, end))
            except (KeyError, TypeError, ValueError):
                continue
            if a <= now <= b:
                return True
        return False

    def is_target(self, row):
        code = row.get("futcode")
        if code is not None and not isinstance(code, str):
            raise Unsafe("账户记录的合约代码格式不符")
        if code:
            return code == SYMBOL
        name = row.get("futname")
        if not isinstance(name, str) or not name or (self.known_names and name not in self.known_names):
            raise Unsafe("账户记录缺少可识别的合约名称，禁止把未知持仓视为空仓")
        return name in self.names

    def account(self):
        body = self.call("query_account_detail")
        try:
            row = body["futureAccountDetail"]
            equity = number(row["currentEquity"])
            raw = row["useRate"]
            use_rate = number(raw[:-1]) / 100 if isinstance(raw, str) and raw.endswith("%") else number(raw)
        except (KeyError, TypeError):
            raise Unsafe("账户结构不符") from None
        if equity <= 0 or not 0 <= use_rate <= 1:
            raise Unsafe("账户权益或保证金使用率异常")
        return {"equity": equity, "use_rate": use_rate}

    def holdings(self):
        body = self.call("query_account_holdings")
        if (not isinstance(body, dict) or not isinstance(body.get("futureAccountHoldings"), list)
                or not all(isinstance(r, dict) for r in body["futureAccountHoldings"])):
            raise Unsafe("持仓结构不符")
        result = {"long": 0, "short": 0, "available_long": 0, "available_short": 0}
        for row in body["futureAccountHoldings"]:
            if not self.is_target(row):
                continue
            side = {"多": "long", "空": "short"}.get(row.get("mmfx"))
            if side is None:
                raise Unsafe("IM2610 持仓方向无法识别")
            qty, available = number(row.get("ccsl"), integer=True), number(row.get("kysl"), integer=True)
            if not 0 <= available <= qty:
                raise Unsafe("可平持仓数量异常")
            result[side] += qty
            result["available_" + side] += available
        return result

    def orders(self, pending=False, day=None):
        today = datetime.now(TZ).date().isoformat()
        day = day or today
        if not pending and day != today:
            compact = date.fromisoformat(day).strftime("%Y%m%d")
            rows = self.call("query_history_orders", startDate=compact, endDate=compact)
        else:
            rows = self.call("query_pending_orders" if pending else "query_today_orders")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise Unsafe("委托列表格式不符")
        result = []
        for row in rows:
            if not self.is_target(row):
                continue
            op = str(row.get("operType"))
            state = STATES.get(str(row.get("wtzt", row.get("status"))))
            qty = number(row.get("wtsl"), integer=True)
            remaining = number(row.get("wcjsl"), integer=True)
            price = number(row.get("wtjg"))
            stamp = row.get("wtsj")
            if op not in OPERATIONS or state is None or not stamp or not (0 <= remaining <= qty) or qty <= 0 or price <= 0 or not aligned(price):
                raise Unsafe("委托字段无法安全解析")
            if (state == "FILLED" and remaining != 0) or (state in ("OPEN", "PARTIAL") and remaining == 0):
                raise Unsafe("委托状态与剩余数量冲突")
            raw_id = row.get("wth", "")
            if pending and (type(raw_id) not in (str, int) or not str(raw_id).strip()):
                raise Unsafe("挂单缺少可撤委托号")
            oid = str(raw_id) if type(raw_id) in (str, int) else ""
            result.append({"op": op, "price": price, "qty": qty, "remaining": remaining,
                           "state": state, "time": str(stamp), "remote_id": oid, "day": day})
        return result

    def trades(self):
        rows = self.call("query_today_trades")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise Unsafe("成交列表格式不符")
        result = []
        for row in rows:
            if self.is_target(row):
                op = str(row.get("operType"))
                if op not in OPERATIONS:
                    raise Unsafe("成交方向无法识别")
                price, qty = number(row.get("cjjg")), number(row.get("cjsl"), integer=True)
                if price <= 0 or not aligned(price) or qty <= 0 or not row.get("cjsj"):
                    raise Unsafe("成交字段无法安全解析")
                result.append({"time": str(row["cjsj"]), "operation": OPERATIONS[op], "price": price, "qty": qty})
        return result

    def submit(self, plan, quote):
        validate_order(plan, quote)
        def guard():
            try:
                quote.fresh(getattr(self, "max_quote_age", 8))
                if not in_session(datetime.fromtimestamp(time.time(), TZ)):
                    raise Unsafe("提交前已离开交易时段")
                if getattr(self, "before_submit", None):
                    self.before_submit(plan, quote)
            except Unsafe as exc:
                raise NotSent(str(exc)) from None
        self.call("order_submission", _before_send=guard,
                  market="CFFEX", futcode=SYMBOL, operType=plan["op"],
                  wtjg=f"{plan['price']:.1f}", wtsl=str(plan["qty"]))

    def cancel(self, remote_id):
        if not remote_id:
            raise Unsafe("缺少委托号，不能撤单")
        self.call("cancel_order", futcode=SYMBOL, wth=remote_id)


def validate_order(plan, quote):
    price = number(plan.get("price"))
    qty = number(plan.get("qty"), integer=True)
    op = plan.get("op")
    if op not in OPERATIONS or qty <= 0 or not aligned(price) or not quote.lower <= price <= quote.upper:
        raise Unsafe("下单参数不合法")
    if (op in ("0", "2") and price > quote.bid) or (op in ("1", "3") and price < quote.ask):
        raise Unsafe("报价不再被动，取消本轮下单")
