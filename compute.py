"""Flow compute for the watchlist. Reusable: `from compute import compute`.

Only Unusual Whales API calls, and only inside a regular session. The key is
env UW_API_KEY (see q.py); it is never printed. Writes latest.json, appends
snapshots.csv, and caches raw trades under data/ (gitignored).

Off-hours, weekends, and NYSE holidays return before any HTTP call:

    python3 compute.py
"""
import os
import sys
import json
import csv
import math
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import holidays
import q

ET = ZoneInfo("America/New_York")
TICKERS = "AMD NVDA MSFT AAPL TSLA META MU".split()
GAUGES = ["SPY", "QQQ"]
MIN_PREM = 25000
GAUGE_MIN_PREM = 100000
BIG = 200000
FOCUS_LEAN = 65.0
FOCUS_BIG_MIN = 1_000_000
WIN_MIN = 15
# The 4:00 PM cron can start a few minutes late. Past this, leave the last file alone.
CLOSE_GRACE = dt.timedelta(minutes=10)
# $200K+ premium on the lean side must be at least this multiple of the other side.
CLEAR_RATIO = 2.0
# A repeat is "large" once the same contract has 2+ prints totaling at least this.
REPEAT_MIN = 400_000
# Session-range tests. A level is named only from prints we actually saw.
LEVEL_BAND = 0.20       # last price in the outer 20% of the session range
MIN_RANGE = 0.002       # range must be at least 0.2% of price, or no level is cited
FLAT = 0.0015           # up less than 0.15% from the first print still counts as flat
MOVE = 0.003            # 0.3% down is a dip, 0.3% up is a rip
DATA = os.path.join(HERE, "data")
LATEST = os.path.join(HERE, "latest.json")
SNAP = os.path.join(HERE, "snapshots.csv")
CALLS = {"n": 0}


def now_et():
    return dt.datetime.now(ET)


def is_trading_day(d):
    return holidays.is_trading_day(d)


def session_open(d):
    return dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)


def session_close(d):
    hh, mm = holidays.session_close_hm(d)
    return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)


