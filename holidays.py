"""NYSE full-day closures and 1:00 PM early closes.

Trimmed to the calendar the flow page needs. Full-day dates are the table
previously imported from uw_earnings_vol_scan (through 2028, not extrapolated).
Early closes are the EARLY rows in market_holidays.txt. A year the table does
not cover raises, rather than guessing.
"""
import datetime as dt

# Full-day closures, month-day, by year.
_FULL = {
    2025: "01-01 01-09 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
    2028: "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25",
}

# 1:00 PM ET closes. The session still trades; it just ends at 1:00.
_EARLY = {
    "2026-11-27",
    "2026-12-24",
    "2027-11-26",
}


def full_holidays(year):
    try:
        spec = _FULL[year]
    except KeyError:
        lo, hi = min(_FULL), max(_FULL)
        raise ValueError(f"NYSE holiday table covers {lo}-{hi}, not {year}")
    return {dt.date(year, int(md[:2]), int(md[3:])) for md in spec.split()}


def is_trading_day(d):
    if d.weekday() >= 5:
        return False
    return d not in full_holidays(d.year)


def is_early_close(d):
    return f"{d:%Y-%m-%d}" in _EARLY


def session_close_hm(d):
    """(hour, minute) of the cash close. 13:00 on an early-close day, else 16:00."""
    return (13, 0) if is_early_close(d) else (16, 0)
