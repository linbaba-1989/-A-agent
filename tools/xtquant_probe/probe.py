"""Isolated read-only xtquant compatibility probe. Run in a fresh process."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter

CODES = ["000001.SZ", "600000.SH", "600519.SH"]
FIELDS = ["InstrumentName", "PreClose", "FloatVolume", "TotalVolume", "VolumeMultiple",
          "OpenDate", "InstrumentStatus", "IsTrading"]


def plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return repr(value)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_capital(detail):
    try:
        floating, total = float(detail["FloatVolume"]), float(detail["TotalVolume"])
        return floating > 1_000_000 and total >= floating
    except (KeyError, TypeError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-parent", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    parent = Path(args.package_parent).resolve()
    package = parent / "xtquant"
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(package))
    sys.path.insert(0, str(parent))

    report = {"label": args.label, "python_executable": sys.executable, "python_version": sys.version,
              "package_parent": str(parent), "codes": CODES}
    try:
        import xtquant
        from xtquant import xtdata
        report.update(xtquant_file=str(Path(xtquant.__file__).resolve()), xtdata_file=str(Path(xtdata.__file__).resolve()))
        native = package / "IPythonApiClient.cp311-win_amd64.pyd"
        report["native"] = {"path": str(native), "size": native.stat().st_size,
                            "modified": native.stat().st_mtime, "sha256": sha256(native)}
        report["sector_list_ok"] = isinstance(xtdata.get_sector_list(), (list, tuple))
        report["stock_list_count"] = len(xtdata.get_stock_list_in_sector("沪深A股") or [])
        started = perf_counter()
        ticks = xtdata.get_full_tick(CODES)
        report["full_tick"] = {"seconds": perf_counter() - started, "values": plain(ticks)}
        report["instrument_detail"] = {}
        for code in CODES:
            report["instrument_detail"][code] = {}
            for complete in (False, True):
                detail = xtdata.get_instrument_detail(code, complete) or {}
                report["instrument_detail"][code][str(complete)] = {
                    "selected": {field: plain(detail.get(field)) for field in FIELDS},
                    "full": plain(detail), "capital_valid": valid_capital(detail),
                }
        report["turnover_crosscheck"] = {}
        for code in CODES:
            tick = ticks.get(code, {})
            detail = report["instrument_detail"][code]["False"]["selected"]
            if valid_capital(detail):
                floating = float(detail["FloatVolume"])
                by_volume = float(tick.get("volume") or 0) * 100 / floating * 100
                by_pvolume = float(tick.get("pvolume") or 0) / floating * 100
                report["turnover_crosscheck"][code] = {
                    "volume_x100_percent": by_volume, "pvolume_percent": by_pvolume,
                    "absolute_difference_percentage_points": abs(by_volume - by_pvolume),
                }
            else:
                report["turnover_crosscheck"][code] = "unavailable"
        history = xtdata.get_local_data(field_list=["time", "close", "suspendFlag"], stock_list=CODES,
                                        period="1d", count=2, dividend_type="none", fill_data=False)
        report["local_data"] = {code: {"rows": len(frame), "columns": list(frame.columns)} for code, frame in history.items()}
        report["success"] = True
    except Exception as exc:
        report.update(success=False, error=repr(exc))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    raise SystemExit(0 if report.get("success") else 1)


if __name__ == "__main__":
    main()
