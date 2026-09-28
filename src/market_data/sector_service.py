"""Cached official membership lookup; never invent a heat score from membership."""
from threading import RLock
from .contracts import canonical_symbol


class SectorService:
    def __init__(self, provider):
        self.provider = provider
        self._lock = RLock()
        self.progress = {}

    def catalog(self, tag="industry"):
        with self._lock:
            return self.provider.catalog(tag)

    def members(self, code):
        code = canonical_symbol(code, allow_index=True)
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
        for tag in ("industry", "cn_concept"):
            data = self.provider.cache.read("catalog_" + tag) or {}
            catalogs += bool(data.get("item"))
            rows = data.get("item", [])
            total += len(rows)
            for row in rows:
                data = self.provider.cache.read("members_" + row["index_thscode"])
                if data is None:
                    continue
                covered += 1
                if any(r["symbol"] == symbol for r in data.get("item", [])):
                    groups.append({k: row[k] for k in ("index_thscode", "index_name", "tag")})
        return dict(symbol=symbol, source="hithink", groups=groups, covered=covered,
                    total=total, quality_status="COMPLETE" if catalogs == 2 and total and covered == total else "PARTIAL")

    def sync(self, stop=None, max_requests=100):
        """Serial bounded resume. Cached entries are skipped; failures remain retryable.

        Each invocation attempts an uncached member at most once; auth/rate limits
        stop the run. The persisted ledger survives process restarts.
        """
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("positive_request_budget_required")
        ledger = self.provider.cache.read("sector_sync_progress") or {}
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
                if self.provider.cache.read("members_" + code) is not None:
                    ledger[code] = "success"
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
        return "COMPLETE" if complete else "PARTIAL"
