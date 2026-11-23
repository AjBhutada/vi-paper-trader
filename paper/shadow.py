"""SHADOW live bot for the two Vodafone Idea plans. It does in real time exactly what a real trading bot would do,
but sends its orders to the Upstox SANDBOX (pretend orders, no money). Without a sandbox token it only logs them.

  morning  (start ~08:50 India): connect to the live feed; 09:00:05 send the pre-open orders
             A: sell last night's futures (market order, NRML)      - only if the paper bot holds a position
             B: limit SELL of VI shares at yesterday's last price x 1.01 (intraday)
           09:15 watch the auction price; if B's limit was reached, buy back at 09:16:00, else cancel the limit order.
  afternoon (start ~15:15 India): 15:25:00 buy the futures lots (market order, NRML).

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
EQ = S["equity_key"]
OUT = ROOT / "state" / "shadow"


def walk(levels, qty):
    """Average price for buying `qty` through the visible (price, qty) levels; beyond them, 1 paisa worse."""
    left, cost = qty, 0.0
    for p, q in levels or []:
        if not p:
            continue
        take = min(left, q); cost += take * p; left -= take
        if left <= 0:
            return round(cost / qty, 4)
    if not levels:
        return None
    return round((cost + left * (levels[-1][0] + 0.01)) / qty, 4)


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
        feeds = (m or {}).get("feeds") or {}
        now = time.time()
        with self.lock:
            for k, v in feeds.items():
                mff = (v.get("fullFeed") or {}).get("marketFF") or {}
                q = [x for x in (mff.get("marketLevel") or {}).get("bidAskQuote", []) if x]
                ohlc = [o for o in (mff.get("marketOHLC") or {}).get("ohlc", []) if o.get("interval") == "1d"]
                self.snap[k] = {"rx": now, "ltp": (mff.get("ltpc") or {}).get("ltp"), "cp": (mff.get("ltpc") or {}).get("cp"),
                                "bid": [(x.get("bidP"), int(x.get("bidQ", 0) or 0)) for x in q if x.get("bidP")],
                                "ask": [(x.get("askP"), int(x.get("askQ", 0) or 0)) for x in q if x.get("askP")],
                                "open": ohlc[0].get("open") if ohlc else None, "vtt": mff.get("vtt")}
                self.n += 1

    def get(self, k):
        with self.lock:
            return dict(self.snap.get(k) or {})


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
    ap.add_argument("part", choices=["morning", "afternoon", "check"])
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--at")
    a = ap.parse_args()
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
        feed = Feed(keys, log); feed.start()
        clock.wait_until("09:00:05")
        snaps = {k: feed.get(k) for k in keys}
        log("preopen_snapshot", snaps)
        a_id = orders.place("A-sell", pos["key"], "SELL", pos["qty"], "MARKET", "D") if pos else None
        b_id = orders.place("B-limit-short", EQ, "SELL", qty_b, "LIMIT", "I", limit)
        for hms in ("09:03:00", "09:05:00", "09:07:00", "09:07:50", "09:08:30", "09:12:00"):
            clock.wait_until(hms)
            log(f"preopen_{hms}", {k: {x: feed.get(k).get(x) for x in ("ltp", "open", "ask", "bid")} for k in keys})
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
        if o and o >= limit:
            log("fill", {"leg": "B-short", "price": o, "qty": qty_b, "limit": limit, "prev_last": last})
            clock.wait_until("09:16:00")
            s = feed.get(EQ)
            log("cover_quote", {"best_ask": s.get("ask", [None])[:3], "ltp": s.get("ltp")})
            orders.place("B-cover", EQ, "BUY", qty_b, "MARKET", "I")
            log("fill", {"leg": "B-cover", "price": walk(s.get("ask"), qty_b), "qty": qty_b,
                         "quote_age_s": round(time.time() - s["rx"], 2) if s.get("rx") else None})
        else:
            log("fill", {"leg": "B-none", "open": o, "limit": limit})
            orders.cancel("B-limit-short", b_id)
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
        clock.wait_until("15:24:55")
        s = feed.get(c["key"])
        est = (s.get("ask") or [(s.get("ltp"), 0)])[0][0] or s.get("ltp")
        eqA = m["equity"]["A"]
        lot = c["lot"]
        margin = S["fut_margin_frac"] * est * lot if est else None
        lots = 0
        if margin and eqA >= margin:
            lots = max(1, min(int(eqA // margin), round(eqA / ((S["fut_margin_frac"] + S["lot_cushion_frac"]) * est * lot))))
        log("plan", {"contract": c["symbol"], "lot": lot, "lots": lots, "equity_A": round(eqA), "est_price": est})
        clock.wait_until("15:25:00")
        s = feed.get(c["key"])
        log("buy_quote", {"best_ask": s.get("ask", [None])[:3], "ltp": s.get("ltp"), "age_s": round(time.time() - s["rx"], 2) if s.get("rx") else None})
        if lots:
            orders.place("A-buy", c["key"], "BUY", lots * lot, "MARKET", "D")
            log("fill", {"leg": "A-buy", "price": walk(s.get("ask"), lots * lot), "qty": lots * lot, "symbol": c["symbol"],
                         "key": c["key"], "quote_age_s": round(time.time() - s["rx"], 2) if s.get("rx") else None})
        time.sleep(2)
        feed.stop()
    log("feed_stats", {"messages": feed.n})
    finish(day, a.part, events, st)


def finish(day, part, events, st):
    f = OUT / f"{day}_{part}.json"
    f.write_text(json.dumps({"day": str(day), "part": part, "events": events}, indent=1, default=str))
    # short summary for the dashboard: the last 40 key events across days
    keep = ("plan", "auction", "cover_quote", "buy_quote", "fill", "order_sent", "order_dry", "order_error", "cancel_sent",
            "cancel_dry", "missed", "skip", "error", "feed_error", "note")
    rows = []
    for g in sorted(OUT.glob("*.json"))[-20:]:
        js = json.loads(g.read_text())
        rows += [{"day": js["day"], "part": js["part"], **e} for e in js["events"] if e["what"] in keep]
    (ROOT / "docs" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "data" / "shadow.json").write_text(json.dumps(rows[-60:], indent=1, default=str))
    # the shadow bot only READS the paper accounts; it never saves state/meta.json (the paper bot owns it)


if __name__ == "__main__":
    main()
