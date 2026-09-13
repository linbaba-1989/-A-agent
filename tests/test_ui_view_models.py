from types import SimpleNamespace
from pathlib import Path

from ui.view_models import (cn_change_color, display_value, filter_scan_rows, market_label,
                            format_amount, format_number, format_security_status, public_market_status,
                            normalize_stock_name, RANGE_PLACEHOLDERS, raw_json_expanded_default,
                            role_state, safe_error)


def test_a_share_change_colors_are_red_up_green_down():
    assert cn_change_color(1) == "#d92d20"
    assert cn_change_color(-1) == "#079455"
    assert cn_change_color(0) == "#7a8494"


def test_market_closed_and_unavailable_are_not_invented():
    assert market_label("closed") == "已收盘"
    assert market_label(None) == "unavailable"
    assert display_value("unavailable") == "--"


def test_public_provider_status_contains_no_token_value():
    selection = SimpleNamespace(name="XtDataCenter Token", status="connected",
                                qmt_fallback_status="configured / unavailable")
    status = public_market_status(selection, {"market_status": "closed", "raw_count": 6049,
                                              "active_count": 5557, "valid_tick_count": 5557})
    assert status["provider"] == "XtDataCenter Token"
    assert status["provider"] != "QMT Local"
    assert status["qmt_fallback"] == "configured / unavailable"
    assert "secret-token" not in repr(status)


def test_ai_states_and_user_friendly_errors():
    assert role_state("working") == "运行中"
    assert role_state("degraded") == "降级"
    assert safe_error("schema_validation_failed")[0] == "模型返回结构异常，系统已尝试修复"
    assert safe_error("TimeoutError")[0] == "模型响应超时"


def test_raw_json_is_collapsed_by_default():
    assert raw_json_expanded_default() is False


def test_scan_filter_does_not_treat_unavailable_as_a_number():
    rows = [{"symbol": "600001.SH", "last_price": 10, "change_pct": 2},
            {"symbol": "000001.SZ", "last_price": "unavailable", "change_pct": -1}]
    result = filter_scan_rows(rows, {"market": "沪市", "price_min": 5})
    assert [row["symbol"] for row in result] == ["600001.SH"]


def test_scanner_display_formatting():
    assert format_number(40.756) == "40.76"
    assert format_number("unavailable") == "--"
    assert format_amount(4_190_976_500) == "41.91亿"
    assert format_amount(12_500) == "1.25万"
    assert format_amount(9999) == "9999.00元"
    assert format_security_status("normal") == "正常"
    assert format_security_status("unknown") == "未知"


def test_scanner_filter_reads_real_last_price_field():
    rows = [{"symbol": "600498.SH", "lastPrice": 40.75}]
    assert filter_scan_rows(rows, {"market": "全部", "price_min": 40}) == rows


def test_stock_name_decodes_supported_bytes_and_preserves_unicode():
    expected = "烽火通信"
    assert normalize_stock_name(expected) is expected
    assert normalize_stock_name(expected.encode("utf-8")) == expected
    assert normalize_stock_name(expected.encode("gbk")) == expected
    assert normalize_stock_name(expected.encode("gb18030")) == expected


def test_stock_name_replacement_character_falls_back_without_garbage():
    assert normalize_stock_name("���", "600498.SH") == "600498.SH"
    assert normalize_stock_name(b"\xff", "") == "--"


def test_range_controls_use_chinese_minimum_and_maximum_labels():
    assert RANGE_PLACEHOLDERS == ("最低", "最高")
    assert not ({"min", "max", "明", "马克斯"} & set(RANGE_PLACEHOLDERS))
    scanner_source = (Path(__file__).parents[1] / "ui" / "pages" / "scanner.py").read_text(encoding="utf-8")
    assert 'placeholder="min"' not in scanner_source
    assert 'placeholder="max"' not in scanner_source
    assert "马克斯" not in scanner_source
