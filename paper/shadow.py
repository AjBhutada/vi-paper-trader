"""SHADOW live bot for the three plans (A and B on Vodafone Idea, C on the 13-stock basket). It does in real time
exactly what a real trading bot would do, but sends its orders to the Upstox SANDBOX (pretend orders, no money).
Without a sandbox token it only logs them.

  morning  (start ~08:40 India): connect to the live feed; 09:00:05 send the pre-open orders
             A: sell last night's futures (market order, NRML)      - only if the paper bot holds a position
             B: limit SELL of VI shares at yesterday's last price x 1.01 (intraday)
           09:06:30 Plan C pick: read each basket stock's INDICATIVE auction price (Upstox feed field 'iep', sent only
             during the pre-open); stocks already >= yesterday's last x 1.02 are taken in priority order, up to 3 and
             5 x the Plan C account, and get a limit SELL at that price. (NSE closes pre-open order entry at a random
             second between 09:07 and 09:08, so the pick must be done before 09:07.)
           09:15 watch the auction prices; B: if its limit was reached, buy back from 09:16:00, else cancel.
             C: each picked stock whose limit was reached is shorted at the auction price; others are cancelled.
           09:20:00 C: buy back every filled C short (market order at the best asks).
  afternoon (start ~15:05 India): limit buy at 15:20:00, market buy at 15:25:00 if unfilled (futures, NRML).

Every step is logged with the live prices seen at that instant and the order replies -> state/shadow/.
Sizes come from the paper accounts (state/meta.json), so shadow and paper describe the same trades.
The paper bot (vi_bot.py) stays the official P&L record; this bot checks timing, orders and the live connection.

Usage: python paper/shadow.py morning|afternoon [--dry] [--at HH:MM:SS]   (--at: pretend the session starts now
       at that clock time, for tests; all waits are shifted)
Tokens (environment only, never printed): UPSTOX_ACCESS_TOKEN = live feed; UPSTOX_SANDBOX_TOKEN = sandbox orders.
"""
import argparse, json, math, os, sys, threading, time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vi_bot as vb                                         # shared settings, candles, contracts, holidays, sizing rules

S, IST, ROOT = vb.S, vb.IST, vb.ROOT
C_ = vb.C_
EQ = S["equity_key"]
OUT = ROOT / "state" / "shadow"


def walk(levels, qty):
    """Average price for buying `qty` through the visible (price, qty) levels; beyond them, one price step worse."""
    left, cost = qty, 0.0
    for p, q in levels or []:
        if not p:
            continue
        take = min(left, q); cost += take * p; left -= take
        if left <= 0:
            return round(cost / qty, 4)
    if not levels:
        return None
    return round((cost + left * (levels[-1][0] + vb.tick_size(levels[-1][0]))) / qty, 4)


def c_setup(bot, day, log):
    """Before 09:00: for each Plan C stock (priority order) yesterday's last price, the 2% limit (on the price step)
    and the usual opening-minute traded value (the size cap). [] before Plan C's start date."""
    if str(day) < C_["start_date"]:
        return []
    pd_ = bot.prev_tday(day)
    rows = []
    for rank, s in enumerate(C_["stocks"], 1):
        key = bot.eqk.get(s)
        if not key:
            log("c_skip", f"{s}: not in Upstox's instrument list"); continue
        prev = vb.candles(key, pd_) if pd_ else {}
        if not prev:
            log("c_skip", f"{s}: no candles on {pd_}"); continue
        pc = prev[max(prev)][3]
        raw = pc * (1 + C_["gap_threshold_pct"] / 100)
        step = vb.tick_size(raw)
        limit = round(math.ceil(round(raw / step, 6)) * step, 2)
        try:
            fmv = vb.opening_value_median(key, day)
        except Exception as e:
            fmv = None; log("c_skip", f"{s}: opening value not available ({str(e)[:80]})")
        rows.append(dict(rank=rank, sym=s, key=key, prev=pc, limit=limit, open_value=round(fmv) if fmv else None))
    return rows


