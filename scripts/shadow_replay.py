"""Offline observed-evidence replay. Never invent counterfactual primary responses."""
import argparse
from collections import Counter
from datetime import datetime, date
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.market_data.quality import Health, provider_failure_reasons
from src.market_data.hithink_history import HithinkHistoryProvider, date_ms
from src.market_data.hithink_reference import HithinkCalendar, ReferenceCache
from src.market_data.hithink_client import ApiResult
from src.market_data.history_cache import FreeHistoryCache


def deny_network(*args, **kwargs):
    raise RuntimeError("offline_replay_network_forbidden")


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replay(evidence, output, calendar_path):
    output.mkdir(parents=True, exist_ok=True)
    rows = read(evidence/"snapshots.jsonl")
    old_switches, timeline, new_switches, unknown = [], [], [], 0
    health = Health()
    active, failures, previous_fingerprint = "tencent", [], None
    last_switch, last_probe = float("-inf"), float("-inf")
    for index, row in enumerate(rows):
        if index and row["source"] != rows[index-1]["source"]:
            old_switches.append(dict(timestamp=row["sample_started"],from_source=rows[index-1]["source"],to_source=row["source"]))
        observation = row["health"]["tencent"]
        returned = row["source"] == "tencent"
        fingerprint = json.dumps(observation, sort_keys=True)
        observed = returned or fingerprint != previous_fingerprint
        previous_fingerprint = fingerprint
        if not returned:
            unknown += 1
        reasons = None
        if observed:
            # Non-returned probes have transport/parse fields and counts MISSING.
            # Aggregate-only projection; explicit uncertainty is retained below.
            reasons = provider_failure_reasons(coverage=observation["coverage_ratio"],
                valid_ratio=observation["valid_price_ratio"],latency=observation["latency"],
                has_prices=observation["valid_price_ratio"]>0,advanced=observation["timestamp_advanced"],
                session=row["market_session"],has_recent_quotes=
                    bool(row["statuses"].get("LIVE",0)+row["statuses"].get("CACHED",0)) if returned else True,
                errors=row["errors"] if returned else [])
            # Exercise the production Health state machine with compact equivalent counts.
            recent = not ("TIMESTAMP_STALL" in reasons and observation["timestamp_advanced"] is not False)
            batch = SimpleNamespace(coverage_ratio=observation['coverage_ratio'],valid_price_ratio=observation['valid_price_ratio'],
                latency=observation['latency'],timestamp_advanced=observation['timestamp_advanced'],
                snapshots={'sample':SimpleNamespace(quote_status='CACHED' if recent else 'STALE')},
                returned_symbols=['sample'],valid_symbols=['sample'] if observation['valid_price_ratio'] else [],
                errors=row['errors'] if returned else [],market_session=row['market_session'],
                provider_evidence=row.get('provider_evidence',{}) if returned else {})
            health.observe(batch)
            reasons = [] if health.last_sample_good else health.reason.split(',')
            if reasons:
                failures.append(dict(timestamp=row['sample_started'],reasons=reasons,partial_evidence=not returned))
            stamp=datetime.fromisoformat(row['sample_started']).timestamp()
            if active=='tencent' and health.consecutive_failures>=health.unavailable_after and stamp-last_switch>=30:
                # Promote only an actually observed good fallback, not an invented response.
                if row['source']=='sina' and row['health']['sina']['last_sample_good']:
                    new_switches.append(dict(timestamp=row['sample_started'],from_source=active,to_source='sina',reasons=reasons))
                    active='sina';last_switch=stamp;last_probe=stamp
            elif active=='sina' and stamp-last_probe>=30:
                last_probe=stamp
                if health.state=='HEALTHY' and stamp-last_switch>=30:
                    new_switches.append(dict(timestamp=row['sample_started'],from_source=active,to_source='tencent',reasons=['RECOVERY']))
                    active='tencent';last_switch=stamp
        timeline.append(dict(timestamp=row['sample_started'],provider_health=health.state,reasons=reasons,
            observed_primary=observed,primary_payload_present=returned,projected_source=active,
            actual_source=row['source'],coverage=row['coverage_ratio'],freshness=row['statuses']))
    (output/'provider_health_timeline.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in timeline),encoding='utf-8')
    summary=dict(old_switch_count=len(old_switches),new_switch_count=len(new_switches),
        new_count_semantics='observed-metrics projection, NOT exact counterfactual routing',
        exact_counterfactual_verified=False,rows_without_primary_payload=unknown,
        limitation='Original collector omitted triggering primary HTTP/parse/count fields and unrequested counterfactual polls; missing is not success.',
        fallback_timeline=new_switches,remaining_primary_failure_reasons=failures)
    (output/'source_switch_timeline.json').write_text(json.dumps(dict(old=old_switches,new=new_switches),indent=2),encoding='utf-8')
    (output/'coverage_timeline.jsonl').write_text(''.join(json.dumps(dict(timestamp=r['sample_started'],
        source=r['source'],coverage_ratio=r['coverage_ratio'],valid_price_ratio=r['valid_price_ratio'],
        errors=r['errors']))+'\n' for r in rows),encoding='utf-8')
    distribution = Counter()
    for row in rows:
        distribution.update(row['statuses'])
    (output/'freshness_distribution.json').write_text(json.dumps(distribution,indent=2),encoding='utf-8')
    # Reuse immutable saved bars and calendar; no live source, no production cache.
    saved=read(evidence/'history_sample.jsonl')
    calendar_data=json.loads(calendar_path.read_text(encoding='utf-8'))['data']
    now=datetime.fromisoformat('2026-09-28T10:00:00+08:00')
    results=[]
    with tempfile.TemporaryDirectory(prefix='aagent-shadow-replay-') as directory:
        for sample in saved:
            bars=sample['bars']; symbol=sample['metrics']['symbol']; calls=[]
            def get(endpoint, params=None):
                if endpoint=='calendar':return ApiResult('SUCCESS',data=calendar_data)
                calls.append(endpoint)
                return ApiResult('SUCCESS',data={'item':[dict(date_ms=date_ms(date.fromisoformat(b['trade_date'])),
                    open_price=b['open'],high_price=b['high'],low_price=b['low'],close_price=b['close'],
                    volume=b['volume_shares'],turnover=b['amount_cny']) for b in bars]})
            client=SimpleNamespace(get=get)
            calendar=HithinkCalendar(client,ReferenceCache(Path(directory)/'reference',lambda:now),clock=lambda:now)
            cache=FreeHistoryCache(Path(directory)/'history')
            provider=HithinkHistoryProvider(client,calendar,cache,clock=lambda:now)
            first=provider.load(symbol,120);second=provider.load(symbol,120)
            provider=HithinkHistoryProvider(client,calendar,cache,clock=lambda:now)
            third=provider.load(symbol,120,network=False)
            identity=lambda r:(r.source,r.adjustment,len(r.bars),r.quality_status,
                               [b.trade_date.isoformat() for b in r.bars],sorted(r.warnings))
            path=cache._path(symbol,'hithink','raw','equity','official_rest')
            metadata=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            results.append(dict(symbol=symbol,first=first.metrics(),second=second.metrics(),third=third.metrics(),
                deterministic=identity(first)==identity(second)==identity(third),network_fetches=len(calls),
                cache_metadata={k:v for k,v in metadata.items() if k!='bars'}))
    (output/'history_reread.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    summary['history_deterministic']=sum(r['deterministic'] for r in results)
    summary['history_qualities']=dict(Counter(r['third']['quality_status'] for r in results))
    (output/'replay_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--calendar',type=Path,required=True)
    args=parser.parse_args()
    socket.create_connection=deny_network
    socket.socket.connect=deny_network
    replay(args.evidence,args.output,args.calendar)
