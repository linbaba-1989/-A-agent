"""P3 pattern identities stay frozen. Outcomes are used only for conditional validation."""
import json
from collections import defaultdict
import pandas as pd
from ..behavior.pattern_statistics import summarize, supported
from .evaluation import RegimeLookup
from .models import REGIMES, RegimeConfig, VERSION, encode, clean

def conditional_statistics(labels, lookup, boundary, comparisons=1):
    groups=defaultdict(list)
    for label in labels:
        if label['known_at']<=boundary:
            groups[lookup.at(label['signal_time'])].append(label)
    return {regime:summarize(groups[regime],5,10,comparisons) for regime in REGIMES}

def evaluate_patterns(history, records):
    lookup=RegimeLookup(records); result=[]; stored=[]; config=RegimeConfig()
    profiles=history.db.execute('''SELECT DISTINCT p.analysis_id,p.profile_json FROM behavior_jobs j
        JOIN behavior_profiles p USING(analysis_id) WHERE j.job_id IN ('p3-pilot-v11:1d','p3-pilot-v11:30m')
        AND j.status='DONE' ORDER BY p.analysis_id''').fetchall()
    if len(profiles)!=20: raise ValueError('expected_twenty_frozen_p3_profiles')
    for aid,raw in profiles:
        p=json.loads(raw)
        rows=history.db.execute('''SELECT o.segment_id,o.horizon,o.known_at,o.forward_return,o.mfe,o.mae,
            s.confirmation_time,c.pattern_id,c.split,c.similarity FROM behavior_pattern_outcomes o
            JOIN behavior_segments s USING(analysis_id,segment_id)
            JOIN behavior_pattern_occurrences c USING(analysis_id,segment_id) WHERE o.analysis_id=?''',[aid]).fetchall()
        labels=[dict(segment_id=r[0],horizon=r[1],known_at=pd.Timestamp(r[2]).isoformat(),return_=r[3],mfe=r[4],mae=r[5],
            signal_time=pd.Timestamp(r[6]).isoformat(),pattern_id=r[7],split=r[8],similarity=r[9]) for r in rows]
        for pattern in p['patterns']:
            pid=pattern['pattern_id']; stats={}; baseline={}
            for phase,boundary in [('train',p['split']['train_end']),('validation',p['split']['validation_end']),('out_of_sample',p['split']['out_of_sample_end'])]:
                stats[phase]={}; baseline[phase]={}
                for h in (1,3,5,10,20):
                    selected=[r for r in labels if r['pattern_id']==pid and r['split']==phase and r['horizon']==h
                              and r['similarity']>=p['config']['similarity_floor'] and r['known_at']<=boundary]
                    unconditional=summarize(selected)
                    baseline[phase][str(h)]=unconditional
                    grouped=conditional_statistics(selected,lookup,boundary,max(1,len(p['patterns'])*len(REGIMES)*5))
                    stats[phase][str(h)]=grouped
                    for regime,stat in grouped.items():
                        stored.append(dict(as_of=(pd.Timestamp(boundary)+pd.Timedelta(minutes=1)).isoformat(),
                            frequency=p['period'],model_version=VERSION,config_key=config.key,data_end_time=boundary,
                            symbol=p['symbol'],pattern_version=p['version']+':'+aid,pattern_id=pid,regime=regime,
                            split=phase,horizon=h,statistics_json=encode(dict(stat,source_analysis_id=aid))))
            support=[]
            for regime in REGIMES:
                if regime=='UNKNOWN': continue
                phases=[supported(stats[phase]['5'][regime],10,p['config']['cost_buffer'],baseline[phase]['5']['mean_return'] or 0)
                        for phase in ('train','validation','out_of_sample')]
                if all(phases): support.append(regime)
            result.append(dict(symbol=p['symbol'],period=p['period'],source_analysis_id=aid,pattern_id=pid,
                original_usable=pattern['usable'],statistics=stats,unconditional=baseline,
                supported_regimes=support,selected_before_oos=[regime for regime in REGIMES if regime!='UNKNOWN' and all(
                    supported(stats[phase]['5'][regime],10,p['config']['cost_buffer'],baseline[phase]['5']['mean_return'] or 0)
                    for phase in ('train','validation'))],
                finding='CONDITIONAL_RESEARCH_SUPPORT' if support else 'NO_STATISTICALLY_USEFUL_PATTERN',
                usable_from=(pd.Timestamp(p['data_end_date'])+pd.Timedelta(minutes=1)).isoformat(),
                warning='Conditional descriptive research only; no P3 identity, candidate or backtest is rewritten.'))
    return clean(result),stored
