"""Vodafone Idea paper trader - two plans, Rs 5 lakh each, compounding. PAPER ONLY: no orders are ever sent.

Plan A  OVERNIGHT   : buy the VI future in one order at 15:25, sell at the next 09:15 pre-open auction price.
                      Roll to next month when fewer than 5 trading days are left.
Plan B  GAP-UP SHORT: a limit sell at yesterday's last price x 1.01 in the pre-open: fills only if the auction
                      price is that high, at the auction price. Short 5x the account, buy back at 09:16.

Auction prices = the 09:15 minute's open from Upstox's public 1-minute candles (exact: one price for everyone).
Buys outside the auction = the real best ask from the tick recorder's files (needs TICKS_KEY), walking the
visible depth; if the recorder has no ticks, the minute's open + half a paisa. Any run can work out any past
day: missed runs are caught up, and running twice changes nothing.

Usage:  python paper/vi_bot.py [--now "YYYY-MM-DD HH:MM"] [--state DIR] [--start YYYY-MM-DD] [--no-report]
        --now is a fake clock for tests; --state points at another state folder for replays.
"""
import argparse, gzip, http.cookiejar, io, json, math, os, subprocess, sys, tarfile, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
S = json.loads((ROOT / "paper" / "settings.json").read_text())
CACHE = ROOT / ".cache"


def log(*a):
    print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- state
class State:
    def __init__(self, folder):
        self.dir = Path(folder); self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_f = self.dir / "meta.json"
        self.meta = json.loads(self.meta_f.read_text()) if self.meta_f.exists() else {}
        self.trades_f, self.days_f = self.dir / "trades.csv", self.dir / "days.csv"

    def init(self, start):
        if not self.meta:
            self.meta = {"start_date": start, "equity": {"A": S["capital_overnight"], "B": S["capital_gapshort"]},
                         "last_morning": None, "last_evening": None, "open": None, "alerted": {}, "last_run": None}

    def save(self):
        self.meta_f.write_text(json.dumps(self.meta, indent=1))

    def add_trade(self, row):
        cols = ["plan", "date", "instrument", "side", "qty", "entry_time", "entry", "exit_date", "exit_time", "exit",
                "gross_rs", "charges_rs", "net_rs", "net_bps", "equity_after", "note"]
        new = not self.trades_f.exists()
        with open(self.trades_f, "a", encoding="utf-8") as h:
            if new:
                h.write(",".join(cols) + "\n")
            h.write(",".join(str(row.get(c, "")).replace(",", ";") for c in cols) + "\n")

    def add_day(self, row):
        cols = ["date", "equity_A", "equity_B", "total", "A_lots_overnight", "A_contract", "gap_pct", "B_traded", "note"]
        new = not self.days_f.exists()
        with open(self.days_f, "a", encoding="utf-8") as h:
            if new:
                h.write(",".join(cols) + "\n")
            h.write(",".join(str(row.get(c, "")).replace(",", ";") for c in cols) + "\n")

    def alert(self, key, text):
        """Once per key: the workflow turns alerts_pending.md into a GitHub issue that emails the owner."""
        if self.meta["alerted"].get(key):
            return
        self.meta["alerted"][key] = datetime.now(IST).isoformat(timespec="minutes")
        with open(self.dir / "alerts_pending.md", "a", encoding="utf-8") as h:
            h.write(f"- {datetime.now(IST):%d %b %Y %H:%M} IST: {text}\n")
        log("ALERT", text)


# ---------------------------------------------------------------- data
def http_get(url, tries=4, headers=None, opener=None):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
            return (opener or urllib.request.build_opener()).open(req, timeout=60).read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(2 + 3 * k)
        except Exception:
            time.sleep(2 + 3 * k)
    raise RuntimeError(f"could not download {url}")


