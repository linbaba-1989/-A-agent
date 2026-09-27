"""Cached official membership lookup; never invent a heat score from membership."""
from threading import RLock
from .contracts import canonical_symbol


class SectorService:
    def __init__(self, provider):
        self.provider = provider
        self._lock = RLock()

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
        groups, covered, total = [], 0, 0
        for tag in ("industry", "cn_concept"):
            data = self.provider.cache.read("catalog_" + tag) or {}
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
                    total=total, quality_status="COMPLETE" if total and covered == total else "PARTIAL")

    def sync(self, stop=None):
        """Explicit background bootstrap, serial and cached; stop at first source error."""
        for tag in ("industry", "cn_concept"):
            result = self.catalog(tag)
            if result.status not in {"SUCCESS", "CACHED"}:
                return result.status
            for row in result.data["item"]:
                if stop is not None and stop.wait(.25):
                    return "STOPPED"
                result = self.provider.members(row["index_thscode"], row["index_name"], tag)
                if result.status not in {"SUCCESS", "CACHED"}:
                    return result.status
        return "COMPLETE"
