"""Read-only final P3 audit, after profile writers exit. No history download."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.market_data.historical_store import HistoricalStore, DEFAULT_HISTORY_DB
from src.behavior.models import FEATURES, BehaviorConfig
from src.behavior.swing_detector import SwingDetector
from src.behavior.store import read_profile, encode
from scripts.behavior_profiles import PILOT, atomic

BASE_COUNTS={"1d":2626244,"1m":908585,"5m":180942,"15m":60312,"30m":20498708}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=DEFAULT_HISTORY_DB)
    parser.add_argument("--job-id",default="p3-all-daily-v11")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    report={"audited_at":datetime.now().astimezone().isoformat(),"job_id":args.job_id}
    with HistoricalStore(args.db,read_only=True) as history:
        report["history_counts"]=history.counts()
        report["history_rows_preserved"]=report["history_counts"]==BASE_COUNTS
        names=[r[0] for r in history.db.execute("SHOW TABLES").fetchall() if r[0].startswith("behavior_")]
        report["tables"]={name:history.db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in names}
        report["jobs"]=history.db.execute("SELECT job_id,status,count(*) count FROM behavior_jobs GROUP BY job_id,status ORDER BY job_id,status").fetchdf().to_dict("records")
        report["current_job_states"]={r["status"]:r["count"] for r in report["jobs"] if r["job_id"]==args.job_id+":1d"}
        report["failures"]=history.db.execute("SELECT symbol,period,error FROM behavior_jobs WHERE job_id=? AND status='FAILED'",[args.job_id+":1d"]).fetchdf().to_dict("records")
        report["skipped_reasons"]=history.db.execute("SELECT error,count(*) count FROM behavior_jobs WHERE job_id=? AND status='SKIPPED' GROUP BY error",[args.job_id+":1d"]).fetchdf().to_dict("records")
        report["causal_checks"]={
            "segment_confirmation_not_after_extreme":history.db.execute("SELECT count(*) FROM behavior_segments WHERE confirmation_time<=end_time").fetchone()[0],
            "outcome_not_after_confirmation":history.db.execute("""SELECT count(*) FROM behavior_pattern_outcomes o JOIN behavior_segments s USING(analysis_id,segment_id) WHERE o.known_at<=s.confirmation_time""").fetchone()[0],
            "forbidden_feature_keys":history.db.execute("""SELECT count(*) FROM behavior_segments WHERE regexp_matches(features_json,'future|outcome|forward_return|known_at')""").fetchone()[0],
            "candidate_backdated":history.db.execute("""SELECT count(*) FROM behavior_strategy_candidates c JOIN behavior_profiles p USING(analysis_id) WHERE c.usable_from<=p.data_end_date""").fetchone()[0],
            "checkpoint_without_profile":history.db.execute("""SELECT count(*) FROM behavior_jobs j LEFT JOIN behavior_profiles p USING(analysis_id) WHERE j.status='DONE' AND p.analysis_id IS NULL""").fetchone()[0],
        }
        feature_rows=history.db.execute("SELECT features_json FROM behavior_segments").fetchall()
        report["feature_allowlist_exact"]=all(set(json.loads(r[0]))==set(FEATURES) for r in feature_rows)
        report["feature_rows_audited"]=len(feature_rows)
        report["cohort"]=[]
        for symbol in PILOT:
            data=history.get_two_year_behavior_dataset(symbol,"2026-09-30")
            f=data["daily"]
            train=f.iloc[:int(len(f)*.6)]
            returns=train.close.pct_change().dropna()
            path=train.close.to_numpy(float)
            efficiency=abs(path[-1]-path[0])/np.abs(np.diff(path)).sum() if np.abs(np.diff(path)).sum() else 0.0
            report["cohort"].append(dict(symbol=symbol,ready=data["ready"],daily_rows=len(f),
                thirty_rows=len(data["bars_30m"]),train_annualized_volatility=float(returns.std()*np.sqrt(252)),
                train_path_efficiency=float(efficiency),last_close=float(f.close.iloc[-1])))
        vol_order=sorted(report["cohort"],key=lambda r:r["train_annualized_volatility"])
        trend_order=sorted(report["cohort"],key=lambda r:r["train_path_efficiency"])
        for row in report["cohort"]:
            row["relative_volatility_group"]="LOW" if row in vol_order[:3] else "HIGH" if row in vol_order[-3:] else "MID"
            row["relative_path_group"]="RANGING" if row in trend_order[:3] else "TRENDING" if row in trend_order[-3:] else "MIXED"
        focus=history.get_bars("600498.SH","1d","2024-10-01","2026-09-30")
        pivots=SwingDetector().detect(focus)
        moves=[b.pivot_price/a.pivot_price-1 for a,b in zip(pivots,pivots[1:])]
        durations=[b.pivot_index-a.pivot_index for a,b in zip(pivots,pivots[1:])]
        report["600498_confirmed_swings"]=dict(count=len(moves),up=sum(x>0 for x in moves),down=sum(x<0 for x in moves),
            median_up_move=float(np.median([x for x in moves if x>0])),
            median_down_move=float(np.median([x for x in moves if x<0])),
            median_duration=float(np.median(durations)),scope="All confirmed swings, including indicator warmup.")
        report["focus"]={p:read_profile(history,"600498.SH",p) for p in ("1d","30m")}
        report["query_seconds"]={}
        for period in ("1d","30m"):
            samples=[]
            for _ in range(20):
                start=time.perf_counter(); read_profile(history,"600498.SH",period); samples.append(time.perf_counter()-start)
            report["query_seconds"][period]=dict(p50=float(np.quantile(samples,.5)),p95=float(np.quantile(samples,.95)),max=max(samples))
        before=sum(report["tables"].values())
    with HistoricalStore(args.db,read_only=True) as history:
        after=sum(history.db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in names)
        report["restart_reload_match"]=before==after
        report["all_daily_totals"]=history.db.execute("""SELECT count(*) profiles,
            sum(json_extract(p.profile_json,'$.summary.segments')::INTEGER) segments,
            sum(json_extract(p.profile_json,'$.summary.patterns')::INTEGER) patterns,
            sum(json_extract(p.profile_json,'$.summary.usable_patterns')::INTEGER) usable_patterns,
            sum(json_extract(p.profile_json,'$.summary.strategy_candidates')::INTEGER) strategy_candidates
            FROM behavior_jobs j JOIN behavior_profiles p USING(analysis_id) WHERE j.job_id=? AND j.status='DONE'""",[args.job_id+":1d"]).fetchdf().to_dict("records")[0]
    report["db_bytes"]=args.db.stat().st_size
    wal=Path(str(args.db)+".wal"); report["wal_bytes"]=wal.stat().st_size if wal.exists() else 0
    states=report["current_job_states"]
    report["current_job_complete"]=sum(states.values())==5578 and states.get("DONE",0)>0 and not states.get("PENDING",0) and not states.get("FAILED",0)
    report["status"]="PASS" if report["history_rows_preserved"] and not any(report["causal_checks"].values()) and report["feature_allowlist_exact"] and report["restart_reload_match"] and report["current_job_complete"] and all(r["ready"] for r in report["cohort"]) else "FAIL"
    atomic(args.output,report)
    print(encode({k:report[k] for k in ["status","history_rows_preserved","causal_checks","600498_confirmed_swings","all_daily_totals","db_bytes","query_seconds"]}))
    return 0 if report["status"]=="PASS" else 1

if __name__=="__main__":
    sys.exit(main())
