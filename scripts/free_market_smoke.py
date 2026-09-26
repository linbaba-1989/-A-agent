"""Minimal opt-in entry: python scripts/free_market_smoke.py [--network] [--full]."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.market_data.factory import create_market_provider, market_data_mode
from src.market_data.free_provider import FreeMarketDataProvider
from src.market_data.universe import SecurityUniverse
from src.market_clock import market_session, to_beijing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", action="store_true", help="Explicitly allow a few public quote requests")
    parser.add_argument("--full", action="store_true", help="One dynamic-universe snapshot per source")
    parser.add_argument("--output", type=Path, default=Path("outputs/diagnostics/p21a_free_smoke.json"))
    parser.add_argument("--universe-cache", type=Path,
                        default=Path("data/free_market/universe.json"))
    args = parser.parse_args()
    mode = market_data_mode()
    if mode != "free":
        parser.error("This acceptance entry requires A_AGENT_MARKET_DATA_MODE=free; no paid source is probed.")
    universe = SecurityUniverse(args.universe_cache)
    provider = create_market_provider(free_factory=lambda: FreeMarketDataProvider(universe=universe))
    now = to_beijing()
    report = {"mode": mode, "initialized": True, "network": args.network,
              "market_session": market_session(now), "started_at": now.isoformat(),
              "acceptance": "initialization_only", "sources": {}}
    if args.network:
        report["acceptance"] = ("closed_session_smoke_only" if market_session(now) not in {"open", "auction"}
                                else "limited_smoke_not_long_run_acceptance")
        for source, adapter in (("tencent", provider.primary), ("sina", provider.fallback)):
            batches = {"single": adapter.snapshot(["600498.SH"])}
            bootstrap_error = None
            if args.full:
                try:
                    batches["full"] = adapter.snapshot()
                except RuntimeError as exc:
                    bootstrap_error = {"error": str(exc), "cause": universe.last_error}
            report["sources"][source] = {
                name: {**batch.metrics(), "status_counts": {
                    status: sum(q.quote_status == status for q in batch.snapshots.values())
                    for status in ("LIVE", "STALE", "CACHED", "UNAVAILABLE")},
                    "sample": [q.to_dict() for q in list(batch.snapshots.values())[:3]]}
                for name, batch in batches.items()}
            report["sources"][source]["health"] = asdict(adapter.health)
            if bootstrap_error:
                report["sources"][source]["full"] = bootstrap_error
        report["universe_as_of"] = str(universe.as_of)
        report["universe_error"] = universe.last_error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "acceptance": report["acceptance"],
                      "sources": {source: {name: {
                          key: len(value) if isinstance(value, list) else value
                          for key, value in item.items()
                          if key in {"returned_symbols", "valid_symbols", "zero_price_symbols",
                                     "coverage_ratio", "valid_price_ratio", "latency", "status_counts"}}
                          for name, item in groups.items() if name != "health"}
                          for source, groups in report["sources"].items()}}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    main()
