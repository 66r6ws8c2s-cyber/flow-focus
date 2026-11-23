"""Time-of-day baselines: what a normal session looks like by each 15-min slot.

Per ticker and slot: bullish/bearish order counts and premium for the under-$200K
and $200K+ tiers, same filters as compute.py. Stored in baselines.json.

  python3 baseline.py rollover [YYYY-MM-DD]   # add that day from data/trades_YYYYMMDD.json
  python3 baseline.py show AAPL 10:15 [YYYY-MM-DD]

Rollover reads the feed cache only (no API). Tape backfill needs a tape module
this repo does not ship; without it, a tape day is skipped.
"""
import os
import sys
import json
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import compute as c

BASE = os.path.join(HERE, "baselines.json")
SLOTS = [f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 15, 30, 45) if "09:45" <= f"{h:02d}:{m:02d}" <= "16:00"]
KEYS = ["n_bull_small", "n_bull_big", "n_bear_small", "n_bear_big", "bull_small", "bull_big", "bear_small", "bear_big"]
N_DAYS = 10
TAPE_GAP = 0.5


def load():
    if os.path.exists(BASE):
        return json.load(open(BASE))
    return {"version": 1, "slots": SLOTS, "keys": KEYS, "calibration": {}, "days": {}}


def save(b):
    tmp = BASE + ".tmp"
    json.dump(b, open(tmp, "w"))
    os.replace(tmp, BASE)


def slot_metrics(rows, day):
    """{slot: [8 values]} cumulative from 9:30 to each slot."""
    lo = c.session_open(day)
    buckets = [[0.0] * 8 for _ in SLOTS]
    for x in rows:
        if x.get("canceled"):
            continue
        t = c.ts(x)
        if not (lo <= t <= c.session_close(day)) or c.multi_leg(x) or c.deep_itm(x):
            continue
        sd = c.side(x)
        if not sd:
            continue
        p = float(x["premium"])
        tier = "big" if p >= c.BIG else "small"
        mins = (t - lo).total_seconds() / 60
        i = max(0, min(len(SLOTS) - 1, int(-(-mins // 15)) - 1))
        buckets[i][KEYS.index(f"n_{sd}_{tier}")] += 1
        buckets[i][KEYS.index(f"{sd}_{tier}")] += p
    out, run = {}, [0.0] * 8
    for s, bkt in zip(SLOTS, buckets):
        run = [a + d for a, d in zip(run, bkt)]
        out[s] = [round(v, 2) for v in run]
    return out


def feed_rows(day):
    p = os.path.join(c.DATA, f"trades_{day:%Y%m%d}.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    if d.get("_v") != c.CACHE_V:
        return None
    rows = {tk: list(d[tk]["rows"].values()) for tk in c.TICKERS if tk in d}
    last = max((c.ts(x) for r in rows.values() for x in r), default=None)
    if not last or last < c.session_close(day) - dt.timedelta(minutes=5):
        return None
    return rows


def tape_rows(day):
    try:
        import tape
    except ImportError:
        return None
    T = tape.load(day, min_prem=0, tickers=set(c.TICKERS))
    if T is None:
        return None
    return {tk: tape.aggregate(T.get(tk, []), TAPE_GAP) for tk in c.TICKERS}


def add_day(b, day, prefer="feed"):
    rows, src = (feed_rows(day), "feed") if prefer == "feed" else (None, None)
    if rows is None:
        rows, src = tape_rows(day), "tape"
    if rows is None:
        return None
    tks = {}
    for tk in c.TICKERS:
        m = slot_metrics(rows.get(tk, []), day)
        if src == "tape":
            f = b["calibration"]["factors"][tk]
            for s in m:
                m[s] = [round(v * f[("n_" if k.startswith("n_") else "") + k.rsplit("_", 1)[1]], 2) for v, k in zip(m[s], KEYS)]
        tks[tk] = m
    b["days"][f"{day}"] = {"source": src if src == "feed" else f"tape x calibration({b['calibration']['day']})", "tickers": tks}
    for k in sorted(b["days"])[:-20]:
        del b["days"][k]
    return src


def slot_for(now):
    hm = now.strftime("%H:%M")
    past = [s for s in SLOTS if s <= hm]
    return past[-1] if past else None


def baseline_for(day, now, b=None):
    """{ticker: averages} from the N_DAYS trading days before `day`, at the slot <= now."""
    b = b or load()
    s = slot_for(now)
    if not s:
        return {}
    days = [d for d in sorted(b["days"]) if d < f"{day}"][-N_DAYS:]
    out = {}
    for tk in c.TICKERS:
        vals = [b["days"][d]["tickers"][tk][s] for d in days if tk in b["days"][d]["tickers"]]
        if not vals:
            continue
        avg = {k: round(sum(v[i] for v in vals) / len(vals), 1) for i, k in enumerate(KEYS)}
        r = {}
        for tier in ("small", "big"):
            rs = [v[KEYS.index(f"n_bull_{tier}")] / v[KEYS.index(f"n_bear_{tier}")] for v in vals if v[KEYS.index(f"n_bear_{tier}")] > 0]
            r[f"ratio_{tier}"] = round(sum(rs) / len(rs), 2) if rs else None
        out[tk] = dict(slot=s, days=len(vals), first=days[0], last=days[-1], **avg, **r)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    b = load()
    if cmd == "rollover":
        day = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else c.now_et().date()
        if c.is_trading_day(day):
            src = add_day(b, day, prefer="feed")
            if src == "feed":
                save(b)
            print(day, "rollover", src)
    elif cmd == "show":
        tk, hm = sys.argv[2], sys.argv[3]
        day = dt.date.fromisoformat(sys.argv[4]) if len(sys.argv) > 4 else c.now_et().date()
        now = dt.datetime.combine(day, dt.time.fromisoformat(hm), tzinfo=c.ET)
        print(json.dumps(baseline_for(day, now, b).get(tk), indent=1))
    else:
        print("usage: baseline.py rollover [YYYY-MM-DD] | show TICKER HH:MM [YYYY-MM-DD]")
