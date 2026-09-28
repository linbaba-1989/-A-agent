"""Official snapshots: paginated acquisition and shared upstream-time provenance."""
from dataclasses import dataclass, field, replace
from datetime import timedelta
from time import perf_counter
from threading import RLock
from .contracts import MarketSnapshot, SnapshotBatch, canonical_symbol
from .quality import Freshness, Health
from .hithink_reference import milliseconds, number
from ..market_clock import market_session, to_beijing


@dataclass
class HithinkSnapshotBatch(SnapshotBatch):
    capability_status: str = "UNAVAILABLE"
    total: int | None = None
    offset: int = 0
    page_size: int | None = None
    page_count: int = 0
    pages: list[dict] = field(default_factory=list)
    timestamp_scope: str = "page_latest_upstream_not_per_symbol"


class OfficialFreshness(Freshness):
    def status(self, quote, now):
        status, advanced = super().status(quote,now)
        now = to_beijing(now)
        # Official aggregate time can be refreshed on a non-trading date.
        if market_session(now,self.calendar) not in {"open","auction"}:
            day = now.date()
            while not self.calendar.is_trading_day(day):
                day -= timedelta(days=1)
            if (quote.price is not None and quote.price > 0 and quote.quote_time is not None
                and day <= quote.quote_time.date() <= now.date()
                and quote.quote_time <= now+timedelta(seconds=2)):
                return "CACHED",advanced
        return status,advanced


def parse_snapshot(row, stamp, names, index=False):
    symbol = canonical_symbol(row["thscode"],allow_index=index)
    price = number(row.get("last_price"))
    if price is not None and price <= 0:
        price = None
    volume, amount = number(row.get("volume")), number(row.get("turnover"))
    if (volume is not None and volume < 0) or (amount is not None and amount < 0):
        raise ValueError("negative_quote_volume_or_amount")
    return MarketSnapshot(symbol=symbol,name=names.get(symbol),price=price,
        prev_close=number(row.get("prev_price")),open=number(row.get("open_price")),
        high=number(row.get("high_price")),low=number(row.get("low_price")),
        change=number(row.get("price_change")) if price is not None else None,
        pct_change=number(row.get("price_change_ratio_pct")) if price is not None else None,
        volume_shares=volume,amount_cny=amount,quote_time=milliseconds(stamp),source="hithink")


