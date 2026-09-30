"""Long-only, next-bar-open A-share simulation with explicit execution assumptions."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
import numpy as np
import pandas as pd
from ..behavior.swing_detector import prepare_bars

@dataclass(frozen=True)
class TradingRules:
    initial_cash: float = 100000.0
    lot_size: int = 100
    min_buy_quantity: int = 100
    max_position: float = 0.25
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage: float = 0.0005
    regular_limit_pct: float = 0.10
    tick_size: float = 0.01
    allow_estimated_limits: bool = False
    def __post_init__(self):
        if self.initial_cash<=0 or self.lot_size<1 or self.min_buy_quantity<1 or not 0<self.max_position<=1:
            raise ValueError("invalid_cash_or_lot")
        if min(self.commission_rate,self.minimum_commission,self.stamp_tax_rate,self.transfer_fee_rate,self.slippage)<0:
            raise ValueError("negative_cost")
        if not 0<self.regular_limit_pct<1 or self.tick_size<=0:
            raise ValueError("invalid_price_rules")

def performance(equity, trades, initial_cash):
    if not equity:
        raise ValueError("empty_equity")
    e=pd.DataFrame(equity)
    daily=e.assign(day=pd.to_datetime(e.timestamp).dt.normalize()).groupby("day",sort=True).equity.last()
    capital=np.r_[initial_cash,daily.to_numpy()]
    returns=capital[1:]/capital[:-1]-1
    peak=np.maximum.accumulate(capital)
    pnl=np.array([x["pnl"] for x in trades],dtype=float)
    trade_returns=np.array([x["net_return"] for x in trades],dtype=float)
    wins=trade_returns[trade_returns>0]; losses=trade_returns[trade_returns<0]
    loss_streak=worst=0
    for value in pnl:
        loss_streak=loss_streak+1 if value<0 else 0
        worst=max(worst,loss_streak)
    total=float(capital[-1]/initial_cash-1)
    return dict(trades=len(trades), win_rate=float((pnl>0).mean()) if len(pnl) else None,
        average_win=float(wins.mean()) if len(wins) else None,
        average_loss=float(losses.mean()) if len(losses) else None,
        profit_loss_ratio=float(wins.mean()/abs(losses.mean())) if len(wins) and len(losses) else None,
        expectancy=float(trade_returns.mean()) if len(trade_returns) else None,
        profit_factor=float(pnl[pnl>0].sum()/abs(pnl[pnl<0].sum())) if (pnl<0).any() else None,
        total_return=total, annualized_return=float((1+total)**(252/len(daily))-1),
        max_drawdown=float(np.min(capital/peak-1)),
        Sharpe=float(returns.mean()/returns.std(ddof=1)*np.sqrt(252)) if len(returns)>1 and returns.std(ddof=1)>0 else None,
        max_consecutive_losses=worst,
        average_holding_period=float(np.mean([x["holding_sessions"] for x in trades])) if trades else None,
        trading_sessions=len(daily), final_equity=float(capital[-1]))

class BacktestEngine:
    def __init__(self, rules=None):
        self.rules=rules or TradingRules()
    def _fee(self, notional, sell=False):
        r=self.rules
        return max(r.minimum_commission,notional*r.commission_rate)+notional*(r.transfer_fee_rate+(r.stamp_tax_rate if sell else 0))
    def _round(self, value):
        tick=Decimal(str(self.rules.tick_size))
        return float((Decimal(str(value))/tick).quantize(Decimal("1"),rounding=ROUND_HALF_UP)*tick)
    def run(self, frame, signals, period="1d"):
        f=prepare_bars(frame,period)
        if f.empty:
            raise ValueError("empty_backtest")
        r=self.rules
        dates=f.timestamp.dt.normalize()
        sessions=list(dates.drop_duplicates())
        session_index={d:i for i,d in enumerate(sessions)}
        previous_closes=f.assign(day=dates).groupby("day").close.last().shift()
        cash=r.initial_cash; quantity=0; position=None; pending=None
        signals=sorted([dict(s,signal_time=pd.Timestamp(s["signal_time"])) for s in signals], key=lambda s:s["signal_time"])
        for s in signals:
            if s.get("action") not in ("BUY","SELL"):
                raise ValueError("invalid_order_action")
            if s.get("stop_loss",.05)<=0 or s.get("take_profit",.10)<=0 or s.get("max_holding_days",5)<1:
                raise ValueError("invalid_exit_policy")
        cursor=0; fills=[]; trades=[]; blocked=[]; equity=[]
        for i, row in f.iterrows():
            day=dates.iloc[i]; day_i=session_index[day]
            open_time=day+pd.Timedelta(hours=9,minutes=30) if period=="1d" else row.timestamp-pd.Timedelta(minutes=30)
            # At a shared 30m boundary, the previous bar is processed before the next bar.
            while cursor<len(signals) and signals[cursor]["signal_time"]<=open_time:
                s=signals[cursor]; cursor+=1
                if (s["action"]=="BUY" and not quantity and pending is None) or (s["action"]=="SELL" and quantity):
                    pending=s
            if pending:
                side=pending["action"]
                reason=None
                if bool(row.get("suspended",False)) or row.volume<=0:
                    reason="SUSPENDED_OR_NO_VOLUME"
                if side=="SELL" and quantity and day<=position["entry_day"]:
                    reason="T_PLUS_ONE"
                upper=row.get("limit_up",np.nan); lower=row.get("limit_down",np.nan)
                if not np.isfinite(upper) or not np.isfinite(lower):
                    ref=previous_closes.get(day,np.nan)
                    if r.allow_estimated_limits and np.isfinite(ref):
                        upper=self._round(ref*(1+r.regular_limit_pct))
                        lower=self._round(ref*(1-r.regular_limit_pct))
                    else:
                        reason=reason or "LIMIT_METADATA_UNAVAILABLE"
                price=float(row.open*(1+r.slippage if side=="BUY" else 1-r.slippage))
                if np.isfinite(upper) and np.isfinite(lower) and (price>=upper-r.tick_size/2 or price<=lower+r.tick_size/2):
                    reason=reason or "PRICE_LIMIT_NO_FILL"
                if reason:
                    blocked.append(dict(timestamp=open_time.isoformat(),signal_time=pending["signal_time"].isoformat(),side=side,reason=reason))
                else:
                    fill_time=max(open_time,pending["signal_time"]+pd.Timedelta(microseconds=1))
                    if side=="BUY" and not quantity:
                        budget=min(cash,(cash+quantity*row.open)*r.max_position)
                        q=int(budget/price/r.lot_size)*r.lot_size
                        while q>0 and q*price+self._fee(q*price)>budget:
                            q-=r.lot_size
                        if q >= r.min_buy_quantity:
                            fee=self._fee(q*price); cash-=q*price+fee; quantity=q
                            position=dict(entry_day=day,entry_session=day_i,entry_price=price,entry_fee=fee,
                                entry_time=fill_time.isoformat(),signal_time=pending["signal_time"].isoformat(),
                                stop_loss=float(pending.get("stop_loss",.05)),take_profit=float(pending.get("take_profit",.1)),
                                max_holding_days=int(pending.get("max_holding_days",5)),pattern_id=pending.get("pattern_id"))
                            fills.append(dict(side="BUY",signal_time=position["signal_time"],order_time=position["signal_time"],
                                fill_time=fill_time.isoformat(),fill_price=price,quantity=q,fee=fee,exit_reason=None))
                        else:
                            blocked.append(dict(timestamp=open_time.isoformat(),side=side,reason="INSUFFICIENT_CASH"))
                        pending=None
                    elif side=="SELL" and quantity:
                        fee=self._fee(quantity*price,True); cash+=quantity*price-fee
                        pnl=quantity*(price-position["entry_price"])-fee-position["entry_fee"]
                        fills.append(dict(side="SELL",signal_time=pending["signal_time"].isoformat(),
                            order_time=pending["signal_time"].isoformat(),fill_time=fill_time.isoformat(),
                            fill_price=price,quantity=quantity,fee=fee,exit_reason=pending.get("exit_reason","SIGNAL")))
                        trades.append(dict(pattern_id=position["pattern_id"],entry_time=position["entry_time"],
                            exit_time=fill_time.isoformat(),entry_signal_time=position["signal_time"],
                            exit_signal_time=pending["signal_time"].isoformat(),entry_price=position["entry_price"],
                            exit_price=price,quantity=quantity,pnl=float(pnl),
                            net_return=float(pnl/(quantity*position["entry_price"]+position["entry_fee"])),
                            fees=fee+position["entry_fee"],holding_sessions=day_i-position["entry_session"],
                            exit_reason=pending.get("exit_reason","SIGNAL")))
                        quantity=0; position=None; pending=None
            # Close-observed stops deliberately become next-open orders, not same-bar fills.
            if quantity and pending is None:
                move=float(row.close/position["entry_price"]-1)
                # After N completed holding sessions (including entry day),
                # schedule exit at the following session's first tradable open.
                holding_complete=(row.available_at.hour==15 and row.available_at.minute==0
                    and day_i-position["entry_session"]+1>=position["max_holding_days"])
                reason="STOP_LOSS" if move<=-position["stop_loss"] else "TAKE_PROFIT" if move>=position["take_profit"] else "MAX_HOLDING" if holding_complete else None
                if reason:
                    pending=dict(action="SELL",signal_time=row.available_at,exit_reason=reason)
            equity.append(dict(timestamp=row.available_at.isoformat(),cash=float(cash),quantity=quantity,
                sellable_quantity=quantity if quantity and day>position["entry_day"] else 0,
                equity=float(cash+quantity*row.close)))
        return dict(metrics=performance(equity,trades,r.initial_cash),trades=trades,fills=fills,blocked_orders=blocked,
            equity=equity,open_quantity=quantity,pending_order=bool(pending),rules=asdict(r),
            assumptions=["Price-return simulation on raw bars; no unprovided dividends or corporate actions invented.",
                         "Stops observed at bar close; exits attempt the next bar open, subject to T+1 and liquidity.",
                         "Both price-limit boundaries conservatively reject fills; no queue priority model.",
                         "Session-specific limit_up/limit_down override configured regular-board estimates."]+
                         (["Historical limit references estimated from previous session close; IPO/ST/ex-right overrides require explicit metadata."] if r.allow_estimated_limits else []))
