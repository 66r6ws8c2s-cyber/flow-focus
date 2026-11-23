"""Offline checks for the call line, the session guard, and the page shell.

No API key and no network. Writes fixtures/preview.json so the page can be opened
with ?fixture=fixtures/preview.json.
"""
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compute as c
import holidays
import q

DAY = dt.date(2026, 11, 20)
NOW = dt.datetime(2026, 11, 20, 10, 15, tzinfo=c.ET)
_N = {"n": 0}


def at(h, m, day=DAY):
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=c.ET)


def make(when, prem, spot, bull, strike="100", expiry="2027-01-15", cond="slan", canceled=False):
    _N["n"] += 1
    return {
        "id": f"t{_N['n']}",
        "executed_at": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "premium": prem,
        "option_type": "call",
        "strike": str(strike),
        "expiry": expiry,
        "tags": [] if cond[:1] in ("m", "t") else (["ask_side"] if bull else ["bid_side"]),
        "size": max(1, int(round(prem / 500))),
        "price": "5.00",
        "underlying_price": f"{float(spot):.2f}",
        "canceled": canceled,
        "report_flags": [],
        "nbbo_bid": "4.90",
        "nbbo_ask": "5.00",
        "cond": cond,
    }


def book(bull, path, big=2_000_000, opp=200_000):
    """Directional book plus a price path. path is (hour, minute, spot), earliest first."""
    rows = []
    h, m, px = path[0]
    rows.append(make(at(h, m), opp, px, bull=not bull, strike="90"))
    for i in range(6):
        rows.append(make(at(h, m + 1 + i), 150_000, px, bull=bull, strike=300 + i))
    for h, m, px in path[1:-1]:
        rows.append(make(at(h, m), 150_000, px, bull=bull, strike="150"))
    h, m, px = path[-1]
    rows.append(make(at(h, m), big, px, bull=bull, strike="160"))
    return rows


def balanced():
    rows = []
    for i in range(4):
        rows.append(make(at(9, 31 + i), 120_000, 100, True, strike=100 + i))
        rows.append(make(at(9, 31 + i), 120_000, 100, False, strike=110 + i))
    rows.append(make(at(9, 40), 500_000, 100, True, strike="130"))
    rows.append(make(at(9, 41), 500_000, 100, False, strike="140"))
    return rows


SCENARIOS = {
    "AMD": ("Nothing significant", None, balanced()),
    "NVDA": ("Bulls absorbing the selling near 95.10, could flip up", "bull",
             book(True, [(9, 31, 100), (9, 50, 97), (10, 5, 95.10)])),
    "MSFT": ("Whales buying the dip", "bull",
             book(True, [(9, 31, 100), (9, 45, 96), (10, 8, 99)])),
    "AAPL": ("Big money leaning bullish", "bull",
             book(True, [(9, 31, 100), (10, 10, 103)])),
    "TSLA": ("Bears selling into the high near 105.00", "bear",
             book(False, [(9, 31, 100), (9, 48, 99.50), (10, 12, 105)])),
    "META": ("Whales selling the rip", "bear",
             book(False, [(9, 31, 100), (9, 50, 108), (10, 9, 103)])),
    "MU": ("Big money leaning bearish", "bear",
             book(False, [(9, 31, 100), (9, 55, 98.50), (10, 11, 97)])),
}


def build_preview():
    out = dict(updated="2026-11-20 10:15", updated_hm="10:15 AM", date="2026-11-20",
               source="synthetic fixture", status="ok", tickers={}, gauges={
                   "SPY": {"lean": 54.4, "bull": 1, "bear": 1},
                   "QQQ": {"lean": 45.2, "bull": 1, "bear": 1},
               }, tide={"time": "10:15", "ncp": -1_000_000.0, "npp": 2_000_000.0, "read": "Bearish"},
               focus=None, errors=[])
    for tk, (_, _, rows) in SCENARIOS.items():
        out["tickers"][tk] = c.analyze(rows, NOW)
    c.attach_baselines(out["tickers"], DAY, NOW)
    out["focus"] = c.pick_focus(out["tickers"])
    return out


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)


