"""Fixed offline entry filters. Never changes production risk or exit quotes."""
from core import EntryFilter, TICK, make_quotes

# Research assumptions, fixed before comparison; not fitted to returns or data.
WINDOW_SECONDS = EntryFilter.WINDOW_SECONDS
WARMUP_SECONDS = EntryFilter.WARMUP_SECONDS
MIN_OBSERVATIONS = EntryFilter.MIN_OBSERVATIONS
RESET_GAP_SECONDS = EntryFilter.RESET_GAP_SECONDS
MAX_SPREAD_TICKS = EntryFilter.MAX_SPREAD_TICKS
SELECTIVE_MAX_EFFICIENCY = EntryFilter.MAX_EFFICIENCY
SELECTIVE_MAX_JUMP_TICKS = EntryFilter.MAX_JUMP_TICKS
SELECTIVE_MIN_DEPTH = EntryFilter.MIN_DEPTH
TREND_MIN_EFFICIENCY = .60
TREND_MIN_MOVEMENT_TICKS = 8
TREND_MAX_JUMP_TICKS = 30
PULLBACK_MIN_TICKS = 1
PULLBACK_MAX_TICKS = 6


class Policy:
    def __init__(self, name, opening_priority="buy_first"):
        if name not in ("baseline", "selective_mm", "trend_pullback", "flat"):
            raise ValueError("Unknown offline research policy")
        if opening_priority not in ("buy_first", "sell_first"):
            raise ValueError("Unknown opening priority")
        self.name, self.opening_priority = name, opening_priority
        self.window = EntryFilter()
        self.samples = self.window.samples
        self.opening_ops = set()
        self.stats = {"observed": 0, "allowed": 0, "blocked": 0}
        # ponytail: keys stay in RAM for one offline experiment; make a new Policy per day/trial.
        self.seen_openings = set()

    def observe(self, q):
        """Only unique source observations advance the window, never repeated engine calls."""
        self.window.observe(q)
        self.opening_ops = self.allowed_openings(q)

    def allowed_openings(self, q):
        if self.name == "baseline":
            return {"0", "1"}
        if self.name == "selective_mm":
            return {"0", "1"} if self.window.status["allowed"] else set()
        if self.name == "flat" or len(self.samples) < MIN_OBSERVATIONS:
            return set()
        if self.samples[-1][0]-self.samples[0][0] < WARMUP_SECONDS:
            return set()
        if q.ask-q.bid > MAX_SPREAD_TICKS*TICK+1e-8:
            return set()
        mids = [mid for _, mid in self.samples]
        moves = [b-a for a, b in zip(mids, mids[1:])]
        movement, variation = mids[-1]-mids[0], sum(abs(step) for step in moves)
        efficiency = abs(movement)/variation if variation else 0
        jump = max(abs(step) for step in moves)
        if (efficiency < TREND_MIN_EFFICIENCY-1e-8
                or abs(movement) < TREND_MIN_MOVEMENT_TICKS*TICK-1e-8
                or jump > TREND_MAX_JUMP_TICKS*TICK+1e-8):
            return set()
        direction = 1 if movement > 0 else -1
        extreme = max(mids[:-1]) if direction > 0 else min(mids[:-1])
        pullback = direction*(extreme-mids[-1])
        if (direction*moves[-1] < -1e-8
                and PULLBACK_MIN_TICKS*TICK-1e-8 <= pullback <= PULLBACK_MAX_TICKS*TICK+1e-8):
            return {"0" if direction > 0 else "1"}
        return set()

    def __call__(self, q, inventory, available_long, available_short, config,
                 fair=None, reduce_only=False):
        self.observe(q)
        original = make_quotes(q, inventory, available_long, available_short, config, fair, reduce_only)
        for plan in original:
            if plan["op"] not in ("0", "1"):
                continue
            # Count eligible opening opportunities per source+operation, not repeated calls or fills.
            key = (q.timestamp, plan["op"])
            if key not in self.seen_openings:
                self.seen_openings.add(key)
                self.stats["observed"] += 1
                self.stats["allowed" if plan["op"] in self.opening_ops else "blocked"] += 1
        plans = [p for p in original if p["op"] not in ("0", "1") or p["op"] in self.opening_ops]
        first = "0" if self.opening_priority == "buy_first" else "1"
        openings = iter(sorted((p for p in plans if p["op"] in ("0", "1")),
                               key=lambda p: p["op"] != first))
        # Preserve every original closing quote and its slot; swap only opening slots.
        return [next(openings) if p["op"] in ("0", "1") else p for p in plans]
