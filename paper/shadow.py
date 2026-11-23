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
            t = order_text("B-cover", "Plan B buy-back")
            if t:
                lines.append(t)
            lines.append(f"Bought back at ₹{fills['B-cover'].get('price')} (best price to buy at 09:16:00).")
        if any(e["what"] in ("cancel_sent", "cancel_dry") for e in ev):
            lines.append("Unfilled limit short cancelled" + (" (logged only)." if get("cancel_dry") else " in the sandbox."))
    elif part == "afternoon":
        p = det(get("plan")[0]) if get("plan") else {}
        if p:
            lines.append(f"Plan A account ₹{_inr(p.get('equity_A', 0))} → {p.get('lots')} lot(s) of {p.get('contract')} "
                         f"({_inr(p.get('lots', 0) * p.get('lot', 0))} shares).")
        q = det(get("buy_quote")[0]) if get("buy_quote") else {}
        asks = q.get("best_ask") or []
        if asks:
            lines.append(f"At 15:25:00 the best price to buy was ₹{asks[0][0]} ({_inr(asks[0][1])} available).")
        t = order_text("A-buy", "Plan A buy")
        if t:
            lines.append(t)
        fb = next((det(e) for e in get("fill") if det(e).get("leg") == "A-buy"), None)
        if fb:
            lines.append(f"Bought at ₹{fb.get('price')} (live price at that second).")
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
