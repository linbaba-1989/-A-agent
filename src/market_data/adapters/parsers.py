"""Read-only public quote formats verified in P2.0; isolate malformed records."""
import math
import re
from datetime import datetime
from ...market_clock import BEIJING_TZ
from ..contracts import MarketSnapshot, canonical_symbol


def from_wire(value):
    match = re.fullmatch(r"(sh|sz|bj)(\d{6})", value.lower())
    if not match:
        raise ValueError("invalid_vendor_symbol")
    return match[2] + "." + match[1].upper()


def to_wire(value):
    code, exchange = canonical_symbol(value).split(".")
    return exchange.lower() + code


def number(value, multiplier=1, nonnegative=False):
    try:
        result = float(value) * multiplier
        if not math.isfinite(result) or (nonnegative and result < 0):
            return None
        return result
    except (ValueError, TypeError):
        return None


def timestamp(value, fmt):
    try:
        return datetime.strptime(value, fmt).replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        return None


def parse_tencent(text):
    rows, errors = {}, []
    for match in re.finditer(r'v_((?:sh|sz|bj)\d{6})="([^"]*)";?', text):
        symbol = from_wire(match[1])
        fields = match[2].split("~")
        if len(fields) < 35 or fields[2] != symbol[:6]:
            errors.append(symbol + ":malformed_record")
            continue
        def n(index, scale=1, positive=False):
            return number(fields[index] if index < len(fields) else None, scale, positive)
        price, previous = n(3, positive=True), n(4, positive=True)
        rows[symbol] = MarketSnapshot(
            symbol=symbol, name=fields[1] or None, price=price, prev_close=previous,
            open=n(5, positive=True), high=n(33, positive=True), low=n(34, positive=True),
            change=n(31), pct_change=n(32), volume_shares=n(6, 100, True),
            amount_cny=n(37, 10000, True), turnover_rate=n(38, positive=True),
            volume_ratio=n(49, positive=True), total_market_cap=n(45, 1e8, True),
            float_market_cap=n(44, 1e8, True),
            quote_time=timestamp(fields[30], "%Y%m%d%H%M%S"), source="tencent")
    return rows, errors


def parse_sina(text):
    rows, errors = {}, []
    for match in re.finditer(r'hq_str_((?:sh|sz|bj)\d{6})="([^"]*)";?', text):
        symbol = from_wire(match[1])
        fields = match[2].split(",")
        if len(fields) < 32:
            errors.append(symbol + ":malformed_record")
            continue
        def n(index):
            return number(fields[index], nonnegative=True)
        price, previous = n(3), n(2)
        change = price - previous if price is not None and previous is not None else None
        pct = change / previous * 100 if change is not None and previous > 0 else None
        rows[symbol] = MarketSnapshot(
            symbol=symbol, name=fields[0] or None, price=price, prev_close=previous,
            open=n(1), high=n(4), low=n(5), change=change, pct_change=pct,
            volume_shares=n(8), amount_cny=n(9),
            quote_time=timestamp(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S"),
            source="sina")
    return rows, errors
