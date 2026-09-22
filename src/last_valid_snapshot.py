"""Atomic, source-isolated storage of the last valid full-market Token snapshot."""
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .market_clock import to_beijing

TOKEN_SOURCE = "XtDataCenter Token"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "xtdc_last_valid_snapshot.json"


def valid_row(row):
    if not isinstance(row, dict) or row.get("source") != TOKEN_SOURCE:
        return False
    if not isinstance(row.get("symbol"), str) or not row["symbol"]:
        return False
    for key in ("lastPrice", "quote_timestamp"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return False
    return isinstance(row.get("snapshot_seq"), int) and row["snapshot_seq"] > 0


class LastValidSnapshot:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    def load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("source") != TOKEN_SOURCE or payload.get("version") != 1:
                return []
            rows = payload["rows"]
            if not isinstance(rows, list) or not rows or not all(valid_row(row) for row in rows):
                return []
            return rows
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return []

    def save(self, rows):
        if not rows or not all(valid_row(row) for row in rows):
            return False
        saved_at = to_beijing().isoformat(timespec="seconds")
        records = [{**row, "last_price": row["lastPrice"], "last_close": row.get("lastClose"),
                    "turnover": row.get("turnover_rate"), "saved_at": saved_at} for row in rows]
        payload = {"version": 1, "source": TOKEN_SOURCE, "saved_at": saved_at, "rows": records}
        temporary = None
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            return True
        except (OSError, ValueError, TypeError):
            return False  # A disk failure must not discard usable in-memory quotes.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
