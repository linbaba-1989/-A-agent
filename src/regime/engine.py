"""Versioned, prespecified rules. Confidence measures evidence coverage/margin, not win probability."""
import numpy as np
import pandas as pd
from .models import INDICES, RegimeConfig, VERSION, available_at, clean, validate_features

def unit(value, low=0, high=1):
    return float(np.clip((value-low)/(high-low),0,1))

def classify_scores(trend, short, risk, *, repair=False, config=None):
    c=config or RegimeConfig()
    if risk>=c.risk_off:
        return 'RISK_OFF'
    if trend>=c.strong_trend and risk<50:
        return 'TREND_STRONG'
    if short>=c.hot_short and risk<55:
        return 'SHORT_HOT'
    if repair and short>=50 and risk<60:
        return 'SHORT_REPAIR'
    if trend>=52 and risk<55:
        return 'TREND_ROTATION'
    return 'MIXED'

class MarketRegimeEngine:
    def __init__(self, config=None):
        self.config=config or RegimeConfig()

    def evaluate(self, features):
        c=self.config; output=[]; prior_risks=[]; pending=None; streak=0
        for row in sorted(features,key=lambda r:r['day']):
            validate_features(row)
            b=clean(row['breadth']); indexes=clean(row['index_state']); reasons=[]
            required_b=('above_ma5','above_ma20','above_ma60','new_high20','new_low20','amount_ratio20',
                'dispersion','advance_ratio','decline_ratio','momentum_persistence1','momentum_persistence3',
                'active_large_move_ratio','high_volatility_participation','large_move_amount_share','top_decile_amount_share',
                'market_30m_acceleration','new_low_expansion','breadth_persistence5','gain_gt3','loss_lt3','return_samples')
            required_i=('ma60','ma20_slope','realized_volatility','drawdown_60','trend_persistence','above_ma20',
                'above_ma60','trend_30m','macd_30m','volatility_30m','return_3')
            if set(indexes)!=set(INDICES) or any(any(v.get(k) is None for k in required_i) for v in indexes.values()):
                reasons.append('INCOMPLETE_INDEX_OR_WARMUP')
            if any(b.get(k) is None for k in required_b):
                reasons.append('INCOMPLETE_BREADTH_OR_WARMUP')
            if (b.get('return_coverage') or 0)<c.min_coverage or (b.get('indicator_coverage') or 0)<c.min_indicator_coverage:
                reasons.append('INSUFFICIENT_CROSS_SECTION_COVERAGE')
            intra=(b.get('intraday_samples') or 0)/max(1,b.get('expected_symbols') or 1)
            if intra<c.min_coverage:
                reasons.append('INSUFFICIENT_30M_COVERAGE')
            record=dict(as_of=available_at(row['day']).isoformat(),data_end_time=(pd.Timestamp(row['day'])+pd.Timedelta(hours=15)).isoformat(),
                model_version=VERSION,frequency=c.frequency,config_key=c.key,
                breadth=b,index_state=indexes,limit_indicators=row['limit_indicators'],
                warnings=['CURRENT_UNIVERSE_SURVIVORSHIP_BIAS','RAW_PRICE_NOT_TOTAL_RETURN','LIMIT_COUNTS_UNKNOWN'],
                confidence_definition='Coverage and rule margin; not a probability of profitable trading.')
            if reasons:
                record.update(regime='UNKNOWN',confidence=0.,trend_score=None,short_score=None,risk_score=None,
                    risk_level='UNKNOWN',evidence={'missing':reasons},rule_candidate='UNKNOWN')
                pending=None; streak=0; prior_risks=[]
                output.append(record); continue
            avg=lambda key:float(np.mean([x[key] for x in indexes.values()]))
            components={
                'trend':{
                    'index_trend':.20*(.5*avg('above_ma20')+.5*avg('above_ma60')),
                    'index_slope':.10*unit(avg('ma20_slope'),-.03,.03),
                    'breadth':.10*b['advance_ratio'],
                    'ma_above':.20*(b['above_ma20']+b['above_ma60'])/2,
                    'high_low_balance':.10*unit(b['new_high20']-b['new_low20'],-.15,.15),
                    'persistence':.15*(avg('trend_persistence')+b['breadth_persistence5'])/2,
                    'drawdown':.10*(1-unit(-avg('drawdown_60'),0,.20)),
                    'volume_confirmation':.05*unit((b.get('volume_up_ratio') or 0)-(b.get('volume_down_ratio') or 0),-.3,.3)},
                'short':{
                    'short_breadth':.25*unit((b['gain_gt3']-b['loss_lt3'])/max(1,b['return_samples']),-.20,.20),
                    'high_volatility_participation':.10*unit(b['high_volatility_participation'],0,.25),
                    'large_move_concentration':.10*unit(b['large_move_amount_share'],0,.4)*b['advance_ratio'],
                    'amount_concentration':.10*unit(b['top_decile_amount_share'],.3,.75)*b['advance_ratio'],
                    'persistence1':.15*unit(b['momentum_persistence1'],.1,.6),
                    'persistence3':.15*unit(b['momentum_persistence3'],.2,.8),
                    'acceleration30m':.15*unit(b['market_30m_acceleration'],-.002,.002)},
                'risk':{
                    'drawdown':.20*unit(-avg('drawdown_60'),.02,.25),
                    'volatility':.15*unit(avg('realized_volatility'),.12,.5),
                    'decline_breadth':.20*unit(b['decline_ratio'],.4,.85),
                    'new_low_expansion':.10*unit(b['new_low20']+max(0,b['new_low_expansion']),0,.2),
                    'dispersion':.10*unit(b['dispersion'],.01,.05),
                    'index_break':.15*(1-(avg('above_ma20')+avg('above_ma60'))/2),
                    'liquidity_contraction':.10*unit(1-b['amount_ratio20'],0,.5)}}
            trend,short,risk=[100*sum(components[name].values()) for name in ('trend','short','risk')]
            repair=bool(prior_risks and max(prior_risks[-3:])-risk>=8 and avg('return_3')>0 and b['advance_ratio']>.5)
            candidate=classify_scores(trend,short,risk,repair=repair,config=c)
            streak=streak+1 if candidate==pending else 1; pending=candidate
            # Risk reduction is immediate; directional labels need two closes.
            regime=candidate if candidate in ('RISK_OFF','MIXED') or streak>=2 else 'MIXED'
            margin=abs(risk-c.risk_off) if candidate=='RISK_OFF' else max(abs(trend-c.strong_trend),abs(short-c.hot_short))
            coverage=min(1,b['return_coverage'],b['indicator_coverage'],intra)
            record.update(regime=regime,rule_candidate=candidate,confidence=float(coverage*(.5+.4*unit(margin,0,25))),
                trend_score=trend,short_score=short,risk_score=risk,
                risk_level='HIGH' if risk>=65 else 'MEDIUM' if risk>=40 else 'LOW',
                evidence={'components':components,'repair_from_prior_closes':repair,'candidate_streak':streak,
                          'directional_confirmation_closes':2,'coverage':coverage})
            output.append(clean(record)); prior_risks.append(risk)
        return output

