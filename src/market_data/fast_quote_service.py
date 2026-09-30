"""Bounded small-symbol lane. No full-market provider, locks or cache are shared."""
from collections import deque
from dataclasses import replace
from functools import partial
from itertools import islice
from threading import Event, RLock, Lock, Thread
from time import monotonic
from .free_provider import FreeMarketDataProvider
from .adapters.realtime import TencentRealtimeProvider, SinaRealtimeProvider
from .adapters.public_http import get_text
from .contracts import canonical_symbol
from .quality import Health
from ..compact_market_history import RealtimeSnapshotBuffer
from ..market_clock import to_beijing, market_session


class RequestHealth(Health):
    """Reuse health thresholds/hysteresis for transport; quotes retain real freshness.

    A small, possibly inactive watchlist cannot establish market timestamp health.
    Never feed its lack of trades into either lane's global health decision.
    """
    def observe(self, batch):
        health_quotes = {s: replace(q, quote_status='CACHED') if s in batch.valid_symbols else q
                         for s, q in batch.snapshots.items()}
        super().observe(replace(batch, snapshots=health_quotes, timestamp_advanced=None,
                                provider_evidence={}, market_session='closed'))


def request_provider():
    transport = partial(get_text, timeout=3)
    primary = TencentRealtimeProvider(transport=transport, snapshot_budget=3)
    fallback = SinaRealtimeProvider(transport=transport, snapshot_budget=3)
    primary.health, fallback.health = RequestHealth(), RequestHealth()
    return FreeMarketDataProvider(primary=primary, fallback=fallback)


class FastQuoteService:
    def __init__(self, provider=None, publish=None, *, interval=1, limit=50,
                 clock=to_beijing, timer=monotonic, autostart=True):
        self.provider = provider or request_provider()
        self.publish = publish or (lambda payload: None)
        self.interval = max(1, interval)
        self.limit = min(50, max(1, limit))
        self.clock, self.timer = clock, timer
        self.lock, self.poll_lock = RLock(), Lock()
        self.stop = Event()
        self.subscriptions = {}
        self.quotes, self.samples = {}, {}
        self.buffer = RealtimeSnapshotBuffer(retention_seconds=600)
        self.metrics = deque(maxlen=1200)
        self.request_count = self.success = self.failure = self.sequence = 0
        self.next_due = 0
        self.failures = 0
        self.worker = None
        if autostart:self.start()

    def start(self):
        with self.lock:
            if self.worker is None and not self.stop.is_set():
                self.worker = Thread(target=self._run, daemon=True, name='fast-quotes')
                self.worker.start()

    def subscribe(self, owner, current_symbol=None, watchlist_symbols=()):
        current = canonical_symbol(current_symbol) if current_symbol else None
        watch = tuple(dict.fromkeys(canonical_symbol(s) for s in islice(watchlist_symbols, self.limit+1)))
        with self.lock:
            if owner not in self.subscriptions and len(self.subscriptions)>=64:
                raise ValueError('subscriber_limit')
            self.subscriptions[owner] = (current, watch)
            selected = self.symbols()
            rejected = [s for s in ((current,) if current else ()) + watch if s not in selected]
            return dict(active_symbols=selected, rejected_symbols=rejected,
                        subscription_status='DEGRADED_LIMIT' if rejected else 'ACTIVE')

    def unsubscribe(self, owner):
        with self.lock:self.subscriptions.pop(owner, None)

    def symbols(self):
        with self.lock:
            # Current symbols precede watchlists; deterministic union across viewers.
            current = [c for c, _ in self.subscriptions.values() if c]
            watch = [s for _, w in self.subscriptions.values() for s in w]
            return list(dict.fromkeys(current + watch))[:self.limit]

    def poll_once(self):
        if not self.poll_lock.acquire(blocking=False):return None
        try:
            selected = self.symbols()
            started = self.timer()
            if not selected or started < self.next_due:return None
            request_at = self.clock().isoformat()
            self.provider.last_observations.clear()
            batch = self.provider.snapshot(selected)
            received = self.clock()
            good = self.provider.health[batch.source].last_sample_good
            self.failures = 0 if good else self.failures + 1
            delay = self.interval if good else min(30, 2 ** min(self.failures, 5))
            if market_session(received) not in {'open', 'auction'}:delay = max(15, delay)
            self.next_due = max(started + delay, self.timer())
            with self.lock:
                active = set(self.symbols())
                # Reclaim removed symbols, including parser freshness/last-good state.
                if set(self.buffer._index) - active:
                    old = self.buffer
                    self.buffer = RealtimeSnapshotBuffer(retention_seconds=600)
                    for symbol in active:
                        for point in old.points(symbol):self.buffer.append(symbol, point)
                self.quotes = {s:q for s,q in self.quotes.items() if s in active}
                self.samples = {s:v for s,v in self.samples.items() if s in active}
                for adapter in (getattr(self.provider, 'primary', None), getattr(self.provider, 'fallback', None)):
                    if adapter:
                        for name in ('_last_good', '_previous_raw'):
                            cache=getattr(adapter,name,{})
                            for s in list(cache):
                                if s not in active:cache.pop(s)
                        adapter.freshness.previous = {k:v for k,v in adapter.freshness.previous.items() if k[0] in active}
                self.sequence += 1
                for symbol, q in batch.snapshots.items():
                    if symbol not in active:continue
                    record=q.to_dict()
                    record.update(channel='fast', trade_date=q.quote_time.date().isoformat() if q.quote_time else None,
                        received_at=received.isoformat(), request_at=request_at, request_status='SUCCESS' if symbol in batch.returned_symbols else 'UNAVAILABLE',
                        snapshot_seq=self.sequence, lastPrice=q.price,lastClose=q.prev_close,change_pct=q.pct_change,amount=q.amount_cny)
                    samples=self.samples.setdefault(symbol,deque(maxlen=1200))
                    if q.quote_time and q.price and q.quote_status!='UNAVAILABLE':
                        timestamp=q.quote_time.timestamp()
                        if not samples or timestamp>samples[-1]['timestamp']:
                            self.buffer.append_price(symbol,timestamp,q.price)
                            samples.append(dict(timestamp=timestamp,price=q.price,
                                cumulative_volume=q.volume_shares,cumulative_amount=q.amount_cny))
                            while samples and samples[0]['timestamp']<timestamp-600:samples.popleft()
                    record.update({f'speed_{m}m':self.buffer.speed(symbol,m) for m in (1,3,5)})
                    self.quotes[symbol]=record
                self.request_count += len(self.provider.last_observations) or 1
                self.success += int(good);self.failure += int(not good)
                metric=dict(request_at=request_at,received_at=received.isoformat(),latency=self.timer()-started,
                    active_symbols=selected,source=batch.source,success=good,errors=batch.errors)
                self.metrics.append(metric)
                payload=dict(channel='fast',snapshot_seq=self.sequence,source=batch.source,
                    received_at=received.isoformat(),quotes=dict(self.quotes),metrics=metric,
                    request_count=self.request_count,success=self.success,failure=self.failure)
            self.publish(payload)
            return payload
        finally:self.poll_lock.release()

    def _run(self):
        while not self.stop.wait(.05):
            try:self.poll_once()
            except Exception as exc:
                self.failures += 1
                self.next_due=self.timer()+min(30,2**min(self.failures,5))
                with self.lock:self.metrics.append(dict(error=type(exc).__name__,request_at=self.clock().isoformat()))

    def close(self):
        self.stop.set()
        if self.worker:self.worker.join(timeout=8)
        self.provider.close()
