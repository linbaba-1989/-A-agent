"""One persistent DuckDB historical store, separate from production realtime."""
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
import json
import duckdb
import pandas as pd
from ..market_clock import beijing_now
from .contracts import canonical_symbol
from .history_quality import PERIODS, moment, HistoricalCalendar, validate_frame

DEFAULT_HISTORY_DB = Path(__file__).resolve().parents[2] / 'data/market_history.duckdb'


class HistoricalStore:
    def __init__(self, path=DEFAULT_HISTORY_DB, read_only=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock = RLock()
        self.db = duckdb.connect(str(self.path),read_only=read_only)
        self.db.execute("SET memory_limit='4GB'")
        self.db.execute("SET threads=2")
        self.db.execute("SET preserve_insertion_order=false")
        if not read_only:self._schema()

    def _schema(self):
        for table in PERIODS.values():
            self.db.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                symbol VARCHAR NOT NULL, timestamp TIMESTAMP NOT NULL, trade_date DATE NOT NULL,
                open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
                volume DOUBLE NOT NULL, amount DOUBLE NOT NULL, source VARCHAR NOT NULL,
                provider VARCHAR NOT NULL, adjustment VARCHAR NOT NULL, quality_status VARCHAR NOT NULL,
                volume_unit VARCHAR NOT NULL, amount_unit VARCHAR NOT NULL, ingested_at TIMESTAMP NOT NULL,
                PRIMARY KEY(symbol,timestamp,adjustment,source),
                CHECK(low<=open AND open<=high AND low<=close AND close<=high),
                CHECK(volume>=0 AND amount>=0), CHECK(adjustment IN ('raw','qfq','hfq')))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS historical_calendar(day DATE PRIMARY KEY, source VARCHAR);
            CREATE TABLE IF NOT EXISTS history_settings(key VARCHAR PRIMARY KEY, value VARCHAR);
            CREATE TABLE IF NOT EXISTS history_universe(snapshot_id VARCHAR,symbol VARCHAR,listing_date DATE,
                reference_source VARCHAR,recorded_at TIMESTAMP,PRIMARY KEY(snapshot_id,symbol));
            CREATE TABLE IF NOT EXISTS history_selection(symbol VARCHAR,period VARCHAR,adjustment VARCHAR,source VARCHAR,
                PRIMARY KEY(symbol,period,adjustment));
            CREATE TABLE IF NOT EXISTS history_gaps(symbol VARCHAR,period VARCHAR,adjustment VARCHAR,source VARCHAR,
                trade_date DATE,first_missing TIMESTAMP,last_missing TIMESTAMP,missing_count INTEGER,
                classification VARCHAR,reason VARCHAR,checked_at TIMESTAMP,
                PRIMARY KEY(symbol,period,adjustment,source,trade_date));
            CREATE TABLE IF NOT EXISTS history_quality_events(id VARCHAR PRIMARY KEY,symbol VARCHAR,period VARCHAR,
                adjustment VARCHAR,source VARCHAR,classification VARCHAR,invalid_rows INTEGER,
                duplicate_rows INTEGER,reverse_pairs INTEGER,reasons VARCHAR,recorded_at TIMESTAMP);
            CREATE TABLE IF NOT EXISTS history_sync_tasks(job_id VARCHAR,symbol VARCHAR,period VARCHAR,
                adjustment VARCHAR,start_time TIMESTAMP,end_time TIMESTAMP,source VARCHAR,status VARCHAR,
                attempts INTEGER DEFAULT 0,error_code VARCHAR,row_count BIGINT,updated_at TIMESTAMP,
                PRIMARY KEY(job_id,symbol,period,adjustment,start_time));
            CREATE TABLE IF NOT EXISTS history_sync_events(id VARCHAR PRIMARY KEY,job_id VARCHAR,
                symbol VARCHAR,period VARCHAR,source VARCHAR,error_code VARCHAR,recorded_at TIMESTAMP);''')

    def set_calendar(self, calendar):
        with self.lock:
            old=self.db.execute("SELECT value FROM history_settings WHERE key='calendar_start'").fetchone()
            if old and (old[0] != str(calendar.start) or self.db.execute("SELECT value FROM history_settings WHERE key='calendar_end'").fetchone()[0] != str(calendar.end)):
                # Calendar windows may grow, but never silently discard old sessions.
                current=self.calendar()
                if calendar.start > current.start or calendar.end < current.end:raise ValueError('calendar_window_shrink')
            self.db.execute('BEGIN')
            try:
                self.db.execute('DELETE FROM historical_calendar')
                self.db.executemany('INSERT INTO historical_calendar VALUES (?,?)',[(d,calendar.source) for d in sorted(calendar.sessions)])
                for k,v in [('calendar_start',str(calendar.start)),('calendar_end',str(calendar.end)),('calendar_source',calendar.source)]:
                    self.db.execute('INSERT OR REPLACE INTO history_settings VALUES (?,?)',[k,v])
                self.db.execute('COMMIT')
            except Exception:self.db.execute('ROLLBACK');raise

    def calendar(self):
        settings=dict(self.db.execute('SELECT key,value FROM history_settings').fetchall())
        return HistoricalCalendar([r[0] for r in self.db.execute('SELECT day FROM historical_calendar').fetchall()],
            settings['calendar_start'],settings['calendar_end'],settings['calendar_source'])

    def selected_source(self,symbol,period,adjustment='raw'):
        row=self.db.execute('SELECT source FROM history_selection WHERE symbol=? AND period=? AND adjustment=?',[symbol,period,adjustment]).fetchone()
        return row[0] if row else None

    def save_universe(self,snapshot_id,rows,reference_source='hithink'):
        frame=pd.DataFrame([dict(snapshot_id=snapshot_id,symbol=canonical_symbol(r['symbol'],allow_index=True),
            listing_date=r.get('list_date'),reference_source=reference_source,recorded_at=beijing_now().replace(tzinfo=None)) for r in rows])
        frame['listing_date']=pd.to_datetime(frame.listing_date,errors='coerce').dt.date
        with self.lock:
            self.db.register('_history_universe',frame)
            self.db.execute('INSERT OR IGNORE INTO history_universe BY NAME SELECT * FROM _history_universe')
            self.db.unregister('_history_universe')

    def enrich_listing_dates(self,verified_dates):
        """Only explicit exchange/SDK metadata may turn unknown gaps into expected ones."""
        with self.lock:
            for symbol,day in verified_dates.items():
                self.db.execute("UPDATE history_universe SET listing_date=?,reference_source=reference_source||';xtdc.OpenDate' WHERE symbol=? AND listing_date IS NULL",[day,symbol])
                self.db.execute("UPDATE history_gaps SET classification='EXPECTED_MISSING',reason='BEFORE_VERIFIED_LISTING_DATE' WHERE symbol=? AND trade_date<? AND classification='UNKNOWN_MISSING'",[symbol,day])

    def ingest(self,symbol,period,frame,start,end,*,source,provider,adjustment='raw',listing_date=None,
               volume_unit='lot',amount_unit='CNY',calendar=None,event_id=None,activate=True):
        import uuid
        symbol=canonical_symbol(symbol,allow_index=True)
        if adjustment not in {'raw','qfq','hfq'}:raise ValueError('invalid_adjustment')
        if not source or not provider:raise ValueError('provenance_required')
        for key,wanted in [('source',source),('adjustment',adjustment)]:
            if key in frame and not frame[key].eq(wanted).all():raise ValueError('provenance_mismatch')
        quality=validate_frame(frame,period,start,end,calendar or self.calendar(),listing_date)
        f=quality.frame.copy();now=beijing_now().replace(tzinfo=None)
        for key,val in dict(symbol=symbol,source=source,provider=provider,adjustment=adjustment,
                quality_status='VALID',volume_unit=volume_unit,amount_unit=amount_unit,ingested_at=now).items():f[key]=val
        table=PERIODS[period]
        with self.lock:
            self.db.execute('BEGIN')
            try:
                if len(f):
                    self.db.register('_history_incoming',f)
                    self.db.execute(f'INSERT OR REPLACE INTO {table} BY NAME SELECT * FROM _history_incoming')
                    self.db.unregister('_history_incoming')
                self.db.execute('DELETE FROM history_gaps WHERE symbol=? AND period=? AND adjustment=? AND source=? AND trade_date BETWEEN ? AND ?',
                    [symbol,period,adjustment,source,moment(start).date(),moment(end,True).date()])
                if quality.gaps:self.db.executemany('INSERT INTO history_gaps VALUES (?,?,?,?,?,?,?,?,?,?,?)',[
                    (symbol,period,adjustment,source,g['trade_date'],g['first_missing'],g['last_missing'],g['missing_count'],g['classification'],g['reason'],now) for g in quality.gaps])
                if quality.invalid_rows or quality.reverse_pairs:
                    self.db.execute('INSERT OR REPLACE INTO history_quality_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        [event_id or str(uuid.uuid4()),symbol,period,adjustment,source,'STRUCTURAL_ERROR',quality.invalid_rows,
                         quality.duplicate_rows,quality.reverse_pairs,json.dumps(quality.reasons),now])
                if activate and len(f):
                    selected=self.selected_source(symbol,period,adjustment)
                    if selected and selected != source:raise ValueError('explicit_series_switch_required')
                    self.db.execute('INSERT OR REPLACE INTO history_selection VALUES (?,?,?,?)',[symbol,period,adjustment,source])
                self.db.execute('COMMIT')
            except Exception:self.db.execute('ROLLBACK');raise
        return quality

    def get_bars(self,symbol,period,start,end,adjustment='raw',*,source=None):
        symbol=canonical_symbol(symbol,allow_index=True)
        if period not in PERIODS or adjustment not in {'raw','qfq','hfq'}:raise ValueError('invalid_series')
        first,last=moment(start),moment(end,True)
        if first>last:raise ValueError('invalid_date_range')
        with self.lock:
            source=source or self.selected_source(symbol,period,adjustment)
            frame=self.db.execute(f'SELECT * FROM {PERIODS[period]} WHERE symbol=? AND adjustment=? AND source=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp',
                [symbol,adjustment,source,first,last]).fetchdf()
        frame.attrs.update(source=source,adjustment=adjustment,time_zone='Asia/Shanghai',network=False)
        return frame

    def provenance(self,symbol,period,adjustment='raw',source=None):
        source=source or self.selected_source(symbol,period,adjustment)
        with self.lock:
            row=self.db.execute(f'SELECT count(*),min(timestamp),max(timestamp),max(ingested_at),any_value(provider) FROM {PERIODS[period]} WHERE symbol=? AND adjustment=? AND source=?',[symbol,adjustment,source]).fetchone()
            gaps=self.db.execute('SELECT classification,sum(missing_count) FROM history_gaps WHERE symbol=? AND period=? AND adjustment=? AND source=? GROUP BY classification',[symbol,period,adjustment,source]).fetchall()
            bad=self.db.execute('SELECT coalesce(sum(invalid_rows+reverse_pairs),0) FROM history_quality_events WHERE symbol=? AND period=? AND adjustment=? AND source=?',[symbol,period,adjustment,source]).fetchone()[0]
        gap_counts=dict(gaps)
        return dict(symbol=symbol,period=period,source=source,provider=row[4],adjustment=adjustment,rows=row[0],first_bar=row[1],last_bar=row[2],ingested_at=row[3],gap_count=sum(gap_counts.values()),gaps=gap_counts,invalid_count=bad,quality='UNAVAILABLE' if not row[0] else 'PARTIAL' if bad or gap_counts.get('UNKNOWN_MISSING',0) else 'VALID')

    def get_two_year_behavior_dataset(self,symbol,end=None):
        end=moment(end or self.calendar().end,True)
        start=(pd.Timestamp(end.date())-pd.DateOffset(years=2)+pd.Timedelta(days=1)).to_pydatetime()
        provenance={p:self.provenance(symbol,p) for p in ['1d','30m']}
        frames={p:self.get_bars(symbol,p,start,end) for p in ['1d','30m']}
        dates=self.db.execute('SELECT DISTINCT listing_date FROM history_universe WHERE symbol=? AND listing_date IS NOT NULL',[symbol]).fetchall()
        listed=dates[0][0] if len(dates)==1 else None
        reasons=[];window_quality={};calendar=self.calendar()
        try:
            calendar.between(start,end)
            for period,frame in frames.items():
                q=validate_frame(frame,period,start,end,calendar,listed)
                window_quality[period]=dict(status=q.status,unknown_missing=sum(g['missing_count'] for g in q.gaps if g['classification']=='UNKNOWN_MISSING'),
                    expected_missing=sum(g['missing_count'] for g in q.gaps if g['classification']=='EXPECTED_MISSING'),invalid=q.invalid_rows)
                if frame.empty or q.status!='VALID':reasons.append(period+':INCOMPLETE_WINDOW')
        except ValueError:reasons.append('CALENDAR_RANGE_UNVERIFIED')
        if any(p['quality']!='VALID' or not p['rows'] for p in provenance.values()):reasons.append('SERIES_QUALITY')
        return dict(symbol=symbol,start=start,end=end,daily=frames['1d'],bars_30m=frames['30m'],provenance=provenance,
            window_quality=window_quality,ready=not reasons,readiness_reasons=reasons,network=False)

    def counts(self):
        return {p:self.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for p,t in PERIODS.items()}

    def checkpoint(self):self.db.execute('CHECKPOINT')
    def close(self):self.db.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