def test_phrases(preview):
    for tk, (text, direction, _) in SCENARIOS.items():
        o = preview["tickers"][tk]
        check(o["call"] == text, f"{tk} call {o['call']!r} != {text!r} (lean {o['lean']} px {o.get('px')})")
        check(o["call_dir"] == direction, f"{tk} dir {o['call_dir']!r}")
        if "near" in text:
            level = text.split("near ", 1)[1].split(",", 1)[0]
            key = "low" if direction == "bull" else "high"
            check(f"{o['px'][key]:.2f}" == level, f"{tk} level {level} != px {o['px']}")


def test_rules():
    # Lean alone is not enough: big money is balanced.
    rows = balanced() + [make(at(9, 42), 80_000, 100, True, strike="131")]
    o = c.analyze(rows, NOW)
    check(o["call"] == "Nothing significant", f"balanced call {o['call']!r} lean {o['lean']}")

    # 1.5x $200K+ premium is not "clearly" one-sided, and there is no repeat.
    rows = [
        make(at(9, 32), 1_500_000, 100, True, strike="180"),
        make(at(9, 33), 1_000_000, 101, False, strike="190"),
    ]
    for i in range(12):
        rows.append(make(at(9, 36), 150_000, 100, True, strike=400 + i))
    rows.append(make(at(9, 37), 100_000, 100, False, strike="210"))
    o = c.analyze(rows, NOW)
    check(o["lean"] >= 65, f"lean {o['lean']}")
    check(o["bull_big"] < c.CLEAR_RATIO * o["bear_big"], "expected ratio under 2")
    check(o["call"] == "Nothing significant", f"1.5x should stay quiet, got {o['call']!r}")

    # Same contract twice can qualify when the ratio is under 2x but that side is ahead.
    rows = [
        make(at(9, 35), 450_000, 100, True, strike="180"),
        make(at(9, 50), 450_000, 101, True, strike="180"),
        make(at(9, 40), 600_000, 100.5, False, strike="190"),
    ]
    for i in range(10):
        rows.append(make(at(9, 36), 150_000, 100, True, strike=500 + i))
    rows.append(make(at(9, 42), 100_000, 100, False, strike="210"))
    o = c.analyze(rows, NOW)
    check(o["lean"] >= 65, f"repeat lean {o['lean']}")
    check(o["bull_big"] < c.CLEAR_RATIO * o["bear_big"], "repeat case should not take the ratio path")
    check(o["bull_big"] > o["bear_big"], "repeat side should still be ahead")
    check(o["call_dir"] == "bull", f"repeat call {o['call']!r}")

    # Repeats on the other side do not make a call.
    rows = [make(at(9, 40), 250_000, 100, True, strike="180")]
    rows.append(make(at(9, 41), 300_000, 100, False, strike="190"))
    rows.append(make(at(9, 48), 300_000, 100, False, strike="190"))
    for i in range(14):
        rows.append(make(at(9, 36), 150_000, 100, True, strike=600 + i))
    o = c.analyze(rows, NOW)
    check(o["lean"] is not None and o["lean"] >= 65, f"other-side lean {o['lean']}")
    check(o["call"] == "Nothing significant", f"other-side repeat fired {o['call']!r}")

    # No underlying price: still a call, with no invented level.
    rows = [make(at(9, 32), 2_000_000, 0, True, strike="180")]
    rows[0]["underlying_price"] = None
    for i in range(4):
        r = make(at(9, 36), 150_000, 0, True, strike=700 + i)
        r["underlying_price"] = None
        rows.append(r)
    o = c.analyze(rows, NOW)
    check(o["call"] == "Big money leaning bullish", o["call"])
    check("px" not in o, "px invented without prints")

    # A canceled print cannot set the session low.
    rows = book(True, [(9, 31, 100), (9, 50, 97), (10, 5, 95.10)])
    rows.append(make(at(9, 33), 50_000, 1.00, True, strike="90", canceled=True))
    o = c.analyze(rows, NOW)
    check(o["call"].endswith("95.10, could flip up"), o["call"])
    check(o["px"]["low"] == 95.10, str(o["px"]))

    # Multi-leg premium is skipped, but its underlying still counts as a print.
    rows = [
        make(at(9, 31), 80_000, 100, True, strike="100"),
        make(at(10, 10), 80_000, 100, True, strike="101"),
        make(at(9, 45), 5_000_000, 90, True, strike="50", cond="mlet"),
    ]
    o = c.analyze(rows, NOW)
    check(o["skipped_multileg"] == 5_000_000, str(o["skipped_multileg"]))
    check(o["px"]["low"] == 90.0, str(o["px"]))
    check(o["call"] == "Nothing significant", o["call"])


