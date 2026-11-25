"""Turn the shadow bot's live session logs (state/shadow/*.json) into trades and accounts, and line them up
with the paper bot's trades (state/trades.csv). Used by vi_bot.report() for the combined dashboard.

Shadow price for each leg = what a real order would have got at that instant, from the LIVE feed:
  A-buy  : best asks at 15:25:00 walked for the quantity      A-sell : futures auction (opening) price
  B-short: VI auction (opening) price, if >= the limit         B-cover: best asks at 09:16:00 walked
  C-short-SYM: the stock's auction price, if picked at 09:06:30 and >= its limit   C-cover-SYM: best asks at 09:20:00 walked
Quantities for A and B are the paper bot's, so any difference between paper and shadow is execution only (price,
timing, a session that was missed or failed). Plan C's live size is set before the auction from the limit price (the
paper bot uses the auction price), and the live pick uses the 09:06:30 indicative prices, so C can also differ in
WHICH stocks are traded - that is what the live test measures.
"""
import csv, json
from pathlib import Path
import vi_bot as vb

S, ROOT = vb.S, vb.ROOT


def _sessions():
    out = {}
    for f in sorted((ROOT / "state" / "shadow").glob("*.json")):
        js = json.loads(f.read_text())
        if js["part"] not in ("morning", "afternoon"):
            continue
        if any(e.get("what") == "skip" and "not a trading day" in str(e.get("detail")) for e in js["events"]):
            continue                             # holiday: no session, so it must not stand in for the next real morning
        out[(js["day"], js["part"])] = js["events"]
    return out


def _c_cover_text(st, sym, c):
    """Order status of a Plan C buy-back: the 09:20 market order, or the stop-loss order if the stop fired."""
    if c and "STOP-LOSS" in str(c.get("how")):
        return "stop-loss " + st.get(f"C-{sym}-stop", "-")
    return st.get(f"C-{sym}-cover", "-")


def _orders(events):
    """label -> short status text for the sandbox orders of a session."""
    st = {}
    for e in events:
        d = e.get("detail") if isinstance(e.get("detail"), dict) else {}
        lab = d.get("label")
        if not lab:
            continue
        if e["what"] == "order_sent":
            st[lab] = f"sent {e['t'][:8]} ({d.get('ms')} ms)" + (" ✓" if str(d.get("status")) == "success" else f" {d.get('status')}")
        elif e["what"] == "order_dry":
            st[lab] = f"logged only {e['t'][:8]} (no sandbox token)"
        elif e["what"] == "order_error":
            st[lab] = f"ERROR {e['t'][:8]}: {str(d.get('error'))[:80]}"
        elif e["what"] in ("cancel_sent", "cancel_dry"):
            st[lab + " cancel"] = ("cancelled" if e["what"] == "cancel_sent" else "cancel logged") + f" {e['t'][:8]}"
    return st


def _leg(st, label):
    """status of a leg that may be a limit order followed by a market order"""
    parts = [x for x in (st.get(label + "-limit"), st.get(label + "-limit cancel"), st.get(label)) if x]
    return " → ".join(parts) if parts else "-"


def _fills(events):
    f = {}
    for e in events:
        if e["what"] == "fill" and isinstance(e.get("detail"), dict):
            f[e["detail"]["leg"]] = {**e["detail"], "t": e["t"]}
    # sessions logged before 'fill' events existed: rebuild the 15:25 buy from the plan + quote
    if "A-buy" not in f:
        plan = next((e["detail"] for e in events if e["what"] == "plan" and isinstance(e.get("detail"), dict) and "lots" in e["detail"]), None)
        q = next((e for e in events if e["what"] == "buy_quote"), None)
        if plan and q and plan.get("lots"):
            asks = [tuple(a) for a in (q["detail"].get("best_ask") or []) if a]
            if asks:
                from shadow import walk
                f["A-buy"] = {"leg": "A-buy", "price": walk(asks, plan["lots"] * plan["lot"]), "qty": plan["lots"] * plan["lot"],
                              "symbol": plan.get("contract"), "t": q["t"]}
    return f