def c_pick(rows, feed, eq_c):
    """Plan C pick from the INDICATIVE auction prices: the paper rules, with the indicative price standing in for the
    auction price that is not known yet. Sizes are worked out on the limit price (the most the order can fill below)."""
    now = time.time()
    for r in rows:
        iep, ieq, rx = feed.iep_of(r["key"])
        r.update(iep=iep, ieq=ieq, iep_age_s=round(now - rx) if rx else None,
                 gap_ind=round((iep / r["prev"] - 1) * 100, 2) if iep else None, signal=False, picked=False)
        if iep and iep >= r["limit"] - 1e-9 and r["gap_ind"] <= 20:      # > 20% = a corporate action
            r["signal"] = True
    budget, n = C_["leverage"] * eq_c, 0
    for r in rows:                                                       # rows are in priority order
        if not r["signal"]:
            continue
        if n >= C_["max_positions"] or budget < C_["min_position_rs"]:
            r["why_not"] = "no room left"; continue
        if not r["open_value"]:
            r["why_not"] = "too little history"; continue
        liq = C_["liquidity_frac"] * r["open_value"]
        notional = min(liq, C_["cap_rs"], budget)
        qty = int(notional // r["limit"])
        if qty * r["limit"] < C_["min_position_rs"]:
            r["why_not"] = "position too small"; continue
        r.update(picked=True, qty=qty, size_why=("5% of its usual opening-minute value" if liq <= min(C_["cap_rs"], budget) else
                                                 "the Rs 50 lakh cap" if C_["cap_rs"] <= budget else "what was left of 5x the account"))
        budget -= qty * r["limit"]; n += 1
    return [r for r in rows if r["picked"]]


def c_book(r, src, log, orders):
    """A picked Plan C stock at the auction: filled at the auction price if that reached the limit, else cancel."""
    o, sym = r["auction"], r["sym"]
    if o >= r["limit"] - 1e-9:
        r["filled"] = True
        log("fill", {"leg": f"C-short-{sym}", "sym": sym, "price": o, "qty": r["qty"], "limit": r["limit"], "prev_last": r["prev"],
                     "iep_at_pick": r["iep"], "rank": r["rank"], "source": src})
    else:
        log("fill", {"leg": f"C-none-{sym}", "sym": sym, "open": o, "limit": r["limit"], "iep_at_pick": r["iep"], "source": src})
        orders.cancel(f"C-{sym}-short", r.get("oid"))


class Clock:
    """Real India time, optionally shifted so a test can rehearse a session at any hour."""
    def __init__(self, at=None):
        self.shift = 0.0
        if at:
            now = datetime.now(IST)
            want = datetime.combine(now.date(), datetime.strptime(at, "%H:%M:%S").time(), IST)
            self.shift = (want - now).total_seconds()

    def now(self):
        return datetime.now(IST) + timedelta(seconds=self.shift)

    def real_ts(self, hms):
        """Real Unix time of HH:MM:SS today on this clock (feed receive times are real time)."""
        target = datetime.combine(self.now().date(), datetime.strptime(hms, "%H:%M:%S").time(), IST)
        return time.time() - (self.now() - target).total_seconds()

    def wait_until(self, hms):
        target = datetime.combine(self.now().date(), datetime.strptime(hms, "%H:%M:%S").time(), IST)
        while True:
            left = (target - self.now()).total_seconds()
            if left <= 0:
                return
            time.sleep(min(left, 0.2 if left < 2 else 1.0))


class Feed:
    """Latest full-mode snapshot per instrument from the Upstox V3 live feed."""
    def __init__(self, keys, log):
        import upstox_client
        self.snap, self.log, self.n = {}, log, 0
        self.low = {}                                       # lowest traded price per key since reset_low()
        self.iep = {}                                       # key -> (indicative auction price, its quantity, receive time)
        self.preopen = None                                 # NSE_EQ pre-open status from 'market_info' messages
        self.lock = threading.Lock()
        cfg = upstox_client.Configuration()
        cfg.access_token = os.environ["UPSTOX_ACCESS_TOKEN"]
        self.s = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg), list(keys), "full")
        self.s.on("message", self.on_msg)
        self.s.on("open", lambda: log("feed", "connected"))
        self.s.on("error", lambda e: log("feed_error", str(e)[:300]))
        self.s.on("close", lambda *a: log("feed", "closed " + str(a)[:200]))
        self.s.auto_reconnect(True, 3, 30)

    def start(self):
        self.s.connect()

    def stop(self):
        try:
            self.s.disconnect()
        except Exception:
            pass

    def on_msg(self, m):
        m = m or {}
        if m.get("type") == "market_info":
            st = ((m.get("marketInfo") or {}).get("preOpenSessionStatus") or {}).get("NSE_EQ")
            if st and st.get("status") != self.preopen:
                self.preopen = st.get("status")
                self.log("preopen_status", st)
        feeds = m.get("feeds") or {}
        now = time.time()
        with self.lock:
            for k, v in feeds.items():
                mff = (v.get("fullFeed") or {}).get("marketFF") or {}
                lt = mff.get("ltpc") or {}
                q = [x for x in (mff.get("marketLevel") or {}).get("bidAskQuote", []) if x]
                ohlc = [o for o in (mff.get("marketOHLC") or {}).get("ohlc", []) if o.get("interval") == "1d"]
                iep = mff.get("iep") or lt.get("iep")              # only sent during the pre-open / closing auctions
                if iep:
                    self.iep[k] = (iep, mff.get("ieq"), now)
                self.snap[k] = {"rx": now, "ltp": lt.get("ltp"), "cp": lt.get("cp"), "iep": iep, "ieq": mff.get("ieq"),
                                "bid": [(x.get("bidP"), int(x.get("bidQ", 0) or 0)) for x in q if x.get("bidP")],
                                "ask": [(x.get("askP"), int(x.get("askQ", 0) or 0)) for x in q if x.get("askP")],
                                "open": ohlc[0].get("open") if ohlc else None, "vtt": mff.get("vtt")}
                ltp = self.snap[k]["ltp"]
                if ltp and k in self.low:
                    self.low[k] = min(self.low[k], ltp)
                self.n += 1

    def reset_low(self, k):
        with self.lock:
            self.low[k] = float("inf")

    def lowest(self, k):
        with self.lock:
            return self.low.get(k, float("inf"))

    def get(self, k):
        with self.lock:
            return dict(self.snap.get(k) or {})

    def iep_of(self, k):
        """Latest indicative auction price seen this session: (price, quantity, receive time) or (None, None, None)."""
        with self.lock:
            return self.iep.get(k) or (None, None, None)


