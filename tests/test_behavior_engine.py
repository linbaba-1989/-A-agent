from dataclasses import replace
import numpy as np
import pandas as pd
import pytest
from src.behavior.models import BehaviorConfig, FEATURES
from src.behavior.swing_detector import SwingDetector
from src.behavior.segment_features import build_segments
from src.behavior.pattern_engine import PatternModel
from src.behavior.pattern_statistics import confidence, outcomes, summarize, walk_forward_dates, rolling_walk_forward
from src.behavior.pattern_matcher import match_segment
from src.behavior.pipeline import BehaviorEngine
from src.behavior.store import BehaviorStore
from src.market_data.historical_store import HistoricalStore

def wave(n=500):
    x=np.arange(n); c=100+10*np.sin(x/8)+.03*x
    return pd.DataFrame(dict(timestamp=pd.bdate_range("2024-01-01",periods=n),open=c,high=c+1,low=c-1,close=c,
                             volume=1000+200*np.sin(x/5),amount=(1000+200*np.sin(x/5))*c*100))

def test_pivots_cannot_be_used_on_extremum_day_and_prefixes_do_not_repaint():
    f=wave()
    full=SwingDetector().detect(f)
    assert len(full)>10
    for p in full:
        assert p.confirmation_index>p.pivot_index
        assert p.confirmation_time>p.pivot_time
        before=SwingDetector().detect(f.iloc[:p.confirmation_index])
        assert p not in before
    for n in (100,200,350):
        assert SwingDetector().detect(f.iloc[:n])==[p for p in full if p.confirmation_index<n]

def test_segment_features_are_available_only_at_confirmation_and_future_invariant():
    f=wave(); n=300
    p=SwingDetector().detect(f); s=build_segments(f,p)
    prefix=build_segments(f.iloc[:n],SwingDetector().detect(f.iloc[:n]))
    assert prefix==[x for x in s if x["confirmation_index"]<n]
    assert all(set(x["features"])==set(FEATURES) for x in s)
    assert all(x["confirmation_time"]>x["end"] for x in s)

def test_outcomes_are_separate_and_censored_at_end():
    f=wave(); s=build_segments(f,SwingDetector().detect(f))
    labels=outcomes(f,s)
    assert labels and all(x["known_at"]>x["signal_time"] for x in labels)
    assert not any("future" in k or "outcome" in k for k in s[0]["features"])
    assert max(o["end_index"] for o in labels)<len(f)

def test_model_ignores_outcome_fields_and_is_deterministic():
    f=wave(); s=build_segments(f,SwingDetector().detect(f))
    a=PatternModel().fit(s)
    for item in s:
        item["future_return"]=1000
        item["features"]["future_return"]=-1000
    b=PatternModel().fit(s)
    assert a.payload()==b.payload()

def test_small_samples_and_overlapping_labels_do_not_claim_reliable_success():
    assert [confidence(n) for n in (2,5,9,10)]==["INSUFFICIENT","LOW_CONFIDENCE","LOW_CONFIDENCE","NORMAL"]
    rows=[dict(signal_time=f"2025-01-{i:02d}",known_at="2025-01-30",return_=.1,mfe=.2,mae=-.05) for i in range(1,11)]
    s=summarize(rows)
    assert s["sample_count"]==10 and s["effective_samples"]==1
    assert s["confidence"]=="INSUFFICIENT" and s["mean_lower_bound"] is None

def test_walk_forward_train_model_and_outcome_purge_no_random_shuffle():
    f=wave()
    a=BehaviorEngine().analyze("600498.SH",f)
    g=f.copy(); g.loc[400:,["open","high","low","close"]]*=1.5
    b=BehaviorEngine().analyze("600498.SH",g)
    assert a["model"]==b["model"]
    assert a["split"]["shuffle"] is False
    assert a["split"]["train_end"]<a["split"]["validation_end"]<a["split"]["out_of_sample_end"]
    for pattern in a["patterns"]:
        for h,stat in pattern["statistics"]["train"].items():
            eligible=[o for o in a["outcomes"] if o["pattern_id"]==pattern["pattern_id"] and o["split"]=="train" and str(o["horizon"])==h and o["known_at"]<=a["split"]["train_end"] and o["similarity"]>=a["config"]["similarity_floor"]]
            assert stat["sample_count"]==len(eligible)
    assert list(rolling_walk_forward(f))

def test_match_rejects_future_pivot_and_does_not_match_itself():
    f=wave(); s=build_segments(f,SwingDetector().detect(f)); m=PatternModel().fit(s[:10])
    with pytest.raises(ValueError,match="unconfirmed"):
        match_segment(m,s[-1],s,"2020-01-01",BehaviorConfig())
    result=match_segment(m,s[-1],s,s[-1]["confirmation_time"],BehaviorConfig())
    assert all(x["date"]<s[-1]["confirmation_time"] for x in result["historical_matches"])

def test_behavior_duckdb_atomic_persistence_and_resume(tmp_path):
    path=tmp_path/"history.duckdb"; f=wave(200); result=BehaviorEngine().analyze("600498.SH",f)
    with HistoricalStore(path) as history:
        store=BehaviorStore(history)
        store.plan("job",["600498.SH","000001.SZ"],"1d",result["config_key"],result["data_end_date"])
        aid=store.save(result,store.fingerprint(f),"job")
        assert store.pending("job")==["000001.SZ"]
        columns=[r[1] for r in store.db.execute("PRAGMA table_info('behavior_segments')").fetchall()]
        assert "forward_return" not in columns and "features_json" in columns
        store.plan("job",["600498.SH","000001.SZ"],"1d",result["config_key"],result["data_end_date"])
        assert store.status("job")=={"DONE":1,"PENDING":1}
        assert history.counts()["1d"]==0

    with HistoricalStore(path) as history:
        store=BehaviorStore(history)
        assert store.load("600498.SH")["summary"]==result["summary"]
        assert store.pending("job")==["000001.SZ"]
        with pytest.raises(ValueError,match="configuration"):
            store.plan("job",["600498.SH","000001.SZ"],"1d","changed",result["data_end_date"])