class HithinkRealtimeProvider:
    def __init__(self,client,universe,calendar,clock=to_beijing):
        self.client,self.universe,self.calendar,self.clock=client,universe,calendar,clock
        self.freshness,self.health=OfficialFreshness(calendar=calendar),Health()
        self._lock=RLock()
        self.last_batch=None
        self._index_prices = {}

    def quote(self,symbol):
        symbol=canonical_symbol(symbol)
        return self.snapshot([symbol]).snapshots[symbol]

    def snapshot_all(self):
        return self.snapshot()

    def snapshot(self,symbols=None,*,index=False):
        if index and symbols is None:
            raise ValueError("index_requires_explicit_symbols")
        selected=None if symbols is None else list(dict.fromkeys(
            canonical_symbol(s,allow_index=index) for s in symbols))
        if selected == []:
            raise ValueError("empty_symbol_request")
        with self._lock:
            # One daily cached reference refresh, never one ticker request per snapshot page.
            self.universe.refresh()
            self.calendar.refresh()
            names={s:r["name"] for s,r in self.universe.rows.items()}
            started=perf_counter()
            rows,zero,errors,pages,advances={},[],[],[],[]
            index_evidence = {}
            total,offset,full=None,0,selected is None
            page_size=100  # Official default. No inferred maximum.
            groups=[selected[i:i+100] for i in range(0,len(selected),100)] if selected else None
            status="SUCCESS"
            for page in range(1000):  # Local safety bound, not an API limit.
                if full:
                    params={"limit":page_size,"offset":offset}
                else:
                    if page>=len(groups):
                        break
                    params={"thscodes":",".join(groups[page])}
                response=self.client.get("index_snapshot" if index else "snapshot",params)
                pages.append(dict(offset=offset if full else None,**response.metrics()))
                if not response.ok:
                    errors.append(response.status)
                    status=response.status
                    # Stop the batch on throttling/auth; isolate other failed pages if total known.
                    if response.retry_after or response.status in {"AUTH_NOT_CONFIGURED","AUTH_INVALID","PERMISSION_DENIED"} or total is None:
                        break
                else:
                    data=response.data
                    if full:
                        page_total=data.get("total")
                        if type(page_total) is not int or page_total<0:
                            status="PROTOCOL_ERROR"
                            errors.append("invalid_total")
                            break
                        if total is not None and total != page_total:
                            errors.append("total_changed_during_paging")
                        total=page_total
                    for row in data["item"]:
                        try:
                            q=parse_snapshot(row,data.get("timestamp"),names,index)
                            if selected is not None and q.symbol not in selected:
                                raise ValueError("unrequested_symbol")
                            if q.symbol in rows:
                                errors.append("duplicate_snapshot:"+q.symbol)
                            quote_status,advanced=self.freshness.status(q,self.clock())
                            if index:
                                page_time = q.quote_time
                                current = to_beijing(self.clock())
                                previous_price = self._index_prices.get(q.symbol)
                                available = q.price is not None and page_time is not None
                                same_day = available and page_time.date() == current.date()
                                in_session = market_session(current, self.calendar) in {"open", "auction"}
                                recent = same_day and -2 <= (current-page_time).total_seconds() <= 60
                                quote_status = ("UNAVAILABLE" if not available else
                                                "STALE" if in_session and not recent else "CACHED")
                                index_evidence[q.symbol] = dict(symbol=q.symbol, price=q.price,
                                    prev_close=q.prev_close, pct_change=q.pct_change,
                                    page_timestamp=page_time.isoformat() if page_time else None,
                                    price_changed_since_previous_sample=None if previous_price is None else q.price != previous_price,
                                    availability="AVAILABLE_CURRENT_SESSION" if in_session and recent else quote_status,
                                    per_symbol_live_verified=False)
                                self._index_prices[q.symbol] = q.price
                                q = replace(q, quote_time=None)  # Page time is not a trade timestamp.
                                advanced = None
                            rows[q.symbol]=replace(q,quote_status=quote_status)
                            if row.get("last_price")==0:
                                zero.append(q.symbol)
                            if advanced is not None:
                                advances.append(advanced)
                        except (ValueError,KeyError,TypeError):
                            errors.append("invalid_snapshot_row")
                if full:
                    offset+=page_size
                    if total is not None and offset>=total:
                        break
            else:
                errors.append("snapshot_page_safety_bound")
            requested=selected if selected is not None else sorted(set(rows)|set(self.universe.rows))
            denominator=len(selected) if selected is not None else total or len(requested)
            missing=[s for s in requested if s not in rows]
            snapshots={s:rows.get(s,MarketSnapshot(s,source="hithink")) for s in requested}
            valid=[s for s,q in rows.items() if q.price is not None and q.price>0]
            if errors or missing or (denominator and len(rows)!=denominator):
                status="PARTIAL" if rows else status
                if missing:
                    errors.append("missing_snapshots:"+str(len(missing)))
            elif rows:
                status="SUCCESS"
            elif status=="SUCCESS":
                status="DATA_NOT_READY"
            batch=HithinkSnapshotBatch(snapshots,requested,sorted(rows),valid,sorted(set(zero)),
                missing,len(rows)/denominator if denominator else 0,
                len(valid)/len(rows) if rows else 0,perf_counter()-started,"hithink",errors,
                any(advances) if advances else None,market_session(self.clock(),self.calendar),
                self.clock(),status,total,0,page_size if full else None,len(pages),pages)
            self.health.observe(batch)
            if index:
                batch.provider_evidence = dict(index_observations=index_evidence,
                    limitation="page timestamp only; no per-symbol trade timestamp; LIVE not asserted")
            self.last_batch=batch
            return batch