def limit_then_market(clock, feed, orders, log, key, qty, product, label, fallback_hms):
    """Live version of the limit rule: limit buy 1 paisa below the last traded price NOW; counted filled only if the
    market trades below it before `fallback_hms`; otherwise cancel it and send a market buy then.
    Logs a 'fill' event {leg: label, price, qty, how}."""
    off = S["limit_orders"]["offset_paise"] / 100
    s = feed.get(key)
    ref = s.get("ltp") or (s.get("ask") or [(None, 0)])[0][0]
    p = round(ref - off, 2)
    feed.reset_low(key)
    lid = orders.place(label + "-limit", key, "BUY", qty, "LIMIT", product, p)
    target = datetime.combine(clock.now().date(), datetime.strptime(fallback_hms, "%H:%M:%S").time(), IST)
    while clock.now() < target:
        if feed.lowest(key) <= p - 0.01 + 1e-9:
            log("fill", {"leg": label, "price": p, "qty": qty, "how": f"limit {p:.2f} (1 paisa below {ref}) filled when it traded at {feed.lowest(key)}"})
            return
        time.sleep(0.2)
    orders.cancel(label + "-limit", lid)
    s = feed.get(key)
    log("quote", {"leg": label, "best_ask": s.get("ask", [None])[:3], "ltp": s.get("ltp")})
    orders.place(label, key, "BUY", qty, "MARKET", product)
    log("fill", {"leg": label, "price": walk(s.get("ask"), qty), "qty": qty,
                 "how": f"limit {p:.2f} not filled by {fallback_hms[:5]}; market buy at the best ask"})


