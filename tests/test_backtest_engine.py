from dataclasses import replace
import pandas as pd
import pytest
from src.backtest.engine import BacktestEngine, TradingRules

def bars(times=None):
    times=list(pd.bdate_range("2026-01-05",periods=6)) if times is None else times
    return pd.DataFrame(dict(timestamp=times,open=[100.]*len(times),high=[102.]*len(times),low=[98.]*len(times),
        close=[100.]*len(times),volume=[1000.]*len(times),amount=[100000.]*len(times),
        limit_up=[110.]*len(times),limit_down=[90.]*len(times)))

def test_next_open_timing_never_close_fill():
    result=BacktestEngine().run(bars(),[dict(action="BUY",signal_time="2026-01-05T15:00:00"),dict(action="SELL",signal_time="2026-01-06T15:00:00")])
    assert result["fills"][0]["fill_time"].startswith("2026-01-06T09:30")
    assert result["fills"][1]["fill_time"].startswith("2026-01-07T09:30")
    assert all(x["fill_time"]>x["signal_time"] for x in result["fills"])
    assert result["metrics"]["trades"]==1

def test_intraday_t_plus_one_sellable_quantity():
    f=bars(pd.to_datetime(["2026-01-05 10:00","2026-01-05 10:30","2026-01-05 11:00","2026-01-06 10:00"]))
    result=BacktestEngine().run(f,[dict(action="BUY",signal_time="2026-01-05T10:00:00"),dict(action="SELL",signal_time="2026-01-05T10:30:00")],"30m")
    assert any(x["reason"]=="T_PLUS_ONE" for x in result["blocked_orders"])
    assert result["fills"][1]["fill_time"].startswith("2026-01-06")
    assert result["equity"][1]["sellable_quantity"]==0

@pytest.mark.parametrize("kind",["limit","suspension","missing_limits"])
def test_untradeable_bars_do_not_fill(kind):
    f=bars()
    if kind=="limit":
        f.loc[1,["open","close"]]=110.; f.loc[1,"high"]=111.; f.loc[1,"low"]=109.
    elif kind=="suspension":
        f.loc[1,"volume"]=0
    else:
        f=f.drop(columns=["limit_up","limit_down"])
    r=BacktestEngine().run(f,[dict(action="BUY",signal_time="2026-01-05T15:00:00")])
    assert r["blocked_orders"]
    assert not any(x["fill_time"].startswith("2026-01-06") for x in r["fills"])

def test_fees_stamp_tax_slippage_cash_and_lot_constraints():
    f=bars(); signals=[dict(action="BUY",signal_time="2026-01-05T15:00:00"),dict(action="SELL",signal_time="2026-01-06T15:00:00")]
    r=BacktestEngine().run(f,signals)
    zero=TradingRules(commission_rate=0,minimum_commission=0,stamp_tax_rate=0,transfer_fee_rate=0,slippage=0)
    free=BacktestEngine(zero).run(f,signals)
    assert r["trades"][0]["pnl"]<free["trades"][0]["pnl"]==0
    assert r["fills"][1]["fee"]>r["fills"][0]["fee"]
    assert all(x["quantity"]%100==0 for x in r["fills"])
    assert all(x["cash"]>=0 for x in r["equity"])
    assert r["metrics"]["max_consecutive_losses"]==1

def test_stop_uses_known_close_and_next_open_not_same_bar_low():
    f=bars(); f.loc[1,["open","high","low","close"]]=[100,101,90,94]
    r=BacktestEngine().run(f,[dict(action="BUY",signal_time="2026-01-05T15:00:00",stop_loss=.03)])
    assert r["trades"][0]["exit_reason"]=="STOP_LOSS"
    assert r["trades"][0]["exit_time"].startswith("2026-01-07")

def test_no_forced_end_liquidation_and_empty_statistics():
    r=BacktestEngine().run(bars(),[])
    assert r["metrics"]["trades"]==0 and r["metrics"]["win_rate"] is None
    assert r["metrics"]["total_return"]==0

def test_minimum_quantity_and_increment_are_separate():
    rules=TradingRules(initial_cash=100000,lot_size=1,min_buy_quantity=200,max_position=.257,
                       commission_rate=0,minimum_commission=0,transfer_fee_rate=0,slippage=0)
    result=BacktestEngine(rules).run(bars(),[dict(action="BUY",signal_time="2026-01-05T15:00:00")])
    assert result["fills"][0]["quantity"]==257
    assert result["fills"][0]["quantity"]>=200

def test_strict_limits_override_estimate_and_cash_cannot_buy_fractional_lot():
    f=bars(); f["limit_up"]=99
    result=BacktestEngine(TradingRules(allow_estimated_limits=True)).run(f,[dict(action="BUY",signal_time="2026-01-05T15:00:00")])
    assert not result["fills"]
    result=BacktestEngine(TradingRules(initial_cash=1000)).run(bars(),[dict(action="BUY",signal_time="2026-01-05T15:00:00")])
    assert not result["fills"] and any(b["reason"]=="INSUFFICIENT_CASH" for b in result["blocked_orders"])

@pytest.mark.parametrize("period",["1d","30m"])
def test_one_session_max_holding_sells_next_session_open(period):
    times=None if period=="1d" else pd.to_datetime(["2026-01-05 15:00","2026-01-06 10:00","2026-01-06 15:00","2026-01-07 10:00"])
    result=BacktestEngine().run(bars(times),[dict(action="BUY",signal_time="2026-01-05T15:00:00",max_holding_days=1)],period)
    trade=result["trades"][0]
    assert trade["entry_time"].startswith("2026-01-06")
    assert trade["exit_time"].startswith("2026-01-07T09:30")
    assert trade["holding_sessions"]==1
    assert trade["exit_reason"]=="MAX_HOLDING"
