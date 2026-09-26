"""Daily, atomic free security master; no paid provider lookup."""
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
import json
import os
import tempfile
from .contracts import Security, canonical_symbol
from ..market_clock import to_beijing

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "free_market" / "universe.json"


def board_for(symbol):
    code, exchange = canonical_symbol(symbol).split(".")
    if exchange == "BJ":
        return "BSE"
    if code.startswith(("688", "689")) and exchange == "SH":
        return "STAR"
    if code.startswith(("300", "301")) and exchange == "SZ":
        return "CHINEXT"
    return "MAIN"


class SecurityUniverse:
    def __init__(self, path=DEFAULT_PATH, loader=None, clock=to_beijing):
        if loader is None:
            from .adapters.public_http import tencent_universe
            loader = tencent_universe
        self.path, self.loader, self.clock = Path(path), loader, clock
        self._rows, self.as_of, self.last_error = [], None, None
        self._attempt_date = None
        self._lock = RLock()

    @staticmethod
    def _validate(rows):
        unique = {}
        for row in rows:
            canonical_symbol(row.symbol)
            if row.exchange != row.symbol[-2:] or row.board != board_for(row.symbol):
                raise ValueError("invalid_security_metadata")
            unique[row.symbol] = row
        if not unique:
            raise ValueError("empty_universe")
        return list(unique.values())

    def get(self):
        with self._lock:
            today = self.clock().date()
            if not self._rows:
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    if payload.get("version") != 1:
                        raise ValueError("universe_cache_version")
                    self._rows = self._validate([Security(**r) for r in payload["securities"]])
                    self.as_of = datetime.fromisoformat(payload["as_of"]).date()
                except (OSError, ValueError, TypeError, KeyError):
                    self._rows, self.as_of = [], None
            if self.as_of == today:
                return list(self._rows)
            if self._attempt_date != today:
                self._attempt_date = today
                try:
                    rows = self._validate(self.loader())
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    payload = {"version": 1, "as_of": today.isoformat(),
                               "securities": [asdict(r) for r in rows]}
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                dir=self.path.parent, delete=False, suffix=".tmp") as f:
                            temporary = f.name
                            json.dump(payload, f, ensure_ascii=False)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(temporary, self.path)
                    finally:
                        if temporary and os.path.exists(temporary):
                            os.unlink(temporary)
                    self._rows, self.as_of, self.last_error = rows, today, None
                except Exception as exc:
                    self.last_error = type(exc).__name__
            # Known dated list may bootstrap during an outage, but never indefinitely.
            if not self._rows or self.as_of is None or not today - timedelta(days=7) <= self.as_of <= today:
                raise RuntimeError("free_universe_unavailable")
            return list(self._rows)