class Orders:
    """Sandbox order sender. dry=True: build and log the exact request but do not send it."""
    def __init__(self, dry, log):
        self.dry, self.log, self.api = dry, log, None
        if not dry:
            import upstox_client
            cfg = upstox_client.Configuration(sandbox=True)
            cfg.access_token = os.environ["UPSTOX_SANDBOX_TOKEN"]
            self.api = upstox_client.OrderApiV3(upstox_client.ApiClient(cfg))
            self.uc = upstox_client

    def place(self, label, key, side, qty, order_type, product, price=0.0):
        body = dict(quantity=int(qty), product=product, validity="DAY", price=round(price, 2), tag=f"vi-shadow-{label}"[:20],
                    instrument_token=key, order_type=order_type, transaction_type=side, disclosed_quantity=0,
                    trigger_price=0.0, is_amo=False, slice=False)
        t0 = time.time()
        if self.dry:
            self.log("order_dry", {"label": label, "request": body})
            return None
        try:
            r = self.api.place_order(self.uc.PlaceOrderV3Request(**body))
            ms = round((time.time() - t0) * 1000)
            ids = getattr(getattr(r, "data", None), "order_ids", None)
            self.log("order_sent", {"label": label, "request": body, "status": getattr(r, "status", None), "order_ids": ids, "ms": ms})
            return (ids or [None])[0]
        except Exception as e:
            self.log("order_error", {"label": label, "request": body, "error": str(e)[:400], "ms": round((time.time() - t0) * 1000)})
            return None

    def cancel(self, label, order_id):
        if self.dry or not order_id:
            self.log("cancel_dry" if self.dry else "cancel_skipped", {"label": label, "order_id": order_id})
            return
        t0 = time.time()
        try:
            r = self.api.cancel_order(order_id)
            self.log("cancel_sent", {"label": label, "order_id": order_id, "status": getattr(r, "status", None), "ms": round((time.time() - t0) * 1000)})
        except Exception as e:
            self.log("cancel_error", {"label": label, "order_id": order_id, "error": str(e)[:400]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["morning", "afternoon", "check", "summary"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--at")
    a = ap.parse_args()
    if a.part == "summary":
        write_summary()
        return
    clock = Clock(a.at)
    day = clock.now().date()
    OUT.mkdir(parents=True, exist_ok=True)
    events = []

    def log(what, detail=""):
        t = clock.now()
        events.append({"t": t.strftime("%H:%M:%S.%f")[:-3], "what": what, "detail": detail})
        print(t.strftime("%H:%M:%S"), what, json.dumps(detail, default=str)[:300] if not isinstance(detail, str) else detail, flush=True)

    if a.part == "check":
        # sandbox token test: a far-away limit BUY of 1 VI share at Rs 1.00, then cancel it (sandbox = no money)
        if not os.environ.get("UPSTOX_SANDBOX_TOKEN"):
            print("UPSTOX_SANDBOX_TOKEN is not set", flush=True); sys.exit(1)
        orders = Orders(False, log)
        oid = orders.place("check", EQ, "BUY", 1, "LIMIT", "I", 1.00)
        time.sleep(1)
        orders.cancel("check", oid)
        st = vb.State(ROOT / "state"); st.init(S["start_date"])
        ok = any(e["what"] == "order_sent" for e in events)
        print("SANDBOX CHECK:", "OK - token works" if ok else "FAILED (see order_error above)", flush=True)
        a.part = f"check-{clock.now():%H%M}"
        finish(day, a.part, events, st)
        sys.exit(0 if ok else 1)
    if (OUT / f"{day}_{a.part}.json").exists() and not a.at:
        print(f"{day} {a.part}: this session already ran today - nothing to do", flush=True)
        return
    st = vb.State(ROOT / "state")
    st.init(S["start_date"])
    m = st.meta
    hol = set(m.get("holidays", {}).get("days", []))
    if day.weekday() >= 5 or day.isoformat() in hol:
        log("skip", f"{day} is not a trading day")
        return finish(day, a.part, events, st)
    dry = a.dry or not os.environ.get("UPSTOX_SANDBOX_TOKEN")
    if dry and not a.dry:
        log("note", "UPSTOX_SANDBOX_TOKEN not set: orders are logged, not sent")
    if not os.environ.get("UPSTOX_ACCESS_TOKEN"):
        log("error", "UPSTOX_ACCESS_TOKEN not set: no live feed")
        st.alert(f"shadow-notoken-{day}", "Shadow bot: the live-feed token (UPSTOX_ACCESS_TOKEN) is missing.")
        return finish(day, a.part, events, st)
    bot = vb.Bot(st, clock.now())
    orders = Orders(dry, log)

    if a.part == "morning":
        pos = m.get("open")
        pd_ = bot.prev_tday(day)
        prev = vb.candles(EQ, pd_) if pd_ else {}
        if not prev:
            log("error", "no previous day's candles; cannot set the gap-up limit")
            return finish(day, a.part, events, st)
        last = prev[max(prev)][3]
        limit = math.ceil(last * (1 + S["gap_threshold_pct"] / 100) * 100 - 1e-9) / 100
        qty_b = int(min(S["short_leverage"] * m["equity"]["B"], S["short_cap_rs"]) // limit)
        keys = {EQ} | ({pos["key"]} if pos else set())
        log("plan", {"prev_last": last, "B_limit": limit, "B_qty": qty_b, "A_sell": pos and {"symbol": pos["symbol"], "qty": pos["qty"]}})
        if clock.now().strftime("%H:%M:%S") > "09:07:30":
            log("missed", "started after the pre-open order window; nothing sent today")
            return finish(day, a.part, events, st)
        PICK = C_.get("live_pick_at", "09:06:30")
        crows = c_setup(bot, day, log)
        if crows:
            log("c_plan", {"equity_C": round(m["equity"]["C"]), "pick_at": PICK,
                           "stocks": [{x: r[x] for x in ("sym", "prev", "limit", "open_value")} for r in crows]})
        ckeys = {r["key"] for r in crows}
        cname = {r["key"]: r["sym"] for r in crows}
        feed = Feed(keys | ckeys, log); feed.start()
        clock.wait_until("09:00:05")
        snaps = {k: feed.get(k) for k in keys}
        log("preopen_snapshot", snaps)
        a_id = orders.place("A-sell", pos["key"], "SELL", pos["qty"], "MARKET", "D") if pos else None
        b_id = orders.place("B-limit-short", EQ, "SELL", qty_b, "LIMIT", "I", limit)
        brief = lambda k: {x: feed.get(k).get(x) for x in ("ltp", "iep", "ieq", "open")}
        chosen = []
        for hms in sorted({"09:03:00", "09:05:00", "09:07:00", "09:07:50", "09:08:30", "09:12:00"} | ({PICK} if crows else set())):
            clock.wait_until(hms)
            log(f"preopen_{hms}", {**{k: {x: feed.get(k).get(x) for x in ("ltp", "iep", "open", "ask", "bid")} for k in keys},
                                   **{cname[k]: brief(k) for k in ckeys - keys}})
            if crows and hms == PICK:
                if (clock.now() - datetime.combine(day, datetime.strptime(PICK, "%H:%M:%S").time(), IST)).total_seconds() > 20:
                    log("c_missed", f"started too late for the Plan C pick at {PICK}; no Plan C orders today")
                    crows = []; continue
                chosen = c_pick(crows, feed, m["equity"]["C"])
                log("c_pick", {"at": clock.now().strftime("%H:%M:%S"), "equity_C": round(m["equity"]["C"]),
                               "stocks": [{x: r.get(x) for x in ("sym", "key", "rank", "prev", "limit", "iep", "ieq", "iep_age_s", "gap_ind",
                                                                 "signal", "picked", "why_not", "qty", "size_why")} for r in crows]})
                for r in chosen:
                    r["oid"] = orders.place(f"C-{r['sym']}-short", r["key"], "SELL", r["qty"], "LIMIT", "I", r["limit"])
        clock.wait_until("09:15:01")
        o = None
        for _ in range(40):                       # up to ~20 s for the first post-auction snapshot
            s = feed.get(EQ)
            o = s.get("open") or None
            if o:
                break
            time.sleep(0.5)
        fs = feed.get(pos["key"]) if pos else {}
        log("auction", {"VI_open": o, "B_limit": limit, "B_filled": bool(o and o >= limit),
                        "A_future_open": fs.get("open")})
        if pos:
            log("fill", {"leg": "A-sell", "price": fs.get("open"), "qty": pos["qty"], "symbol": pos["symbol"], "bought_on": pos["date"]})
        b_filled = bool(o and o >= limit)
        if b_filled:
            log("fill", {"leg": "B-short", "price": o, "qty": qty_b, "limit": limit, "prev_last": last})
        else:
            log("fill", {"leg": "B-none", "open": o, "limit": limit})
            orders.cancel("B-limit-short", b_id)
        # ---- Plan C at the auction: each picked stock is shorted at its auction price if that reached the limit
        t915 = clock.real_ts("09:15:00")
        live_open = lambda k: feed.get(k).get("open") if (feed.get(k).get("rx") or 0) >= t915 else None
        while chosen and clock.now().strftime("%H:%M:%S") < "09:15:45" and not all(live_open(r["key"]) for r in chosen):
            time.sleep(0.5)
        late = []
        for r in chosen:
            r["auction"] = live_open(r["key"])
            if r["auction"]:
                c_book(r, "live feed", log, orders)
            else:
                late.append(r)                     # auction price not seen live yet: read it from the 09:15 candle below
        if b_filled:
            L = S["limit_orders"]
            clock.wait_until(L["B_place"] + ":00")
            limit_then_market(clock, feed, orders, log, EQ, qty_b, "I", "B-cover", L["B_fallback"] + ":00")
        if late:
            clock.wait_until("09:16:05")           # the 09:15 one-minute candle is complete by then
        for r in late:
            cs = vb.candles(r["key"], day)
            r["auction"] = cs["09:15"][0] if "09:15" in cs else None
            if r["auction"]:
                c_book(r, "09:15 one-minute candle (not seen live by 09:15:45)", log, orders)
            else:
                log("error", f"Plan C: {r['sym']} auction price not found; its order is left as it is")
        # ---- Plan C buy-back at 09:20:00: market buy of every filled short at the best asks
        held = [r for r in chosen if r.get("filled")]
        if crows:
            clock.wait_until(C_["cover_minute"] + ":00")
            quotes = {r["key"]: feed.get(r["key"]) for r in held}
            for r in held:
                s = quotes[r["key"]]
                orders.place(f"C-{r['sym']}-cover", r["key"], "BUY", r["qty"], "MARKET", "I")
                px, how = walk(s.get("ask"), r["qty"]), f"market buy at the best asks at {C_['cover_minute']}:00"
                if px is None and s.get("ltp"):
                    half = max(vb.tick_size(s["ltp"]) / 2, s["ltp"] * 0.0002)
                    px, how = round(s["ltp"] + half, 4), f"no asks in the feed; last price {s['ltp']} + {half:.4f}"
                log("fill", {"leg": f"C-cover-{r['sym']}", "sym": r["sym"], "price": px, "qty": r["qty"], "how": how,
                             "best_ask": (s.get("ask") or [None])[:3]})
            log("c_opens", {r["sym"]: {"open": feed.get(r["key"]).get("open"), "prev": r["prev"], "limit": r["limit"],
                                       "picked": r["picked"]} for r in crows})
        time.sleep(2)
        feed.stop()
    else:
        c = bot.contract_for(day)
        if not c:
            log("error", "no futures contract")
            return finish(day, a.part, events, st)
        if clock.now().strftime("%H:%M:%S") > "15:25:00":
            log("missed", "started after 15:25; nothing sent today")
            return finish(day, a.part, events, st)
        feed = Feed({c["key"]}, log); feed.start()
        L = S["limit_orders"]
        clock.wait_until(L["A_place"] + ":00")
        s = feed.get(c["key"])
        est = s.get("ltp") or (s.get("ask") or [(None, 0)])[0][0]
        eqA = m["equity"]["A"]
        lot = c["lot"]
        margin = S["fut_margin_frac"] * est * lot if est else None
        lots = 0
        if margin and eqA >= margin:
            lots = max(1, min(int(eqA // margin), round(eqA / ((S["fut_margin_frac"] + S["lot_cushion_frac"]) * est * lot))))
        log("plan", {"contract": c["symbol"], "lot": lot, "lots": lots, "equity_A": round(eqA), "est_price": est})
        if lots:
            limit_then_market(clock, feed, orders, log, c["key"], lots * lot, "D", "A-buy", L["A_fallback"] + ":00")
        time.sleep(2)
        feed.stop()
    log("feed_stats", {"messages": feed.n})
    finish(day, a.part, events, st)


def finish(day, part, events, st):
    f = OUT / f"{day}_{part}.json"
    f.write_text(json.dumps({"day": str(day), "part": part, "events": events}, indent=1, default=str))
    write_summary()


def _inr(v, dec=0):
    """Rupees / quantities with Indian digit grouping (10,72,125)."""
    neg, v = v < 0, abs(v)
    whole, frac = (f"{v:.{dec}f}".split(".") + [""])[:2]
    head, tail = whole[:-3], whole[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail; head = head[:-2]
    s = (head + "," + tail) if head else tail
    return ("-" if neg else "") + s + ("." + frac if frac else "")


def summarize(js):
    """One plain-language summary of a session: {day, session, result, lines[], raw[]}."""
    ev = js["events"]
    get = lambda w: [e for e in ev if e["what"] == w]
    det = lambda e: e["detail"] if isinstance(e.get("detail"), dict) else {}
    part = js["part"]
    names = {EQ: "VI shares"}
    for e in get("plan"):
        d = det(e)
        if d.get("contract"):
            names["fut"] = d["contract"]
        if isinstance(d.get("A_sell"), dict) and d["A_sell"].get("symbol"):
            names["fut"] = d["A_sell"]["symbol"]
    for e in get("c_pick"):
        for x in det(e).get("stocks", []):
            if x.get("key") and x["key"] not in names:
                names[x["key"]] = f"{x['sym']} shares"
    name = lambda key: names.get(key) or ("VI shares" if key == EQ else names.get("fut", key))
    lines, result = [], "OK"

    def order_text(label, what):
        e = next((x for x in ev if x["what"] in ("order_sent", "order_dry", "order_error") and det(x).get("label") == label), None)
        if not e:
            return None
        d = det(e); r = d.get("request", {})
        txt = (f"{e['t'][:8]} {what}: {r.get('transaction_type', '').lower()} {_inr(r.get('quantity', 0))} {name(r.get('instrument_token'))}, "
               f"{'limit ₹' + format(r.get('price'), '.2f') if r.get('order_type') == 'LIMIT' else 'market order'}")
        if e["what"] == "order_sent":
            return txt + f" → accepted by the Upstox sandbox ({d.get('ms')} ms)"
        if e["what"] == "order_dry":
            return txt + " → logged only (sandbox token not added yet)"
        return txt + f" → REJECTED: {str(d.get('error'))[:120]}"

    if part.startswith("check"):
        ok = bool(get("order_sent"))
        c = det(get("cancel_sent")[0]) if get("cancel_sent") else {}
        o = det(get("order_sent")[0]) if ok else {}
        lines.append("Sandbox token test: a test order (buy 1 VI share at ₹1.00) was "
                     + (f"accepted in {o.get('ms')} ms and cancelled in {c.get('ms')} ms." if ok else "NOT accepted."))
        return {"day": js["day"], "session": "Token test", "result": "OK - token works" if ok else "FAILED", "lines": lines, "raw": ev}

    for e in get("skip"):
        return {"day": js["day"], "session": part.title(), "result": "Holiday / no trading", "lines": [str(e["detail"])], "raw": ev}
    conn = next((e for e in ev if e["what"] == "feed" and e["detail"] == "connected"), None)
    lines.append(f"Connected to the live Upstox feed at {conn['t'][:8]}." if conn else "Could NOT connect to the live feed.")
    if not conn:
        result = "Problem"
    for e in get("missed"):
        lines.append("Started too late: " + str(e["detail"])); result = "Missed"
    for e in get("error") + get("feed_error"):
        lines.append("Problem: " + str(e["detail"])[:160]); result = "Problem"
    dry = bool(get("order_dry"))

    if part == "morning":
        p = det(get("plan")[0]) if get("plan") else {}
        if p:
            lines.append(f"Yesterday's last price ₹{p.get('prev_last')} → gap-up limit ₹{p.get('B_limit'):.2f} (1% higher); "
                         f"short size {_inr(p.get('B_qty', 0))} shares.")
            if p.get("A_sell"):
                lines.append(f"Overnight position to sell: {_inr(p['A_sell']['qty'])} {p['A_sell']['symbol']}.")
        for lab, what in (("A-sell", "Plan A pre-open sell"), ("B-limit-short", "Plan B pre-open limit short")):
            t = order_text(lab, what)
            if t:
                lines.append(t)
        fills = {det(e).get("leg"): det(e) for e in get("fill")}
        a = det(get("auction")[0]) if get("auction") else {}
        if "A-sell" in fills:
            lines.append(f"09:15 auction: overnight futures sold at ₹{fills['A-sell'].get('price')}.")
        if a:
            reached = a.get("B_filled")
            lines.append(f"09:15 auction: VI opened at ₹{a.get('VI_open')} — the ₹{a.get('B_limit'):.2f} limit was "
                         + ("REACHED, so the short filled at the auction price." if reached else "not reached, so no short today."))
        if "B-cover" in fills:
            for lab, what in (("B-cover-limit", "Plan B limit buy-back"), ("B-cover", "Plan B market buy-back")):
                t = order_text(lab, what)
                if t:
                    lines.append(t)
            f = fills["B-cover"]
            lines.append(f"Bought back at ₹{f.get('price')}" + (f" — {f['how']}." if f.get("how") else " (best price to buy at 09:16:00)."))
        if any(e["what"] in ("cancel_sent", "cancel_dry") and det(e).get("label") == "B-limit-short" for e in ev):
            lines.append("Unfilled limit short cancelled" + (" (logged only)." if get("cancel_dry") else " in the sandbox."))
        # ---- Plan C
        for e in get("c_missed"):
            lines.append("Plan C: " + str(e["detail"])); result = "Missed"
        cp = det(get("c_pick")[0]) if get("c_pick") else {}
        if cp:
            stocks = cp.get("stocks", [])
            up = [x for x in stocks if x.get("signal")]
            lines.append(f"Plan C pre-open check at {cp.get('at')} (Plan C account ₹{_inr(cp.get('equity_C', 0))}): "
                         + ("; ".join(f"{x['sym']} indicative ₹{x['iep']} ({x['gap_ind']:+.2f}%, limit ₹{x['limit']:.2f})" for x in up)
                            + " — at least 2% up." if up else "no stock was 2% up at that moment."))
            near = sorted((x for x in stocks if x.get("gap_ind") is not None and not x.get("signal")), key=lambda x: -x["gap_ind"])[:3]
            if near:
                lines.append("Closest below 2%: " + ", ".join(f"{x['sym']} {x['gap_ind']:+.2f}%" for x in near) + ".")
            noiep = [x["sym"] for x in stocks if x.get("iep") is None]
            if noiep:
                lines.append("No indicative price from the feed for: " + ", ".join(noiep) + ".")
            for x in up:
                if x.get("picked"):
                    lines.append(f"Plan C picked {x['sym']} (priority {x['rank']}): limit short {_inr(x['qty'])} shares at "
                                 f"₹{x['limit']:.2f}; size: {x.get('size_why')}.")
                    t = order_text(f"C-{x['sym']}-short", f"Plan C pre-open limit short {x['sym']}")
                    if t:
                        lines.append(t)
                else:
                    lines.append(f"Plan C did not take {x['sym']}: {x.get('why_not')}.")
        for leg, f in fills.items():
            if leg.startswith("C-short-"):
                src = "" if f.get("source") == "live feed" else f" (price from the {f.get('source')})"
                lines.append(f"09:15 auction: {f['sym']} opened at ₹{f['price']} — the ₹{f['limit']:.2f} limit was REACHED, "
                             f"so the short filled at the auction price{src}.")
            elif leg.startswith("C-none-"):
                lines.append(f"09:15 auction: {f['sym']} opened at ₹{f.get('open')} — below the ₹{f['limit']:.2f} limit, so no short; "
                             "order cancelled.")
        for leg, f in fills.items():
            if leg.startswith("C-cover-"):
                t = order_text(f"C-{f['sym']}-cover", f"Plan C buy-back {f['sym']}")
                if t:
                    lines.append(t)
                lines.append(f"Bought back {f['sym']} at ₹{f.get('price')} — {f.get('how')}.")
        co = det(get("c_opens")[0]) if get("c_opens") else {}
        late = [f"{s} (opened ₹{v['open']}, limit ₹{v['limit']:.2f})" for s, v in co.items()
                if v.get("open") and v["open"] >= v["limit"] - 1e-9 and not v.get("picked")]
        if late:
            lines.append("Opened 2%+ up at the auction but was not picked at the pre-open check: " + ", ".join(late) + ".")
    elif part == "afternoon":
        p = det(get("plan")[0]) if get("plan") else {}
        if p:
            lines.append(f"Plan A account ₹{_inr(p.get('equity_A', 0))} → {p.get('lots')} lot(s) of {p.get('contract')} "
                         f"({_inr(p.get('lots', 0) * p.get('lot', 0))} shares); last price ₹{p.get('est_price')}.")
        q = det(get("buy_quote")[0]) if get("buy_quote") else {}
        asks = q.get("best_ask") or []
        if asks:
            lines.append(f"At 15:25:00 the best price to buy was ₹{asks[0][0]} ({_inr(asks[0][1])} available).")
        for lab, what in (("A-buy-limit", "Plan A limit buy"), ("A-buy", "Plan A market buy")):
            t = order_text(lab, what)
            if t:
                lines.append(t)
        fb = next((det(e) for e in get("fill") if det(e).get("leg") == "A-buy"), None)
        if fb:
            lines.append(f"Bought at ₹{fb.get('price')}" + (f" — {fb['how']}." if fb.get("how") else " (live price at that second)."))
    if result == "OK" and dry:
        result = "OK - orders logged only"
    elif result == "OK" and get("order_sent"):
        result = "OK - sandbox orders accepted"
    return {"day": js["day"], "session": part.title(), "result": result, "lines": lines, "raw": ev}


def write_summary():
    """docs/data/shadow.json for the dashboard's session log: one plain-language summary per session, rebuilt from ALL
    session files (so two runs can't clash). Raw events are kept for the 'details' view."""
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for g in sorted(OUT.glob("*.json"))[-30:]:
        try:
            rows.append(summarize(json.loads(g.read_text())))
        except Exception as e:
            rows.append({"day": g.stem[:10], "session": g.stem[11:], "result": "Could not summarise", "lines": [str(e)[:200]], "raw": []})
    (ROOT / "docs" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "data" / "shadow.json").write_text(json.dumps(rows, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    # the shadow bot only READS the paper accounts; it never saves state/meta.json (the paper bot owns it)


if __name__ == "__main__":
    main()
