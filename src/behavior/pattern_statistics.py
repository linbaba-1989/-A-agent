"""Outcome labels and purged statistics, never consumed by the feature model."""
from statistics import NormalDist
import numpy as np
import pandas as pd
from .models import HORIZONS

def confidence(n, minimum=5, normal=10):
    return "INSUFFICIENT" if n < minimum else "LOW_CONFIDENCE" if n < normal else "NORMAL"

def outcomes(frame, segments, period="1d"):
    f = frame.reset_index(drop=True)
    dates = pd.to_datetime(f.timestamp).dt.normalize()
    sessions = list(dates.drop_duplicates())
    day_positions = {day:i for i,day in enumerate(sessions)}
    last_indices = {day:int(np.flatnonzero((dates==day).to_numpy())[-1]) for day in sessions}
    result = []
    for n, s in enumerate(segments):
        i = s["confirmation_index"]
        day_index = day_positions[dates.iloc[i]]
        for horizon in HORIZONS:
            if day_index+horizon >= len(sessions):
                continue
            j = last_indices[sessions[day_index+horizon]]
            future = f.iloc[i+1:j+1]
            base = float(f.close.iloc[i])
            end = pd.Timestamp(f.timestamp.iloc[j])
            if period == "1d":
                end = end.normalize()+pd.Timedelta(hours=15)
            result.append(dict(segment_index=n, horizon=horizon, known_at=end.isoformat(),
                signal_time=s["confirmation_time"], end_index=j,
                return_=float(f.close.iloc[j]/base-1),
                mfe=max(0.0,float(future.high.max()/base-1)), mae=min(0.0,float(future.low.min()/base-1))))
    return result

def summarize(labels, minimum=5, normal=10, comparisons=1):
    if not labels:
        return dict(sample_count=0, effective_samples=0, confidence="INSUFFICIENT",
                    positive_rate=None, mean_return=None, median_return=None, mfe=None, mae=None,
                    average_win=None, average_loss=None, profit_loss_ratio=None, expectancy=None,
                    mean_lower_bound=None, positive_rate_lower_bound=None)
    labels = sorted(labels, key=lambda x:x["signal_time"])
    independent=[]; last_end=None
    for row in labels:
        if last_end is None or row["signal_time"] > last_end:
            independent.append(row); last_end=row["known_at"]
    r = np.array([x["return_"] for x in independent])
    n=len(r); wins=r[r>0]; losses=r[r<0]
    z=NormalDist().inv_cdf(1-.05/(2*max(1,comparisons)))
    p=float((r>0).mean())
    average_win=float(wins.mean()) if len(wins) else 0.0
    average_loss=float(losses.mean()) if len(losses) else 0.0
    lower=float(r.mean()-z*r.std(ddof=1)/np.sqrt(n)) if n>1 else None
    wilson=(p+z*z/(2*n)-z*np.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)
    return dict(sample_count=len(labels), effective_samples=n, confidence=confidence(n,minimum,normal),
        positive_rate=p, mean_return=float(r.mean()), median_return=float(np.median(r)),
        mfe=float(np.mean([x["mfe"] for x in independent])),
        mae=float(np.mean([x["mae"] for x in independent])),
        average_win=average_win, average_loss=average_loss,
        profit_loss_ratio=average_win/abs(average_loss) if average_loss<0 else None,
        expectancy=float(r.mean()), mean_lower_bound=lower, positive_rate_lower_bound=float(wilson),
        positive_rate_is_descriptive_only=n<minimum)

def supported(stat, minimum, cost, baseline=0):
    return (stat["effective_samples"] >= minimum and stat["mean_lower_bound"] is not None
            and stat["mean_lower_bound"] > cost+max(0,baseline)
            and stat["positive_rate_lower_bound"] > .5)

def walk_forward_dates(frame, fractions=(.6,.2,.2)):
    if len(fractions)!=3 or any(x<=0 for x in fractions) or abs(sum(fractions)-1)>1e-8:
        raise ValueError("invalid_time_split")
    dates=pd.to_datetime(frame.timestamp).dt.normalize().drop_duplicates().tolist()
    if len(dates)<30:
        raise ValueError("insufficient_history")
    a,b=int(len(dates)*fractions[0]),int(len(dates)*sum(fractions[:2]))
    return dict(train_end=dates[a-1].replace(hour=15).isoformat(),
                validation_end=dates[b-1].replace(hour=15).isoformat(),
                out_of_sample_end=dates[-1].replace(hour=15).isoformat(),
                fractions=list(fractions), shuffle=False)

def rolling_walk_forward(frame, train_sessions=252, validation_sessions=63, test_sessions=63, step=63):
    if min(train_sessions,validation_sessions,test_sessions,step)<1:
        raise ValueError("positive_window_required")
    dates=pd.to_datetime(frame.timestamp).dt.normalize().drop_duplicates().tolist()
    width=train_sessions+validation_sessions+test_sessions
    for start in range(0,len(dates)-width+1,step):
        yield dict(train_start=dates[start].isoformat(),
                   train_end=dates[start+train_sessions-1].replace(hour=15).isoformat(),
                   validation_end=dates[start+train_sessions+validation_sessions-1].replace(hour=15).isoformat(),
                   out_of_sample_end=dates[start+width-1].replace(hour=15).isoformat())
