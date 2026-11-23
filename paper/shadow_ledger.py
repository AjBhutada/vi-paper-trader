"""Turn the shadow bot's live session logs (state/shadow/*.json) into trades and accounts, and line them up
with the paper bot's trades (state/trades.csv). Used by vi_bot.report() for the combined dashboard.

Shadow price for each leg = what a real order would have got at that instant, from the LIVE feed:
  A-buy  : best asks at 15:25:00 walked for the quantity      A-sell : futures auction (opening) price
  B-short: VI auction (opening) price, if >= the limit         B-cover: best asks at 09:16:00 walked
Quantities are the paper bot's, so any difference between paper and shadow is execution only (price, timing,
a session that was missed or failed).
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
        out[(js["day"], js["part"])] = js["events"]
    return out


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


def build():
    ses = _sessions()
    paper = list(csv.DictReader(open(ROOT / "state" / "trades.csv", encoding="utf-8"))) if (ROOT / "state" / "trades.csv").exists() else []
    pA = {r["date"]: r for r in paper if r["plan"].startswith("A")}
    pB = {r["date"]: r for r in paper if r["plan"].startswith("B")}
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
             "orders": "; ".join(x for x in (f"buy: {_orders(ev).get('A-buy', '-')}", f"sell: {sell_orders.get('A-sell', '-')}" if sell_day else "") if x)}
        if sell and sell["price"] and fb["price"]:
            q = fb["qty"]
            r["shadow_net"] = round((sell["price"] - fb["price"]) * q - vb.charges("fut", "buy", fb["price"] * q) - vb.charges("fut", "sell", sell["price"] * q))
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
             "orders": f"short: {_orders(ev).get('B-limit-short', '-')}; cover: {_orders(ev).get('B-cover', '-')}"}
        if c and c["price"]:
            q = s["qty"]
            r["shadow_net"] = round((s["price"] - c["price"]) * q - vb.charges("cash", "sell", s["price"] * q) - vb.charges("cash", "buy", c["price"] * q))
        p = pB.get(d)
        if p:
            r.update(paper_entry=float(p["entry"]), paper_exit=float(p["exit"]), paper_net=float(p["net_rs"]), paper_qty=int(p["qty"]))
        rows.append(r)
    # ---- paper trades the shadow bot did not take (missed / failed session)
    have = {(r["plan"], r["date"]) for r in rows}
    for pl, book in (("A", pA), ("B", pB)):
        for d, p in book.items():
            if (pl, d) not in have and d >= min(days, default="9999"):
                rows.append({"plan": pl, "date": d, "exit_date": p["exit_date"], "qty": int(p["qty"]), "instrument": p["instrument"],
                             "paper_entry": float(p["entry"]), "paper_exit": float(p["exit"]), "paper_net": float(p["net_rs"]),
                             "orders": "shadow bot missed this trade (no live session log)", "missed": True})
    for r in rows:
        if r.get("shadow_net") is not None and r.get("paper_net") is not None:
            r["diff_rs"] = round(r["shadow_net"] - r["paper_net"])
        for k in ("entry", "exit"):
            if r.get(f"shadow_{k}") and r.get(f"paper_{k}"):
                r[f"{k}_gap_paise"] = round((r[f"shadow_{k}"] - r[f"paper_{k}"]) * 100, 2)
    rows.sort(key=lambda r: (r["date"], r["plan"]))
    # ---- shadow accounts (Rs 5 lakh each, P&L booked on the exit day)
    eq = {"A": S["capital_overnight"], "B": S["capital_gapshort"]}
    curve, done = [], [r for r in rows if r.get("shadow_net") is not None]
    for d in sorted({r["exit_date"] for r in done}):
        for r in done:
            if r["exit_date"] == d:
                eq[r["plan"]] += r["shadow_net"]
        curve.append({"date": d, "A": round(eq["A"]), "B": round(eq["B"])})

    def summ(pl, start):
        t = [r for r in done if r["plan"] == pl]
        diffs = [r["diff_rs"] for r in t if "diff_rs" in r]
        return {"start": start, "now": round(eq[pl]), "pnl": round(eq[pl] - start), "ret_pct": round((eq[pl] / start - 1) * 100, 2),
                "trades": len(t), "wins": sum(r["shadow_net"] > 0 for r in t),
                "vs_paper_rs": round(sum(diffs)) if diffs else None,
                "missed": sum(1 for r in rows if r["plan"] == pl and r.get("missed"))}
    A, B = summ("A", S["capital_overnight"]), summ("B", S["capital_gapshort"])
    return {"plans": {"A": A, "B": B},
            "combined": {"now": A["now"] + B["now"], "pnl": A["pnl"] + B["pnl"],
                         "ret_pct": round((A["now"] + B["now"]) / (A["start"] + B["start"]) * 100 - 100, 2)},
            "compare": rows[-100:], "curve": curve,
            "open": next(({"date": r["date"], "qty": r["qty"], "instrument": r["instrument"], "entry": r["shadow_entry"]}
                          for r in rows if r["plan"] == "A" and r.get("shadow_exit") is None and not r.get("missed") and not r.get("missed_leg")), None)}
