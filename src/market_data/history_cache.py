"""Independent atomic JSON cache; legacy in-memory cache lacks basis metadata."""
from dataclasses import replace
from datetime import date
from pathlib import Path
import json
import os
import tempfile
from .historical import HistoricalBar, HistoryResult, validate
from ..market_clock import DEFAULT_TRADING_CALENDAR

DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data/free_history"


class FreeHistoryCache:
    def __init__(self, path=DEFAULT_CACHE):
        self.path = Path(path)

    def _path(self, symbol, source, adjustment, kind, endpoint):
        # All components are validated by the facade before reaching this boundary.
        for value in (symbol, source, adjustment, kind, endpoint):
            if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for c in value):
                raise ValueError("unsafe_cache_key")
        return self.path / f"{symbol}.{source}.{endpoint}.{kind}.{adjustment}.json"

    def read(self, symbol, source, adjustment, kind, endpoint, now, calendar=DEFAULT_TRADING_CALENDAR):
        path = self._path(symbol, source, adjustment, kind, endpoint)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (payload["version"], payload["symbol"], payload["source"], payload["adjustment"],
                payload["kind"], payload["endpoint"]) != (1, symbol, source, adjustment, kind, endpoint):
                return None
            bars = [HistoricalBar(**{**r, "trade_date": date.fromisoformat(r["trade_date"])})
                    for r in payload["bars"]]
            result = validate(bars, symbol, source, adjustment, date.min,
                              date.fromisoformat(payload["last_trade_date"]), now, calendar)
            if result.quality_status != "VALID" or not result.bars:
                return None
            if result.bars[-1].trade_date.isoformat() != payload["last_trade_date"]:
                return None
            result.data_status = "CACHED"
            result.bars = [replace(b, data_status="CACHED") for b in result.bars]
            result.updated_at = payload["updated_at"]
            result.warnings.extend(payload.get("warnings", []))
            return result
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def write(self, result, kind, endpoint, now):
        if result.quality_status != "VALID" or not result.bars:
            return
        path = self._path(result.symbol, result.source, result.adjustment, kind, endpoint)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(version=1, symbol=result.symbol, source=result.source,
            adjustment=result.adjustment, kind=kind, endpoint=endpoint,
            last_trade_date=str(result.bars[-1].trade_date), updated_at=now.isoformat(),
            warnings=result.warnings, bars=[b.to_dict() for b in result.bars])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                              delete=False, suffix=".tmp") as f:
                temporary = f.name
                json.dump(payload, f, ensure_ascii=False, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
