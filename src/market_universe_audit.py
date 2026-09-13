"""Evidence-based raw/active A-share universe audit."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable


REASONS = (
    "delisted", "listing_suspended", "suspended", "new_stock_not_listed",
    "beijing_code_compatibility", "quote_permission_missing",
    "non_a_share_misclassified", "sector_history_residual", "unknown",
)


def market_of(code: str) -> str:
    return code.rsplit(".", 1)[-1] if "." in code else "unknown"


def parse_date(value: Any) -> date | None:
    text = str(value or "").replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def classify_missing_symbol(code: str, detail: dict[str, Any] | None,
                            is_a_share: Callable[[str], bool], suspend_flag: Any = None,
                            quote_markets: set[str] | None = None,
                            today: date | None = None) -> tuple[str, dict[str, Any]]:
    evidence = {"market": market_of(code)}
    if not is_a_share(code):
        return "non_a_share_misclassified", evidence
    if not detail:
        return "unknown", {**evidence, "instrument_detail": "unavailable"}
    name = str(detail.get("InstrumentName") or detail.get("name") or "")
    status = str(detail.get("InstrumentStatus") or "")
    evidence.update({"name": name, "InstrumentStatus": status or None,
                     "OpenDate": detail.get("OpenDate"), "ExpireDate": detail.get("ExpireDate")})
    if "退市" in name or "退市" in status or "delist" in status.lower():
        return "delisted", evidence
    if "暂停上市" in name or "暂停上市" in status:
        return "listing_suspended", evidence
    opened = parse_date(detail.get("OpenDate"))
    if opened and opened > (today or date.today()):
        return "new_stock_not_listed", evidence
    expired = parse_date(detail.get("ExpireDate"))
    if expired and expired < (today or date.today()):
        return "sector_history_residual", evidence
    try:
        if int(suspend_flag) == 1:
            return "suspended", {**evidence, "suspendFlag": 1}
    except (TypeError, ValueError):
        pass
    market = market_of(code)
    if quote_markets is not None and market not in quote_markets:
        return "quote_permission_missing", {**evidence, "connected_quote_markets": sorted(quote_markets)}
    exchange = str(detail.get("ExchangeID") or detail.get("exchangeID") or "").upper()
    if market == "BJ" and exchange and exchange not in {"BJ", "BSE"}:
        return "beijing_code_compatibility", {**evidence, "ExchangeID": exchange}
    return "unknown", evidence


def summarize_missing(missing: list[dict[str, Any]]) -> dict[str, int]:
    counts = {reason: 0 for reason in REASONS}
    for row in missing:
        reason = row.get("reason", "unknown")
        counts[reason if reason in counts else "unknown"] += 1
    return counts
