"""Vodafone Idea paper trader - three plans, Rs 5 lakh each, compounding. PAPER ONLY: no orders are ever sent.

Plan A  OVERNIGHT   : buy the VI future in one order at 15:25, sell at the next 09:15 pre-open auction price.
                      Roll to next month when fewer than 5 trading days are left.
Plan B  GAP-UP SHORT: a limit sell at yesterday's last price x 1.01 in the pre-open: fills only if the auction
                      price is that high, at the auction price. Short 5x the account, buy back at 09:16.
Plan C  GAP-UP BASKET: the same idea on 13 stocks (settings "plan_c"): limit sell at yesterday's last price x 1.02
                      in the pre-open; up to 3 stocks a morning in the list's priority order, each sized to the
                      stock's opening liquidity and at most 2x the account (2:2:1 of the 5x); buy back at 09:20,
                      or at once if the price trades 5% above the short first (disaster stop, from 09:15).

Auction prices = the 09:15 minute's open from Upstox's public 1-minute candles (exact: one price for everyone).
Buys outside the auction = the real best ask from the tick recorder's files (needs TICKS_KEY), walking the
visible depth; if the recorder has no ticks, the minute's open + half a paisa. Any run can work out any past
day: missed runs are caught up, and running twice changes nothing.

Usage:  python paper/vi_bot.py [--now "YYYY-MM-DD HH:MM"] [--state DIR] [--start YYYY-MM-DD] [--no-report]
        --now is a fake clock for tests; --state points at another state folder for replays.
"""
import argparse, csv, gzip, hashlib, http.cookiejar, io, json, math, os, statistics, subprocess, sys, tarfile, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
S = json.loads((ROOT / "paper" / "settings.json").read_text())
C_ = S["plan_c"]
CACHE = ROOT / ".cache"
DAY_COLS = ["date", "equity_A", "equity_B", "equity_C", "total", "A_lots_overnight", "A_contract", "gap_pct", "B_traded",
            "C_trades", "note"]


def log(*a):
    print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- state
class State:
    def __init__(self, folder):
        self.dir = Path(folder); self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_f = self.dir / "meta.json"
        self.meta = json.loads(self.meta_f.read_text()) if self.meta_f.exists() else {}
        self.trades_f, self.days_f = self.dir / "trades.csv", self.dir / "days.csv"

    def init(self, start, start_c=None):
        if not self.meta:
            self.meta = {"start_date": start, "equity": {"A": S["capital_overnight"], "B": S["capital_gapshort"]},
                         "last_morning": None, "last_evening": None, "open": None, "alerted": {}, "last_run": None}
        self.upgrade(start_c)

    def upgrade(self, start_c=None):
        """States made before Plan C get its account, start date and an equity_C column in days.csv."""
        m = self.meta
        m["equity"].setdefault("C", C_["capital"])
        m.setdefault("last_c", None)
        m.setdefault("start_c", start_c or C_["start_date"])
        if self.days_f.exists():
            rows = list(csv.DictReader(open(self.days_f, encoding="utf-8")))
            if rows and "equity_C" not in rows[0]:
                with open(self.days_f, "w", encoding="utf-8") as h:
                    h.write(",".join(DAY_COLS) + "\n")
                    for r in rows:
                        r["equity_C"] = C_["capital"]
                        r["total"] = round(float(r["equity_A"]) + float(r["equity_B"]) + C_["capital"])
                        h.write(",".join(str(r.get(c, "")) for c in DAY_COLS) + "\n")
                log("days.csv upgraded with the Plan C column")

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
        cols = DAY_COLS
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
    eqk = st.meta.setdefault("eq_keys", {})
    stamp = st.meta.get("contracts_fetched")
    if not stamp or stamp < str(datetime.now(IST).date()) or any(s not in eqk for s in C_["stocks"]):
        try:
            raw = http_get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz")
            for x in json.load(gzip.open(io.BytesIO(raw))):
                if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT" and x.get("underlying_symbol") == S["underlying"]:
                    exp = datetime.fromtimestamp(x["expiry"] / 1000, IST).date().isoformat()
                    known[x["instrument_key"]] = {"symbol": x["trading_symbol"], "expiry": exp, "lot": x["lot_size"]}
                if (x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
                        and x.get("trading_symbol") in C_["stocks"]):          # Plan C shares
                    eqk[x["trading_symbol"]] = x["instrument_key"]
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
    """Download + decrypt one recorder file and keep only the lines of `keys` (VI shares + futures, Plan C shares).
    Cached per key set, so adding stocks never reuses a cache that lacks them."""
    tag = hashlib.md5((",".join(sorted(keys)) + "|v2").encode()).hexdigest()[:8]      # v2: also keeps the pre-open iep/ieq
    f = CACHE / "ticks" / (name.replace(".tgz.enc", "") + f"_{tag}.jsonl")
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
                    lt = mff.get("ltpc") or {}
                    out.append({"rx": j["rx"], "key": k, "asks": asks, "bids": bids, "ltp": lt.get("ltp"),
                                "iep": mff.get("iep") or lt.get("iep"), "ieq": mff.get("ieq")})
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("w", encoding="utf-8") as h:
        for r in out:
            h.write(json.dumps(r) + "\n")
    log(f"ticks: {name} -> {len(out):,} snapshots")
    return out


