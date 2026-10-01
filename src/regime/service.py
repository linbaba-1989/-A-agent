"""Time-aware local read API. A date means that date's end, a timestamp is exact."""
import json
import pandas as pd
from ..market_data.historical_store import HistoricalStore, DEFAULT_HISTORY_DB
from ..market_clock import beijing_now
from .models import VERSION, RegimeConfig

def cutoff(value, end_of_date=True):
    t=pd.Timestamp(value)
    if t.tzinfo is not None:
        t=t.tz_convert('Asia/Shanghai').tz_localize(None)
    if end_of_date and isinstance(value,str) and len(value)==10:
        t=t+pd.Timedelta(days=1)-pd.Timedelta(microseconds=1)
    return t

def get_market_regime(date, *, db_path=DEFAULT_HISTORY_DB):
    at=cutoff(date)
    with HistoricalStore(db_path,read_only=True) as h:
        tables={r[0] for r in h.db.execute('SHOW TABLES').fetchall()}
        row=h.db.execute('''SELECT result_json FROM market_regimes WHERE as_of<=?
            AND model_version=? AND config_key=? AND frequency='1d' ORDER BY as_of DESC LIMIT 1''',
            [at,VERSION,RegimeConfig().key]).fetchone() if 'market_regimes' in tables else None
        if not row:
            return dict(as_of=None,requested_as_of=at.isoformat(),regime='UNKNOWN',confidence=0.,
                trend_score=None,short_score=None,risk_score=None,risk_level='UNKNOWN',breadth={},index_state={},
                evidence={},warnings=['NO_AVAILABLE_STORED_REGIME'],strategy_preference='INSUFFICIENT_EVIDENCE')
        result=json.loads(row[0]); result['requested_as_of']=at.isoformat()
        preference=None
        if 'regime_strategy_statistics' in tables:
            preference=h.db.execute('''SELECT statistics_json FROM regime_strategy_statistics
                WHERE as_of<=? AND model_version=? AND config_key=? AND regime=? AND family='COMPARISON'
                ORDER BY as_of DESC LIMIT 1''',[at,VERSION,RegimeConfig().key,result['regime']]).fetchone()
        result['strategy_preference']='RISK_OFF' if result['regime']=='RISK_OFF' else json.loads(preference[0])['preference'] if preference else 'INSUFFICIENT_EVIDENCE'
        result['preference_evidence']=json.loads(preference[0]) if preference else None
        return result

def get_current_market_regime(*, db_path=DEFAULT_HISTORY_DB):
    return get_market_regime(beijing_now(),db_path=db_path)

def get_regime_history(start, end, *, db_path=DEFAULT_HISTORY_DB):
    with HistoricalStore(db_path,read_only=True) as h:
        exists=h.db.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='market_regimes'").fetchone()[0]
        if not exists: return []
        rows=h.db.execute('''SELECT result_json FROM market_regimes WHERE as_of BETWEEN ? AND ?
            AND frequency='1d' AND model_version=? AND config_key=? ORDER BY as_of''',
            [cutoff(start,False),cutoff(end),VERSION,RegimeConfig().key]).fetchall()
        return [json.loads(r[0]) for r in rows]
