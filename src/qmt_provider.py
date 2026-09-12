"""Read-only QMT/xtdata adapter. No mock, web quote, or trading fallback exists."""
from __future__ import annotations

import os
import subprocess
import sys
import importlib.metadata
import site
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import pandas as pd

from .models import MarketDiagnostics, ProviderStatus

TEST_SYMBOLS = ["600000.SH", "000001.SZ"]
QUOTE_FIELDS = ("lastPrice", "open", "high", "low", "lastClose", "volume", "pvolume", "amount", "stockStatus")
RUNTIME_NAME = "official_230825b"
RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "tools" / "xtquant_runtime" / "230825b"


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


class QMTProvider:
    def __init__(self, qmt_path: str | None = None, port: int | None = None):
        self.qmt_path = qmt_path or os.getenv("QMT_PATH", "")
        self.port = port or int(os.getenv("QMT_PORT", "58610"))
        self._xtdata = None

    @staticmethod
    def _runtime_root() -> Path:
        package = RUNTIME_ROOT / "xtquant"
        if not package.is_dir():
            raise RuntimeError(f"xtquant_runtime_missing: {package}")
        python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
        required = [package / "__init__.py", package / "xtdata.py"]
        natives = list(package.glob(f"*.{python_tag}-win_amd64.pyd"))
        if any(not path.is_file() for path in required) or not natives:
            raise RuntimeError(f"xtquant_runtime_incomplete: {package}, python_tag={python_tag}")
        return RUNTIME_ROOT.resolve()

    def _site_packages(self) -> Path | None:
        if not self.qmt_path:
            return None
        root = Path(self.qmt_path).expanduser()
        candidates = [root, root / "Lib" / "site-packages", root / "bin.x64" / "Lib" / "site-packages"]
        return next((path.resolve() for path in candidates if (path / "xtquant").is_dir()), None)

    def _load_xtdata(self):
        if self._xtdata is not None:
            return self._xtdata
        runtime_root = self._runtime_root()
        expected_package = (runtime_root / "xtquant").resolve()
        preloaded = {
            name: Path(module.__file__).resolve()
            for name, module in sys.modules.items()
            if name == "xtquant" or name.startswith("xtquant.")
            if getattr(module, "__file__", None)
        }
        mixed = {name: path for name, path in preloaded.items() if not path.is_relative_to(expected_package)}
        if mixed:
            raise RuntimeError(f"xtquant_component_mixed: preloaded={mixed}, expected={expected_package}")
        if str(runtime_root) not in sys.path:
            sys.path.insert(0, str(runtime_root))
        from xtquant import xtdata  # type: ignore
        loaded_package = Path(xtdata.__file__).resolve().parent
        loaded_modules = {
            name: Path(module.__file__).resolve()
            for name, module in sys.modules.items()
            if name == "xtquant" or name.startswith("xtquant.")
            if getattr(module, "__file__", None)
        }
        mixed = {name: path for name, path in loaded_modules.items() if not path.is_relative_to(expected_package)}
        if loaded_package != expected_package or mixed:
            raise RuntimeError(
                f"xtquant_component_mixed: expected={expected_package}, wrapper={loaded_package}, modules={mixed}"
            )
        self._xtdata = xtdata
        return xtdata

    def runtime_info(self) -> dict[str, Any]:
        """Report package/native provenance without modifying the QMT install."""
        xtdata = self._load_xtdata()
        import xtquant  # type: ignore
        package_dir = Path(xtquant.__file__).resolve().parent
        loaded = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
                  if name.startswith("xtquant") and getattr(module, "__file__", None)}
        natives = sorted(str(path.resolve()) for path in package_dir.glob("*.pyd"))
        search_roots = list(dict.fromkeys(sys.path + site.getsitepackages() + [site.getusersitepackages()]))
        candidates = sorted(str((Path(root) / "xtquant").resolve()) for root in search_roots
                            if root and (Path(root) / "xtquant").is_dir())
        try:
            version = importlib.metadata.version("xtquant")
        except importlib.metadata.PackageNotFoundError:
            version = getattr(xtquant, "__version__", None)
        relevant_files = [Path(xtquant.__file__).resolve(), Path(xtdata.__file__).resolve()]
        relevant_files.extend(Path(path) for path in natives if "cp311" in path or "win_amd64" not in path)
        files = [{"path": str(path), "size": path.stat().st_size,
                  "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}
                 for path in relevant_files if path.exists()]
        consistent = all(Path(path).is_relative_to(package_dir) for path in loaded.values())
        qmt_bundle = self._site_packages()
        return {"runtime": RUNTIME_NAME, "runtime_root": str(self._runtime_root()),
                "python_executable": sys.executable, "python_version": sys.version,
                "xtquant_file": str(Path(xtquant.__file__).resolve()), "xtdata_file": str(Path(xtdata.__file__).resolve()),
                "version": version, "loaded_modules": loaded, "native_modules": natives,
                "xtquant_sys_path_entries": [entry for entry in sys.path if "xtquant" in entry.lower() or "qmt" in entry.lower()],
                "candidate_packages": candidates, "single_install_consistent": consistent,
                "qmt_bundled_xtquant": {"path": str(qmt_bundle / "xtquant") if qmt_bundle else None,
                                         "status": "legacy / capital decode broken" if qmt_bundle else "not found"},
                "a_agent_runtime_xtquant": {"path": str(package_dir), "status": "official 230825b / verified"},
                "files": files}

    @staticmethod
    def _processes() -> list[str]:
        if os.name != "nt":
            return []
        try:
            output = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                    encoding="mbcs", errors="replace", timeout=5, check=False).stdout
            names = [line.split('","', 1)[0].strip('"') for line in output.splitlines()]
            return sorted({name for name in names if any(token in name.lower() for token in ("qmt", "xtmini", "xtitclient"))})
        except Exception:
            return []

    @staticmethod
    def _valid_tick(tick: dict[str, Any] | None, require_time: bool = False) -> bool:
        if not tick:
            return False
        try:
            valid = float(tick.get("lastPrice") or 0) > 0 and float(tick.get("lastClose") or 0) > 0
            return valid and (not require_time or bool(tick.get("time") or tick.get("timetag")))
        except (TypeError, ValueError):
            return False

    def connection_diagnostics(self) -> MarketDiagnostics:
        """Three-level check: import, basic RPC, then usable real ticks."""
        started = perf_counter()
        processes = self._processes()
        result = MarketDiagnostics(
            connected=False, xtquant_path=None, process_detected=bool(processes), processes=processes,
            python_version=sys.version, message="QMT 尚未完成检测",
        )
        try:
            xtdata = self._load_xtdata()
            result.xtquant_imported = True
            result.xtquant_path = str(Path(xtdata.__file__).resolve())
        except Exception as exc:
            result.raw_error = repr(exc)
            result.message = "xtquant_import_failed"
            result.elapsed_seconds = perf_counter() - started
            return result
        try:
            sectors = xtdata.get_sector_list()
            result.rpc_request_success = isinstance(sectors, (list, tuple))
            if not result.rpc_request_success:
                raise RuntimeError(f"get_sector_list 返回类型异常：{type(sectors).__name__}")
        except Exception as exc:
            result.raw_error = repr(exc)
            result.message = "xtdata_rpc_request_failed"
            result.elapsed_seconds = perf_counter() - started
            return result
        try:
            ticks = xtdata.get_full_tick(TEST_SYMBOLS)
            result.full_tick_count = len(ticks) if isinstance(ticks, dict) else 0
            result.full_tick_success = isinstance(ticks, dict) and any(
                self._valid_tick(ticks.get(code), require_time=True) for code in TEST_SYMBOLS
            )
            if not result.full_tick_success:
                raise RuntimeError(f"测试行情缺少有效 lastPrice/lastClose/time：{ticks!r}")
            result.connected = True
            result.valid_quote_count = sum(self._valid_tick(ticks.get(code), True) for code in TEST_SYMBOLS)
            result.message = "QMT XtData RPC 与真实行情可用"
        except Exception as exc:
            result.raw_error = repr(exc)
            result.message = "qmt_full_tick_failed"
        result.elapsed_seconds = perf_counter() - started
        return result

    def status(self) -> ProviderStatus:
        diagnostic = self.connection_diagnostics()
        return ProviderStatus(diagnostic.connected, diagnostic.message, diagnostic.to_dict())

    def get_stock_universe(self) -> list[str]:
        xtdata = self._load_xtdata()
        candidates: list[str] = []
        errors: list[str] = []
        for sector in ("沪深京A股", "沪深A股", "沪A", "深A", "北交所", "京A"):
            try:
                candidates.extend(xtdata.get_stock_list_in_sector(sector) or [])
            except Exception as exc:
                errors.append(f"{sector}: {exc!r}")
        stocks = sorted({code for code in candidates if self._is_a_share(code)})
        if not stocks:
            raise RuntimeError(f"无法获取全 A 股票池：{'; '.join(errors) or 'QMT 返回空股票池'}")
        return stocks

    @staticmethod
    def _is_a_share(code: str) -> bool:
        if code.endswith(".SH"):
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if code.endswith(".SZ"):
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        return code.endswith(".BJ") and code.startswith(("4", "8", "9"))

    def get_full_ticks(self, symbols: list[str], batch_size: int = 500) -> dict[str, dict[str, Any]]:
        xtdata = self._load_xtdata()
        result: dict[str, dict[str, Any]] = {}
        for batch in _chunks(symbols, batch_size):
            payload = xtdata.get_full_tick(batch)
            if not isinstance(payload, dict):
                raise RuntimeError(f"get_full_tick 返回类型异常：{type(payload).__name__}")
            result.update({code: tick for code, tick in payload.items() if isinstance(tick, dict)})
        return result

    def get_market_ticks(self, markets: list[str] | None = None) -> dict[str, dict[str, Any]]:
        payload = self._load_xtdata().get_full_tick(markets or ["SH", "SZ", "BJ"])
        if not isinstance(payload, dict):
            raise RuntimeError(f"市场全推返回类型异常：{type(payload).__name__}")
        return {code: tick for code, tick in payload.items() if isinstance(tick, dict)}

    def get_instrument_detail(self, symbol: str) -> dict[str, Any]:
        detail = self._load_xtdata().get_instrument_detail(symbol, False) or {}
        if not isinstance(detail, dict):
            raise RuntimeError(f"get_instrument_detail({symbol}) 返回类型异常")
        return detail

    def get_local_history(self, symbols: list[str], count: int = 61, batch_size: int = 300) -> dict[str, pd.DataFrame]:
        xtdata = self._load_xtdata()
        result: dict[str, pd.DataFrame] = {}
        fields = ["time", "open", "high", "low", "close", "volume", "amount", "suspendFlag"]
        for batch in _chunks(symbols, batch_size):
            payload = xtdata.get_local_data(field_list=fields, stock_list=batch, period="1d", count=count,
                                            dividend_type="none", fill_data=False) or {}
            result.update({code: frame.copy() for code, frame in payload.items() if isinstance(frame, pd.DataFrame)})
        return result

    # Backwards-compatible single-stock UI helpers.
    def get_history(self, symbols: list[str], period: str, count: int, fields=None, batch_size: int = 300):
        if period != "1d":
            raise ValueError("盘中分钟数据不得通过历史接口用于实时指标")
        return self.get_local_history(symbols, count, batch_size)

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        tick = self.get_full_ticks([symbol]).get(symbol)
        return self.normalize_tick(symbol, tick) if self._valid_tick(tick) else None

    @staticmethod
    def tick_timestamp(tick: dict[str, Any]) -> float | None:
        value = tick.get("time") or tick.get("timetag")
        if not isinstance(value, (int, float)) or value <= 0:
            return None
        return value / 1000 if value > 10_000_000_000 else float(value)

    @classmethod
    def normalize_tick(cls, symbol: str, tick: dict[str, Any]) -> dict[str, Any]:
        seconds = cls.tick_timestamp(tick)
        timestamp = datetime.fromtimestamp(seconds).isoformat(timespec="seconds") if seconds else "unavailable"
        result = {"symbol": symbol, **{field: tick.get(field, "unavailable") for field in QUOTE_FIELDS}}
        result.update({"timestamp": timestamp, "source": "QMT/xtquant.get_full_tick"})
        return result

    def diagnose(self, include_market_sample: bool = True) -> MarketDiagnostics:
        result = self.connection_diagnostics()
        if not result.connected or not include_market_sample:
            return result
        started = perf_counter()
        try:
            universe = self.get_stock_universe()
            tick_started = perf_counter()
            ticks = self.get_full_ticks(universe)
            result.full_tick_seconds = perf_counter() - tick_started
            result.stock_pool_size = len(universe)
            result.sh_count = sum(code.endswith(".SH") for code in universe)
            result.sz_count = sum(code.endswith(".SZ") for code in universe)
            result.bj_count = sum(code.endswith(".BJ") for code in universe)
            result.full_tick_count = len(ticks)
            result.valid_quote_count = sum(self._valid_tick(tick) for tick in ticks.values())
            result.invalid_quote_count = len(universe) - result.valid_quote_count
        except Exception as exc:
            result.raw_error = repr(exc)
            result.message = f"行情诊断失败：{exc!r}"
        result.elapsed_seconds += perf_counter() - started
        return result
