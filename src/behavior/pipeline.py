"""Train, validate, then evaluate untouched chronological OOS; no paid models."""
from dataclasses import asdict
import hashlib
import numpy as np
import pandas as pd
from .models import BehaviorConfig, HORIZONS, PatternStrategyCandidate, Pivot, VERSION
from .swing_detector import SwingDetector, prepare_bars
from .segment_features import build_segments
from .pattern_engine import PatternModel
from .pattern_statistics import outcomes, summarize, supported, walk_forward_dates
from .pattern_matcher import match_segment
from ..backtest.engine import BacktestEngine, TradingRules

class BehaviorEngine:
    def __init__(self, store=None, config=None):
        self.store=store; self.config=config or BehaviorConfig()
    def analyze(self, symbol, frame, period="1d", *, end=None, execution_probe=False):
        f=prepare_bars(frame,period)
        if end is not None:
            f=f[f.available_at<=pd.Timestamp(end)].reset_index(drop=True)
        split=walk_forward_dates(f)
        pivots=SwingDetector(self.config).detect(f,period)
        segments=build_segments(f,pivots,period,self.config)
        for i,s in enumerate(segments):
            s["segment_id"]=hashlib.sha256((symbol+period+s["confirmation_time"]+self.config.key).encode()).hexdigest()[:24]
            s["split"]="train" if s["confirmation_time"]<=split["train_end"] else "validation" if s["confirmation_time"]<=split["validation_end"] else "out_of_sample"
        train=[s for s in segments if s["split"]=="train"]
        model=PatternModel(self.config).fit(train)
        for s in segments:
            s["pattern_id"],s["similarity"]=model.predict(s)
        labels=outcomes(f,segments,period)
        for o in labels:
            s=segments[o["segment_index"]]
            o.update(segment_id=s["segment_id"],pattern_id=s["pattern_id"],split=s["split"],similarity=s["similarity"])
        cluster_count=len(model.centers)
        patterns=[]
        for i in range(cluster_count):
            pid=f"P{i:02d}"
            stats={}
            for phase,boundary in [("train",split["train_end"]),("validation",split["validation_end"]),("out_of_sample",split["out_of_sample_end"])]:
                stats[phase]={}
                for h in HORIZONS:
                    rows=[o for o in labels if o["pattern_id"]==pid and o["split"]==phase and o["horizon"]==h and o["known_at"]<=boundary and o["similarity"]>=self.config.similarity_floor]
                    stats[phase][str(h)]=summarize(rows,self.config.min_samples,self.config.normal_samples,cluster_count)
            count=sum(s["pattern_id"]==pid for s in train)
            patterns.append(dict(pattern_id=pid,sample_count=count,statistics=stats))
        h=str(self.config.outcome_horizon)
        # An all-pattern benchmark is fixed within each split; no OOS baseline enters selection.
        baselines={}
        for phase,boundary in [("train",split["train_end"]),("validation",split["validation_end"]),("out_of_sample",split["out_of_sample_end"])]:
            rows=[o for o in labels if o["split"]==phase and str(o["horizon"])==h and o["known_at"]<=boundary]
            baselines[phase]=summarize(rows)["mean_return"] or 0.0
        policies={}
        for p in patterns:
            train_stat=p["statistics"]["train"][h]; validation=p["statistics"]["validation"][h]
            p["train_supported"]=supported(train_stat,self.config.normal_samples,self.config.cost_buffer,baselines["train"])
            p["validation_supported"]=supported(validation,self.config.min_samples,self.config.cost_buffer,baselines["validation"])
            # OOS selection is frozen using only train+validation evidence.
            p["selected_before_oos"]=p["train_supported"] and p["validation_supported"]
            training_labels=[o for o in labels if o["pattern_id"]==p["pattern_id"] and str(o["horizon"])==h and o["split"]=="train" and o["known_at"]<=split["train_end"]]
            if training_labels:
                policies[p["pattern_id"]]=dict(stop_loss=max(.005,float(abs(np.quantile([o["mae"] for o in training_labels],.25)))),
                    take_profit=max(.01,float(np.quantile([o["mfe"] for o in training_labels],.5))),max_holding_days=int(h))
        signals=[dict(signal_time=s["confirmation_time"],action="BUY",pattern_id=s["pattern_id"],**policies[s["pattern_id"]])
                 for s in segments if s["split"]=="out_of_sample" and s["similarity"]>=self.config.similarity_floor
                 and s["pattern_id"] in policies and next(p["selected_before_oos"] for p in patterns if p["pattern_id"]==s["pattern_id"])]
        test_frame=f[f.available_at>pd.Timestamp(split["validation_end"])].reset_index(drop=True)
        rules=TradingRules(regular_limit_pct=.2 if symbol.startswith(("300","301","688","689")) else .3 if symbol.endswith(".BJ") else .1,
                           lot_size=1 if symbol.startswith(("688","689")) or symbol.endswith(".BJ") else 100,
                           min_buy_quantity=200 if symbol.startswith(("688","689")) else 100,allow_estimated_limits=True)
        backtest=BacktestEngine(rules).run(test_frame,signals,period)
        probe=None
        if execution_probe:
            probe=BacktestEngine(rules).run(test_frame,[dict(action="BUY",signal_time=test_frame.available_at.iloc[0].isoformat(),max_holding_days=5)],period)
            probe["purpose"]="EXECUTION_INTEGRATION_TEST_NOT_A_PATTERN_STRATEGY"
        candidates=[]
        for p in patterns:
            oos=p["statistics"]["out_of_sample"][h]
            p["oos_supported"]=supported(oos,self.config.min_oos_samples,self.config.cost_buffer,baselines["out_of_sample"])
            trades=[t for t in backtest["trades"] if t["pattern_id"]==p["pattern_id"]]
            p["usable"]=p["selected_before_oos"] and p["oos_supported"] and len(trades)>=self.config.min_oos_samples and sum(t["pnl"] for t in trades)>0
            if p["usable"]:
                policy=policies[p["pattern_id"]]
                candidates.append(asdict(PatternStrategyCandidate(
                    strategy_id=symbol+":"+period+":"+p["pattern_id"],version=VERSION+":"+self.config.key,
                    pattern_id=p["pattern_id"],entry_condition=dict(frozen_model=model.payload(),similarity_at_least=self.config.similarity_floor),
                    confirmation="Use only completed segment confirmation_time; enter no earlier than next bar open.",
                    confidence="NORMAL",data_end_date=split["out_of_sample_end"],
                    usable_from=(pd.Timestamp(split["out_of_sample_end"])+pd.Timedelta(microseconds=1)).isoformat(),
                    evidence=dict(train=p["statistics"]["train"][h],validation=p["statistics"]["validation"][h],out_of_sample=oos,
                                  independent_oos_trades=len(trades),research_only=True,execution_assumptions=backtest["assumptions"]),**policy)))
        current=dict(pattern_id=None,similarity=0,sample_count=0,confidence="INSUFFICIENT",historical_matches=[])
        if pivots and len(f)-1-pivots[-1].pivot_index >= self.config.min_bars:
            endpoint=Pivot(len(f)-1,len(f)-1,f.timestamp.iloc[-1].isoformat(),f.available_at.iloc[-1].isoformat(),float(f.close.iloc[-1]),"PROVISIONAL_ENDPOINT")
            partial=build_segments(f,[pivots[-1],endpoint],period,self.config)
            if partial:
                if segments:
                    partial[-1]["features"]["previous_swing_return"]=segments[-1]["features"]["return"]
                    partial[-1]["features"]["previous_swing_duration"]=segments[-1]["duration"]
                current=match_segment(model,partial[-1],segments,split["out_of_sample_end"],self.config)
                current["state"]="CURRENT_PARTIAL_SEGMENT"
                current["endpoint_is_confirmed_pivot"]=False
                current["statistically_supported"]=any(c["pattern_id"]==current["pattern_id"] for c in candidates)
        moves=[s["features"]["return"] for s in segments]
        up=[v for v in moves if v>0]; down=[v for v in moves if v<0]
        return dict(symbol=symbol,period=period,version=VERSION,config=asdict(self.config),config_key=self.config.key,
            data_end_date=split["out_of_sample_end"],split=split,pivots=[asdict(p) for p in pivots],
            segments=segments,patterns=patterns,outcomes=labels,model=model.payload(),current_match=current,
            strategy_candidates=candidates,backtest=backtest,execution_probe=probe,baselines=baselines,
            summary=dict(segments=len(segments),confirmed_swings=max(0,len(pivots)-1),
                up_swings=sum(pivots[i].pivot_price>pivots[i-1].pivot_price for i in range(1,len(pivots))),
                down_swings=sum(pivots[i].pivot_price<pivots[i-1].pivot_price for i in range(1,len(pivots))),
                median_up_move=float(np.median(up)) if up else None,median_down_move=float(np.median(down)) if down else None,
                median_duration=float(np.median([s["duration"] for s in segments])) if segments else None,
                patterns=len(patterns),usable_patterns=sum(p["usable"] for p in patterns),strategy_candidates=len(candidates),
                trades=backtest["metrics"]["trades"],
                finding="SUPPORTED_RESEARCH_CANDIDATE" if candidates else "NO_STATISTICALLY_USEFUL_PATTERN"),
            limitations=["Surviving current-universe snapshot; not a historical survivorship-free market universe.",
                         "Raw-price features/outcomes, not dividend-reinvested total returns.",
                         "Current match uses the partial segment ending at the last observed close; its endpoint is not a confirmed pivot.",
                         "Outcome returns start at confirmation close; execution results separately use next-open fills."])
    def match_current_pattern(self, symbol, period="1d"):
        if self.store is None:
            raise ValueError("historical_store_required")
        data=self.store.get_two_year_behavior_dataset(symbol)
        if not data["ready"]:
            return dict(symbol=symbol,status="DATASET_NOT_READY",ready=False,reasons=data["readiness_reasons"])
        return self.analyze(symbol,data["daily" if period=="1d" else "bars_30m"],period)["current_match"]
