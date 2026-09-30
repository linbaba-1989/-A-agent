"""Opt-in historical backfill CLI. Never switches production realtime."""
from pathlib import Path
import argparse
import json
import signal
import sys
import shutil
import time
from threading import Event
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.market_data.historical_store import HistoricalStore,DEFAULT_HISTORY_DB
from src.market_data.historical_sync import HistoricalSyncService
from src.market_data.xtdc_history import XtHistorySource,daily_fallbacks,safe_error
from src.market_data.history_quality import moment
from src.market_data.historical import completed_date


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['initial_backfill','resume','incremental_update','gap_check'])
    parser.add_argument('--period',choices=['1d','1m','5m','15m','30m'],default='1d')
    parser.add_argument('--start',default='2024-10-01');parser.add_argument('--end',default='2026-09-30')
    parser.add_argument('--symbols',nargs='+');parser.add_argument('--all-a',action='store_true')
    parser.add_argument('--job-id',required=True);parser.add_argument('--db',type=Path,default=DEFAULT_HISTORY_DB)
    parser.add_argument('--batch-size',type=int,default=20)
    parser.add_argument('--stop-file',type=Path);parser.add_argument('--progress-file',type=Path)
    parser.add_argument('--xtdc-only',action='store_true')
    args=parser.parse_args()
    if args.action in {'initial_backfill','incremental_update'} and moment(args.end).date()>completed_date():
        parser.error('history end must be a completed trading date')
    if not args.all_a and not args.symbols and args.action!='resume':parser.error('use --all-a or --symbols')
    if args.all_a and args.period=='1m':parser.error('all-A 1m requires a separately capacity-reviewed retention plan')
    universe=json.loads((root/'data/hithink/reference/tickers.json').read_text(encoding='utf-8'))['data']['item']
    symbols=sorted({r['symbol'] for r in universe}) if args.all_a else args.symbols
    stop=Event();signal.signal(signal.SIGINT,lambda *_:stop.set())
    source=XtHistorySource();store=None;started=time.perf_counter()
    def progress(data):
        if args.stop_file and args.stop_file.exists():stop.set()
        free=shutil.disk_usage(args.db.parent).free
        if free<8*1024**3:stop.set();data['stop_reason']='DISK_RESERVE'
        data.update(elapsed_seconds=round(time.perf_counter()-started,2),db_bytes=args.db.stat().st_size if args.db.exists() else 0)
        print(json.dumps(data),flush=True)
        if args.progress_file:
            args.progress_file.parent.mkdir(parents=True,exist_ok=True)
            temp=args.progress_file.with_suffix('.tmp')
            temp.write_text(json.dumps(data,indent=2),encoding='utf-8');temp.replace(args.progress_file)
    try:
        if args.action!='gap_check':
            print('CONFIGURED='+str(source.client.configured).lower(),flush=True)
            source.connect();print('AUTH_SUCCESS',flush=True)
        store=HistoricalStore(args.db)
        if args.all_a:store.save_universe(args.job_id,universe)
        if args.action!='gap_check':
            verified=source.listing_dates([r['symbol'] for r in universe if not r.get('list_date')])
            store.enrich_listing_dates(verified)
            for row in universe:
                if not row.get('list_date') and row['symbol'] in verified:row['list_date']=verified[row['symbol']]
        try:
            calendar=store.calendar()
            if args.action not in {'resume','gap_check'} and (moment(args.start).date()<calendar.start or moment(args.end).date()>calendar.end):
                store.set_calendar(source.calendar(min(calendar.start,moment(args.start).date()),max(calendar.end,moment(args.end).date())))
        except KeyError:
            if args.action=='gap_check':raise ValueError('calendar_not_loaded')
            store.set_calendar(source.calendar(args.start,args.end))
        service=HistoricalSyncService(store,source,daily_fallbacks=() if args.xtdc_only else daily_fallbacks(),
            listing_dates={r['symbol']:r.get('list_date') for r in universe},batch_size=args.batch_size,stop_event=stop,progress=progress)
        progress({'job':args.job_id,'status':'STARTING'})
        if stop.is_set():return
        if args.action=='initial_backfill':result=service.initial_backfill(symbols,args.period,args.start,args.end,job_id=args.job_id)
        elif args.action=='resume':result=service.resume(args.job_id)
        elif args.action=='incremental_update':
            service.incremental_update(symbols,args.period,args.end,job_id=args.job_id,start_if_empty=args.start);result={'job':args.job_id,'status':'INCREMENTAL_COMPLETE'}
        else:
            result={'gaps':{s:len(service.gap_check(s,args.period,args.start,args.end).gaps) for s in symbols}}
        store.checkpoint();result['rows']=store.counts();progress(result)
    except Exception as exc:
        import traceback
        progress({'job':args.job_id,'status':'FAILED','error_code':safe_error(exc),'error_type':type(exc).__name__,
            'locations':[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(exc.__traceback__)]});sys.exit(1)
    finally:
        source.close()
        if store:store.close()


if __name__=='__main__':main()
