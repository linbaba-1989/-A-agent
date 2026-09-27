"""Endpoint-specific units; vendor symbol spellings stay in adapters."""
from datetime import date
import math
from ..historical import HistoricalBar


def number(value):
    if value is None or value == "":
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def parse_rows(rows, symbol, source, adjustment, flavor):
    bars, warnings = [], []
    chinese = flavor == "akshare_em"
    for row in rows:
        try:
            raw_date = row.get("日期" if chinese else "date")
            day = date.fromisoformat(str(raw_date)[:10])
            if flavor == "baostock":
                expected = {"raw": "3", "qfq": "2", "hfq": "1"}[adjustment]
                if row.get("adjustflag") not in (None, "", expected):
                    raise ValueError("adjustment_mismatch")
                if row.get("code") not in (None, "", symbol[-2:].lower() + "." + symbol[:6]):
                    raise ValueError("symbol_mismatch")
            mapping = {"open": "开盘", "high": "最高", "low": "最低", "close": "收盘"}
            values = {k: number(row.get(v if chinese else k)) for k,v in mapping.items()}
            volume = number(row.get("成交量" if chinese else "volume"))
            # EM docs: lots; Sina equity and BaoStock: shares.
            if volume is not None and chinese:
                volume *= 100
            amount = number(row.get("成交额" if chinese else "amount"))
            if flavor == "akshare_index_sina":
                volume = None  # Index aggregate volume unit not independently verified.
            prev = number(row.get("preclose") if flavor == "baostock" else row.get("prev_close"))
            pct = number(row.get("pctChg") if flavor == "baostock" else row.get("涨跌幅"))
            bars.append(HistoricalBar(symbol, day, **values, volume_shares=volume,
                                      amount_cny=amount, prev_close=prev, pct_change=pct,
                                      source=source, adjustment=adjustment))
        except (ValueError, TypeError) as exc:
            warnings.append("parse_error:" + str(exc))
    return bars, warnings