# ---------------------------------------------------------------- Plan C helpers
def tick_size(p):
    """NSE price step by price band (stocks below Rs 250 move in 1 paisa since 10 Jun 2024)."""
    return 0.01 if p < 250 else 0.05 if p < 1000 else 0.10 if p < 5000 else 0.5 if p < 10000 else 1.0


def opening_value_median(key, d, n=20):
    """Median traded value (Rs) of the 09:15 minute over the n trading days before d (None if fewer than 5).
    This is how much a stock trades at its opening auction - Plan C never takes more than 5% of it. Cached."""
    f = CACHE / "openvalue" / f"{key.replace('|', '_')}_{d}.json"
    if f.exists():
        return json.loads(f.read_text())["v"]
    q = urllib.parse.quote(key, safe="")
    vals, b = {}, d - timedelta(days=1)
    for _ in range(3):                                   # 1-minute history comes in up-to-1-month pieces
        a = b - timedelta(days=28)
        raw = http_get(f"https://api.upstox.com/v3/historical-candle/{q}/minutes/1/{b}/{a}")
        for t, o, h, l, c, v, *_ in (json.loads(raw)["data"]["candles"] if raw else []):
            if t[11:16] == "09:15" and v:
                vals[t[:10]] = o * v
        if len(vals) >= n:
            break
        b = a - timedelta(days=1)
    days = sorted(vals)[-n:]
    if len(days) < 5:
        return None
    med = statistics.median(vals[x] for x in days)
    f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps({"v": med, "days": len(days)}))
    return med


def preopen_cap(iep, ieq):
    """Most a Plan C short may be: preopen_frac (10%) of the stock's pre-open auction value at the pick
    (indicative price x indicative matched quantity). None = no pre-open record, so no cap can be set."""
    try:
        v = float(iep) * float(ieq)
    except (TypeError, ValueError):
        return None
    return C_.get("preopen_frac", 1.0) * v


