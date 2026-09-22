from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_realtime_ui_surfaces_use_the_unified_quote_status_display():
    surfaces = (
        ROOT / "app.py",
        ROOT / "ui" / "components" / "market_status.py",
        ROOT / "ui" / "components" / "topbar.py",
        ROOT / "ui" / "pages" / "realtime_market.py",
        ROOT / "ui" / "pages" / "stock_research.py",
        ROOT / "ui" / "pages" / "watchlist.py",
    )
    for path in surfaces:
        assert "quote_status_display" in path.read_text(encoding="utf-8"), path

    assert "apply_realtime_quote_status" not in (ROOT / "app.py").read_text(encoding="utf-8")
    assert "display_status['market']" in (ROOT / "ui" / "components" / "market_status.py").read_text(encoding="utf-8")


def test_realtime_page_does_not_use_a_single_symbol_quote_as_market_status():
    source = (ROOT / "ui" / "pages" / "realtime_market.py").read_text(encoding="utf-8")
    assert "ranked[0].get('quote_time')" not in source
    assert "market_quote_timestamp(rows, feed.last_quote_timestamp)" in source
