"""Fuyao REST envelope and bounded cooldown; contract: official llms-full.txt."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
from threading import RLock
from time import monotonic, perf_counter
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

ENDPOINTS = {
    "tickers": "/api/meta/tickers/list",
    "search": "/api/meta/tickers/search",
    "snapshot": "/api/a-share/prices/snapshot",
    "history": "/api/a-share/prices/historical",
    "calendar": "/api/a-share/calendar/trading-days",
    "index_snapshot": "/api/a-share-index/prices/snapshot",
    "index_history": "/api/a-share-index/prices/historical",
    "catalog": "/api/a-share-index/catalog/ths-index-list",
    "members": "/api/a-share-index/constituents/ths-stock-list",
}
CODE_STATUS = {0:"SUCCESS",1001:"REQUEST_ERROR",1002:"REQUEST_ERROR",1003:"REQUEST_ERROR",
    1004:"REQUEST_ERROR",2001:"AUTH_INVALID",2003:"PERMISSION_DENIED",3001:"SYMBOL_NOT_FOUND",
    3002:"DATA_NOT_READY",3004:"UNSUPPORTED",4001:"RATE_LIMITED",5001:"SERVER_ERROR",
    5002:"UPSTREAM_TIMEOUT",5003:"UPSTREAM_UNAVAILABLE"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, headers, timeout):
    opener = build_opener(NoRedirect())
    try:
        response = opener.open(Request(url, headers=headers), timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise ValueError("response_size_limit")
        return response.code, dict(response.headers), body


@dataclass
class ApiResult:
    status: str
    http_status: int | None = None
    code: int | None = None
    message: str = ""
    request_id: str | None = None
    elapsed: float = 0
    data: dict | None = field(default=None, repr=False)
    retry_after: float = 0

    @property
    def ok(self):
        return self.status == "SUCCESS"

    def metrics(self):
        data = self.data or {}
        return dict(status=self.status, http_status=self.http_status, code=self.code,
                    message=self.message, request_id=self.request_id, elapsed=self.elapsed,
                    item_count=len(data.get("item", [])), timestamp=data.get("timestamp"),
                    retry_after=self.retry_after)


class HithinkRestClient:
    def __init__(self, api_key=None, base_url=None, transport=http_get, timer=monotonic):
        self._key = os.environ.get("FUYAO_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.environ.get("FUYAO_BASE_URL", "https://fuyao.aicubes.cn")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid_fuyao_base_url")
        self.base_url = self.base_url.rstrip("/")
        self.transport, self.timer = transport, timer
        self.next_allowed, self.failures = 0, 0
        self.cooldown_status = "RATE_LIMITED"
        self._lock = RLock()
        self.events = []

    @property
    def configured(self):
        return bool(self._key and self._key.strip())

    def redact(self, value):
        if isinstance(value, dict):
            return {k:self.redact(v) for k,v in value.items()
                    if (k == "api_key_configured" and type(v) is bool) or not any(
                        x in k.lower().replace("-", "_") for x in ("api_key","authorization","token","secret"))}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            if self._key:
                value = value.replace(self._key, "[REDACTED]")
                if len(self._key) >= 16:
                    value = value.replace(self._key[:8], "[REDACTED]").replace(self._key[-8:], "[REDACTED]")
            return re.sub(r"(?i)(x-api-key|authorization|api[_ -]?key)\s*[:=]\s*[^\r\n,}]+",
                          "[REDACTED]", value)
        return value

    def _record(self, capability, result):
        self.events.append(dict(capability=capability, **result.metrics()))
        self.events = self.events[-500:]
        return result

    def get(self, capability, params=None):
        if capability not in ENDPOINTS:
            raise ValueError("unsupported_capability")
        with self._lock:
            if not self.configured:
                return self._record(capability, ApiResult("AUTH_NOT_CONFIGURED"))
            if self.timer() < self.next_allowed:
                return self._record(capability, ApiResult(self.cooldown_status, message="backoff_active",
                                                          retry_after=self.next_allowed-self.timer()))
            started = perf_counter()
            result = ApiResult("NETWORK_ERROR")
            headers = {}
            try:
                query = urlencode(params or {})
                status, headers, body = self.transport(
                    self.base_url + ENDPOINTS[capability] + ("?" + query if query else ""),
                    {"X-api-key": self._key, "Accept":"application/json"}, 15)
                result.http_status = status
                try:
                    payload = json.loads(body)
                except (ValueError, TypeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                payload = self.redact(payload)
                code = payload.get("code")
                result.code = code if type(code) is int else None
                result.message = str(payload.get("message", ""))[:500]
                result.request_id = payload.get("request_id")
                result.status = ("RATE_LIMITED" if status == 429 or result.code == 4001 else
                    "HTTP_ERROR" if status != 200 else CODE_STATUS.get(result.code, "PROTOCOL_ERROR"))
                if result.ok:
                    data = payload.get("data")
                    if not isinstance(data, dict) or not isinstance(data.get("item"), list):
                        result.status = "PROTOCOL_ERROR"
                    else:
                        result.data = data
            except Exception as exc:
                result.message = type(exc).__name__  # Never repr(exception/request/headers).
            result.elapsed = perf_counter() - started
            if result.status in {"RATE_LIMITED","NETWORK_ERROR","SERVER_ERROR","UPSTREAM_TIMEOUT","UPSTREAM_UNAVAILABLE"}:
                self.failures += 1
                delay = min(60, 2 ** min(self.failures, 6))
                retry = next((v for k,v in headers.items() if k.lower()=="retry-after"), None)
                try:
                    retry = float(retry)
                except (TypeError, ValueError):
                    try:
                        retry = (parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        retry = 0
                result.retry_after = min(600, max(delay, retry))
                self.next_allowed = self.timer() + result.retry_after
                self.cooldown_status = result.status
            elif result.ok:
                self.failures = 0
            return self._record(capability, result)
