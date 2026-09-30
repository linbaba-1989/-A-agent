"""Behavior tables alongside the existing historical store; atomic per-symbol checkpoints."""
import json
import hashlib
from datetime import datetime
import pandas as pd

def encode(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,default=str)

class BehaviorStore:
    def __init__(self, historical_store):
        self.history=historical_store; self.db=historical_store.db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS behavior_segments(
                analysis_id VARCHAR,segment_id VARCHAR,symbol VARCHAR,period VARCHAR,
                start_time TIMESTAMP,end_time TIMESTAMP,confirmation_time TIMESTAMP,
                direction VARCHAR,features_json VARCHAR,PRIMARY KEY(analysis_id,segment_id));
            CREATE TABLE IF NOT EXISTS behavior_patterns(
                analysis_id VARCHAR,pattern_id VARCHAR,model_json VARCHAR,PRIMARY KEY(analysis_id,pattern_id));
            CREATE TABLE IF NOT EXISTS behavior_pattern_occurrences(
                analysis_id VARCHAR,segment_id VARCHAR,pattern_id VARCHAR,confirmation_time TIMESTAMP,
                split VARCHAR,similarity DOUBLE,PRIMARY KEY(analysis_id,segment_id));
            CREATE TABLE IF NOT EXISTS behavior_pattern_outcomes(
                analysis_id VARCHAR,segment_id VARCHAR,horizon INTEGER,known_at TIMESTAMP,
                forward_return DOUBLE,mfe DOUBLE,mae DOUBLE,PRIMARY KEY(analysis_id,segment_id,horizon));
            CREATE TABLE IF NOT EXISTS behavior_pattern_statistics(
                analysis_id VARCHAR,pattern_id VARCHAR,split VARCHAR,horizon INTEGER,statistics_json VARCHAR,
                PRIMARY KEY(analysis_id,pattern_id,split,horizon));
            CREATE TABLE IF NOT EXISTS behavior_strategy_candidates(
                analysis_id VARCHAR,strategy_id VARCHAR,usable_from TIMESTAMP,candidate_json VARCHAR,
                PRIMARY KEY(analysis_id,strategy_id));
            CREATE TABLE IF NOT EXISTS behavior_backtest_results(
                analysis_id VARCHAR PRIMARY KEY,result_json VARCHAR);
            CREATE TABLE IF NOT EXISTS behavior_profiles(
                analysis_id VARCHAR PRIMARY KEY,symbol VARCHAR,period VARCHAR,data_end_date TIMESTAMP,
                config_key VARCHAR,input_fingerprint VARCHAR,profile_json VARCHAR,created_at TIMESTAMP);
            CREATE TABLE IF NOT EXISTS behavior_jobs(
                job_id VARCHAR,symbol VARCHAR,period VARCHAR,status VARCHAR,analysis_id VARCHAR,
                error VARCHAR,updated_at TIMESTAMP,PRIMARY KEY(job_id,symbol,period));
            CREATE TABLE IF NOT EXISTS behavior_job_config(
                job_id VARCHAR PRIMARY KEY,config_key VARCHAR,data_end_date VARCHAR,period VARCHAR);
        """)
    def plan(self, job_id, symbols, period, config_key, data_end_date):
        expected=(config_key,data_end_date,period)
        old=self.db.execute("SELECT config_key,data_end_date,period FROM behavior_job_config WHERE job_id=?",[job_id]).fetchone()
        if old and old!=expected:
            raise ValueError("job_configuration_mismatch")
        self.db.execute("INSERT OR IGNORE INTO behavior_job_config VALUES (?,?,?,?)",[job_id,*expected])
        existing={x[0] for x in self.db.execute("SELECT symbol FROM behavior_jobs WHERE job_id=?",[job_id]).fetchall()}
        if existing and existing!=set(symbols):
            raise ValueError("job_universe_mismatch")
        self._insert("behavior_jobs",[dict(job_id=job_id,symbol=s,period=period,status="PENDING",analysis_id=None,error=None,updated_at=datetime.now()) for s in symbols])
    def _insert(self, table, records):
        if not records:
            return
        self.db.register("_behavior_rows",pd.DataFrame(records))
        try:
            self.db.execute(f"INSERT OR IGNORE INTO {table} BY NAME SELECT * FROM _behavior_rows")
        finally:
            self.db.unregister("_behavior_rows")
    def pending(self, job_id):
        return [x[0] for x in self.db.execute("SELECT symbol FROM behavior_jobs WHERE job_id=? AND status='PENDING' ORDER BY symbol",[job_id]).fetchall()]
    def status(self, job_id):
        return dict(self.db.execute("SELECT status,count(*) FROM behavior_jobs WHERE job_id=? GROUP BY status",[job_id]).fetchall())
    def mark(self, job_id, symbol, period, status, error=None, analysis_id=None):
        if status not in ("DONE","FAILED","SKIPPED"):
            raise ValueError("invalid_job_state")
        self.db.execute("UPDATE behavior_jobs SET status=?,analysis_id=?,error=?,updated_at=? WHERE job_id=? AND symbol=? AND period=?",
                        [status,analysis_id,error,datetime.now(),job_id,symbol,period])
    def save(self, result, fingerprint, job_id):
        aid=hashlib.sha256((result["symbol"]+result["period"]+result["version"]+result["config_key"]+result["data_end_date"]+fingerprint).encode()).hexdigest()[:32]
        self.db.execute("BEGIN")
        try:
            self._insert("behavior_segments",[dict(analysis_id=aid,segment_id=s["segment_id"],symbol=result["symbol"],period=result["period"],
                start_time=s["start"],end_time=s["end"],confirmation_time=s["confirmation_time"],direction=s["direction"],features_json=encode(s["features"])) for s in result["segments"]])
            self._insert("behavior_patterns",[dict(analysis_id=aid,pattern_id=p["pattern_id"],model_json=encode(result["model"])) for p in result["patterns"]])
            self._insert("behavior_pattern_occurrences",[dict(analysis_id=aid,segment_id=s["segment_id"],pattern_id=s["pattern_id"],
                confirmation_time=s["confirmation_time"],split=s["split"],similarity=s["similarity"]) for s in result["segments"]])
            self._insert("behavior_pattern_outcomes",[dict(analysis_id=aid,segment_id=o["segment_id"],horizon=o["horizon"],known_at=o["known_at"],
                forward_return=o["return_"],mfe=o["mfe"],mae=o["mae"]) for o in result["outcomes"]])
            self._insert("behavior_pattern_statistics",[dict(analysis_id=aid,pattern_id=p["pattern_id"],split=phase,horizon=int(h),statistics_json=encode(stats))
                for p in result["patterns"] for phase,stat in p["statistics"].items() for h,stats in stat.items()])
            self._insert("behavior_strategy_candidates",[dict(analysis_id=aid,strategy_id=c["strategy_id"],usable_from=c["usable_from"],candidate_json=encode(c)) for c in result["strategy_candidates"]])
            self._insert("behavior_backtest_results",[dict(analysis_id=aid,result_json=encode(result["backtest"]))])
            profile={k:v for k,v in result.items() if k not in ("segments","outcomes","backtest","pivots","model")}
            self._insert("behavior_profiles",[dict(analysis_id=aid,symbol=result["symbol"],period=result["period"],data_end_date=result["data_end_date"],
                config_key=result["config_key"],input_fingerprint=fingerprint,profile_json=encode(profile),created_at=datetime.now())])
            self.mark(job_id,result["symbol"],result["period"],"DONE",analysis_id=aid)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK"); raise
        return aid
    def load(self, symbol, period="1d"):
        row=self.db.execute("SELECT analysis_id,profile_json FROM behavior_profiles WHERE symbol=? AND period=? ORDER BY data_end_date DESC,created_at DESC LIMIT 1",[symbol,period]).fetchone()
        if not row:
            return None
        result=json.loads(row[1]); result["analysis_id"]=row[0]
        result["backtest"]=json.loads(self.db.execute("SELECT result_json FROM behavior_backtest_results WHERE analysis_id=?",[row[0]]).fetchone()[0])
        return result
    @staticmethod
    def fingerprint(frame):
        columns=["timestamp","open","high","low","close","volume","amount"]
        return hashlib.sha256(pd.util.hash_pandas_object(frame[columns],index=False).values.tobytes()).hexdigest()

def read_profile(history, symbol, period="1d"):
    # Read-only UI does not construct BehaviorStore (which creates schemas).
    row=history.db.execute("SELECT analysis_id,profile_json FROM behavior_profiles WHERE symbol=? AND period=? ORDER BY data_end_date DESC,created_at DESC LIMIT 1",[symbol,period]).fetchone()
    if not row:
        return None
    result=json.loads(row[1])
    result["backtest"]=json.loads(history.db.execute("SELECT result_json FROM behavior_backtest_results WHERE analysis_id=?",[row[0]]).fetchone()[0])
    return result
