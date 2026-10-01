"""Versioned P4 derived tables in the existing DuckDB; P2/P3 are read-only inputs."""
from datetime import datetime
import pandas as pd
from .models import encode, VERSION, RegimeConfig, validate_features

class RegimeStore:
    def __init__(self, history):
        self.db=history.db
        self.db.execute('''
        CREATE TABLE IF NOT EXISTS market_regime_features(
            as_of TIMESTAMP,frequency VARCHAR,model_version VARCHAR,config_key VARCHAR,
            data_end_time TIMESTAMP,created_at TIMESTAMP,features_json VARCHAR,
            PRIMARY KEY(as_of,frequency,model_version,config_key));
        CREATE TABLE IF NOT EXISTS market_regimes(
            as_of TIMESTAMP,frequency VARCHAR,model_version VARCHAR,config_key VARCHAR,
            data_end_time TIMESTAMP,created_at TIMESTAMP,regime VARCHAR,result_json VARCHAR,
            PRIMARY KEY(as_of,frequency,model_version,config_key));
        CREATE TABLE IF NOT EXISTS regime_strategy_statistics(
            as_of TIMESTAMP,frequency VARCHAR,model_version VARCHAR,config_key VARCHAR,
            data_end_time TIMESTAMP,created_at TIMESTAMP,regime VARCHAR,family VARCHAR,split VARCHAR,
            statistics_json VARCHAR,PRIMARY KEY(as_of,frequency,model_version,config_key,regime,family,split));
        CREATE TABLE IF NOT EXISTS pattern_regime_statistics(
            as_of TIMESTAMP,frequency VARCHAR,model_version VARCHAR,config_key VARCHAR,
            data_end_time TIMESTAMP,created_at TIMESTAMP,symbol VARCHAR,pattern_version VARCHAR,
            pattern_id VARCHAR,regime VARCHAR,split VARCHAR,horizon INTEGER,statistics_json VARCHAR,
            PRIMARY KEY(as_of,frequency,model_version,config_key,symbol,pattern_version,pattern_id,regime,split,horizon));
        ''')

    def _insert(self, table, rows):
        if not rows: return
        self.db.register('_p4_write_rows',pd.DataFrame(rows))
        try:
            self.db.execute(f'INSERT OR REPLACE INTO {table} BY NAME SELECT * FROM _p4_write_rows')
        finally:
            self.db.unregister('_p4_write_rows')

    def save(self, records, statistics=(), conditional=()):
        if any(pd.Timestamp(r['data_end_time'])>=pd.Timestamp(r['as_of']) for r in [*records,*statistics,*conditional]):
            raise ValueError('backdated_regime_record')
        now=datetime.now(); raw=[]; results=[]
        for r in records:
            validate_features(r)
            base={k:r[k] for k in ('as_of','frequency','model_version','config_key','data_end_time')}
            base['created_at']=now
            raw.append(dict(**base,features_json=encode({k:r[k] for k in ('breadth','index_state','limit_indicators')})))
            results.append(dict(**base,regime=r['regime'],result_json=encode(r)))
        self.db.execute('BEGIN')
        try:
            self._insert('market_regime_features',raw)
            self._insert('market_regimes',results)
            self._insert('regime_strategy_statistics',[dict(r,created_at=now) for r in statistics])
            self._insert('pattern_regime_statistics',[dict(r,created_at=now) for r in conditional])
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK'); raise

def statistics_record(regime, family, split, data_end, statistics, frequency='1d'):
    c=RegimeConfig()
    return dict(as_of=(pd.Timestamp(data_end)+pd.Timedelta(minutes=1)).isoformat(),frequency=frequency,
        model_version=VERSION,config_key=c.key,data_end_time=data_end,regime=regime,family=family,split=split,
        statistics_json=encode(statistics))
