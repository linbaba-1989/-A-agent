"""Official range-bounded REST daily history, same bar/frame shape as free history."""
from dataclasses import replace
from datetime import datetime,time,timedelta
from threading import RLock
from time import perf_counter,monotonic
from .contracts import canonical_symbol
from .historical import HistoricalBar,HistoryResult,validate,completed_date,ADJUSTMENTS,INDEX_SYMBOLS
from .hithink_reference import milliseconds, number
from ..market_clock import BEIJING_TZ,to_beijing

ADJUST={"raw":"none","qfq":"forward","hfq":"backward"}


def date_ms(day):
    return int(datetime.combine(day,time(),BEIJING_TZ).timestamp()*1000)


def parse_bars(rows,symbol,adjustment):
    bars,errors=[],[]
    for row in rows:
        try:
            stamp=milliseconds(row.get("date_ms"))
            if stamp is None:
                raise ValueError("missing_bar_date")
            bars.append(HistoricalBar(symbol,stamp.date(),
                **{k:number(row.get(k+"_price")) for k in ("open","high","low","close")},
                volume_shares=number(row.get("volume")),amount_cny=number(row.get("turnover")),
                source="hithink",adjustment=adjustment))
        except (ValueError,TypeError,KeyError):
            errors.append("invalid_historical_row")
    return bars,errors


class HithinkHistoryProvider:
    name="hithink"
    endpoint="official_rest"
    def __init__(self,client,calendar,cache,clock=to_beijing,timer=monotonic):
        self.client,self.calendar,self.cache,self.clock=client,calendar,cache,clock
        self.timer=timer
        self._retry={}
        self._lock=RLock()
        self.last_results={}

    def daily(self,symbol,start,end,adjustment="raw",kind="equity",interval="1d"):
        symbol=canonical_symbol(symbol,allow_index=kind=="index")
        if adjustment not in ADJUSTMENTS or kind not in {"equity","index"} or interval!="1d":
            raise ValueError("unsupported_history_request")
        if kind=="index" and adjustment!="raw":
            return HistoryResult(symbol,source=self.name,adjustment=adjustment,
                                 quality_status="UNSUPPORTED",warnings=["index_has_no_adjustment"])
        if start>end or (end-start).days>3650:
            raise ValueError("invalid_or_over_ten_year_window")
        params=dict(thscode=symbol,interval="1d",start=date_ms(start),end=date_ms(end))
        if kind!="index":
            params["adjust"]=ADJUST[adjustment]
        response=self.client.get("index_history" if kind=="index" else "history",params)
        if not response.ok:
            return HistoryResult(symbol,source=self.name,adjustment=adjustment,
                warnings=[response.status],latency=response.elapsed)
        bars,errors=parse_bars(response.data["item"],symbol,adjustment)
        result=validate(bars,symbol,self.name,adjustment,start,end,self.clock(),self.calendar)
        result.warnings.extend(errors)
        if errors:
            result.quality_status="DEGRADED" if bars else "UNAVAILABLE"
            result.data_status="PARTIAL" if bars else "UNAVAILABLE"
        result.latency=response.elapsed
        return result

    def load(self,symbol,count=120,adjustment="raw",kind=None,network=True):
        kind=kind or ("index" if symbol in INDEX_SYMBOLS or symbol.endswith(".TI") else "equity")
        symbol=canonical_symbol(symbol,allow_index=kind=="index")
        if type(count) is not int or not 1<=count<=1500 or adjustment not in ADJUSTMENTS:
            raise ValueError("invalid_history_request")
        started=perf_counter()
        with self._lock:
            if network:
                self.calendar.refresh()
            now=self.clock()
            target=completed_date(now,self.calendar)
            start=target-timedelta(days=max(30,int(count*2.2)+14))
            cached=self.cache.read(symbol,self.name,adjustment,kind,self.endpoint,now,self.calendar)
            key=(symbol,adjustment,kind)
            enough=cached and len(cached.bars)>=count
            if enough and cached.bars[-1].trade_date==target:
                result=cached
            elif not network or self.timer()<self._retry.get(key,float("-inf")):
                result=cached or HistoryResult(symbol,source=self.name,adjustment=adjustment)
                result.warnings.append("cache_only_or_retry_cooldown")
            else:
                request_start=cached.bars[-1].trade_date if enough else start
                result=self.daily(symbol,request_start,target,adjustment,kind)
                if result.quality_status=="VALID" and result.bars:
                    if enough:
                        old={b.trade_date:b for b in cached.bars}
                        overlap=[b for b in result.bars if b.trade_date in old]
                        rebased=adjustment!="raw" and (not overlap or any(
                            any(getattr(b,k)!=getattr(old[b.trade_date],k) for k in ("open","high","low","close"))
                            for b in overlap))
                        if rebased:
                            result=self.daily(symbol,cached.bars[0].trade_date,target,adjustment,kind)
                            result.warnings.append("adjusted_basis_changed_full_refresh")
                        else:
                            old.update({b.trade_date:b for b in result.bars})
                            result=validate(list(old.values()),symbol,self.name,adjustment,
                                cached.bars[0].trade_date,target,now,self.calendar)
                    self.cache.write(result,kind,self.endpoint,now)
                if result.quality_status!="VALID" or len(result.bars)<count:
                    self._retry[key]=self.timer()+300
                if not result.bars and cached:
                    cached.warnings.extend(result.warnings+["cached_after_official_failure"])
                    cached.quality_status="DEGRADED"
                    result=cached
            result.bars=result.bars[-count:]
            # Validate the requested tail, not discarded fetch slack outside the
            # official rolling calendar. Keep every gap inside the returned series.
            if (len(result.bars) == count and result.bars[-1].trade_date == target
                    and result.warnings and all(w.startswith("missing_session:") for w in result.warnings)
                    and all(w.split(":", 1)[1] < result.bars[0].trade_date.isoformat() for w in result.warnings)):
                result=validate(result.bars,symbol,self.name,adjustment,
                                result.bars[0].trade_date,target,now,self.calendar)
                if network:
                    self.cache.write(result,kind,self.endpoint,now)
            if result.bars and (len(result.bars)<count or result.bars[-1].trade_date<target):
                if result.data_status!="CACHED":
                    result.data_status="PARTIAL"
                if result.bars[-1].trade_date<target:
                    result.quality_status="DEGRADED"
                result.warnings.append("insufficient_or_outdated_history")
            result.requested_end=target
            result.latency=perf_counter()-started
            self.last_results[symbol]=result
            return result

    def get_history(self,symbols,period="1d",count=120,**kwargs):
        if period!="1d":
            raise ValueError("only_daily_supported")
        return {s:self.load(s,count,**kwargs).frame() for s in dict.fromkeys(symbols)}

    def get_local_history(self,symbols,count=120,**kwargs):
        return self.get_history(symbols,"1d",count,network=False,**kwargs)
