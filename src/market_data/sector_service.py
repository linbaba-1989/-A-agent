"""Cached official membership lookup; never invent a heat score from membership."""
from threading import RLock
import json
import re
from .contracts import canonical_symbol


class SectorService:
    def __init__(self, provider):
        self.provider = provider
        self._lock = RLock()
        self.progress = {}

    def unsupported(self):
        """Capability evidence is persistent, unlike the daily membership cache."""
        try:
            payload = json.loads((self.provider.cache.path / 'unsupported_market_scope.json').read_text(encoding='utf-8'))
            data = payload['data']
            return data['groups'] if data.get('scope_version') == 1 else {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def record_unsupported_market(self, code, official_items):
        """Record verified mixed-market membership without silently dropping rows.

        Caller must supply the successful official response. Malformed or unknown
        symbols remain parse failures, never unsupported-market evidence.
        """
        code = canonical_symbol(code, allow_index=True)
        unsupported = []
        for row in official_items:
            symbol = row['thscode']
            if re.fullmatch(r'\d{6}\.NQ', symbol):
                unsupported.append(symbol)
            else:
                canonical_symbol(symbol)
        if not unsupported:
            raise ValueError('no_unsupported_market_evidence')
        with self._lock:
            groups = self.unsupported()
            groups[code] = dict(status='UNSUPPORTED_MARKET', source='hithink',
                                unsupported_symbols=unsupported, member_count=len(official_items),
                                verified_at=self.provider.cache.clock().isoformat())
            self.provider.cache.write('unsupported_market_scope', dict(scope_version=1, groups=groups))

    def _cached(self, key):
        """Return disk data and whether it was accepted only as stale evidence."""
        fresh = self.provider.cache.read(key)
        if fresh is not None:
            return fresh, False
        reader = getattr(self.provider.cache, 'read_persistent', None)
        stale = reader(key) if callable(reader) else None
        return stale, stale is not None

    def catalog(self, tag="industry"):
        with self._lock:
            return self.provider.catalog(tag)

    def members(self, code):
        code = canonical_symbol(code, allow_index=True)
        from .hithink_client import ApiResult
        if code in self.unsupported():
            return ApiResult('UNSUPPORTED_MARKET', data=self.unsupported()[code])
        with self._lock:
            for tag in ("industry", "cn_concept"):
                result = self.provider.catalog(tag)
                for row in (result.data or {}).get("item", []):
                    if row["index_thscode"] == code:
                        return self.provider.members(code, row["index_name"], tag)
        from .hithink_client import ApiResult
        return ApiResult("SYMBOL_NOT_FOUND", message="not_in_available_catalog")

    def memberships(self, symbol):
        """Read cached membership only; PARTIAL explicitly means not all groups known."""
        symbol = canonical_symbol(symbol)
        groups, covered, total, catalogs = [], 0, 0, 0
        stale_catalogs, stale_members = 0, 0
        unsupported = self.unsupported()
        excluded = []
        for tag in ("industry", "cn_concept"):
            data, catalog_stale = self._cached("catalog_" + tag)
            data = data or {}
            stale_catalogs += int(catalog_stale)
            catalogs += bool(data.get("item"))
            rows = data.get("item", [])
            total += len(rows)
            for row in rows:
                if row['index_thscode'] in unsupported:
                    excluded.append(dict(code=row['index_thscode'], **unsupported[row['index_thscode']]))
                    continue
                data, member_stale = self._cached("members_" + row["index_thscode"])
                if data is None:
                    continue
                covered += 1
                stale_members += int(member_stale)
                if any(r["symbol"] == symbol for r in data.get("item", [])):
                    groups.append({k: row[k] for k in ("index_thscode", "index_name", "tag")})
        supported = total-len(excluded)
        complete = catalogs == 2 and supported > 0 and covered == supported
        return dict(symbol=symbol, source="hithink", groups=groups, covered=covered,
                    total=total, catalog_total=total, supported_total=supported,
                    unsupported_total=len(excluded), cached_supported=covered,
                    supported_coverage=covered/supported if supported else None,
                    fresh_catalogs=catalogs-stale_catalogs, stale_catalogs=stale_catalogs,
                    fresh_members=covered-stale_members, stale_members=stale_members,
                    unsupported_groups=excluded,
                    quality_status=("COMPLETE_SUPPORTED_SCOPE" if excluded else "COMPLETE") if complete else "PARTIAL")

    def sync(self, stop=None, max_requests=100):
        """Serial bounded resume. Cached entries are skipped; failures remain retryable.

        Each invocation attempts an uncached member at most once; auth/rate limits
        stop the run. The persisted ledger survives process restarts.
        """
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("positive_request_budget_required")
        ledger = self.provider.cache.read("sector_sync_progress") or {}
        unsupported = self.unsupported()
        requests, complete = 0, True
        categories = {"RATE_LIMITED":"rate_limited", "PERMISSION_DENIED":"permission",
                      "AUTH_INVALID":"permission", "AUTH_NOT_CONFIGURED":"permission",
                      "PROTOCOL_ERROR":"parse_error"}
        for tag in ("industry", "cn_concept"):
            result = self.catalog(tag)
            if result.status not in {"SUCCESS", "CACHED"}:
                return result.status
            for row in result.data["item"]:
                code = row["index_thscode"]
                if code in unsupported:
                    ledger[code] = 'UNSUPPORTED_MARKET'
                    continue
                cached, stale = self._cached("members_" + code)
                if cached is not None:
                    ledger[code] = "cached_stale" if stale else "success"
                    continue
                if requests >= max_requests:
                    ledger.setdefault(code, "queued")
                    complete = False
                    continue
                if stop is not None and stop.wait(.25):
                    self.provider.cache.write("sector_sync_progress", ledger)
                    return "STOPPED"
                requests += 1
                result = self.provider.members(code, row["index_name"], tag)
                ledger[code] = "success" if result.status in {"SUCCESS", "CACHED"} else categories.get(result.status, "failed")
                self.provider.cache.write("sector_sync_progress", ledger)
                self.progress = dict(ledger)
                if result.status not in {"SUCCESS", "CACHED"}:
                    complete = False
                    if ledger[code] in {"rate_limited", "permission"}:
                        return result.status
        self.progress = dict(ledger)
        self.provider.cache.write("sector_sync_progress", ledger)
        return ("COMPLETE_SUPPORTED_SCOPE" if unsupported else "COMPLETE") if complete else "PARTIAL"