def c_size_why(liq, per, budget, pre=None):
    """Which limit set a Plan C position's size (plain words for the trade note)."""
    low = min(liq, C_["cap_rs"], budget, per, pre if pre is not None else float("inf"))
    return (f"{C_.get('preopen_frac', 1.0):.0%} of its pre-open auction at {C_.get('live_pick_at', '09:06:30')}"
            if pre is not None and pre <= low else
            "5% of its usual opening-minute value" if liq <= low else
            f"{C_.get('max_position_x', C_['leverage']):g}x the account (2:2:1 rule)" if per <= low else
            "the Rs 50 lakh cap" if C_["cap_rs"] <= low else "what was left of 5x the account")


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
        self.eqk = st.meta.get("eq_keys", {})
        self.ticks_on = bool(os.environ.get("TICKS_KEY"))
        if not self.ticks_on:
            log("TICKS_KEY not set: fills outside the auction use minute candles")
        self._ticks = {}

    # ---- tick-exact fills
    def ticks(self, d):
        """All snapshots (VI shares + futures, Plan C shares) the recorder has for day d so far."""
        if not self.ticks_on:
            return []
        if d in self._ticks:
            return self._ticks[d]
        keys = {S["equity_key"]} | {c["key"] for c in self.cons} | set(self.eqk.values())
        snaps = []
        try:
            for name, url in tick_assets(d):
                snaps += load_tick_asset(name, url, keys)
        except Exception as e:
            log("ticks not available:", e)
        snaps.sort(key=lambda s: s["rx"])
        self._ticks[d] = snaps
        return snaps

    def preopen_at(self, d, key, hms):
        """(indicative price, indicative quantity) from the last recorded snapshot at or before HH:MM:SS in the pre-open
        (from 09:00), or None if the recorder has no pre-open for that day."""
        t0 = datetime.combine(d, datetime.strptime("09:00:00", "%H:%M:%S").time(), IST).timestamp()
        t1 = datetime.combine(d, datetime.strptime(hms, "%H:%M:%S").time(), IST).timestamp()
        last = None
        for s in self.ticks(d):
            if s["rx"] > t1:
                break
            if s["key"] == key and s["rx"] >= t0 and s.get("iep"):
                last = (s["iep"], s.get("ieq"))
        return last

    def tick_buy(self, d, key, hm, qty):
        """Buy `qty` at the best asks of the first snapshot at/after HH:MM:00 (within max_gap_seconds),
        walking the visible depth. Returns (avg price, time, note) or None if the recorder has no such snapshot."""
        t0 = datetime.combine(d, datetime.strptime(hm, "%H:%M").time(), IST).timestamp()
        cand = [s for s in self.ticks(d) if s["key"] == key and t0 <= s["rx"] <= t0 + S["ticks"]["max_gap_seconds"] and s["asks"]]
        if not cand:
            return None
        return self.buy_at_snapshot(cand[0], qty)

    def buy_at_snapshot(self, s, qty):
        """Market buy of `qty` against one recorded snapshot's asks (walking the visible depth)."""
        left, cost = qty, 0.0
        for p, q in s["asks"]:
            take = min(left, q); cost += take * p; left -= take
            if left <= 0:
                break
        note = ""
        if left > 0:
            step = tick_size(s["asks"][-1][0])
            cost += left * (s["asks"][-1][0] + step)
            note = f"; {left:,} beyond the visible depth priced one step ({step:g}) worse"
        when = datetime.fromtimestamp(s["rx"], IST).strftime("%H:%M:%S")
        return cost / qty, when, f"tick best ask at {when} (ask {s['asks'][0][0]} x {s['asks'][0][1]:,}){note}"

    def c_stop_check(self, d, x, cov):
        """Plan C disaster stop (settings plan_c.stop_pct): if the stock trades at the short price x (1 + stop%) or higher
        after the 09:15 auction and before the buy-back time, buy back at that moment. Stop-loss orders are not allowed in
        the pre-open, so the stop only works from 09:15. Returns (price, time, how), or None if the stop was not reached."""
        sp = C_.get("stop_pct")
        if not sp:
            return None
        stop = x["o"] * (1 + sp / 100)
        t0 = datetime.combine(d, datetime.strptime(C_["entry_minute"], "%H:%M").time(), IST).timestamp()
        t1 = datetime.combine(d, datetime.strptime(cov, "%H:%M").time(), IST).timestamp()
        snaps = [s for s in self.ticks(d) if s["key"] == x["key"] and t0 <= s["rx"] < t1 and s.get("ltp")]
        if snaps:
            hit = next((s for s in snaps if s["ltp"] >= stop - 1e-9), None)
            if not hit:
                return None
            when = datetime.fromtimestamp(hit["rx"], IST).strftime("%H:%M:%S")
            if hit["asks"]:
                px, _, note = self.buy_at_snapshot(hit, x["qty"])
            else:
                half = max(tick_size(hit["ltp"]) / 2, hit["ltp"] * 0.0002)
                px, note = hit["ltp"] + half, f"no asks recorded; last price + {half:.4f}"
            return px, when, f"STOP-LOSS: traded {hit['ltp']} at {when}, at or above the +{sp:g}% stop {stop:.2f}; bought back at once ({note})"
        for hm in sorted(k for k in x["cs"] if C_["entry_minute"] <= k < cov):      # no ticks: the 1-minute candles
            o_, h_ = x["cs"][hm][0], x["cs"][hm][1]
            if hm != C_["entry_minute"] and o_ >= stop:
                p0, why = o_, f"the {hm} minute opened at {o_}, above the +{sp:g}% stop {stop:.2f}"
            elif h_ >= stop - 1e-9:
                p0, why = stop, f"the {hm} minute's high {h_} reached the +{sp:g}% stop {stop:.2f}"
            else:
                continue
            extra = max(tick_size(p0) / 2, p0 * 0.0002) + p0 * 0.0005
            return p0 + extra, hm, f"STOP-LOSS (no ticks): {why}; filled at {p0:.2f} + half a price step + 0.05% slippage"
        return None

    def limit_then_market(self, d, key, place_hm, fallback_hm, qty):
        """Limit buy at (last traded price at place_hm - 1 paisa); FILLED only if the market later trades below it
        before fallback_hm; otherwise market buy at fallback_hm (best ask, walking depth).
        Returns (price, time, how) or None if the recorder has no ticks for this."""
        off = S["limit_orders"]["offset_paise"] / 100
        t0 = datetime.combine(d, datetime.strptime(place_hm, "%H:%M").time(), IST).timestamp()
        t1 = datetime.combine(d, datetime.strptime(fallback_hm, "%H:%M").time(), IST).timestamp()
        snaps = [s for s in self.ticks(d) if s["key"] == key and s.get("ltp")]
        first = [s for s in snaps if t0 <= s["rx"] <= t0 + S["ticks"]["max_gap_seconds"]]
        if not first:
            return None
        ref = first[0]["ltp"]
        p = round(ref - off, 2)
        hit = next((s for s in snaps if t0 <= s["rx"] < t1 and s["ltp"] <= p - 0.01 + 1e-9), None)
        if hit:
            when = datetime.fromtimestamp(hit["rx"], IST).strftime("%H:%M:%S")
            return p, when, f"limit {p:.2f} (1 paisa below {ref}) placed {place_hm}, filled {when} when it traded at {hit['ltp']}"
        tb = self.tick_buy(d, key, fallback_hm, qty)
        if not tb:
            return None
        return tb[0], tb[1], f"limit {p:.2f} not filled by {fallback_hm}; market: {tb[2]}"

    def minute_limit(self, cs, place_hm, fallback_hm):
        """Same rule on 1-minute candles (used only if the recorder has no ticks)."""
        if place_hm not in cs or fallback_hm not in cs:
            return None
        p = round(cs[place_hm][0] - S["limit_orders"]["offset_paise"] / 100, 2)
        lows = [v[2] for k, v in cs.items() if place_hm <= k < fallback_hm]
        if lows and min(lows) <= p - 0.01 + 1e-9:
            return p, place_hm, f"no ticks: minute rule, limit {p:.2f} filled"
        return cs[fallback_hm][0] + self.slip, fallback_hm, f"no ticks: limit {p:.2f} not filled; {fallback_hm} minute open + {S['slip_paise']} paisa"

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
                L = S["limit_orders"]
                tb = self.limit_then_market(d, S["equity_key"], L["B_place"], L["B_fallback"], qty)
                if tb is None and self.waiting("morning"):
                    log("B: gap-up day, waiting for the recorder's 09:16-09:17 ticks")
                    return False
                if tb is None:
                    tb = self.minute_limit(eq, L["B_place"], L["B_fallback"])
                if tb:
                    x, when, how = tb
                else:
                    x, when, how = eq[c_min][0] + self.slip, c_min, f"no ticks: {c_min} minute open + {S['slip_paise']} paisa"
                b = dict(pc=pc, o=o, gap=gap, qty=qty, x=x, how=how, when=when)
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
                              entry_time=o_min, entry=o, exit_date=str(d), exit_time=b.get("when", c_min), exit=round(x, 4),
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
        L = S["limit_orders"]
        est = fc[L["A_place"]][0] if L["A_place"] in fc else fc[hm][0]
        lot = c["lot"]
        eqA = m["equity"]["A"]
        lot_margin = S["fut_margin_frac"] * est * lot
        lots = 0
        if eqA >= lot_margin:
            lots = max(1, min(int(eqA // lot_margin), round(eqA / ((S["fut_margin_frac"] + S["lot_cushion_frac"]) * est * lot))))
        else:
            st.alert(f"lowcap-{d}", f"Plan A account Rs {eqA:,.0f} is below one lot's margin (Rs {lot_margin:,.0f}); no overnight position.")
        if lots:
            tb = self.limit_then_market(d, c["key"], L["A_place"], L["A_fallback"], lots * lot)
            if tb is None and self.waiting("evening"):
                log(f"A: waiting for the recorder's {L['A_place']}-{L['A_fallback']} ticks")
                return False
            if tb is None:
                tb = self.minute_limit(fc, L["A_place"], L["A_fallback"])
            if tb:
                px, when, how = tb
            else:
                px, when, how = fc[hm][0] + self.slip, hm, f"no ticks: {hm} minute open + {S['slip_paise']} paisa"
            m["open"] = {"date": str(d), "key": c["key"], "symbol": c["symbol"], "lot": lot, "lots": lots,
                         "qty": lots * lot, "entry": px, "entry_time": when, "how": how}
            log(f"A: bought {lots} lot(s) = {lots * lot:,} {c['symbol']} at {px:.4f} ({how})")
        return self._end_day(d, c if lots else None, lots)

    def _end_day(self, d, c, lots=0):
        m = self.st.meta
        eq = m["equity"]
        self.st.add_day(dict(date=str(d), equity_A=round(eq["A"]), equity_B=round(eq["B"]), equity_C=round(eq["C"]),
                             total=round(eq["A"] + eq["B"] + eq["C"]), A_lots_overnight=lots,
                             A_contract=c["symbol"] if c else "", gap_pct=m.get("_gap"), B_traded=m.get("_btrade", False),
                             C_trades=m.get("_c_trades", "") if m.get("last_c") == str(d) else ""))
        m["last_evening"] = str(d)
        return True

    # ---- Plan C: gap-up auction short on a basket of stocks
    def morning_c(self, d):
        """Book Plan C for day d. Returns False when it is worth trying again later (today's data not complete)."""
        st, m = self.st, self.st.meta
        today_early = d == self.today and self.now.time() < datetime.strptime("10:30", "%H:%M").time()
        pd_ = self.prev_tday(d)
        if not pd_:
            st.alert(f"noprev-c-{d}", f"Plan C: could not find the previous trading day before {d}; skipped.")
            m["last_c"], m["_c_trades"] = str(d), 0
            return True
        ent, cov = C_["entry_minute"], C_["cover_minute"]
        signals, seen = [], []
        for rank, s in enumerate(C_["stocks"], 1):
            key = self.eqk.get(s)
            if not key:
                st.alert(f"nokey-{s}", f"Plan C: {s} not found in Upstox's instrument list; it is skipped.")
                continue
            cs = candles(key, d, self.today)
            if ent not in cs or cov not in cs:
                if today_early:
                    log(f"C: {s} has no {ent}/{cov} candle yet - will retry"); return False
                log(f"C: {s} has no {ent}/{cov} candle on {d} (not traded?) - skipped"); continue
            prev = candles(key, pd_, self.today)
            if not prev:
                log(f"C: {s} has no candles on {pd_} - skipped"); continue
            pc, o = prev[max(prev)][3], cs[ent][0]
            raw = pc * (1 + C_["gap_threshold_pct"] / 100)          # the limit must sit on the price step of its own band
            limit = math.ceil(round(raw / tick_size(raw), 6)) * tick_size(raw)
            gap = (o / pc - 1) * 100
            seen.append(f"{s} {gap:+.1f}%")
            if o >= limit - 1e-9 and gap <= 20:          # > 20% = a corporate action, not a real gap
                signals.append(dict(rank=rank, sym=s, key=key, cs=cs, pc=pc, o=o, gap=gap, limit=limit))
        log(f"C {d}: gaps " + ", ".join(seen))
        signals.sort(key=lambda x: (x["rank"], -x["gap"]))
        budget, chosen, passed = C_["leverage"] * m["equity"]["C"], [], []
        for x in signals:
            if len(chosen) >= C_["max_positions"] or budget < C_["min_position_rs"]:
                passed.append(f"{x['sym']} (no room left)"); continue
            fmv = opening_value_median(x["key"], d)
            if fmv is None:
                passed.append(f"{x['sym']} (too little history)"); continue
            liq = C_["liquidity_frac"] * fmv
            per = C_.get("max_position_x", C_["leverage"]) * m["equity"]["C"]      # 2:2:1 rule: each stock <= 2x the account
            po = self.preopen_at(d, x["key"], C_.get("live_pick_at", "09:06:30"))   # <= 10% of the pre-open auction, as live
            pre = (preopen_cap(*po) or 0.0) if po else None                           # None = no pre-open recording that day
            notional = min(liq, C_["cap_rs"], budget, per, pre if pre is not None else float("inf"))
            qty = int(notional // x["o"])
            if qty * x["o"] < C_["min_position_rs"]:
                passed.append(f"{x['sym']} (pre-open auction too small)" if pre is not None and pre <= min(liq, C_["cap_rs"], budget, per)
                              else f"{x['sym']} (position too small)"); continue
            why = c_size_why(liq, per, budget, pre)
            if pre is None:
                why += " (no pre-open recording, so the 10% pre-open cap could not be checked)"
            x.update(qty=qty, why=why); chosen.append(x); budget -= qty * x["o"]
        fills = []
        for x in chosen:
            if self.waiting("morning") and not self.ticks(d):
                log(f"C: {x['sym']} gapped up, waiting for the recorder's {cov} ticks"); return False
            tb = self.c_stop_check(d, x, cov) or self.tick_buy(d, x["key"], cov, x["qty"])
            if tb is None:
                c0 = x["cs"][cov][0]
                half = max(tick_size(c0) / 2, c0 * 0.0002)
                tb = (c0 + half, cov, f"no ticks: {cov} minute open + {half:.4f} (half a price step, at least 0.02%)")
            fills.append((x, *tb))
        for x, px, when, how in fills:
            qty, o = x["qty"], x["o"]
            gross = (o - px) * qty
            ch = charges("cash", "sell", o * qty) + charges("cash", "buy", px * qty)
            net = gross - ch
            m["equity"]["C"] += net
            st.add_trade(dict(plan="C gap-up basket", date=str(d), instrument=f"{x['sym']} shares (MIS)", side="short", qty=qty,
                              entry_time=ent, entry=o, exit_date=str(d), exit_time=when, exit=round(px, 4),
                              gross_rs=round(gross), charges_rs=round(ch), net_rs=round(net),
                              net_bps=round(net / (o * qty) * 1e4, 1), equity_after=round(m["equity"]["C"]),
                              note=f"priority {x['rank']} of {len(C_['stocks'])}; gap {x['gap']:+.2f}% (prev {x['pc']}; limit {x['limit']:.2f}; "
                                   f"auction {o}); size: {x['why']}; covered: {how}"))
            log(f"C: {x['sym']} gap {x['gap']:+.2f}% -> shorted {qty:,} at {o}, covered {px:.4f} ({how}), net Rs {net:,.0f}")
        if passed:
            log("C: signals not taken: " + ", ".join(passed))
        if not signals:
            log(f"C: no stock opened {C_['gap_threshold_pct']}% up -> no trade")
        m["last_c"], m["_c_trades"] = str(d), len(fills)
        return True

    def run(self):
        m = self.st.meta
        d = date.fromisoformat(m["start_date"])
        last_m = date.fromisoformat(m["last_morning"]) if m["last_morning"] else d - timedelta(days=1)
        last_e = date.fromisoformat(m["last_evening"]) if m["last_evening"] else d - timedelta(days=1)
        start_c = date.fromisoformat(m["start_c"])
        last_c = date.fromisoformat(m["last_c"]) if m.get("last_c") else start_c - timedelta(days=1)
        last_c = max(last_c, start_c - timedelta(days=1))
        d = min(last_m, last_e, last_c) + timedelta(days=1)
        t = self.now.time()
        due = C_.get("rerank_due")
        if due and self.today >= date.fromisoformat(due):
            self.st.alert(f"rerank-{due}", f"Plan C's stock order is due for its 6-monthly re-rank (due {due}): ask Claude to "
                                           "re-run plan_c_order.py on the last 12 months and update paper/settings.json.")
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
            if d > last_c:
                if is_today and t < datetime.strptime("09:21", "%H:%M").time():
                    break
                if not self.morning_c(d):
                    break
                last_c = d
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
    A, B, Cp = summary("A", S["capital_overnight"]), summary("B", S["capital_gapshort"]), summary("C", C_["capital"])
    Cp["stocks_traded"] = sorted({x["instrument"].split()[0] for x in trades if x["plan"].startswith("C")})
    tot0 = S["capital_overnight"] + S["capital_gapshort"] + C_["capital"]
    tot = m["equity"]["A"] + m["equity"]["B"] + m["equity"]["C"]
    dash = {"updated": now.strftime("%d %b %Y %H:%M IST"), "start_date": m["start_date"], "start_c": m["start_c"],
            "plans": {"A": A, "B": B, "C": Cp},
            "combined": {"start": tot0, "now": round(tot), "pnl": round(tot - tot0), "ret_pct": round((tot / tot0 - 1) * 100, 2)},
            "plan_c": {"stocks": C_["stocks"], "rerank_due": C_.get("rerank_due"), "last_c": m.get("last_c")},
            "open": m["open"], "days": days, "trades": trades[-300:], "reference": S["backtest_reference"],
            "last_gap_pct": m.get("_gap"), "rules": {k: v for k, v in S.items() if not k.startswith("_") and k != "backtest_reference"}}
    try:                                         # the live shadow bot, lined up with the paper trades
        sys.path.insert(0, str(ROOT / "paper"))
        import shadow_ledger
        dash["shadow"] = shadow_ledger.build(m.get("open"), m.get("last_c"))
        dash["shadow"]["built"] = now.strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        log("shadow ledger not built:", e)
        dash["shadow"] = {"error": str(e)[:200]}
    (ROOT / "docs" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "data" / "dashboard.json").write_text(json.dumps(dash, indent=1))
    pos = m["open"]
    lines = ["# Vodafone Idea paper trader", "",
             "Three plans, Rs 5 lakh each, compounding: A and B on Vodafone Idea (VI), C on a basket of 13 stocks. "
             "**Paper trading only: no real orders.**",
             f"Dashboard: https://ajbhutada.github.io/vi-paper-trader/  ·  updated {dash['updated']}", "",
             "| Plan | Start | Now | P&L | Return | Trades | Win rate | Avg / trade | Worst trade | Deepest dip |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    rs = lambda v: "-" if v is None else f"Rs {v:+,}"
    for name, x in (("A Overnight futures", A), ("B Gap-up auction short", B), ("C Gap-up basket (13 stocks)", Cp)):
        win = "-" if x["win_pct"] is None else f"{x['win_pct']}%"
        lines.append(f"| {name} | Rs {x['start']:,} | Rs {x['now']:,} | Rs {x['pnl']:+,} | {x['ret_pct']:+.2f}% | {x['trades']} | "
                     f"{win} | {rs(x['avg_net_rs'])} | {rs(x['worst_rs'])} | {x['max_dip_pct']}% |")
    c = dash["combined"]
    lines += [f"| **All three** | Rs {c['start']:,} | Rs {c['now']:,} | Rs {c['pnl']:+,} | {c['ret_pct']:+.2f}% | | | | | |", "",
              f"Open overnight position: {pos['lots']} lot(s) = {pos['qty']:,} {pos['symbol']} bought at {pos['entry']:.4f} on {pos['date']}"
              if pos else "Open overnight position: none", "",
              f"Plan C starts {m['start_c']}. Stocks in priority order: {', '.join(C_['stocks'])}.", "",
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
    P, c = dash["plans"], dash["combined"]
    week_tr = [x for x in dash["trades"] if (d - date.fromisoformat(x["exit_date"])).days < 7]
    body = [f"@AjBhutada weekly report for the VI paper trader, week ending {d:%d %b %Y}.", "",
            "| Plan | Now | P&L since start | This week | Trades this week |", "|---|---|---|---|---|"]
    for name, p in (("A Overnight", "A"), ("B Gap-up short", "B"), ("C Gap-up basket", "C")):
        x = P[p]
        wk_pnl = sum(float(t["net_rs"]) for t in week_tr if t["plan"].startswith(p))
        body.append(f"| {name} | Rs {x['now']:,} | Rs {x['pnl']:+,} ({x['ret_pct']:+.2f}%) | Rs {wk_pnl:+,.0f} | "
                    f"{sum(t['plan'].startswith(p) for t in week_tr)} |")
    body += [f"| **All three** | Rs {c['now']:,} | Rs {c['pnl']:+,} ({c['ret_pct']:+.2f}%) | | |", "",
             "Dashboard: https://ajbhutada.github.io/vi-paper-trader/", "",
             "*Research on past data / paper trading only, not investment advice.*"]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "weekly_pending.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    m["last_weekly"] = wk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now"); ap.add_argument("--state", default=str(ROOT / "state"))
    ap.add_argument("--start"); ap.add_argument("--start-c", help="Plan C start date for a replay (default: settings)")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args()
    now = datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=IST) if a.now else datetime.now(IST)
    if not a.now and now.time() < datetime.strptime("09:17", "%H:%M").time() and now.time() > datetime.strptime("09:00", "%H:%M").time():
        wait = (datetime.combine(now.date(), datetime.strptime("09:17:40", "%H:%M:%S").time(), IST) - now).total_seconds()
        log(f"waiting {wait:.0f} s for the 09:16 candle"); time.sleep(max(0, wait)); now = datetime.now(IST)
    if not a.now and datetime.strptime("15:29", "%H:%M").time() <= now.time() < datetime.strptime("15:31", "%H:%M").time():
        wait = (datetime.combine(now.date(), datetime.strptime("15:31:10", "%H:%M:%S").time(), IST) - now).total_seconds()
        log(f"waiting {wait:.0f} s for the 15:29 candle"); time.sleep(max(0, wait)); now = datetime.now(IST)
    st = State(a.state)
    st.init(a.start or S["start_date"], a.start_c)
    log(f"run at {now:%d %b %Y %H:%M} IST; last morning {st.meta['last_morning']}, last evening {st.meta['last_evening']}, "
        f"last Plan C {st.meta['last_c']}")
    Bot(st, now).run()
    st.save()
    if not a.no_report:
        dash = report(st, now)
        weekly(st, now, dash)
        st.save()
    e = st.meta["equity"]
    log(f"done. A Rs {e['A']:,.0f}  B Rs {e['B']:,.0f}  C Rs {e['C']:,.0f}  open: {bool(st.meta['open'])}")


if __name__ == "__main__":
    main()
