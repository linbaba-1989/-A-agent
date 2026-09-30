"""Match only confirmed, already observable segments with a frozen model."""
import numpy as np
from .models import FEATURES
from .pattern_statistics import confidence

def match_segment(model, segment, history, as_of, config):
    if segment["confirmation_time"] > as_of:
        raise ValueError("unconfirmed_segment")
    pattern, similarity = model.predict(segment)
    if pattern is None:
        return dict(pattern_id=None, similarity=0.0, sample_count=0, confidence="INSUFFICIENT",
                    as_of=as_of, match_status="NO_TRAINED_PATTERN", historical_matches=[])
    matches=[]
    z=(np.array([segment["features"][k] for k in FEATURES])-model.mean)/model.scale
    for old in history:
        if old["confirmation_time"] >= segment["confirmation_time"] or old["confirmation_time"] > as_of:
            continue
        label,_=model.predict(old)
        if label != pattern:
            continue
        oz=(np.array([old["features"][k] for k in FEATURES])-model.mean)/model.scale
        sim=float(np.exp(-np.sqrt(np.mean((z-oz)**2))))
        if sim < config.similarity_floor:
            continue
        differences=sorted(zip(FEATURES,(z-oz).tolist()),key=lambda x:abs(x[1]),reverse=True)[:5]
        matches.append(dict(date=old["confirmation_time"],similarity=sim,
                            key_feature_differences=dict(differences)))
    matches.sort(key=lambda x:(-x["similarity"],x["date"]))
    count=len(matches)
    matched=similarity>=config.similarity_floor
    return dict(pattern_id=pattern if matched else None, nearest_pattern_id=pattern,
                match_status="MATCHED" if matched else "OUT_OF_DISTRIBUTION", similarity=similarity, sample_count=count,
                confidence=confidence(count,config.min_samples,config.normal_samples) if matched else "INSUFFICIENT",
                as_of=as_of, segment_confirmed_at=segment["confirmation_time"],
                state="LATEST_CONFIRMED_SEGMENT", historical_matches=matches)
