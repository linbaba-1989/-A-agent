"""Official ticker/calendar/catalog caches. No automatic cross-vendor fallback."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import tempfile
from .contracts import Security, canonical_symbol
from .universe import board_for
from .hithink_client import ApiResult
from ..market_clock import DEFAULT_TRADING_CALENDAR, BEIJING_TZ, to_beijing


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def milliseconds(value):
    if type(value) not in (int, float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, BEIJING_TZ)
    except (ValueError, OSError, OverflowError):
        return None


class ReferenceCache:
    def __init__(self, path, clock=to_beijing):
        self.path, self.clock = Path(path), clock

    def read(self, key):
        try:
            payload = json.loads((self.path / (key + ".json")).read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(payload["updated_at"])
            if payload["version"] != 1 or not timedelta(0) <= self.clock()-updated < timedelta(days=1):
                return None
            return payload["data"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def read_persistent(self, key):
        """Read an existing disk snapshot without treating it as fresh data.

        Reference data such as sector membership remains useful for coverage
        reporting after its refresh TTL. Callers must expose its stale state and
        must not use this method as evidence of a current remote refresh.
        """
        try:
            payload = json.loads((self.path / (key + ".json")).read_text(encoding="utf-8"))
            if payload["version"] != 1:
                return None
            return payload["data"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write(self, key, data):
        self.path.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path, encoding="utf-8",
                                             suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(dict(version=1, updated_at=self.clock().isoformat(), data=data),
                          stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path/(key+".json"))
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def parse_ticker(row):
    symbol = canonical_symbol(row["thscode"])
    if row.get("exchange") != symbol[-2:] or row.get("asset_type") != "a-share" or row.get("currency") != "CNY":
        raise ValueError("ticker_contract_mismatch")
    if row.get("ticker") != symbol[:6]:
        raise ValueError("ticker_identity_mismatch")
    return dict(symbol=symbol, ticker=row["ticker"], name=row.get("name"),
                exchange=row["exchange"], asset_type=row["asset_type"],
                currency=row["currency"], list_date=row.get("list_date"), board=board_for(symbol))


class HithinkSecurityUniverseProvider:
    def __init__(self, client, cache):
        self.client, self.cache = client, cache
        self.rows = {}
        self.last_result = ApiResult("NOT_LOADED")
        self.pages = []

    def refresh(self, force=False):
        if not force:
            cached = self.cache.read("tickers")
            if isinstance(cached, dict) and isinstance(cached.get("item"), list):
                try:
                    # Stored normalized rows retain symbol, not raw thscode.
                    parsed = [parse_ticker({**r, "thscode":r["symbol"]}) for r in cached["item"]]
                    self.rows = {r["symbol"]:r for r in parsed}
                    self.last_result = ApiResult("CACHED", data=cached)
                    return self.last_result
                except (ValueError, KeyError, TypeError):
                    pass
        rows, self.pages, offset = {}, [], 0
        # Official ticker default=1000, max=10000. Use documented default.
        for _ in range(100):
            result = self.client.get("tickers", dict(asset_type="a-share", limit=1000, offset=offset))
            self.pages.append(dict(offset=offset, **result.metrics()))
            if not result.ok:
                self.last_result = result
                return result
            try:
                parsed = [parse_ticker(row) for row in result.data["item"]]
                if any(r["symbol"] in rows for r in parsed):
                    raise ValueError("duplicate_ticker_page")
                rows.update({r["symbol"]:r for r in parsed})
            except (ValueError, KeyError, TypeError):
                self.last_result = ApiResult("PROTOCOL_ERROR", message="invalid_ticker_page")
                return self.last_result
            if len(result.data["item"]) < 1000:
                self.rows = rows
                data = dict(timestamp=result.data.get("timestamp"), item=list(rows.values()))
                self.cache.write("tickers", data)
                self.last_result = replace(result, data=data)
                return self.last_result
            offset += 1000
        self.last_result = ApiResult("PROTOCOL_ERROR", message="ticker_page_safety_bound")
        return self.last_result

    def get(self):
        result = self.refresh()
        if result.status not in {"SUCCESS", "CACHED"}:
            raise RuntimeError(result.status)
        return [Security(r["symbol"],r["name"],r["exchange"],r["board"]) for r in self.rows.values()]


class HithinkCalendar:
    def __init__(self, client, cache, clock=to_beijing, fallback=DEFAULT_TRADING_CALENDAR):
        self.client, self.cache, self.clock, self.fallback = client, cache, clock, fallback
        self.days, self.start, self.end = set(), None, None
        self.last_result = ApiResult("NOT_LOADED")

    def refresh(self, force=False):
        data = None if force else self.cache.read("calendar")
        result = ApiResult("CACHED", data=data) if data else self.client.get("calendar")
        if result.status not in {"SUCCESS","CACHED"}:
            self.last_result = result
            return result
        try:
            days = []
            for row in result.data["item"]:
                day = datetime.strptime(row["date"], "%Y%m%d").date()
                stamp = milliseconds(row["date_ms"])
                if stamp is None or stamp.date()!=day:
                    raise ValueError("calendar_date_mismatch")
                days.append(day)
            if not days or days != sorted(set(days)):
                raise ValueError("invalid_calendar_sequence")
            stamp = milliseconds(result.data["timestamp"])
            if stamp is None:
                raise ValueError("missing_calendar_timestamp")
            self.end = min(stamp.date(), self.clock().date())
            try:
                self.start = self.end.replace(year=self.end.year-1)
            except ValueError:
                self.start = self.end.replace(year=self.end.year-1, day=28)
            if days[0] < self.start or days[-1] > self.end:
                raise ValueError("calendar_window_mismatch")
            self.days = set(days)
            if result.ok:
                self.cache.write("calendar", result.data)
        except (ValueError, TypeError, KeyError):
            result = ApiResult("PROTOCOL_ERROR",message="invalid_calendar")
        self.last_result = result
        return result

    def is_trading_day(self, value):
        day = value.date() if isinstance(value,datetime) else value
        if self.start is not None and self.start <= day <= self.end:
            return day in self.days
        return self.fallback.is_trading_day(day)


class HithinkSectorProvider:
    def __init__(self, client, cache):
        self.client, self.cache = client, cache

    def catalog(self, tag="cn_concept", force=False):
        if tag not in {"cn_concept","industry"}:
            raise ValueError("unsupported_catalog_tag")
        key = "catalog_"+tag
        data = None if force else self.cache.read(key)
        if data:
            return ApiResult("CACHED",data=data)
        result = self.client.get("catalog",dict(tag=tag))
        if result.ok:
            try:
                rows=[dict(index_thscode=canonical_symbol(r["thscode"],allow_index=True),
                           index_name=r["name"],tag=tag,members=None,
                           updated_at=self.cache.clock().isoformat()) for r in result.data["item"]]
                result.data = {**result.data, "item":rows}
                self.cache.write(key,result.data)
            except (ValueError, KeyError, TypeError):
                return ApiResult("PROTOCOL_ERROR",message="invalid_catalog")
        return result

    def members(self, index_thscode, index_name=None, tag=None, force=False):
        index_thscode = canonical_symbol(index_thscode,allow_index=True)
        key = "members_"+index_thscode
        data = None if force else self.cache.read(key)
        if data:
            return ApiResult("CACHED",data=data)
        result = self.client.get("members",dict(thscode=index_thscode))
        if result.ok:
            try:
                rows=[dict(symbol=canonical_symbol(r["thscode"]),ticker=r["ticker"],name=r.get("name"))
                      for r in result.data["item"]]
                result.data = {**result.data,"item":rows,"index_thscode":index_thscode,
                    "index_name":index_name,"tag":tag,"members":rows,
                    "updated_at":self.cache.clock().isoformat()}
                self.cache.write(key,result.data)
            except (ValueError,KeyError,TypeError):
                return ApiResult("PROTOCOL_ERROR",message="invalid_constituents")
        return result
