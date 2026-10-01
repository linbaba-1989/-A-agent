"""Offline P4 history, frozen benchmark evaluation and P3 conditioning. No network."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import time
import numpy as np
import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.market_data.historical_store import HistoricalStore,DEFAULT_HISTORY_DB
from src.regime.models import VERSION,RegimeConfig,encode
from src.regime.features import build_features
from src.regime.engine import MarketRegimeEngine,cluster_diagnostic
from src.regime.evaluation import evaluate_benchmarks,time_splits
from src.regime.conditioning import evaluate_patterns
from src.regime.store import RegimeStore
from src.regime.service import get_current_market_regime,get_market_regime
from scripts.behavior_profiles import atomic

def p3_fingerprint(db):
    return db.execute('SELECT count(*),bit_xor(hash(analysis_id,profile_json)) FROM behavior_profiles').fetchone()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT_HISTORY_DB)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--progress',type=Path,required=True)
    args=parser.parse_args()
    if shutil.disk_usage(args.db.parent).free<15*1024**3: raise RuntimeError('free_disk_below_15GiB')
    process=psutil.Process(); start=time.perf_counter(); cpu=sum(process.cpu_times()[:2]); peak=process.memory_info().rss
    report=dict(model_version=VERSION,config_key=RegimeConfig().key,started_at=datetime.now().astimezone().isoformat(),
                no_network=True,paid_llm_calls=0,p3_patterns_retrained=False,phases={})
    def progress(phase):
        atomic(args.progress,dict(status='RUNNING',phase=phase,pid=process.pid,last_update=datetime.now().astimezone().isoformat()))
    with HistoricalStore(args.db) as history:
        before=history.counts(); p3_before=p3_fingerprint(history.db)
        progress('MARKET_FEATURES')
        then=time.perf_counter(); features=build_features(history)
        records=MarketRegimeEngine().evaluate(features)
        report['phases']['features_seconds']=time.perf_counter()-then
        peak=max(peak,process.memory_info().rss)
        counts=Counter(r['regime'] for r in records)
        if len(records)<400 or len(records)-counts['UNKNOWN']<200:
            raise RuntimeError('insufficient_valid_market_history_for_evaluation')
        atomic(args.output.with_name('regime-feature-evidence.json'),dict(records=records,config_key=RegimeConfig().key))
        report['regime_days']=dict(counts)
        report['transitions_including_unknown']=sum(a['regime']!=b['regime'] for a,b in zip(records,records[1:]))
        report['transitions_known_only']=sum(a['regime']!=b['regime'] for a,b in zip(records,records[1:]) if a['regime']!='UNKNOWN' and b['regime']!='UNKNOWN')
        report['records']=records
        split=time_splits([r['data_end_time'] for r in records])
        report['cluster_diagnostic']=cluster_diagnostic(records,(split['train'][1]+__import__('pandas').Timedelta(minutes=1)).isoformat())
        progress('FROZEN_BENCHMARKS')
        then=time.perf_counter(); benchmark,statistics=evaluate_benchmarks(history,records)
        report['benchmarks']=benchmark; report['phases']['benchmark_seconds']=time.perf_counter()-then
        peak=max(peak,process.memory_info().rss)
        progress('FROZEN_PATTERN_CONDITIONING')
        then=time.perf_counter(); patterns,conditional=evaluate_patterns(history,records)
        report['pattern_conditioning']=patterns; report['phases']['conditioning_seconds']=time.perf_counter()-then
        progress('PERSISTENCE_AUDIT')
        RegimeStore(history).save(records,statistics,conditional)
        report['history_rows_unchanged']=history.counts()==before
        report['history_counts']=history.counts()
        report['p3_profiles_unchanged']=p3_fingerprint(history.db)==p3_before
        report['tables']={name:history.db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in
            ('market_regime_features','market_regimes','regime_strategy_statistics','pattern_regime_statistics')}
        report['availability_violations']=history.db.execute('SELECT count(*) FROM market_regimes WHERE data_end_time>=as_of').fetchone()[0]
        history.checkpoint()
    report['current']=get_current_market_regime(db_path=args.db)
    samples=[]
    for _ in range(20):
        then=time.perf_counter(); get_market_regime('2026-09-30',db_path=args.db); samples.append(time.perf_counter()-then)
    report['query_seconds']=dict(p50=float(np.quantile(samples,.5)),p95=float(np.quantile(samples,.95)),max=max(samples))
    wal=Path(str(args.db)+'.wal')
    report['resources']=dict(elapsed_seconds=time.perf_counter()-start,cpu_seconds=sum(process.cpu_times()[:2])-cpu,
        peak_sampled_rss_bytes=max(peak,process.memory_info().rss),db_bytes=args.db.stat().st_size,
        wal_bytes=wal.stat().st_size if wal.exists() else 0,free_disk_bytes=shutil.disk_usage(args.db.parent).free)
    report['status']='PASS' if report['history_rows_unchanged'] and report['p3_profiles_unchanged'] and not report['availability_violations'] else 'FAIL'
    report['finished_at']=datetime.now().astimezone().isoformat()
    atomic(args.output,report)
    atomic(args.progress,dict(status=report['status'],phase='COMPLETE',pid=process.pid,last_update=report['finished_at']))
    print(encode({k:report[k] for k in ('status','regime_days','transitions_known_only','history_rows_unchanged','p3_profiles_unchanged','tables','resources','query_seconds')}))
    return 0 if report['status']=='PASS' else 1

if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        if '--progress' in sys.argv:
            atomic(Path(sys.argv[sys.argv.index('--progress')+1]),dict(status='FAILED',error=type(exc).__name__,
                last_update=datetime.now().astimezone().isoformat()))
        raise
