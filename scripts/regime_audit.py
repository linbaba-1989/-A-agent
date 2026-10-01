"""Read-only P4 persisted-snapshot, causal availability and real-execution audit."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.market_data.historical_store import HistoricalStore,DEFAULT_HISTORY_DB
from src.regime.models import VERSION,RegimeConfig,validate_features,encode
from src.regime.engine import MarketRegimeEngine
from src.regime.service import get_market_regime
from scripts.behavior_profiles import atomic

BASE_COUNTS={'1d':2626244,'1m':908585,'5m':180942,'15m':60312,'30m':20498708}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT_HISTORY_DB)
    parser.add_argument('--research',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    evidence=json.loads(args.research.read_text(encoding='utf-8'))
    c=RegimeConfig(); violations=dict(availability=0,feature_contract=0,noncausal_fill=0,same_day_sell=0,negative_fee=0,trade_outside_window=0)
    with HistoricalStore(args.db,read_only=True) as h:
        rows=h.db.execute('''SELECT result_json FROM market_regimes WHERE model_version=? AND config_key=? ORDER BY as_of''',[VERSION,c.key]).fetchall()
        records=[json.loads(r[0]) for r in rows]
        raw=[dict(day=pd.Timestamp(r['data_end_time']).normalize(),breadth=r['breadth'],index_state=r['index_state'],limit_indicators=r['limit_indicators']) for r in records]
        for row in raw:
            try: validate_features(row)
            except ValueError: violations['feature_contract']+=1
        prefix_matches=all(MarketRegimeEngine().evaluate(raw[:n])==records[:n] for n in (120,240,360,len(records)))
        for table in ('market_regime_features','market_regimes','regime_strategy_statistics','pattern_regime_statistics'):
            violations['availability']+=h.db.execute(f'SELECT count(*) FROM {table} WHERE data_end_time>=as_of').fetchone()[0]
        feature_payloads=[json.loads(r[0]) for r in h.db.execute('SELECT features_json FROM market_regime_features WHERE model_version=? AND config_key=? ORDER BY as_of',[VERSION,c.key]).fetchall()]
        payloads_match=feature_payloads==[{k:r[k] for k in ('breadth','index_state','limit_indicators')} for r in records]
        history_preserved=h.counts()==BASE_COUNTS
        stats=h.db.execute("SELECT data_end_time,statistics_json FROM regime_strategy_statistics WHERE family IN ('TREND','SHORT') AND model_version=? AND config_key=?",[VERSION,c.key]).fetchall()
        trades_checked=0
        for end,text in stats:
            stats_value=json.loads(text)
            for trade in stats_value['trades']:
                trades_checked+=1
                violations['noncausal_fill']+=int(pd.Timestamp(trade['entry_time'])<=pd.Timestamp(trade['entry_signal_time']) or pd.Timestamp(trade['exit_time'])<=pd.Timestamp(trade['exit_signal_time']))
                violations['same_day_sell']+=int(pd.Timestamp(trade['entry_time']).date()>=pd.Timestamp(trade['exit_time']).date())
                violations['negative_fee']+=int(trade['fees']<0)
                violations['trade_outside_window']+=int(pd.Timestamp(trade['exit_time'])>pd.Timestamp(end))
    last=records[-1]; day=pd.Timestamp(last['data_end_time']).date().isoformat()
    early=get_market_regime(day+'T09:30:00',db_path=args.db)
    at_close=get_market_regime(day+'T15:00:00',db_path=args.db)
    after=get_market_regime(day+'T15:01:00',db_path=args.db)
    timing_pass=early['as_of']<last['as_of'] and at_close['as_of']<last['as_of'] and after['as_of']==last['as_of']
    protected=subprocess.run(['git','diff','--exit-code','8b1f7c89f25a39c1b767bf9e7152d99acd42d80f','--','src/behavior','src/backtest'],
        cwd=Path(__file__).resolve().parents[1],capture_output=True).returncode==0
    ok=all((evidence['status']=='PASS',len(records)==485,prefix_matches,payloads_match,history_preserved,timing_pass,protected,evidence['p3_profiles_unchanged'])) and not any(violations.values())
    report=dict(status='PASS' if ok else 'FAIL',records=len(records),violations=violations,
        feature_payloads_match=payloads_match,prefix_replay_matches=prefix_matches,history_rows_preserved=history_preserved,
        actual_daily_availability_pass=timing_pass,p3_engine_code_unchanged=protected,p3_profiles_unchanged=evidence['p3_profiles_unchanged'],
        benchmark_trade_records_checked=trades_checked,
        trade_count_note='ALL and regime-gated simulations are separate runs; this audit count is not a count of unique economic trades.')
    atomic(args.output,report); print(encode(report))
    return 0 if ok else 1

if __name__=='__main__': sys.exit(main())