def candles(key, day, today=None):
    """1-minute candles for one instrument and day as {HH:MM: (o,h,l,c,v)}. Past days are cached.
    Live (intraday) candles are used only for the REAL current India date, so a fake-clock replay reads history."""
    real_today = datetime.now(IST).date()
    f = CACHE / "candles" / f"{key.replace('|', '_')}_{day}.json"
    if day < real_today and f.exists():
        return {k: tuple(v) for k, v in json.loads(f.read_text()).items()}
    q = urllib.parse.quote(key, safe="")
    url = (f"https://api.upstox.com/v3/historical-candle/intraday/{q}/minutes/1" if day == real_today else
           f"https://api.upstox.com/v3/historical-candle/{q}/minutes/1/{day}/{day}")
    raw = http_get(url)
    rows = json.loads(raw)["data"]["candles"] if raw else []
    out = {}
    for t, o, h, l, c, v, *_ in rows:
        if t[:10] == str(day):
            out[t[11:16]] = (o, h, l, c, v)
    out = dict(sorted(out.items()))
    if day < real_today and out:
        f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(out))
    return out


def contracts(st):
    """VI futures (key, symbol, expiry, lot). Kept in the state so contracts stay known after they expire."""
    known = st.meta.setdefault("contracts", {})
    stamp = st.meta.get("contracts_fetched")
    if not stamp or stamp < str(datetime.now(IST).date()):
        try:
            raw = http_get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz")
            for x in json.load(gzip.open(io.BytesIO(raw))):
                if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT" and x.get("underlying_symbol") == S["underlying"]:
                    exp = datetime.fromtimestamp(x["expiry"] / 1000, IST).date().isoformat()
                    known[x["instrument_key"]] = {"symbol": x["trading_symbol"], "expiry": exp, "lot": x["lot_size"]}
            st.meta["contracts_fetched"] = str(datetime.now(IST).date())
        except Exception as e:
            log("instrument list not refreshed:", e)
    return sorted(({"key": k, **v} for k, v in known.items()), key=lambda x: x["expiry"])


def holidays(st):
    """NSE trading holidays, refreshed weekly (NSE needs a cookie from its home page first). Kept in the state."""
    h = st.meta.get("holidays", {})
    if not h.get("fetched") or (date.today() - date.fromisoformat(h["fetched"])).days >= 7:
        try:
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            hd = {"Referer": "https://www.nseindia.com/", "Accept-Language": "en-US,en;q=0.9"}
            http_get("https://www.nseindia.com/", tries=2, headers=hd, opener=op)
            js = json.loads(http_get("https://www.nseindia.com/api/holiday-master?type=trading", tries=3, headers=hd, opener=op))
            days = sorted({datetime.strptime(x["tradingDate"], "%d-%b-%Y").date().isoformat() for x in js.get("FO", js.get("CM", []))})
            h = {"fetched": str(date.today()), "days": days}
            st.meta["holidays"] = h
        except Exception as e:
            log("NSE holiday list not refreshed:", e)
    return set(h.get("days", []))


# ---------------------------------------------------------------- ticks from the recorder
def tick_assets(day):
    """The tick recorder's encrypted files for a day (GitHub release named YYYYMMDD): [(name, url)]."""
    hdr = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    raw = http_get(f"https://api.github.com/repos/{S['ticks']['recorder_repo']}/releases/tags/{day:%Y%m%d}", headers=hdr)
    if not raw:
        return []
    return sorted((a["name"], a["browser_download_url"]) for a in json.loads(raw).get("assets", []) if a["name"].endswith(".tgz.enc"))


def load_tick_asset(name, url, keys):
    """Download + decrypt one recorder file and keep only VI lines (shares + futures). Cached."""
    f = CACHE / "ticks" / (name.replace(".tgz.enc", "") + "_vi.jsonl")
    if f.exists():
        return [json.loads(l) for l in f.open(encoding="utf-8")]
    enc = None
    for k in range(3):
        try:
            enc = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=600).read(); break
        except Exception:
            time.sleep(5 + 5 * k)
    if enc is None:
        raise RuntimeError(f"could not download {name}")
    dec = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "env:TICKS_KEY"], input=enc,
                         capture_output=True, check=True, env=dict(os.environ)).stdout
    needles = [k.split("|")[1] for k in keys]
    out = []
    with tarfile.open(fileobj=io.BytesIO(dec), mode="r|gz") as tf:
        for mem in tf:
            if not mem.name.endswith("ticks.jsonl"):
                continue
            for raw in tf.extractfile(mem):
                line = raw.decode("utf-8", "ignore")
                if not any(n in line for n in needles):
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                for k, v in ((j.get("msg") or {}).get("feeds") or {}).items():
                    if k not in keys:
                        continue
                    mff = (v.get("fullFeed") or {}).get("marketFF") or {}
                    q = [x for x in (mff.get("marketLevel") or {}).get("bidAskQuote", []) if x]
                    asks = [(x["askP"], int(x.get("askQ", 0) or 0)) for x in q if x.get("askP")]
                    bids = [(x["bidP"], int(x.get("bidQ", 0) or 0)) for x in q if x.get("bidP")]
                    out.append({"rx": j["rx"], "key": k, "asks": asks, "bids": bids, "ltp": (mff.get("ltpc") or {}).get("ltp")})
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("w", encoding="utf-8") as h:
        for r in out:
            h.write(json.dumps(r) + "\n")
    log(f"ticks: {name} -> {len(out):,} VI snapshots")
    return out


