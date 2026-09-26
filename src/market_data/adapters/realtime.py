"""Public quote adapters. Batched requests, bounded concurrency, row isolation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import perf_counter
from threading import RLock
from urllib.error import HTTPError
from .parsers import to_wire, parse_tencent, parse_sina
from .public_http import get_text
from ..contracts import MarketSnapshot, SnapshotBatch, canonical_symbol
from ..quality import Freshness, Health
from ...market_clock import to_beijing, market_session


class PublicRealtimeProvider:
    source = ""
    batch_size = 60
    parser = None

    def __init__(self, universe=None, transport=get_text, clock=to_beijing, snapshot_budget=30):
        self.universe, self.transport, self.clock = universe, transport, clock
        self.health, self.freshness = Health(), Freshness()
        self._last_good = {}
        self._lock = RLock()
        self.snapshot_budget = snapshot_budget

    def _request(self, symbols):
        codes = ",".join(to_wire(s) for s in symbols)
        if self.source == "tencent":
            text = self.transport("http://qt.gtimg.cn/q=" + codes)
        else:
            text = self.transport("http://hq.sinajs.cn/list=" + codes,
                                  headers={"Referer": "http://finance.sina.com.cn/"})
        return self.parser(text)

    def quote(self, symbol):
        symbol = canonical_symbol(symbol)
        return self.snapshot([symbol]).snapshots[symbol]

    def snapshot(self, symbols=None):
        with self._lock:
            if symbols is None:
                if self.universe is None:
                    raise RuntimeError("universe_required")
                symbols = [s.symbol for s in self.universe.get()]
            selected = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
            if not selected:
                raise ValueError("empty_symbol_request")
            batches = [selected[i:i+self.batch_size] for i in range(0, len(selected), self.batch_size)]
            raw, errors = {}, []
            started = perf_counter()
            # At most two in-flight requests; on access denial stop scheduling new batches.
            with ThreadPoolExecutor(max_workers=2) as pool:
                denied = False
                for offset in range(0, len(batches), 2):
                    if perf_counter() - started >= self.snapshot_budget:
                        errors.append("snapshot_budget_exceeded")
                        break
                    futures = [pool.submit(self._request, b) for b in batches[offset:offset+2]]
                    for future in futures:
                        try:
                            rows, row_errors = future.result()
                            raw.update(rows)
                            errors.extend(row_errors)
                        except HTTPError as exc:
                            errors.append("HTTP_" + str(exc.code))
                            denied = denied or exc.code in {403, 429}
                        except Exception as exc:
                            errors.append(type(exc).__name__)  # no URL, cookie, or credential logging
                    if denied:
                        break
            now = self.clock()
            selected_set = set(selected)
            raw = {s: q for s, q in raw.items() if s in selected_set}
            zero = [s for s,q in raw.items() if q.price == 0]
            valid = [s for s,q in raw.items() if q.price is not None and q.price > 0]
            snapshots, advances = {}, []
            session = market_session(now)
            for symbol in selected:
                q = raw.get(symbol)
                if q is not None:
                    status, advanced = self.freshness.status(q, now)
                    if advanced is not None:
                        advances.append(advanced)
                    # Zero is evidence, not a usable quote. Counts preserve it, schema price is null.
                    if q.price is not None and q.price <= 0:
                        q = replace(q, price=None, change=None, pct_change=None)
                    q = replace(q, quote_status=status)
                    if status in {"LIVE", "CACHED"}:
                        self._last_good[symbol] = q
                elif symbol in self._last_good:
                    previous = self._last_good[symbol]
                    status, _ = self.freshness.status(previous, now)
                    q = replace(previous, quote_status="STALE" if session in {"open", "auction"}
                                else status)
                else:
                    q = MarketSnapshot(symbol, source=self.source)
                snapshots[symbol] = q
            batch = SnapshotBatch(snapshots, selected, sorted(raw), valid, zero,
                                  [s for s in selected if s not in raw],
                                  len(raw)/len(selected), len(valid)/len(raw) if raw else 0,
                                  perf_counter() - started, self.source, errors,
                                  any(advances) if advances else None, session, now)
            self.health.observe(batch)
            return batch


class TencentRealtimeProvider(PublicRealtimeProvider):
    source = "tencent"
    parser = staticmethod(parse_tencent)
    batch_size = 60


class SinaRealtimeProvider(PublicRealtimeProvider):
    source = "sina"
    parser = staticmethod(parse_sina)
    batch_size = 800
