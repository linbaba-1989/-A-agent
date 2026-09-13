from datetime import date

from src.market_universe_audit import classify_missing_symbol, summarize_missing


def is_a_share(code):
    return code.endswith((".SH", ".SZ", ".BJ"))


def test_missing_symbol_categories_require_evidence():
    assert classify_missing_symbol("600001.SH", {"InstrumentName": "退市样本"}, is_a_share)[0] == "delisted"
    assert classify_missing_symbol("600002.SH", {"InstrumentName": "样本", "OpenDate": "20990101"},
                                   is_a_share, today=date(2026, 9, 13))[0] == "new_stock_not_listed"
    assert classify_missing_symbol("600003.SH", {"InstrumentName": "样本"}, is_a_share, suspend_flag=1)[0] == "suspended"
    assert classify_missing_symbol("600004.SH", {"InstrumentName": "样本"}, is_a_share,
                                   quote_markets={"SZ", "BJ"})[0] == "quote_permission_missing"
    assert classify_missing_symbol("600005.SH", {"InstrumentName": "样本"}, is_a_share,
                                   quote_markets={"SH"})[0] == "unknown"


def test_reason_summary_keeps_unknown():
    counts = summarize_missing([{"reason": "delisted"}, {"reason": "not_proven"}])
    assert counts["delisted"] == 1 and counts["unknown"] == 1
