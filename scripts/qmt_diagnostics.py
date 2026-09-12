"""Detailed, read-only QMT acceptance diagnostic."""
import json
from pathlib import Path
import platform
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.qmt_provider import QMTProvider, TEST_SYMBOLS  # noqa: E402
from src.scanner import MarketScanner  # noqa: E402


def show(label, value):
    print(f"{label}：{json.dumps(value, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    load_dotenv()
    provider = QMTProvider()
    diagnostic = provider.connection_diagnostics()
    runtime = None
    try:
        runtime = provider.runtime_info()
    except Exception as exc:
        show("xtquant运行时诊断错误", repr(exc))
    show("QMT安装目录", provider.qmt_path or None)
    show("xtquant来源", diagnostic.xtquant_path)
    show("Python版本", platform.python_version())
    show("xtquant导入", diagnostic.xtquant_imported)
    show("Windows QMT相关进程", diagnostic.processes)
    show("process_detected", diagnostic.process_detected)
    show("XtData RPC请求", diagnostic.rpc_request_success)
    show("测试代码", TEST_SYMBOLS)
    show("get_full_tick成功", diagnostic.full_tick_success)
    if diagnostic.xtquant_imported and diagnostic.rpc_request_success:
        try:
            ticks = provider.get_full_ticks(TEST_SYMBOLS)
            show("get_full_tick返回", {code: provider.normalize_tick(code, tick) for code, tick in ticks.items()})
        except Exception as exc:
            show("get_full_tick错误原文", repr(exc))
    show("错误原文", diagnostic.raw_error)
    show("xtquant组件与构建信息", runtime)
    pythonw = Path(provider.qmt_path) / "bin.x64" / "pythonw.exe" if provider.qmt_path else None
    show("QMT内置Python", {"path": str(pythonw) if pythonw else None,
                            "exists": bool(pythonw and pythonw.exists()),
                            "minimal_api_test": "requires_QMT_strategy_context"})
    if not diagnostic.connected:
        raise SystemExit(1)

    scanner = MarketScanner(provider)
    xtdata = provider._load_xtdata()
    capital_samples = {}
    for code in ("000001.SZ", "600000.SH", "600519.SH"):
        capital_samples[code] = {}
        for complete in (False, True):
            try:
                detail = xtdata.get_instrument_detail(code, complete) or {}
                selected = {key: detail.get(key) for key in ("InstrumentName", "PreClose", "FloatVolume", "TotalVolume",
                            "VolumeMultiple", "InstrumentStatus", "IsTrading", "OpenDate")}
                try:
                    selected["capital_validation"] = "valid" if float(selected["FloatVolume"]) > 1_000_000 and float(selected["TotalVolume"]) >= float(selected["FloatVolume"]) else "instrument_capital_invalid"
                except (TypeError, ValueError):
                    selected["capital_validation"] = "instrument_capital_invalid"
                capital_samples[code][str(complete)] = selected
            except Exception as exc:
                capital_samples[code][str(complete)] = {"error": repr(exc)}
    show("三只股票合约股本测试", capital_samples)
    benchmark = scanner.compare_tick_strategies()
    show("全市场与分批性能", benchmark)
    result = scanner.scan(top_n=10)
    show("成交量单位字段", scanner.volume_field)
    show("成交量转股乘数", scanner.volume_multiplier)
    show("成交量单位证据", scanner.volume_unit_evidence)
    show("全A验收指标", result.diagnostics.to_dict())
    show("10只股票实测数据", result.rows)