def build(paper_open=None, last_c=None):
    """paper_open = the paper bot's open overnight position (state/meta.json 'open'), shown before it is closed.
    last_c = the last morning the paper bot has booked for Plan C (state/meta.json 'last_c')."""
    ses = _sessions()
    paper = list(csv.DictReader(open(ROOT / "state" / "trades.csv", encoding="utf-8"))) if (ROOT / "state" / "trades.csv").exists() else []
    pA = {r["date"]: r for r in paper if r["plan"].startswith("A")}
    pB = {r["date"]: r for r in paper if r["plan"].startswith("B")}
    pC = {(r["date"], r["instrument"].split()[0]): r for r in paper if r["plan"].startswith("C")}
    rows = []
    days = sorted({d for d, _ in ses})
    # ---- plan A: each afternoon buy, closed by the next morning session's auction sale
    for i, d in enumerate(days):
        ev = ses.get((d, "afternoon"))
        if not ev:
            continue
        fb = _fills(ev).get("A-buy")
        if not fb:
            continue
        sell, sell_day, sell_orders = None, None, {}
        for d2 in days[i + 1:]:
            ev2 = ses.get((d2, "morning"))
            if ev2:
                sell = _fills(ev2).get("A-sell"); sell_day = d2; sell_orders = _orders(ev2)
                break
        p = pA.get(d)
        r = {"plan": "A", "date": d, "exit_date": sell_day or "", "qty": fb["qty"], "instrument": fb.get("symbol"),
             "shadow_entry": fb["price"], "shadow_exit": sell["price"] if sell else None,
             "orders": "; ".join(x for x in (f"buy: {_leg(_orders(ev), 'A-buy')}", f"sell: {sell_orders.get('A-sell', '-')}" if sell_day else "") if x)}
        if sell and sell["price"] and fb["price"]:
            q = fb["qty"]
            r["shadow_net"] = round((sell["price"] - fb["price"]) * q - vb.charges("fut", "buy", fb["price"] * q) - vb.charges("fut", "sell", sell["price"] * q))
        if not p and paper_open and paper_open.get("date") == d:
            r.update(paper_entry=float(paper_open["entry"]), paper_qty=int(paper_open["qty"]))
        if p:
            r.update(paper_entry=float(p["entry"]), paper_exit=float(p["exit"]), paper_net=float(p["net_rs"]), paper_qty=int(p["qty"]))
            if not sell_day and p.get("exit_date"):
                r["orders"] += f"; sell: MISSED (no live morning session on {p['exit_date']})"
                r["missed_leg"] = True
        rows.append(r)
    # ---- plan B: gap-up mornings
    for d in days:
        ev = ses.get((d, "morning"))
        if not ev:
            continue
        f = _fills(ev)
        if "B-short" not in f:
            continue
        s, c = f["B-short"], f.get("B-cover")
        r = {"plan": "B", "date": d, "exit_date": d, "qty": s["qty"], "instrument": "IDEA shares (MIS)",
             "shadow_entry": s["price"], "shadow_exit": c["price"] if c else None,
             "orders": f"short: {_orders(ev).get('B-limit-short', '-')}; cover: {_leg(_orders(ev), 'B-cover')}"}
        if c and c["price"]:
            q = s["qty"]
            r["shadow_net"] = round((s["price"] - c["price"]) * q - vb.charges("cash", "sell", s["price"] * q) - vb.charges("cash", "buy", c["price"] * q))
        p = pB.get(d)
        if p:
            r.update(paper_entry=float(p["entry"]), paper_exit=float(p["exit"]), paper_net=float(p["net_rs"]), paper_qty=int(p["qty"]))
        rows.append(r)
    # ---- plan C: basket shorts picked at 09:06:30 from the indicative auction prices
    why_c = {}                                   # (date, sym) -> why the live pick did not take a stock
    for d in days:
        ev = ses.get((d, "morning"))
        if not ev:
            continue
        for e in ev:
            if e["what"] == "c_pick" and isinstance(e.get("detail"), dict):
                for x in e["detail"].get("stocks", []):
                    if not x.get("picked"):
                        why_c[(d, x["sym"])] = (
                            f"live pick at {e['detail'].get('at')}: {x['why_not']}" if x.get("why_not") else
                            f"live pick at {e['detail'].get('at')}: indicative ₹{x['iep']} ({x['gap_ind']:+.2f}%) was below the ₹{x['limit']:.2f} limit"
                            if x.get("iep") else f"live pick at {e['detail'].get('at')}: no indicative price in the feed")
            elif e["what"] == "c_missed":
                why_c[(d, "*")] = str(e["detail"])
        f, st = _fills(ev), _orders(ev)
        for leg, s in f.items():
            if leg.startswith("C-none-"):
                why_c[(d, s["sym"])] = (f"picked live, but the auction price seen live (₹{s.get('open')}, {s.get('source')}) "
                                        f"was below the ₹{s['limit']:.2f} limit, so the order was cancelled")
            if not leg.startswith("C-short-"):
                continue
            sym = s["sym"]; c = f.get(f"C-cover-{sym}")
            r = {"plan": "C", "date": d, "exit_date": d, "qty": s["qty"], "instrument": f"{sym} shares (MIS)",
                 "shadow_entry": s["price"], "shadow_exit": c["price"] if c else None,
                 "orders": f"{sym} short: {st.get(f'C-{sym}-short', '-')}; cover: {_c_cover_text(st, sym, c)}"}
            if c and c["price"]:
                q = s["qty"]
                r["shadow_net"] = round((s["price"] - c["price"]) * q - vb.charges("cash", "sell", s["price"] * q) - vb.charges("cash", "buy", c["price"] * q))
            p = pC.get((d, sym))
            if p:
                r.update(paper_entry=float(p["entry"]), paper_exit=float(p["exit"]), paper_net=float(p["net_rs"]), paper_qty=int(p["qty"]))
            elif last_c and last_c >= d:
                r["orders"] += "; the paper bot did not take this one (other stocks had priority at the final auction prices)"
            else:
                r["orders"] += "; the paper bot has not booked this morning yet"
            rows.append(r)
    # ---- paper trades the shadow bot did not take (missed / failed session, or not picked live)
    have = {(r["plan"], r["date"], r["instrument"].split()[0] if r["plan"] == "C" else "") for r in rows}
    first = min(days, default="9999")
    for pl, book in (("A", pA), ("B", pB), ("C", pC)):
        for k, p in book.items():
            d, sym = (k if pl == "C" else (k, ""))
            if (pl, d, sym) in have or d < first:
                continue
            why = "shadow bot missed this trade (no live session log)"
            if pl == "C" and ses.get((d, "morning")):
                why = why_c.get((d, sym)) or why_c.get((d, "*")) or "live bot: not picked (see the session log)"
            rows.append({"plan": pl, "date": d, "exit_date": p["exit_date"], "qty": int(p["qty"]), "instrument": p["instrument"],
                         "paper_entry": float(p["entry"]), "paper_exit": float(p["exit"]), "paper_net": float(p["net_rs"]),
                         "orders": why, "missed": True})
    for r in rows:
        if r.get("shadow_net") is not None and r.get("paper_net") is not None:
            r["diff_rs"] = round(r["shadow_net"] - r["paper_net"])
        for k in ("entry", "exit"):
            if r.get(f"shadow_{k}") and r.get(f"paper_{k}"):
                r[f"{k}_gap_paise"] = round((r[f"shadow_{k}"] - r[f"paper_{k}"]) * 100, 2)
    rows.sort(key=lambda r: (r["date"], r["plan"]))
    # ---- shadow accounts (Rs 5 lakh each, P&L booked on the exit day)
    eq = {"A": S["capital_overnight"], "B": S["capital_gapshort"], "C": vb.C_["capital"]}
    curve, done = [], [r for r in rows if r.get("shadow_net") is not None]
    for d in sorted({r["exit_date"] for r in done}):
        for r in done:
            if r["exit_date"] == d:
                eq[r["plan"]] += r["shadow_net"]
        curve.append({"date": d, "A": round(eq["A"]), "B": round(eq["B"]), "C": round(eq["C"])})

    def summ(pl, start):
        t = [r for r in done if r["plan"] == pl]
        diffs = [r["diff_rs"] for r in t if "diff_rs" in r]
        return {"start": start, "now": round(eq[pl]), "pnl": round(eq[pl] - start), "ret_pct": round((eq[pl] / start - 1) * 100, 2),
                "trades": len(t), "wins": sum(r["shadow_net"] > 0 for r in t),
                "vs_paper_rs": round(sum(diffs)) if diffs else None,
                "missed": sum(1 for r in rows if r["plan"] == pl and r.get("missed"))}
    A, B, C = summ("A", S["capital_overnight"]), summ("B", S["capital_gapshort"]), summ("C", vb.C_["capital"])
    C["stocks_traded"] = sorted({r["instrument"].split()[0] for r in done if r["plan"] == "C"})
    tot0, tot = A["start"] + B["start"] + C["start"], A["now"] + B["now"] + C["now"]
    return {"plans": {"A": A, "B": B, "C": C}, "start_c": vb.C_["start_date"],
            "combined": {"now": tot, "pnl": tot - tot0, "ret_pct": round(tot / tot0 * 100 - 100, 2)},
            "compare": rows[-100:], "curve": curve,
            "open": next(({"date": r["date"], "qty": r["qty"], "instrument": r["instrument"], "entry": r["shadow_entry"]}
                          for r in rows if r["plan"] == "A" and r.get("shadow_exit") is None and not r.get("missed") and not r.get("missed_leg")), None)}