def skip_reason(now):
    """Why this clock should exit before any API call, or None to run.

    Covers weekends, the holiday table, the open, and the close (1:00 PM on an
    early-close day). A few minutes after the close still runs, so a late 4:00 PM
    job is not dropped.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    day = now.date()
    if not is_trading_day(day):
        return "weekend or holiday"
    if now < session_open(day):
        return "before the open"
    if now > session_close(day) + CLOSE_GRACE:
        return "after the close"
    return None


def scrub(text):
    return q.scrub(text)


def api(path, **params):
    CALLS["n"] += 1
    return q.g(path, **params)


def ts(x):
    return dt.datetime.fromisoformat(x["executed_at"].replace("Z", "+00:00")).astimezone(ET)


def slim(x):
    d = {k: x.get(k) for k in (
        "id", "executed_at", "premium", "option_type", "strike", "expiry",
        "tags", "size", "price", "underlying_price", "canceled", "report_flags",
        "nbbo_bid", "nbbo_ask")}
    d["cond"] = x.get("upstream_condition_detail")
    return d


CACHE_V = 2


def multi_leg(x):
    """OPRA codes for multi-leg / stock-tied / complex prints. A spread leg is not a one-way bet."""
    c = (x.get("cond") or "").lower()
    return c[:1] in ("m", "t") or c in ("cbmo", "cmpr") or "futures_floor" in (x.get("report_flags") or [])


def pull_new(tk, since_ms, min_prem, max_pages=20, until_ms=None):
    rows, older = [], until_ms
    for _ in range(max_pages):
        params = dict(ticker_symbol=tk, min_premium=min_prem, include_agg_trades="true", limit=500, newer_than=since_ms)
        if older:
            params["older_than"] = older
        status, data = api("option-trades", **params)
        if status != 200:
            raise RuntimeError(f"{tk}: HTTP {status}")
        batch = data.get("data", []) if isinstance(data, dict) else []
        rows += batch
        if len(batch) < 500:
            break
        older = min(int(ts(x).timestamp() * 1000) for x in batch)
    return rows


def load_cache(day):
    p = os.path.join(DATA, f"trades_{day:%Y%m%d}.json")
    return (json.load(open(p)) if os.path.exists(p) else {}), p


DEEP_ITM = 0.15


def deep_itm(x):
    try:
        k, u = float(x["strike"]), float(x["underlying_price"])
    except (TypeError, ValueError, KeyError):
        return False
    if not u:
        return False
    d = (u - k) / u if x["option_type"] == "call" else (k - u) / u
    return d > DEEP_ITM


def side(x):
    """Bullish = calls at the ask + puts at the bid. Bearish = the reverse."""
    if deep_itm(x) or multi_leg(x):
        return None
    tags = x.get("tags") or []
    call = x["option_type"] == "call"
    if "ask_side" in tags:
        return "bull" if call else "bear"
    if "bid_side" in tags:
        return "bear" if call else "bull"
    return None


def side_code(x):
    """A/AA (at/above ask), B/BB (at/below bid), M (mid)."""
    tags = x.get("tags") or []
    try:
        price = float(x.get("price"))
        ask = float(x.get("nbbo_ask") or 0)
        bid = float(x.get("nbbo_bid") or 0)
    except (TypeError, ValueError):
        price = ask = bid = 0
    if "ask_side" in tags:
        return "AA" if ask and price > ask else "A"
    if "bid_side" in tags:
        return "BB" if bid and price < bid else "B"
    return "M"


BUCKETS = [("wk", "This week"), ("2w", "<2 wks"), ("1m", "2 wks–1 mo"), ("1m+", "1 mo+")]


def bucket(expiry, day):
    e = dt.date.fromisoformat(expiry)
    dte = (e - day).days
    fri = day + dt.timedelta(days=(4 - day.weekday()) % 7)
    if e <= fri:
        return "wk", dte
    return ("2w" if dte <= 14 else "1m" if dte <= 30 else "1m+"), dte


def lean(b, r):
    return round(100 * b / (b + r), 1) if (b + r) > 0 else None


def _prem_s(v):
    v = float(v or 0)
    return f"${v/1e6:.1f}M" if v >= 1e6 else f"${v/1e3:.0f}K" if v >= 1e3 else f"${v:.0f}"


def repeat_note(big):
    """Same-contract $200K+ hits (display only)."""
    groups = {}
    for t in big:
        key = (t.get("exp") or "", str(t.get("strike") or ""), (t.get("cp") or "").upper())
        groups.setdefault(key, []).append(t)
    notes = []
    for (exp, k, cp), hits in groups.items():
        if len(hits) < 2:
            continue
        short = "C" if cp.startswith("C") else "P"
        prem = sum(t["premium"] for t in hits)
        roles = []
        for t in hits:
            sc = t.get("sidecode") or ""
            roles.append("buyer" if sc in ("A", "AA") else "seller" if sc in ("B", "BB") else "mid")
        uniq = {r for r in roles if r != "mid"}
        if uniq == {"buyer"}:
            who = "all buyers"
        elif uniq == {"seller"}:
            who = "all sellers"
        elif len(uniq) == 2:
            who = "buyers and sellers"
        else:
            who = ""
        notes.append((prem, len(hits), f"{exp} {k}{short} hit {len(hits)} times ({_prem_s(prem)}" + (f", {who}" if who else "") + ")"))
    notes.sort(key=lambda z: (-z[0], -z[1]))
    return "; ".join(n for _, _, n in notes[:2]) or None


def _spot(x):
    try:
        u = float(x.get("underlying_price"))
    except (TypeError, ValueError):
        return 0.0
    return u if u > 0 else 0.0


def price_context(spots):
    """Session high/low/first/last from underlying prices on the prints. No other levels."""
    if len(spots) < 2:
        return None
    spots = sorted(spots)
    first, last = spots[0][1], spots[-1][1]
    lo = min(p for _, p in spots)
    hi = max(p for _, p in spots)
    rng = hi - lo
    wide = last > 0 and rng / last >= MIN_RANGE
    pos = (last - lo) / rng if wide and rng > 0 else None
    chg = (last - first) / first if first else 0.0
    return dict(
        first=first, last=last, low=lo, high=hi, chg=chg,
        near_low=pos is not None and pos <= LEVEL_BAND,
        near_high=pos is not None and pos >= 1 - LEVEL_BAND,
        down_or_flat=chg <= FLAT,
        dip=chg <= -MOVE,
        rip=chg >= MOVE,
    )


def _px(p):
    return f"{p:.2f}"


def phrase(direction, ctx):
    """One sentence. The price, when present, is the session low or high from prints."""
    if direction == "bull":
        if ctx and ctx["near_low"] and ctx["down_or_flat"]:
            return f"Bulls absorbing the selling near {_px(ctx['low'])}, could flip up"
        if ctx and ctx["dip"]:
            return "Whales buying the dip"
        return "Big money leaning bullish"
    if ctx and ctx["near_high"]:
        return f"Bears selling into the high near {_px(ctx['high'])}"
    if ctx and ctx["rip"]:
        return "Whales selling the rip"
    return "Big money leaning bearish"


def _repeat_large(big, direction):
    """Same contract, 2+ $200K+ prints, all on `direction`, totaling REPEAT_MIN."""
    groups = {}
    for t in big:
        if t.get("dir") != direction:
            continue
        key = (t.get("exp") or "", str(t.get("strike") or ""), (t.get("cp") or "").upper())
        groups.setdefault(key, []).append(t)
    for hits in groups.values():
        if len(hits) >= 2 and sum(t["premium"] for t in hits) >= REPEAT_MIN:
            return True
    return False


def commentary(stats, big, spots):
    """Significant only with a 65%+ lean AND (one-sided $200K+ premium, or a repeat).

    One-sided: that side's $200K+ premium is at least $200K and at least CLEAR_RATIO
    times the other side. Those orders are already at the ask when bullish and at
    the bid when bearish for calls (puts are the mirror: puts at the bid are bullish).
    A repeat can stand in for the ratio when it is large and that side is still ahead.
    """
    out = dict(call="Nothing significant", call_dir=None)
    ctx = price_context(spots)
    if ctx:
        out["px"] = {k: round(ctx[k], 2) for k in ("first", "last", "low", "high")}
        out["px"]["chg"] = round(100 * ctx["chg"], 2)
    level = stats.get("lean")
    if level is None or not (level >= FOCUS_LEAN or level <= 100 - FOCUS_LEAN):
        return out
    direction = "bull" if level >= FOCUS_LEAN else "bear"
    dom = stats["bull_big"] if direction == "bull" else stats["bear_big"]
    opp = stats["bear_big"] if direction == "bull" else stats["bull_big"]
    outweighs = dom >= BIG and dom >= CLEAR_RATIO * opp
    repeated = _repeat_large(big, direction) and dom > opp
    if not (outweighs or repeated):
        return out
    out["call"] = phrase(direction, ctx)
    out["call_dir"] = direction
    return out


def analyze(rows, now):
    o = dict(bull_small=0.0, bull_big=0.0, bear_small=0.0, bear_big=0.0, n=0,
             n_bull_small=0, n_bull_big=0, n_bear_small=0, n_bear_big=0)
    rec = dict(bull=0.0, bear=0.0)
    early = dict(bull=0.0, bear=0.0)
    big, dirs, spots = [], [], []
    bk = {k: dict(bull=0.0, bear=0.0, bull_big=0.0, bear_big=0.0, bull_small=0.0, bear_small=0.0, n_bull=0, n_bear=0, big=[]) for k, _ in BUCKETS}
    cut = now - dt.timedelta(minutes=WIN_MIN)
    o["skipped_multileg"] = 0.0
    o["skipped_itm"] = 0.0
    lo, hi = session_open(now.date()), min(now, session_close(now.date()))
    for x in rows:
        if x.get("canceled"):
            continue
        t = ts(x)
        if not (lo <= t <= hi):
            continue
        u = _spot(x)
        if u:
            spots.append((t, u))
        if multi_leg(x):
            o["skipped_multileg"] += float(x["premium"])
            continue
        if deep_itm(x):
            o["skipped_itm"] += float(x["premium"])
            continue
        sd = side(x)
        if not sd:
            continue
        p = float(x["premium"])
        tier = "big" if p >= BIG else "small"
        o[f"{sd}_{tier}"] += p
        o[f"n_{sd}_{tier}"] += 1
        o["n"] += 1
        dirs.append((t, sd, p))
        bkey, dte = bucket(x["expiry"], now.date())
        B = bk[bkey]
        B[sd] += p
        B[f"{sd}_{tier}"] += p
        B[f"n_{sd}"] += 1
        (rec if t >= cut else early)[sd] += p
        if p >= BIG:
            big.append(dict(
                time=t.strftime("%H:%M"), ts=t.strftime("%-I:%M"),
                contract=f"{x['expiry'][5:]} ${float(x['strike']):g}{x['option_type'][0].upper()}",
                exp=f"{int(x['expiry'][5:7])}/{x['expiry'][8:10]}", strike=f"{float(x['strike']):g}",
                cp=x["option_type"].upper(), size=x.get("size"), price=x.get("price"),
                side=("Ask" if "ask_side" in (x.get("tags") or []) else "Bid"), sidecode=side_code(x),
                sweep="intermarket_sweep" in (x.get("report_flags") or []),
                dir=sd, premium=round(p), spot=x.get("underlying_price"), dte=dte, bucket=bkey))
            B["big"].append(big[-1])
    bull = o["bull_small"] + o["bull_big"]
    bear = o["bear_small"] + o["bear_big"]
    o.update(bull=bull, bear=bear, lean=lean(bull, bear), big_lean=lean(o["bull_big"], o["bear_big"]),
             recent_bull=rec["bull"], recent_bear=rec["bear"], recent_lean=lean(rec["bull"], rec["bear"]),
             early_lean=lean(early["bull"], early["bear"]))
    note = commentary(o, big, spots)
    o.update(note)
    o["top"] = sorted(big, key=lambda z: -z["premium"])[:5]
    o["repeat"] = repeat_note(big)
    for k, B in bk.items():
        B["lean"] = lean(B["bull"], B["bear"])
        B["top"] = sorted(B.pop("big"), key=lambda z: -z["premium"])[:3]
    o["buckets"] = bk
    marks, t0 = [], lo + dt.timedelta(minutes=WIN_MIN)
    while t0 <= hi:
        marks.append(t0)
        t0 += dt.timedelta(minutes=WIN_MIN)
    if not marks or marks[-1] < hi:
        marks.append(hi)
    cum = [[0.0, 0.0] for _ in marks]
    for t, sd, p in dirs:
        for i, mk in enumerate(marks):
            if t <= mk:
                cum[i][0 if sd == "bull" else 1] += p
    o["series"] = [[mk.strftime("%H:%M"), lean(b, r)] for mk, (b, r) in zip(marks, cum) if lean(b, r) is not None]
    rl, el = o["recent_lean"], o["early_lean"]
    rtot = rec["bull"] + rec["bear"]
    if rl is None or rtot < 100000:
        sp = ("quiet", "Quiet last 15m")
    elif el is None:
        sp = ("up_bull", "Picking up bullish") if rl >= 60 else ("up_bear", "Picking up bearish") if rl <= 40 else ("steady", "Steady")
    elif el >= 55 and rl <= 45:
        sp = ("flip_bear", "Flipping bearish")
    elif el <= 45 and rl >= 55:
        sp = ("flip_bull", "Flipping bullish")
    elif rl - el >= 10:
        sp = ("up_bull", "Picking up bullish") if rl >= 50 else ("fade", "Bear side fading")
    elif el - rl >= 10:
        sp = ("up_bear", "Picking up bearish") if rl <= 50 else ("fade", "Bull side fading")
    else:
        sp = ("steady", "Steady")
    o["speed"], o["speed_text"] = sp
    o["recent_total"] = rtot
    return o


def pick_focus(res):
    best = None
    for tk, o in res.items():
        L = o.get("lean")
        if L is None:
            continue
        d = "bull" if L >= FOCUS_LEAN else "bear" if L <= 100 - FOCUS_LEAN else None
        if not d:
            continue
        bigdir = o["bull_big"] if d == "bull" else o["bear_big"]
        bigopp = o["bear_big"] if d == "bull" else o["bull_big"]
        if bigdir < FOCUS_BIG_MIN or bigdir <= bigopp:
            continue
        accel = o["speed"] in (f"up_{d}", f"flip_{d}") or (
            o["speed"] == "steady" and o["recent_lean"] is not None and
            ((d == "bull" and o["recent_lean"] >= 60) or (d == "bear" and o["recent_lean"] <= 40)))
        if not accel:
            continue
        score = abs(L - 50) * math.log10(max(o["bull"] + o["bear"], 1))
        if not best or score > best["score"]:
            best = dict(ticker=tk, dir=d, score=round(score, 1), lean=L, big=bigdir, speed=o["speed_text"])
    return best


def vs_read(o, b):
    """Plain-English 'vs normal' from order counts against the time-of-day baseline."""
    if not b:
        return dict(code="none", text="No baseline yet")
    nb, nr = o["n_bull_small"] + o["n_bull_big"], o["n_bear_small"] + o["n_bear_big"]
    bb, br = b["n_bull_small"] + b["n_bull_big"], b["n_bear_small"] + b["n_bear_big"]
    xb = nb / bb if bb else None
    xr = nr / br if br else None
    if xb is None or xr is None:
        return dict(code="none", text="Baseline too thin")
    if xr >= 1.4 and xr >= 1.2 * xb:
        return dict(code="unusual_bear", text=f"Unusually bearish: {xr:.1f}x normal bearish orders", x=round(xr, 2))
    if xb >= 1.4 and xb >= 1.2 * xr:
        return dict(code="unusual_bull", text=f"Unusually bullish: {xb:.1f}x normal bullish orders", x=round(xb, 2))
    if xb >= 1.4 and xr >= 1.4:
        return dict(code="busy", text=f"Busy both ways: {min(xb, xr):.1f}x+ normal orders", x=round(min(xb, xr), 2))
    if xb <= 0.7 and xr <= 0.7:
        return dict(code="quiet", text=f"Quieter than normal ({max(xb, xr):.1f}x)", x=round(max(xb, xr), 2))
    if xr >= 1.2 * xb and xr > 1.0:
        return dict(code="lean_bear", text=f"Bearish tilt vs normal: bears {xr:.1f}x, bulls {xb:.1f}x", x=round(xr, 2))
    if xb >= 1.2 * xr and xb > 1.0:
        return dict(code="lean_bull", text=f"Bullish tilt vs normal: bulls {xb:.1f}x, bears {xr:.1f}x", x=round(xb, 2))
    return dict(code="normal", text=f"About normal (bulls {xb:.1f}x, bears {xr:.1f}x)", x=round(max(xb, xr), 2))


def flag_short(vs):
    """The one line the card shows for vs-normal."""
    code, text = vs.get("code"), vs.get("text") or ""
    if code == "normal":
        return "About normal"
    if code == "busy":
        return text.replace("x+ normal orders", "x normal")
    if code == "unusual_bear":
        num = text.split(": ", 1)[-1].split("x", 1)[0]
        return f"Bearish orders {num}x normal"
    if code == "unusual_bull":
        num = text.split(": ", 1)[-1].split("x", 1)[0]
        return f"Bullish orders {num}x normal"
    if code == "lean_bear":
        return "Bearish tilt vs normal"
    if code == "lean_bull":
        return "Bullish tilt vs normal"
    return text


READY_X = 1.2

_VERDICT_SHORT = {
    "one-sided lean": "no one-sided lean",
    "$200k+ money": "no $200K+ money",
    "accelerating": "not accelerating",
    "above-normal order count": "count not above normal",
}


def verdict_short(verdict):
    prefix = "Thin — missing: "
    if not verdict or not verdict.startswith(prefix):
        return verdict
    parts = [p.strip() for p in verdict[len(prefix):].split(",")]
    return "Thin: " + ", ".join(_VERDICT_SHORT.get(p, p) for p in parts)


def readiness(o):
    L = o.get("lean")
    if L is None:
        items = [dict(key=k, label=label, ok=None, detail="no data") for k, label in (
            ("lean", "One-sided lean (65%+)"), ("big", "$200K+ money on that side"),
            ("accel", "Accelerating"), ("count", "Above-normal order count"))]
        return dict(dir=None, items=items, ready=False, verdict="No data yet", short="No data yet")
    d = "bull" if L >= 50 else "bear"
    word = "bullish" if d == "bull" else "bearish"
    pct = L if d == "bull" else 100 - L
    big_d = o["bull_big"] if d == "bull" else o["bear_big"]
    big_o = o["bear_big"] if d == "bull" else o["bull_big"]
    rl = o.get("recent_lean")
    if o["speed"] == "quiet" or rl is None:
        acc = (False, "quiet last 15 min")
    else:
        ok = o["speed"] in (f"up_{d}", f"flip_{d}") or (
            o["speed"] == "steady" and ((d == "bull" and rl >= 60) or (d == "bear" and rl <= 40)))
        acc = (ok, f"{o['speed_text'].lower()}, last 15m {rl:.0f}% bull")
    b = o.get("base")
    if b:
        base_n = b[f"n_{d}_small"] + b[f"n_{d}_big"]
        now_n = o[f"n_{d}_small"] + o[f"n_{d}_big"]
        x = now_n / base_n if base_n else None
        cnt = (None, "baseline is zero") if x is None else (x >= READY_X, f"{now_n} {word} orders vs ~{base_n:.0f} normal ({x:.1f}x)")
    else:
        cnt = (None, "no baseline yet")
    items = [
        dict(key="lean", label="One-sided lean (65%+)", ok=pct >= 65, detail=f"{pct:.0f}% {word}"),
        dict(key="big", label=f"$200K+ money on the {word} side", ok=big_d >= 1_000_000 and big_d > big_o,
             detail=f"${big_d/1e6:.1f}M vs ${big_o/1e6:.1f}M other side (needs $1M+ and more)"),
        dict(key="accel", label="Accelerating", ok=acc[0], detail=acc[1]),
        dict(key="count", label="Above-normal order count", ok=cnt[0], detail=cnt[1]),
    ]
    missing = [i["label"].split(" (")[0].replace(f" on the {word} side", "") for i in items if i["ok"] is False]
    unknown = [i for i in items if i["ok"] is None]
    ready = not missing and not unknown
    verdict = ("Enough to act on" if ready else
               ("Thin — missing: " + ", ".join(missing).lower()) if missing else "Can't fully check yet (no baseline)")
    return dict(dir=d, items=items, ready=ready, verdict=verdict, short=verdict_short(verdict))


def attach_baselines(tickers, day, now):
    try:
        import baseline
        B = baseline.baseline_for(day, now)
    except Exception:
        B = {}
    for tk, o in tickers.items():
        b = B.get(tk)
        o["base"] = b
        if b:
            o["vs"] = {k: (round(o[k] / b[k], 2) if b[k] else None) for k in
                       ("n_bull_small", "n_bull_big", "n_bear_small", "n_bear_big")}
        vs = vs_read(o, b)
        vs["short"] = flag_short(vs)
        o["vs_read"] = vs
        o["ready"] = readiness(o)


def market_tide(day, now=None):
    status, data = api("market/market-tide", date=f"{day:%Y-%m-%d}")
    if status != 200 or not isinstance(data, dict):
        return None
    rows = [r for r in data.get("data", []) if r.get("date") == f"{day:%Y-%m-%d}"
            and (now is None or dt.datetime.fromisoformat(r["timestamp"]) <= now)]
    if not rows:
        return None
    r = rows[-1]
    ncp, npp = float(r["net_call_premium"]), float(r["net_put_premium"])
    t = dt.datetime.fromisoformat(r["timestamp"]).astimezone(ET)
    return dict(time=t.strftime("%H:%M"), ncp=ncp, npp=npp,
                read=("Bullish" if ncp > npp else "Bearish" if npp > ncp else "Flat"))


def _meaningful(out):
    return any((o or {}).get("n") for o in list(out["tickers"].values()) + list(out["gauges"].values()))


def compute(save=True, now=None):
    now = now or now_et()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    day = now.date()
    CALLS["n"] = 0
    out = dict(updated=now.strftime("%Y-%m-%d %H:%M"), updated_hm=now.strftime("%-I:%M %p"),
               date=f"{day:%Y-%m-%d}", source="Unusual Whales API", status="ok",
               tickers={}, gauges={}, tide=None, focus=None, errors=[])
    if not is_trading_day(day):
        out["status"] = "closed"
    elif now < session_open(day):
        out["status"] = "preopen"
    elif now > session_close(day) + CLOSE_GRACE:
        out["status"] = "closed"
    if out["status"] != "ok":
        return out
    os.makedirs(DATA, exist_ok=True)
    cache, cpath = load_cache(day)
    if cache.get("_v") != CACHE_V:
        cache = {"_v": CACHE_V}
    open_ms = int(session_open(day).timestamp() * 1000)
    until_ms = int(min(now, session_close(day)).timestamp() * 1000) + 1000
    for tk in TICKERS + GAUGES:
        c = cache.setdefault(tk, {"last_ms": open_ms, "rows": {}})
        try:
            since = max(open_ms, c["last_ms"] - 120000)
            new = pull_new(tk, since, GAUGE_MIN_PREM if tk in GAUGES else MIN_PREM, until_ms=until_ms)
            for x in new:
                if session_open(day) <= ts(x) <= session_close(day):
                    c["rows"][x["id"]] = slim(x)
            if c["rows"]:
                c["last_ms"] = max(int(ts(x).timestamp() * 1000) for x in c["rows"].values())
        except Exception as e:
            out["errors"].append(scrub(e)[:120])
        analyzed = analyze(list(c["rows"].values()), now)
        (out["gauges"] if tk in GAUGES else out["tickers"])[tk] = analyzed
    if out["errors"] and not _meaningful(out):
        # Don't replace a good latest.json with an empty failed pull.
        return out
    with open(cpath, "w") as f:
        json.dump(cache, f)
    for name in os.listdir(DATA):
        if name.startswith("trades_") and name != os.path.basename(cpath):
            try:
                os.remove(os.path.join(DATA, name))
            except OSError:
                pass
    try:
        out["tide"] = market_tide(day, now)
    except Exception as e:
        out["errors"].append(scrub(f"tide: {e}")[:100])
    attach_baselines(out["tickers"], day, now)
    out["focus"] = pick_focus(out["tickers"])
    out["api_calls"] = CALLS["n"]
    if save:
        new = not os.path.exists(SNAP)
        with open(SNAP, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "time", "ticker", "lean", "bull", "bear", "bull_big", "bear_big", "recent_lean", "speed"])
            for tk, o in {**out["tickers"], **out["gauges"]}.items():
                w.writerow([out["date"], now.strftime("%H:%M"), tk, o["lean"], round(o["bull"]), round(o["bear"]),
                            round(o["bull_big"]), round(o["bear_big"]), o["recent_lean"], o["speed"]])
        with open(LATEST, "w") as f:
            json.dump(out, f, indent=1)
            f.write("\n")
    return out


def rollover_baselines(day):
    """Add `day` to baselines.json from the feed cache. No API. Returns the source or None."""
    import baseline
    b = baseline.load()
    src = baseline.add_day(b, day, prefer="feed")
    if src == "feed":
        baseline.save(b)
    return src


def main(now=None):
    now = now or now_et()
    why = skip_reason(now)
    if why:
        stamp = now.astimezone(ET).strftime("%Y-%m-%d %H:%M %Z")
        print(f"skip: {why} ({stamp})")
        return 0
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    r = compute(now=now)
    if r["status"] != "ok":
        print("skip:", r["status"])
        return 0
    if r.get("errors") and not _meaningful(r):
        print("fetch failed; latest.json left unchanged")
        return 1
    focus = (r.get("focus") or {}).get("ticker")
    print(r["status"], r.get("api_calls"), focus, len(r.get("errors") or []))
    if now.astimezone(ET) >= session_close(now.astimezone(ET).date()) - dt.timedelta(minutes=5):
        try:
            src = rollover_baselines(now.astimezone(ET).date())
            print("baseline rollover", src or "skipped")
        except Exception:
            print("baseline rollover skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
