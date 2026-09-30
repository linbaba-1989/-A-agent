"""Resumable, single-writer backfill with bounded calls and durable checkpoints."""
from datetime import datetime,timedelta
from threading import Event
import time
import pandas as pd
from ..market_clock import beijing_now
from .contracts import canonical_symbol
from .history_quality import PERIODS,moment,validate_frame
from .xtdc_history import safe_error,HistorySourceError


class HistoricalSyncService:
    def __init__(self,store,primary,*,daily_fallbacks=(),listing_dates=None,batch_size=20,
                 max_attempts=3,stop_event=None,sleeper=time.sleep,progress=None):
        if not 1<=batch_size<=50:raise ValueError('invalid_batch_size')
        self.store,self.primary=store,primary
        self.sources={s.name:s for s in [primary,*daily_fallbacks]}
        self.fallbacks=list(daily_fallbacks)
        self.listing_dates=listing_dates or {}
        self.batch_size,self.max_attempts=batch_size,max_attempts
        self.stop=stop_event or Event();self.sleeper=sleeper;self.progress=progress or (lambda x:None)
        self.calendar=store.calendar()

    def _error_event(self,task,source,code):
        import uuid
        self.store.db.execute('INSERT INTO history_sync_events VALUES (?,?,?,?,?,?,?)',
            [str(uuid.uuid4()),task[0],task[1],task[2],source,code,beijing_now().replace(tzinfo=None)])

    @staticmethod
    def windows(start,end,period):
        start,end=moment(start),moment(end,True)
        days=180 if period=='1m' else 365
        while start<=end:
            last=min(end,datetime.combine(start.date()+timedelta(days=days-1),datetime.max.time()).replace(microsecond=0))
            yield start,last
            start=last+timedelta(seconds=1)

    def initial_backfill(self,symbols,period,start,end,*,job_id,adjustment='raw'):
        if period not in PERIODS or adjustment!='raw':raise ValueError('foundation_requires_raw_supported_period')
        if moment(start)>moment(end,True):raise ValueError('invalid_date_range')
        self.calendar.between(start,end)
        symbols=list(dict.fromkeys(canonical_symbol(s,allow_index=True) for s in symbols))
        now=beijing_now().replace(tzinfo=None);rows=[]
        existing=self.store.db.execute('SELECT DISTINCT period,adjustment,min(start_time),max(end_time) FROM history_sync_tasks WHERE job_id=? GROUP BY period,adjustment',[job_id]).fetchall()
        if existing and (len(existing)!=1 or existing[0]!=(period,adjustment,moment(start),moment(end,True))):
            # End is normalized below to seconds, matching SDK bounds.
            wanted=(period,adjustment,moment(start),moment(end,True).replace(microsecond=0))
            if len(existing)!=1 or existing[0]!=wanted:raise ValueError('job_id_range_mismatch')
        for first,last in self.windows(start,end,period):
            for symbol in symbols:
                source=self.store.selected_source(symbol,period,adjustment) or self.primary.name
                rows.append((job_id,symbol,period,adjustment,first,last,source,'PENDING',0,None,0,now))
        self.store.db.execute('BEGIN')
        try:
            plans=pd.DataFrame(rows,columns=['job_id','symbol','period','adjustment','start_time','end_time','source','status','attempts','error_code','row_count','updated_at'])
            self.store.db.register('_history_plan',plans)
            self.store.db.execute('INSERT OR IGNORE INTO history_sync_tasks BY NAME SELECT * FROM _history_plan')
            self.store.db.unregister('_history_plan')
            self.store.db.execute('COMMIT')
        except Exception:self.store.db.execute('ROLLBACK');raise
        return self.resume(job_id)

    def _state(self,task,status,*,error=None,rows=0,source=None):
        job,symbol,period,adjustment,start,end,chosen,attempts=task
        self.store.db.execute('UPDATE history_sync_tasks SET status=?,source=?,attempts=attempts+1,error_code=?,row_count=?,updated_at=? WHERE job_id=? AND symbol=? AND period=? AND adjustment=? AND start_time=?',
            [status,source or chosen,error,rows,beijing_now().replace(tzinfo=None),job,symbol,period,adjustment,start])

    def _put(self,task,frame,source):
        job,symbol,period,adjustment,start,end,_,attempts=task
        for key,wanted in [('source',source.name),('adjustment',adjustment)]:
            if key in frame and not frame[key].eq(wanted).all():raise ValueError('provenance_mismatch')
        original=None
        # Revalidate the whole affected trading-day span including already stored bars.
        # Incremental intraday writes must not erase gaps from earlier in the day.
        first=datetime.combine(start.date(),datetime.min.time())
        existing=self.store.get_bars(symbol,period,first,end,adjustment,source=source.name)
        if existing.empty:first=start
        if len(existing):
            original=validate_frame(frame,period,start,end,self.calendar,self.listing_dates.get(symbol))
            frame=original.frame
            incoming=frame.copy()
            if 'time' in incoming:
                incoming['timestamp']=pd.to_datetime(incoming.time,unit='ms',utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
            existing=existing[['timestamp','open','high','low','close','volume','amount']]
            incoming=incoming[['timestamp','open','high','low','close','volume','amount']] if len(incoming) else existing.iloc[:0]
            # Overlap upserts are intentional; only duplicate rows within a fetch are errors.
            if not incoming.timestamp.duplicated().any():
                existing=existing[~existing.timestamp.isin(incoming.timestamp)]
            frame=pd.concat([existing,incoming],ignore_index=True).sort_values('timestamp')
        q=self.store.ingest(symbol,period,frame,first,end,source=source.name,provider=source.provider,
            adjustment=adjustment,listing_date=self.listing_dates.get(symbol),calendar=self.calendar,
            volume_unit='sdk_native' if symbol in {'000001.SH','399001.SZ','399006.SZ'} and source.name=='xtdc' else source.volume_unit,
            event_id=f'{job}:{symbol}:{period}:{start.isoformat()}')
        if original is not None and (original.invalid_rows or original.reverse_pairs):
            import json
            self.store.db.execute('INSERT OR REPLACE INTO history_quality_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                [f'{job}:{symbol}:{period}:{start.isoformat()}',symbol,period,adjustment,source.name,'STRUCTURAL_ERROR',
                 original.invalid_rows,original.duplicate_rows,original.reverse_pairs,json.dumps(original.reasons),beijing_now().replace(tzinfo=None)])
        self._state(task,'DONE',rows=len(q.frame),source=source.name)
        # Once a series chooses a daily fallback, keep its remaining chunks on that source.
        if source.name!=task[6]:
            self.store.db.execute("UPDATE history_sync_tasks SET source=? WHERE job_id=? AND symbol=? AND period=? AND adjustment=? AND status<>'DONE'",[source.name,job,symbol,period,adjustment])

    def _fetch_one(self,task):
        job,symbol,period,adjustment,start,end,name,attempts=task
        source=self.sources[name]
        selected=self.store.selected_source(symbol,period,adjustment)
        candidates=[source]+(self.fallbacks if period=='1d' and not selected and name==self.primary.name else [])
        last_error='EMPTY'
        for candidate in candidates:
            for retry in range(self.max_attempts):
                if self.stop.is_set():return
                try:
                    frames=candidate.fetch([symbol],period,start,end,adjustment)
                    frame=frames.get(symbol,pd.DataFrame())
                    if frame.empty:raise HistorySourceError('EMPTY')
                    self._put(task,frame,candidate);return
                except Exception as exc:
                    last_error=safe_error(exc)
                    self._error_event(task,candidate.name,last_error)
                    self._state(task,'FAILED',error=last_error)
                    if last_error=='RATE_LIMITED':
                        self.progress(dict(job=job,status='RATE_LIMITED',backoff_seconds=15*(2**retry)))
                        self.sleeper(15*(2**retry))
                    elif last_error=='EMPTY':break
                    else:self.sleeper(min(2**retry,8))
        self.record_failed_window(task)

    def record_failed_window(self,task):
        """A wholly failed window is also a local gap, not just an error counter."""
        job,symbol,period,adjustment,start,end,name,attempts=task
        source=self.sources[name]
        existing=self.store.get_bars(symbol,period,start,end,adjustment,source=name)
        self.store.ingest(symbol,period,existing,start,end,source=name,provider=source.provider,
            adjustment=adjustment,listing_date=self.listing_dates.get(symbol),calendar=self.calendar,
            volume_unit=source.volume_unit,activate=False,event_id=f'{job}:{symbol}:{period}:{start.isoformat()}')

    def resume(self,job_id):
        # Refresh after each batch: fallback selection and cancellation are durable.
        seen=set()
        while not self.stop.is_set():
            rows=self.store.db.execute("SELECT job_id,symbol,period,adjustment,start_time,end_time,source,attempts FROM history_sync_tasks WHERE job_id=? AND status<>'DONE' ORDER BY start_time,source,symbol",[job_id]).fetchall()
            rows=[r for r in rows if (r[1],r[2],r[4]) not in seen]
            if not rows:break
            first=rows[0];batch=[r for r in rows if (r[2],r[3],r[4],r[5],r[6])==(first[2],first[3],first[4],first[5],first[6])][:self.batch_size]
            active=[]
            for task in batch:
                listed=self.listing_dates.get(task[1])
                grid=self.calendar.grid(task[2])
                no_observations=not ((grid>=task[4])&(grid<=task[5])).any()
                if no_observations or listed and moment(listed).date()>task[5].date():self._put(task,pd.DataFrame(),self.sources[task[6]])
                else:active.append(task)
            if active:
                source=self.sources[first[6]]
                try:
                    frames=source.fetch([t[1] for t in active],first[2],first[4],first[5],first[3])
                except Exception as exc:
                    code=safe_error(exc)
                    for task in active:
                        self._error_event(task,source.name,code)
                        self._state(task,'FAILED',error=code)
                    if code=='RATE_LIMITED':
                        self.progress(dict(job=job_id,status=code,backoff_seconds=15));self.sleeper(15)
                    frames={}
                for task in active:
                    if self.stop.is_set():break
                    frame=frames.get(task[1],pd.DataFrame())
                    if len(frame):
                        try:self._put(task,frame,source)
                        except Exception as exc:
                            code=safe_error(exc)
                            self._error_event(task,source.name,code);self._state(task,'FAILED',error=code)
                    else:self._fetch_one(task)
            seen.update((t[1],t[2],t[4]) for t in batch)
            stats=self.job_status(job_id);self.progress(stats)
        self.store.checkpoint()
        return self.job_status(job_id)

    def incremental_update(self,symbols,period,end,*,job_id,start_if_empty=None):
        for symbol in symbols:
            p=self.store.provenance(symbol,period)
            if p['last_bar']:
                first=p['last_bar']+timedelta(days=1) if period=='1d' else p['last_bar']+timedelta(seconds=1)
            elif start_if_empty is not None:first=moment(start_if_empty)
            else:raise ValueError('initial_range_required')
            if first<=moment(end,True):self.initial_backfill([symbol],period,first,end,job_id=job_id+':'+symbol)

    def gap_check(self,symbol,period,start,end,adjustment='raw'):
        f=self.store.get_bars(symbol,period,start,end,adjustment)
        return validate_frame(f,period,start,end,self.calendar,self.listing_dates.get(symbol))

    def job_status(self,job_id):
        counts=dict(self.store.db.execute('SELECT status,count(*) FROM history_sync_tasks WHERE job_id=? GROUP BY status',[job_id]).fetchall())
        return dict(job=job_id,tasks=sum(counts.values()),states=counts,stopped=self.stop.is_set())
