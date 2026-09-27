"""Independent official facade. No Tencent/Sina/AKShare/BaoStock auto fallback."""
from pathlib import Path
from .hithink_client import HithinkRestClient
from .hithink_reference import ReferenceCache,HithinkSecurityUniverseProvider,HithinkCalendar,HithinkSectorProvider
from .hithink_realtime import HithinkRealtimeProvider
from .hithink_history import HithinkHistoryProvider
from .history_cache import FreeHistoryCache
from ..market_clock import to_beijing

DEFAULT_PATH=Path(__file__).resolve().parents[2]/"data/hithink"


class HithinkOfficialProvider:
    provider_name="HithinkOfficialProvider"
    def __init__(self,client=None,cache_path=DEFAULT_PATH,clock=to_beijing):
        self.client=client if client is not None else HithinkRestClient()
        references=ReferenceCache(Path(cache_path)/"reference",clock)
        self.universe=HithinkSecurityUniverseProvider(self.client,references)
        self.calendar=HithinkCalendar(self.client,references,clock)
        self.sectors=HithinkSectorProvider(self.client,references)
        self.realtime=HithinkRealtimeProvider(self.client,self.universe,self.calendar,clock)
        self.history=HithinkHistoryProvider(self.client,self.calendar,
                                          FreeHistoryCache(Path(cache_path)/"history"),clock)

    def quote(self,symbol):
        return self.realtime.quote(symbol)

    def snapshot(self,symbols):
        return self.realtime.snapshot(symbols)

    def snapshot_all(self):
        return self.realtime.snapshot_all()

    def index_snapshot(self,symbols):
        return self.realtime.snapshot(symbols,index=True)

    def get_stock_universe(self):
        return [s.symbol for s in self.universe.get()]

    def get_history(self,*args,**kwargs):
        return self.history.get_history(*args,**kwargs)

    def get_local_history(self,*args,**kwargs):
        return self.history.get_local_history(*args,**kwargs)

    def close(self):
        pass
