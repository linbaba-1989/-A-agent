"""Opt-in local P3 pilot and resumable all-A daily profiles. No market downloads."""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import shutil
import signal
import sys
import time
from threading import Event
import numpy as np
import pandas as pd
import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.market_data.historical_store import HistoricalStore, DEFAULT_HISTORY_DB
from src.behavior.models import BehaviorConfig
from src.behavior.pipeline import BehaviorEngine
from src.behavior.store import BehaviorStore, encode

PILOT = ["600498.SH","600519.SH","600036.SH","600900.SH","002053.SZ",
         "000001.SZ","000651.SZ","002475.SZ","300750.SZ","300059.SZ"]
BASELINE = "aa232dacd0a8dbb829a0f8abbb9d82b4032d81f5"
START, END = "2024-10-01","2026-09-30"

def atomic(path, payload, *, required=True):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(encode(payload),encoding="utf-8")
    # Windows readers can briefly deny replacement. Never truncate the live JSON
    # and never abandon an otherwise durable job because its observer is reading.
    for attempt in range(10):
        try:
            temp.replace(path)
            return True
        except PermissionError:
            if attempt==9:
                if required:
                    raise
                return False
            time.sleep(min(.05*2**attempt,.5))

def eligible_symbols(history):
    # These predicates mirror the foundation's selected raw-series quality gate.
    return {r[0] for r in history.db.execute("""
        WITH present AS (
            SELECT s.symbol,count(DISTINCT s.period) n FROM history_selection s
            WHERE s.adjustment='raw' AND s.period IN ('1d','30m') GROUP BY s.symbol),
        bad AS (
            SELECT g.symbol FROM history_gaps g JOIN history_selection s
              ON s.symbol=g.symbol AND s.period=g.period AND s.source=g.source AND s.adjustment=g.adjustment
            WHERE g.period IN ('1d','30m') AND g.adjustment='raw'
              AND g.trade_date BETWEEN ? AND ? AND g.classification<>'EXPECTED_MISSING'
            UNION
            SELECT q.symbol FROM history_quality_events q JOIN history_selection s
              ON s.symbol=q.symbol AND s.period=q.period AND s.source=q.source AND s.adjustment=q.adjustment
            WHERE q.period IN ('1d','30m') AND q.adjustment='raw' AND q.invalid_rows+q.reverse_pairs>0
            UNION
            SELECT symbol FROM history_sync_tasks WHERE job_id IN ('p23b-daily-two-year','p23b-30m-two-year') AND status<>'DONE')
        SELECT p.symbol FROM present p WHERE p.n=2 AND p.symbol NOT IN (SELECT symbol FROM bad)
    """,[START,END]).fetchall()}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=["pilot","all-daily"])
    parser.add_argument("--db",type=Path,default=DEFAULT_HISTORY_DB)
    parser.add_argument("--job-id",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--progress",type=Path,required=True)
    parser.add_argument("--stop-file",type=Path)
    parser.add_argument("--pilot-evidence",type=Path)
    parser.add_argument("--max-new-symbols",type=int,help="Cooperatively stop at a durable checkpoint after this many new profiles.")
    args=parser.parse_args()
    if args.phase=="all-daily":
        if not args.pilot_evidence:
            parser.error("pilot evidence required")
        pilot=json.loads(args.pilot_evidence.read_text(encoding="utf-8"))
        if pilot.get("status")!="PASS" or pilot.get("symbols")!=PILOT or pilot.get("config")!=asdict(BehaviorConfig()) or any(p["config_key"]!=BehaviorConfig().key for p in pilot.get("profiles",[])):
            parser.error("ten-stock pilot must pass before all-A daily")
    config=BehaviorConfig(); stop=Event()
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    process=psutil.Process(); then=time.perf_counter(); cpu_start=sum(process.cpu_times()[:2])
    timings=[]; peak_rss=process.memory_info().rss; processed=0
    report=dict(phase=args.phase,baseline_sha=BASELINE,started_at=datetime.now().astimezone().isoformat(),
                config=asdict(config),symbols=PILOT if args.phase=="pilot" else None,profiles=[],errors=[],
                no_network=True,all_a_30m_started=False)
    with HistoricalStore(args.db) as history:
        before=history.counts(); store=BehaviorStore(history); engine=BehaviorEngine(history,config)
        symbols=PILOT if args.phase=="pilot" else [r[0] for r in history.db.execute("SELECT symbol FROM history_universe WHERE snapshot_id='p23b-daily-two-year' ORDER BY symbol").fetchall()]
        qualified=eligible_symbols(history)
        periods=("1d","30m") if args.phase=="pilot" else ("1d",)
        report["requested_symbols"]=len(symbols)
        for period in periods:
            job=args.job_id+":"+period
            store.plan(job,symbols,period,config.key,END)
            prior=store.status(job)
            pending=store.pending(job)
            for symbol in pending:
                if stop.is_set() or args.stop_file and args.stop_file.exists() or shutil.disk_usage(args.db.parent).free<15*1024**3 or args.max_new_symbols is not None and processed>=args.max_new_symbols:
                    stop.set(); break
                started=time.perf_counter()
                try:
                    if symbol not in qualified:
                        store.mark(job,symbol,period,"SKIPPED","FOUNDATION_DATASET_NOT_READY")
                    else:
                        if args.phase=="pilot":
                            dataset=history.get_two_year_behavior_dataset(symbol,END)
                            if not dataset["ready"]:
                                raise ValueError("pilot_dataset_not_ready")
                            frame=dataset["daily" if period=="1d" else "bars_30m"]
                        else:
                            frame=history.get_bars(symbol,period,START,END)
                        if frame.timestamp.dt.normalize().nunique()<100:
                            store.mark(job,symbol,period,"SKIPPED","INSUFFICIENT_TRAINING_HISTORY")
                        else:
                            result=engine.analyze(symbol,frame,period,execution_probe=args.phase=="pilot")
                            result["provenance"]=history.provenance(symbol,period)
                            result["input_scope"]={"start":START,"end":END,"adjustment":"raw","quality_gate":"FOUNDATION_READY"}
                            store.save(result,store.fingerprint(frame),job)
                except Exception as exc:
                    store.mark(job,symbol,period,"FAILED",type(exc).__name__)
                    report["errors"].append(dict(symbol=symbol,period=period,error=type(exc).__name__))
                timings.append(time.perf_counter()-started)
                processed+=1
                peak_rss=max(peak_rss,process.memory_info().rss)
                atomic(args.progress,dict(job_id=args.job_id,pid=process.pid,period=period,last_symbol=symbol,
                    status="RUNNING",states=store.status(job),last_update=datetime.now().astimezone().isoformat(),
                    elapsed_seconds=time.perf_counter()-then,rss_bytes=process.memory_info().rss,
                    db_bytes=args.db.stat().st_size,free_disk_bytes=shutil.disk_usage(args.db.parent).free),required=False)
            history.checkpoint()
            report.setdefault("jobs",{})[period]=dict(job_id=job,states=store.status(job),previous_states=prior,
                                                     resumed_skipped_done=prior.get("DONE",0))
            if args.phase=="pilot":
                for symbol in symbols:
                    profile=store.load(symbol,period)
                    if profile:
                        report["profiles"].append(profile)
            if stop.is_set():
                break
        report["history_rows_unchanged"]=history.counts()==before
        report["history_counts"]=history.counts()
        report["tables"]=[r[0] for r in history.db.execute("SHOW TABLES").fetchall() if r[0].startswith("behavior_")]
        query_samples=[]
        for _ in range(10):
            started=time.perf_counter(); store.load("600498.SH"); query_samples.append(time.perf_counter()-started)
        report["query_seconds"]=dict(p50=float(np.quantile(query_samples,.5)),p95=float(np.quantile(query_samples,.95)),max=max(query_samples))
        report["aggregate"]=history.db.execute("""SELECT j.period,j.status,count(*) n FROM behavior_jobs j WHERE starts_with(j.job_id,?) GROUP BY j.period,j.status""",[args.job_id+":"]).fetchdf().to_dict("records")
        report["profile_totals"]=history.db.execute("""SELECT j.period,count(*) profiles,
            sum(json_extract(p.profile_json,'$.summary.patterns')::INTEGER) patterns,
            sum(json_extract(p.profile_json,'$.summary.usable_patterns')::INTEGER) usable_patterns,
            sum(json_extract(p.profile_json,'$.summary.strategy_candidates')::INTEGER) strategy_candidates
            FROM behavior_jobs j JOIN behavior_profiles p ON p.analysis_id=j.analysis_id WHERE starts_with(j.job_id,?) GROUP BY j.period""",[args.job_id+":"]).fetchdf().to_dict("records")
    elapsed=time.perf_counter()-then
    report["resources"]=dict(elapsed_seconds=elapsed,cpu_seconds=sum(process.cpu_times()[:2])-cpu_start,
        peak_rss_bytes=peak_rss,db_bytes=args.db.stat().st_size,free_disk_bytes=shutil.disk_usage(args.db.parent).free,
        per_symbol_p50=float(np.quantile(timings,.5)) if timings else None,
        per_symbol_p95=float(np.quantile(timings,.95)) if timings else None)
    complete=all(not j["states"].get("PENDING",0) and not j["states"].get("FAILED",0) for j in report["jobs"].values())
    pilot_pass=len(report["profiles"])==20 and all(p["summary"]["segments"]>0 for p in report["profiles"]) if args.phase=="pilot" else True
    report["status"]="STOPPED" if stop.is_set() else "PASS" if complete and pilot_pass and report["history_rows_unchanged"] else "FAIL"
    report["finished_at"]=datetime.now().astimezone().isoformat()
    atomic(args.output,report)
    atomic(args.progress,dict(job_id=args.job_id,pid=process.pid,status=report["status"],jobs=report["jobs"],last_update=report["finished_at"]),required=False)
    print(encode({k:report[k] for k in ["status","phase","requested_symbols","jobs","resources","profile_totals","history_rows_unchanged"]}))
    return 0 if report["status"]=="PASS" else 2

if __name__=="__main__":
    sys.exit(main())