def test_atomic_progress_retries_windows_reader_lock_without_losing_checkpoint(tmp_path, monkeypatch):
    from pathlib import Path
    from scripts import behavior_profiles
    path=tmp_path/"progress.json"; path.write_text('{"old":true}')
    original=Path.replace; attempts=[]
    def temporarily_locked(self,target):
        attempts.append(1)
        if len(attempts)<3:
            raise PermissionError("reader")
        return original(self,target)
    monkeypatch.setattr(Path,"replace",temporarily_locked)
    monkeypatch.setattr(behavior_profiles.time,"sleep",lambda _:None)
    assert behavior_profiles.atomic(path,{"status":"RUNNING"})
    assert len(attempts)==3 and '"RUNNING"' in path.read_text()
    def locked(*_):
        raise PermissionError("reader")
    monkeypatch.setattr(Path,"replace",locked)
    assert behavior_profiles.atomic(path,{"new":True},required=False) is False
    assert '"RUNNING"' in path.read_text()


def test_30m_outcomes_are_trading_days_not_bar_counts():
    times=[pd.Timestamp("2025-01-06")+pd.Timedelta(days=d,hours=h,minutes=m) for d in range(4) for h,m in [(10,0),(10,30),(11,0),(11,30),(13,30),(14,0),(14,30),(15,0)]]
    f=wave(32); f["timestamp"]=times
    s=[dict(confirmation_index=0,confirmation_time=times[0].isoformat())]
    labels=outcomes(f,s,"30m")
    one=next(x for x in labels if x["horizon"]==1)
    assert one["end_index"]==15

def test_store_rollback_keeps_checkpoint_pending_and_other_symbols_independent(tmp_path, monkeypatch):
    f=wave(200); result=BehaviorEngine().analyze("600498.SH",f)
    with HistoricalStore(tmp_path/"atomic.duckdb") as history:
        store=BehaviorStore(history)
        store.plan("atomic",["600498.SH","000001.SZ"],"1d",result["config_key"],result["data_end_date"])
        original=store._insert
        def injected(table, records):
            if table=="behavior_pattern_outcomes":
                raise RuntimeError("injected_write_failure")
            return original(table,records)
        monkeypatch.setattr(store,"_insert",injected)
        with pytest.raises(RuntimeError):
            store.save(result,store.fingerprint(f),"atomic")
        assert store.status("atomic")=={"PENDING":2}
        assert store.db.execute("SELECT count(*) FROM behavior_segments").fetchone()[0]==0
        store.mark("atomic","600498.SH","1d","FAILED","injected_write_failure")
        assert store.pending("atomic")==["000001.SZ"]

def test_no_model_and_unmatched_current_state_are_not_normal_confidence():
    f=wave(); s=build_segments(f,SwingDetector().detect(f))
    empty=PatternModel().fit([])
    result=match_segment(empty,s[-1],s,s[-1]["confirmation_time"],BehaviorConfig())
    assert result["pattern_id"] is None and result["confidence"]=="INSUFFICIENT"
    model=PatternModel().fit(s[:10])
    from copy import deepcopy
    far=deepcopy(s[-1]); far["features"]={k:v+1e9 for k,v in far["features"].items()}
    result=match_segment(model,far,s,far["confirmation_time"],BehaviorConfig())
    assert result["pattern_id"] is None and result["match_status"]=="OUT_OF_DISTRIBUTION"

def test_current_partial_segment_is_not_backdated_as_a_confirmed_pivot():
    result=BehaviorEngine().analyze("600498.SH",wave())
    current=result["current_match"]
    if current.get("state")=="CURRENT_PARTIAL_SEGMENT":
        assert current["endpoint_is_confirmed_pivot"] is False
        assert current["segment_confirmed_at"]==result["data_end_date"]
    assert all(p["confirmation_index"]>p["pivot_index"] for p in result["pivots"])

def test_backtest_import_is_independent_of_behavior_import_order():
    import subprocess,sys
    result=subprocess.run([sys.executable,"-c","from src.backtest import BacktestEngine; from src.behavior import BehaviorEngine"],capture_output=True)
    assert result.returncode==0, result.stderr.decode()

@pytest.mark.parametrize("mode",["disk","stop_file"])
def test_cli_stops_before_new_symbols_and_preserves_checkpoint(tmp_path, monkeypatch, mode):
    import json
    import sys
    from types import SimpleNamespace
    from scripts import behavior_profiles
    db=tmp_path/"guard.duckdb"; output=tmp_path/"result.json"; progress=tmp_path/"progress.json"
    stop=tmp_path/"stop"
    if mode=="stop_file":
        stop.write_text("stop")
    monkeypatch.setattr(behavior_profiles.shutil,"disk_usage",lambda _:SimpleNamespace(free=(14 if mode=="disk" else 20)*1024**3))
    monkeypatch.setattr(behavior_profiles.signal,"signal",lambda *_:None)
    monkeypatch.setattr(sys,"argv",["behavior_profiles.py","pilot","--db",str(db),"--job-id","guard",
        "--output",str(output),"--progress",str(progress),"--stop-file",str(stop)])
    assert behavior_profiles.main()==2
    report=json.loads(output.read_text())
    assert report["status"]=="STOPPED" and report["history_rows_unchanged"]
    with HistoricalStore(db) as history:
        store=BehaviorStore(history)
        assert store.status("guard:1d")=={"PENDING":10}
        assert history.counts()["1d"]==0
