"""Explicit, sequential REST capability probe. Never prints credentials."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from src.market_clock import to_beijing
from src.market_data.hithink_client import HithinkRestClient


def serialize_report(client, report):
    def date_default(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError("unsupported_report_type")
    return json.dumps(client.redact(report),ensure_ascii=False,indent=2,default=date_default)

def full_acceptance(client,cache_path):
    from src.market_data.hithink_provider import HithinkOfficialProvider
    from src.market_data.adapters.realtime import TencentRealtimeProvider
    from src.market_data.historical import moving_averages
    provider=HithinkOfficialProvider(client=client,cache_path=cache_path)
    universe=provider.universe.refresh(force=True)
    calendar=provider.calendar.refresh(force=True)
    result={"universe":{**universe.metrics(),"page_count":len(provider.universe.pages),
                       "pages":provider.universe.pages,"securities":list(provider.universe.rows.values())},
            "calendar":calendar.metrics()}
    symbols=["600498.SH","600000.SH","000001.SZ","300750.SZ","688981.SH","920982.BJ"]
    samples=provider.snapshot(symbols)
    result["samples"]={s:q.to_dict() for s,q in samples.snapshots.items()}
    result["sample_metrics"]=samples.metrics()
    tencent=TencentRealtimeProvider().snapshot(symbols[:3])
    fields=("price","prev_close","open","high","low","pct_change","volume_shares","amount_cny")
    result["cross_validation"]=[]
    for symbol in symbols[:3]:
        official=samples.snapshots[symbol]
        other=tencent.snapshots[symbol]
        delta={f:getattr(official,f)-getattr(other,f) if getattr(official,f) is not None
               and getattr(other,f) is not None else None for f in fields}
        result["cross_validation"].append(dict(symbol=symbol,hithink=official.to_dict(),
            tencent=other.to_dict(),hithink_minus_tencent=delta,
            time_semantics="Hithink page latest upstream time; Tencent per-symbol vendor quote time"))
    full=provider.snapshot_all()
    result["full_snapshot"]={**full.metrics(),"status_counts":{
        s:sum(q.quote_status==s for q in full.snapshots.values())
        for s in ("LIVE","STALE","CACHED","UNAVAILABLE")}}
    result["full_snapshot"]["quotes"]=[q.to_dict() for q in full.snapshots.values()]
    history=provider.history.load("600498.SH",120)
    result["history"]={**history.metrics(),"bars":[b.to_dict() for b in history.bars],
                       "ma":moving_averages(history)}
    verified=[]
    result["index_directory_checks"]=[]
    for symbol in ("000001.SH","399001.SZ","399006.SZ"):
        response=client.get("search",dict(q=symbol,asset_type="a-share-index",limit=5))
        matches=[r for r in (response.data or {}).get("item",[]) if r.get("thscode")==symbol]
        result["index_directory_checks"].append(dict(symbol=symbol,**response.metrics(),matches=matches))
        if matches:verified.append(symbol)
    result["index_snapshot"]=None
    if verified:
        indices=provider.index_snapshot(verified)
        result["index_snapshot"]={**indices.metrics(),"quotes":[q.to_dict() for q in indices.snapshots.values()]}
    result["index_history"]=[]
    for symbol in verified:
        daily=provider.history.load(symbol,120,kind="index")
        result["index_history"].append({**daily.metrics(),"sample":[b.to_dict() for b in daily.bars[-3:]]})
    result["catalogs"]={}
    chosen=None
    for tag in ("cn_concept","industry"):
        catalog=provider.sectors.catalog(tag,force=True)
        result["catalogs"][tag]={**catalog.metrics(),"items":(catalog.data or {}).get("item",[])}
        if catalog.ok and catalog.data["item"]:
            chosen=catalog.data["item"][0]
    if chosen:
        members=provider.sectors.members(chosen["index_thscode"],chosen["index_name"],chosen["tag"],force=True)
        result["members"]={**members.metrics(),"data":members.data}
    else:
        result["members"]={"status":"NOT_TESTED_NO_CATALOG"}
    before=len(client.events)
    cached=provider.history.load("600498.SH",120)
    result["history_cache_repeat"]={**cached.metrics(),"new_rest_calls":len(client.events)-before}
    result["http_events"]=client.events
    result["rate_limit_events"]=[e for e in client.events if e["http_status"]==429 or e["code"]==4001]
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--network",action="store_true")
    p.add_argument("--full",action="store_true")
    p.add_argument("--cache",type=Path,default=Path("data/hithink"))
    p.add_argument("--output",type=Path,default=Path("outputs/diagnostics/hithink_probe.json"))
    args=p.parse_args()
    if args.full and not args.network:
        p.error("--full requires --network")
    load_dotenv(Path(__file__).resolve().parents[1]/".env",override=False)
    client=HithinkRestClient()
    now=to_beijing()
    end=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=1)
    start=end-timedelta(days=240)
    interval={"interval":"1d","start":int(start.timestamp()*1000),"end":int(end.timestamp()*1000)}
    report={"api_key_configured":client.configured,"started_at":now.isoformat(),"capabilities":{}}
    if args.network:
        requests=[
            ("tickers","tickers",{"asset_type":"a-share","limit":1,"offset":0}),
            ("snapshot","snapshot",{"thscodes":"600498.SH"}),
            ("history","history",{"thscode":"600498.SH","adjust":"none",**interval}),
            ("calendar","calendar",{}),
            ("index_directory","search",{"q":"上证指数","asset_type":"a-share-index","limit":5}),
            ("index_snapshot","index_snapshot",{"thscodes":"000001.SH"}),
            ("index_history","index_history",{"thscode":"000001.SH",**interval}),
            ("cn_concept","catalog",{"tag":"cn_concept"}),
            ("industry","catalog",{"tag":"industry"}),
        ]
        member=None
        for name,capability,params in requests:
            result=client.get(capability,params)
            report["capabilities"][name]={**result.metrics(),"sample":(result.data or {}).get("item",[])[:2]}
            if name in {"cn_concept","industry"} and result.ok and result.data["item"]:
                member=result.data["item"][0]["thscode"]
            print(json.dumps({"capability":name,**result.metrics()},ensure_ascii=True),flush=True)
        if member:
            result=client.get("members",{"thscode":member})
            report["capabilities"]["members"]={**result.metrics(),"index_thscode":member,
                                              "sample":(result.data or {}).get("item",[])[:2]}
        else:
            report["capabilities"]["members"]={"status":"NOT_TESTED_NO_CATALOG"}
    if args.full:
        report["full_acceptance"]=full_acceptance(client,args.cache)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(serialize_report(client,report),encoding="utf-8")


if __name__=="__main__":
    main()
