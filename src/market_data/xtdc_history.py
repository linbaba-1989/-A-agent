"""Bounded XTDC history transport and existing daily fallback adapters."""
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty
from threading import Thread
import os
import subprocess
import sys
import pandas as pd
from dotenv import dotenv_values
from ..xtdc_provider import (XtDataCenterProvider, SubprocessXtDataCenterBackend,
    TOKEN_RUNTIME_VERSION, WORKER_PATH)
from .history_quality import HistoricalCalendar, moment


class HistorySourceError(RuntimeError):
    pass


def safe_error(exc):
    if type(exc).__name__=='OutOfMemoryException':return 'LOCAL_MEMORY_LIMIT'
    if type(exc).__name__ in {'ConstraintException','BinderException','CatalogException'}:return 'LOCAL_STORAGE_ERROR'
    message=str(exc).lower()
    if any(x in message for x in ['rate_limit','rate limit','限流','频繁','频率','429']):return 'RATE_LIMITED'
    if isinstance(exc,TimeoutError) or 'timeout' in message:return 'TIMEOUT'
    if any(x in message for x in ['permission','权限','auth_invalid']):return 'PERMISSION_DENIED'
    if 'empty' in message:return 'EMPTY'
    return 'UPSTREAM_ERROR'


class HistoricalXtBackend(SubprocessXtDataCenterBackend):
    """Only the opt-in history path uses this deadline and quiet worker."""
    def __init__(self,data_home,**kwargs):
        super().__init__(**kwargs)
        self.data_home=Path(data_home).resolve()

    def start(self,token):
        self.data_home.mkdir(parents=True,exist_ok=True)
        self.process=subprocess.Popen([sys.executable,'-u',str(WORKER_PATH),str(self.runtime_root),'--quiet-history'],
            cwd=self.data_home,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',bufsize=1)
        return self.request('init',token=token,version=TOKEN_RUNTIME_VERSION,data_home=str(self.data_home))

    def request(self,command,**payload):
        result=Queue(maxsize=1)
        def call():
            try:result.put((True,super(HistoricalXtBackend,self).request(command,**payload)))
            except Exception as exc:result.put((False,exc))
        Thread(target=call,daemon=True).start()
        try:ok,value=result.get(timeout=self.timeout)
        except Empty:
            if self.process and self.process.poll() is None:
                self.process.terminate();self.process.wait(timeout=5)
            raise TimeoutError('history_rpc_deadline') from None
        if not ok:raise HistorySourceError(safe_error(value)) from None
        return value

    def close(self):
        if self.process and self.process.poll() is None:
            try:self.request('close');self.process.wait(timeout=5)
            except Exception:self.process.terminate();self.process.wait(timeout=5)
        self.process=None


class XtHistorySource:
    name='xtdc'
    provider='XtDataCenter Token'
    volume_unit='lot'
    def __init__(self,env_path=None,data_home=None,timeout=90):
        root=Path(__file__).resolve().parents[2]
        self.env_path=Path(env_path or root/'.env')
        # Deliberately do not inherit an XTDC_TOKEN from process environment.
        token=dotenv_values(self.env_path).get('XTDC_TOKEN','') or ''
        self.client=XtDataCenterProvider(token=token,backend_factory=lambda:HistoricalXtBackend(
            data_home or root/'data/xtdc_history_sdk',timeout=timeout))

    def connect(self):
        if not self.client.init():raise HistorySourceError('AUTH_FAILED')

    def fetch(self,symbols,period,start,end,adjustment='raw'):
        try:
            if not self.client.backend or not self.client.backend.process or self.client.backend.process.poll() is not None:self.connect()
            first,last=moment(start).strftime('%Y%m%d%H%M%S'),moment(end,True).strftime('%Y%m%d%H%M%S')
            self.client.download_history_range(symbols,period,first,last)
            return self.client.get_history_range(symbols,period,first,last,adjustment=adjustment)
        except Exception as exc:raise HistorySourceError(safe_error(exc)) from None

    def calendar(self,start,end):
        dates=self.client.backend.request('get_trading_dates',market='SH',start_time=moment(start).strftime('%Y%m%d'),end_time=moment(end).strftime('%Y%m%d'))
        days=pd.to_datetime(dates,unit='ms',utc=True).tz_convert('Asia/Shanghai').date
        return HistoricalCalendar(days,start,end,'xtdc.get_trading_dates(SH)')

    def listing_dates(self,symbols):
        result={}
        for symbol in symbols:
            try:
                detail=self.client.get_instrument_detail(symbol)
                day=datetime.strptime(str(detail.get('OpenDate','')),'%Y%m%d').date()
                if day.year>=1990:result[symbol]=day.isoformat()
            except Exception:
                pass  # Missing metadata is UNKNOWN, never a fabricated listing date.
        return result

    def close(self):self.client.close()


class DailyHistorySource:
    """Reuse Hithink/AKShare/BaoStock range APIs; no parallel cache architecture."""
    volume_unit='shares'
    def __init__(self,adapter):
        self.adapter=adapter;self.name=adapter.name;self.provider=adapter.name

    def fetch(self,symbols,period,start,end,adjustment='raw'):
        if period!='1d':raise HistorySourceError('DAILY_ONLY')
        frames={}
        for symbol in symbols:
            kind='index' if symbol in {'000001.SH','399001.SZ','399006.SZ'} else 'equity'
            result=self.adapter.daily(symbol,moment(start).date(),moment(end,True).date(),adjustment,kind)
            if 'RATE_LIMITED' in result.warnings:raise HistorySourceError('RATE_LIMITED')
            f=result.frame()
            f['timestamp']=pd.to_datetime(f['trade_date'])
            frames[symbol]=f
        return frames


def daily_fallbacks(env_path=None):
    from .hithink_provider import HithinkOfficialProvider
    from .hithink_client import HithinkRestClient
    from .adapters.history import AKShareHistoryProvider,BaoStockHistoryProvider
    root=Path(__file__).resolve().parents[2]
    values=dotenv_values(env_path or root/'.env')
    key=values.get('HITHINK_FINANCE_API_KEY') or os.getenv('HITHINK_FINANCE_API_KEY')
    creds=Path(os.getenv('APPDATA',''))/'hithink-finance/credentials.env'
    if not key and creds.exists():key=dotenv_values(creds).get('HITHINK_FINANCE_API_KEY')
    key=key or values.get('FUYAO_API_KEY')
    official=HithinkOfficialProvider(client=HithinkRestClient(api_key=key))
    return [DailyHistorySource(official.history),DailyHistorySource(AKShareHistoryProvider()),DailyHistorySource(BaoStockHistoryProvider())]