# ---------------------------------------------------------------- money
def charges(kind, side, value):
    brk = S["brokerage_per_order"]
    if kind == "fut":
        stt = S["stt_fut_sell"] * value if side == "sell" else 0
        txn = S["txn_fut"] * value
        stamp = S["stamp_fut_buy"] * value if side == "buy" else 0
    else:                                       # cash intraday (MIS)
        stt = S["stt_intraday_sell"] * value if side == "sell" else 0
        txn = S["txn_equity"] * value
        stamp = S["stamp_intraday_buy"] * value if side == "buy" else 0
    return brk + stt + txn + stamp + S["sebi"] * value + S["gst"] * (brk + txn)


def vwap(cs, a, b):
    rows = [v for k, v in cs.items() if a <= k <= b and v[4] > 0]
    vol = sum(r[4] for r in rows)
    return sum((r[1] + r[2] + r[3]) / 3 * r[4] for r in rows) / vol if vol else None


# ---------------------------------------------------------------- the two plans
class Bot:
    def __init__(self, st, now):
        self.st, self.now = st, now
        self.today = now.date()
        self.hol = holidays(st)
        self.cons = contracts(st)
        self.slip = S["slip_paise"] / 100
        self.ticks_on = bool(os.environ.get("TICKS_KEY"))
        if not self.ticks_on:
            log("TICKS_KEY not set: fills outside the auction use minute candles")
        self._ticks = {}

    # ---- tick-exact fills
    def ticks(self, d):
        """All VI snapshots (shares + futures) the recorder has for day d so far."""
        if not self.ticks_on:
            return []
        if d in self._ticks:
            return self._ticks[d]
        keys = {S["equity_key"]} | {c["key"] for c in self.cons}
        snaps = []
        try:
            for name, url in tick_assets(d):
                snaps += load_tick_asset(name, url, keys)
        except Exception as e:
            log("ticks not available:", e)
        snaps.sort(key=lambda s: s["rx"])
        self._ticks[d] = snaps
        return snaps

    def tick_buy(self, d, key, hm, qty):
        """Buy `qty` at the best asks of the first snapshot at/after HH:MM:00 (within max_gap_seconds),
        walking the visible depth. Returns (avg price, time, note) or None if the recorder has no such snapshot."""
        t0 = datetime.combine(d, datetime.strptime(hm, "%H:%M").time(), IST).timestamp()
        cand = [s for s in self.ticks(d) if s["key"] == key and t0 <= s["rx"] <= t0 + S["ticks"]["max_gap_seconds"] and s["asks"]]
        if not cand:
            return None
        s = cand[0]
        left, cost = qty, 0.0
        for p, q in s["asks"]:
            take = min(left, q); cost += take * p; left -= take
            if left <= 0:
                break
        note = ""
        if left > 0:
            cost += left * (s["asks"][-1][0] + 0.01)
            note = "; part beyond the visible depth priced 1 paisa worse"
        when = datetime.fromtimestamp(s["rx"], IST).strftime("%H:%M:%S")
        return cost / qty, when, f"tick best ask at {when} (ask {s['asks'][0][0]} x {s['asks'][0][1]:,}){note}"

    def waiting(self, part):
        """True while it is still worth waiting for today's recorder file."""
        until = datetime.strptime(S["ticks"]["wait_for_ticks_until"][part], "%H:%M").time()
        return self.ticks_on and self.today == datetime.now(IST).date() and self.now.time() < until

    def trading(self, d):
        return d.weekday() < 5 and d.isoformat() not in self.hol

    def next_tdays(self, d, until):
        n, x = 0, d
        while x < until:
            x += timedelta(days=1)
            n += self.trading(x)
        return n

    def prev_tday(self, d):
        x = d - timedelta(days=1)
        while not self.trading(x) or not candles(S["equity_key"], x, self.today):
            x -= timedelta(days=1)
            if (d - x).days > 15:
                return None
        return x

    def contract_for(self, d):
        live = [c for c in self.cons if date.fromisoformat(c["expiry"]) >= d]
        if not live:
            return None
        c = live[0]
        if self.next_tdays(d, date.fromisoformat(c["expiry"])) < S["roll_trading_days"] and len(live) > 1:
            c = live[1]
        return c

    # ---- morning: close the overnight position, then the gap-up short
    def morning(self, d):
        st, m = self.st, self.st.meta
        eq = candles(S["equity_key"], d, self.today)
        o_min, c_min = S["short_entry_minute"], S["short_cover_minute"]
        if o_min not in eq or c_min not in eq:
            return False
        # Plan B decision first (it may have to wait for the recorder's ticks before anything is booked)
        pd_ = self.prev_tday(d)
        prev = candles(S["equity_key"], pd_, self.today) if pd_ else {}
        b = None
        if prev:
            pc = prev[max(prev)][3]
            o = eq[o_min][0]
            gap = (o / pc - 1) * 100
            if gap >= S["gap_threshold_pct"]:
                notional = min(S["short_leverage"] * m["equity"]["B"], S["short_cap_rs"])
                qty = int(notional // o)
                tb = self.tick_buy(d, S["equity_key"], c_min, qty)
                if tb is None and self.waiting("morning"):
                    log("B: gap-up day, waiting for the recorder's 09:16 ticks")
                    return False
                if tb:
                    x, when, how = tb
                else:
                    x, how = eq[c_min][0] + self.slip, f"no ticks: {c_min} minute open + {S['slip_paise']} paisa"
                b = dict(pc=pc, o=o, gap=gap, qty=qty, x=x, how=how)
        pos = m["open"]
        if pos and pos["date"] < str(d):
            fc = candles(pos["key"], d, self.today)
            first = min(fc) if fc else None
            if not first:
                if self.now.hour >= 10:
                    st.alert(f"noexit-{d}", f"No {pos['symbol']} candles on {d} to close the overnight position; will retry.")
                return False
            note = "auction open" if first == S["overnight_exit_minute"] else f"no 09:15 print, used first trade at {first}"
            px = fc[first][0]
            qty = pos["qty"]
            gross = (px - pos["entry"]) * qty
            ch = charges("fut", "buy", pos["entry"] * qty) + charges("fut", "sell", px * qty)
            net = gross - ch
            m["equity"]["A"] += net
            st.add_trade(dict(plan="A overnight", date=pos["date"], instrument=pos["symbol"], side="long", qty=qty,
                              entry_time=pos.get("entry_time", ""), entry=round(pos["entry"], 4), exit_date=str(d), exit_time=first,
                              exit=px, gross_rs=round(gross), charges_rs=round(ch), net_rs=round(net),
                              net_bps=round(net / (pos["entry"] * qty) * 1e4, 1), equity_after=round(m["equity"]["A"]),
                              note=f"bought: {pos.get('how', '')}; sold: {note}"))
            log(f"A: sold {qty:,} {pos['symbol']} at {px} (bought {pos['entry']:.4f}) net Rs {net:,.0f}")
            m["open"] = None
        # gap-up short (decided above)
        if not prev:
            st.alert(f"noprev-{d}", f"Could not find VI's previous close before {d}; gap-up check skipped.")
            m["last_morning"] = str(d)
            m["_gap"] = None
            return True
        pc, o = prev[max(prev)][3], eq[o_min][0]
        gap = (o / pc - 1) * 100
        m["_gap"] = round(gap, 2)
        m["_btrade"] = False
        if b:
            qty, x = b["qty"], b["x"]
            gross = (o - x) * qty
            ch = charges("cash", "sell", o * qty) + charges("cash", "buy", x * qty)
            net = gross - ch
            m["equity"]["B"] += net
            m["_btrade"] = True
            st.add_trade(dict(plan="B gap-up short", date=str(d), instrument="IDEA shares (MIS)", side="short", qty=qty,
                              entry_time=o_min, entry=o, exit_date=str(d), exit_time=c_min, exit=round(x, 4),
                              gross_rs=round(gross), charges_rs=round(ch), net_rs=round(net),
                              net_bps=round(net / (o * qty) * 1e4, 1), equity_after=round(m["equity"]["B"]),
                              note=f"gap {gap:+.2f}% (prev {pc}; limit {pc * (1 + S['gap_threshold_pct'] / 100):.2f}; auction {o}); covered: {b['how']}"))
            log(f"B: gap {gap:+.2f}% -> shorted {qty:,} at {o}, covered {x:.4f} ({b['how']}), net Rs {net:,.0f}")
        else:
            log(f"B: gap {gap:+.2f}% below {S['gap_threshold_pct']}% -> no trade")
        m["last_morning"] = str(d)
        return True

    # ---- evening: open the next overnight position
    def evening(self, d):
        st, m = self.st, self.st.meta
        c = self.contract_for(d)
        if not c:
            st.alert(f"nocontract-{d}", "No VI futures contract found in Upstox's instrument list.")
            return False
        fc = candles(c["key"], d, self.today)
        hm = S["overnight_entry_minute"]
        if hm not in fc:
            if self.now.time() >= datetime.strptime("16:30", "%H:%M").time() or d < self.today:
                st.alert(f"noentry-{d}", f"No {c['symbol']} candle at {hm} on {d}; overnight entry skipped.")
                return self._end_day(d, None)
            return False
        est = fc[hm][0]
        lot = c["lot"]
        eqA = m["equity"]["A"]
        lot_margin = S["fut_margin_frac"] * est * lot
        lots = 0
        if eqA >= lot_margin:
            lots = max(1, min(int(eqA // lot_margin), round(eqA / ((S["fut_margin_frac"] + S["lot_cushion_frac"]) * est * lot))))
        else:
            st.alert(f"lowcap-{d}", f"Plan A account Rs {eqA:,.0f} is below one lot's margin (Rs {lot_margin:,.0f}); no overnight position.")
        if lots:
            tb = self.tick_buy(d, c["key"], hm, lots * lot)
            if tb is None and self.waiting("evening"):
                log(f"A: waiting for the recorder's {hm} ticks")
                return False
            if tb:
                px, when, how = tb
            else:
                px, when, how = est + self.slip, hm, f"no ticks: {hm} minute open + {S['slip_paise']} paisa"
            m["open"] = {"date": str(d), "key": c["key"], "symbol": c["symbol"], "lot": lot, "lots": lots,
                         "qty": lots * lot, "entry": px, "entry_time": when, "how": how}
            log(f"A: bought {lots} lot(s) = {lots * lot:,} {c['symbol']} at {px:.4f} ({how})")
        return self._end_day(d, c if lots else None, lots)

    def _end_day(self, d, c, lots=0):
        m = self.st.meta
        self.st.add_day(dict(date=str(d), equity_A=round(m["equity"]["A"]), equity_B=round(m["equity"]["B"]),
                             total=round(m["equity"]["A"] + m["equity"]["B"]), A_lots_overnight=lots,
                             A_contract=c["symbol"] if c else "", gap_pct=m.get("_gap"), B_traded=m.get("_btrade", False)))
        m["last_evening"] = str(d)
        return True

    def run(self):
        m = self.st.meta
        d = date.fromisoformat(m["start_date"])
        last_m = date.fromisoformat(m["last_morning"]) if m["last_morning"] else d - timedelta(days=1)
        last_e = date.fromisoformat(m["last_evening"]) if m["last_evening"] else d - timedelta(days=1)
        d = min(last_m, last_e) + timedelta(days=1)
        t = self.now.time()
        while d <= self.today:
            if not self.trading(d):
                d += timedelta(days=1); continue
            is_today = d == self.today
            if is_today and t < datetime.strptime("09:17", "%H:%M").time():
                break
            if d > last_m:
                if not candles(S["equity_key"], d, self.today):
                    if is_today:
                        if t >= datetime.strptime("10:30", "%H:%M").time():
                            self.st.alert(f"nodata-{d}", f"No VI candles from Upstox on {d} by {t:%H:%M}. Holiday or data problem?")
                        break
                    log(f"{d}: no candles (market closed?) - skipped"); d += timedelta(days=1); continue
                if not self.morning(d):
                    break
                last_m = d
            if d > last_e:
                if is_today and t < datetime.strptime("15:31", "%H:%M").time():
                    break
                if not self.evening(d):
                    break
                last_e = d
            d += timedelta(days=1)
        m["last_run"] = self.now.isoformat(timespec="minutes")


# ---------------------------------------------------------------- report
def report(st, now):
    import csv
    m = st.meta
    trades = list(csv.DictReader(open(st.trades_f, encoding="utf-8"))) if st.trades_f.exists() else []
    days = list(csv.DictReader(open(st.days_f, encoding="utf-8"))) if st.days_f.exists() else []

    def summary(plan, start):
        t = [x for x in trades if x["plan"].startswith(plan)]
        nets = [float(x["net_rs"]) for x in t]
        eq = [start] + [float(x["equity_after"]) for x in t]
        peak, dd = start, 0.0
        for e in eq:
            peak = max(peak, e); dd = min(dd, e / peak - 1)
        bps = sorted(float(x["net_bps"]) for x in t)
        return {"start": start, "now": round(m["equity"][plan]), "pnl": round(m["equity"][plan] - start),
                "ret_pct": round((m["equity"][plan] / start - 1) * 100, 2), "trades": len(t),
                "wins": sum(n > 0 for n in nets), "win_pct": round(100 * sum(n > 0 for n in nets) / len(t), 1) if t else None,
                "avg_net_rs": round(sum(nets) / len(nets)) if nets else None,
                "avg_net_bps": round(sum(bps) / len(bps), 1) if bps else None,
                "median_net_bps": bps[len(bps) // 2] if bps else None,
                "best_rs": round(max(nets)) if nets else None, "worst_rs": round(min(nets)) if nets else None,
                "max_dip_pct": round(dd * 100, 2)}
    A, B = summary("A", S["capital_overnight"]), summary("B", S["capital_gapshort"])
    tot0 = S["capital_overnight"] + S["capital_gapshort"]
    tot = m["equity"]["A"] + m["equity"]["B"]
    dash = {"updated": now.strftime("%d %b %Y %H:%M IST"), "start_date": m["start_date"], "plans": {"A": A, "B": B},
            "combined": {"start": tot0, "now": round(tot), "pnl": round(tot - tot0), "ret_pct": round((tot / tot0 - 1) * 100, 2)},
            "open": m["open"], "days": days, "trades": trades[-200:], "reference": S["backtest_reference"],
            "last_gap_pct": m.get("_gap"), "rules": {k: v for k, v in S.items() if not k.startswith("_") and k != "backtest_reference"}}
    (ROOT / "docs" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "data" / "dashboard.json").write_text(json.dumps(dash, indent=1))
    pos = m["open"]
    lines = ["# Vodafone Idea paper trader", "",
             "Two plans on Vodafone Idea (VI), Rs 5 lakh each, compounding. **Paper trading only: no real orders.**",
             f"Dashboard: https://ajbhutada.github.io/vi-paper-trader/  ·  updated {dash['updated']}", "",
             "| Plan | Start | Now | P&L | Return | Trades | Win rate | Avg / trade | Worst trade | Deepest dip |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, x in (("A Overnight futures", A), ("B Gap-up auction short", B)):
        lines.append(f"| {name} | Rs {x['start']:,} | Rs {x['now']:,} | Rs {x['pnl']:+,} | {x['ret_pct']:+.2f}% | {x['trades']} | "
                     f"{x['win_pct'] if x['win_pct'] is not None else '-'}% | "
                     f"{'Rs %+,d' % x['avg_net_rs'] if x['avg_net_rs'] is not None else '-'} | "
                     f"{'Rs %+,d' % x['worst_rs'] if x['worst_rs'] is not None else '-'} | {x['max_dip_pct']}% |")
    c = dash["combined"]
    lines += [f"| **Both** | Rs {c['start']:,} | Rs {c['now']:,} | Rs {c['pnl']:+,} | {c['ret_pct']:+.2f}% | | | | | |", "",
              f"Open overnight position: {pos['lots']} lot(s) = {pos['qty']:,} {pos['symbol']} bought at {pos['entry']:.4f} on {pos['date']}"
              if pos else "Open overnight position: none", "",
              "Rules: `paper/settings.json`. Results: `state/trades.csv`, `state/days.csv`.", "",
              "*Research on past data / paper trading only, not investment advice.*"]
    (ROOT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dash


def weekly(st, now, dash):
    """On the week's last processed trading day (Friday evening or later), write a report the workflow emails."""
    m = st.meta
    if not m["last_evening"]:
        return
    d = date.fromisoformat(m["last_evening"])
    wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
    if d.weekday() != 4 or m.get("last_weekly") == wk:
        return
    A, B, c = dash["plans"]["A"], dash["plans"]["B"], dash["combined"]
    week_tr = [x for x in dash["trades"] if (d - date.fromisoformat(x["exit_date"])).days < 7]
    body = [f"@AjBhutada weekly report for the VI paper trader, week ending {d:%d %b %Y}.", "",
            "| Plan | Now | P&L since start | This week | Trades this week |", "|---|---|---|---|---|"]
    for name, p, x in (("A Overnight", "A", A), ("B Gap-up short", "B", B)):
        wk_pnl = sum(float(t["net_rs"]) for t in week_tr if t["plan"].startswith(p))
        body.append(f"| {name} | Rs {x['now']:,} | Rs {x['pnl']:+,} ({x['ret_pct']:+.2f}%) | Rs {wk_pnl:+,.0f} | "
                    f"{sum(t['plan'].startswith(p) for t in week_tr)} |")
    body += [f"| **Both** | Rs {c['now']:,} | Rs {c['pnl']:+,} ({c['ret_pct']:+.2f}%) | | |", "",
             "Dashboard: https://ajbhutada.github.io/vi-paper-trader/", "",
             "*Research on past data / paper trading only, not investment advice.*"]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "weekly_pending.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    m["last_weekly"] = wk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now"); ap.add_argument("--state", default=str(ROOT / "state"))
    ap.add_argument("--start"); ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args()
    now = datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=IST) if a.now else datetime.now(IST)
    if not a.now and now.time() < datetime.strptime("09:17", "%H:%M").time() and now.time() > datetime.strptime("09:00", "%H:%M").time():
        wait = (datetime.combine(now.date(), datetime.strptime("09:17:40", "%H:%M:%S").time(), IST) - now).total_seconds()
        log(f"waiting {wait:.0f} s for the 09:16 candle"); time.sleep(max(0, wait)); now = datetime.now(IST)
    if not a.now and datetime.strptime("15:29", "%H:%M").time() <= now.time() < datetime.strptime("15:31", "%H:%M").time():
        wait = (datetime.combine(now.date(), datetime.strptime("15:31:10", "%H:%M:%S").time(), IST) - now).total_seconds()
        log(f"waiting {wait:.0f} s for the 15:29 candle"); time.sleep(max(0, wait)); now = datetime.now(IST)
    st = State(a.state)
    st.init(a.start or S["start_date"])
    log(f"run at {now:%d %b %Y %H:%M} IST; last morning {st.meta['last_morning']}, last evening {st.meta['last_evening']}")
    Bot(st, now).run()
    st.save()
    if not a.no_report:
        dash = report(st, now)
        weekly(st, now, dash)
        st.save()
    log(f"done. A Rs {st.meta['equity']['A']:,.0f}  B Rs {st.meta['equity']['B']:,.0f}  open: {bool(st.meta['open'])}")


if __name__ == "__main__":
    main()