def test_guard_and_calendar():
    check(c.skip_reason(dt.datetime(2026, 11, 21, 10, 0, tzinfo=c.ET)) == "weekend or holiday", "saturday")
    check(c.skip_reason(dt.datetime(2026, 11, 26, 10, 0, tzinfo=c.ET)) == "weekend or holiday", "thanksgiving")
    check(c.skip_reason(dt.datetime(2026, 11, 23, 8, 0, tzinfo=c.ET)) == "before the open", "preopen")
    check(c.skip_reason(dt.datetime(2026, 11, 23, 9, 30, tzinfo=c.ET)) is None, "open")
    check(c.skip_reason(dt.datetime(2026, 11, 23, 16, 5, tzinfo=c.ET)) is None, "grace")
    check(c.skip_reason(dt.datetime(2026, 11, 23, 16, 20, tzinfo=c.ET)) == "after the close", "after")
    check(c.skip_reason(dt.datetime(2026, 11, 27, 12, 0, tzinfo=c.ET)) is None, "early still open")
    check(c.skip_reason(dt.datetime(2026, 11, 27, 13, 20, tzinfo=c.ET)) == "after the close", "early closed")
    check(holidays.is_early_close(dt.date(2026, 11, 27)), "early flag")
    check(not holidays.is_trading_day(dt.date(2026, 12, 25)), "christmas")
    check(holidays.is_trading_day(dt.date(2026, 11, 23)), "monday")
    check(c.skip_reason(dt.datetime(2026, 7, 6, 9, 30, tzinfo=c.ET)) is None, "summer open")
    try:
        holidays.full_holidays(2029)
        raise SystemExit("FAIL: 2029 should raise")
    except ValueError:
        pass
    before = open(os.path.join(HERE, "latest.json"), "rb").read()
    called = []
    real = q.g
    q.g = lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("api"))
    try:
        code = c.main(now=dt.datetime(2026, 11, 21, 11, 0, tzinfo=c.ET))
    finally:
        q.g = real
    check(code == 0, "saturday main")
    check(called == [], "saturday called the api")
    check(open(os.path.join(HERE, "latest.json"), "rb").read() == before, "saturday rewrote latest.json")