def cluster_diagnostic(records, train_end):
    """Train-only deterministic clustering; never changes the production rule labels."""
    keys=('trend_score','short_score','risk_score')
    rows=[r for r in records if r['regime']!='UNKNOWN']
    train=[r for r in rows if r['as_of']<=train_end]
    if len(train)<30:
        return {'status':'INSUFFICIENT_EVIDENCE','used_by_classifier':False}
    x=np.array([[r[k] for k in keys] for r in train]); mean=x.mean(0); scale=x.std(0); scale[scale<1e-9]=1
    z=(x-mean)/scale; centers=[z[0]]
    for _ in range(5):
        d=np.min(np.sum((z[:,None]-np.array(centers)[None,:])**2,axis=2),axis=1)
        if d.max()<1e-9: break
        centers.append(z[np.argmax(d)])
    centers=np.array(centers)
    for _ in range(30):
        labels=np.argmin(((z[:,None]-centers[None,:])**2).sum(2),axis=1)
        update=np.array([z[labels==i].mean(0) if (labels==i).any() else v for i,v in enumerate(centers)])
        if np.allclose(update,centers): break
        centers=update
    memberships=[]
    for r in rows:
        label=int(np.argmin(np.sum((((np.array([r[k] for k in keys])-mean)/scale)-centers)**2,axis=1)))
        memberships.append({'as_of':r['as_of'],'cluster':label,'rule_regime':r['regime'],'in_train':r['as_of']<=train_end})
    return clean(dict(status='PASS',used_by_classifier=False,fit_end=train_end,features=list(keys),mean=mean.tolist(),scale=scale.tolist(),centers=centers.tolist(),memberships=memberships))