def test_copy():
    check(c.verdict_short("Thin — missing: one-sided lean, accelerating") == "Thin: no one-sided lean, not accelerating", "verdict")
    check(c.verdict_short("Thin — missing: one-sided lean, $200k+ money, accelerating")
          == "Thin: no one-sided lean, not $1M+ ahead in $200K+ money, not accelerating", "verdict money")
    check(c.verdict_short("Thin — missing: under $1M of $200K+ money, not accelerating")
          == "Thin: under $1M of $200K+ money, not accelerating", "verdict under 1M")
    check(c.verdict_short("Enough to act on") == "Enough to act on", "ready verdict")
    spot = c.readiness(dict(lean=80.4, bull_big=834081, bear_big=0, speed="quiet", recent_lean=None,
                           speed_text="Quiet last 15m", n_bull_small=17, n_bull_big=1, n_bear_small=0, n_bear_big=0))
    check("under $1M of $200K+ money" in spot["short"] and "no $200K+ money" not in spot["short"], "spot short "+spot["short"])
    aapl = c.readiness(dict(lean=51.2, bull_big=10_004_095, bear_big=13_631_675, speed="quiet", recent_lean=None,
                           speed_text="Quiet last 15m", n_bull_small=1, n_bull_big=1, n_bear_small=1, n_bear_big=1))
    check("other side has more $200K+ money" in aapl["short"] and "no $200K+ money" not in aapl["short"], "aapl short "+aapl["short"])
    shop = c.readiness(dict(lean=32.6, bull_big=0, bear_big=0, speed="quiet", recent_lean=None,
                           speed_text="Quiet last 15m", n_bull_small=1, n_bull_big=0, n_bear_small=1, n_bear_big=0))
    check("no $200K+ money" in shop["short"], "shop short "+shop["short"])
    check(c.flag_short(dict(code="normal", text="About normal (bulls 1.3x, bears 1.4x)")) == "About normal", "flag")
    check(c.flag_short(dict(code="busy", text="Busy both ways: 1.4x+ normal orders")) == "Busy both ways: 1.4x normal", "busy")
    check(c.flag_short(dict(code="unusual_bear", text="Unusually bearish: 1.5x normal bearish orders"))
          == "Bearish orders 1.5x normal", "unusual")
    os.environ.pop("UW_API_KEY", None)
    try:
        q.g("option-trades")
        raise SystemExit("FAIL: missing key should raise")
    except RuntimeError as e:
        check("UW_API_KEY" in str(e) and "Bearer" not in str(e), "key error text")
    check("abcdef" not in q.scrub("Bearer abcdef"), "scrub")
    html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
    for needle in ("class=\"call", "more-d", "Nothing significant", "rule-based", "not trade advice",
                   "latest.json", "fixtures/preview.json", "ff_more"):
        check(needle in html, "index missing " + needle)
    yml = open(os.path.join(HERE, ".github/workflows/refresh.yml"), encoding="utf-8").read()
    for needle in ("workflow_dispatch", "secrets.UW_API_KEY", "latest.json", "baselines.json",
                   "*/15 13-20 * * 1-5", "0 21 * * 1-5"):
        check(needle in yml, "workflow missing " + needle)
    for line in yml.splitlines():
        if "echo" in line:
            check("UW_API_KEY" not in line and "secrets." not in line, "echo of a secret")
    check("git add -- latest.json baselines.json" in yml, "explicit git add")


def test_preview_file(preview):
    path = os.path.join(HERE, "fixtures", "preview.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(preview, f, indent=1)
        f.write("\n")
    again = json.load(open(path))
    for tk, (text, _, _) in SCENARIOS.items():
        check(again["tickers"][tk]["call"] == text, "fixture drift " + tk)
    # Every call is one of the allowed sentences.
    allowed = (
        "Nothing significant",
        "Whales buying the dip",
        "Whales selling the rip",
        "Big money leaning bullish",
        "Big money leaning bearish",
    )
    for tk, o in again["tickers"].items():
        call = o["call"]
        ok = call in allowed or call.startswith("Bulls absorbing the selling near ") or call.startswith("Bears selling into the high near ")
        check(ok, "unexpected call " + call)


def main():
    preview = build_preview()
    test_phrases(preview)
    test_rules()
    test_guard_and_calendar()
    test_copy()
    test_preview_file(preview)
    subprocess.check_call([sys.executable, "-m", "py_compile", "compute.py", "q.py", "holidays.py", "baseline.py"])
    print("ok", {tk: preview["tickers"][tk]["call"] for tk in SCENARIOS})
    print("focus", preview["focus"])


if __name__ == "__main__":
    main()
