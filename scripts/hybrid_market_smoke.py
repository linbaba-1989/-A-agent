"""Explicit read-only network smoke; emits only redacted market data and diagnostics."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from src.market_data.hybrid_provider import HybridMarketDataProvider
from src.market_clock import to_beijing, market_session
from src.scanner import MarketScanner
from src.workforce_acceptance import build_fact_bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.network:
        parser.error("explicit --network required")
    load_dotenv(ROOT/".env", override=False)
    if os.getenv("A_AGENT_MARKET_DATA_MODE") != "hybrid":
        parser.error("explicit A_AGENT_MARKET_DATA_MODE=hybrid required")
    provider = HybridMarketDataProvider()
    report = dict(started=to_beijing().isoformat(), market_session=market_session(),
                  intraday_live_acceptance=False)
    try:
        report["universe_count"] = len(provider.get_stock_universe())
        report["quote"] = provider.quote("600498.SH").to_dict()
        report["security"] = provider.get_instrument_detail("600498.SH")
        history = provider.get_daily_history("600498.SH", 120)
        report["history"] = history.metrics()
        report["indices"] = {s: q.to_dict() for s, q in provider.get_index_snapshot().snapshots.items()}
        report["calendar"] = provider.get_calendar().last_result.status
        report["sectors"] = {}
        for tag in ("industry", "cn_concept"):
            catalog = provider.get_sector_catalog(tag)
            rows = (catalog.data or {}).get("item", [])
            report["sectors"][tag] = dict(status=catalog.status, count=len(rows))
            if rows:
                members = provider.get_sector_members(rows[0]["index_thscode"])
                report["sectors"][tag]["member_sample"] = dict(code=rows[0]["index_thscode"],
                    status=members.status, count=len((members.data or {}).get("item", [])))
        before = len(provider.official.client.events)
        full = provider.snapshot_all()
        report["full_snapshot"] = full.metrics()
        report["full_snapshot"]["official_requests_during_poll"] = len(provider.official.client.events) - before
        scanner = MarketScanner(provider)
        # Real single-symbol consumer acceptance; avoid bootstrapping 5,578 daily histories in a smoke.
        scanner.universe = ["600498.SH"]
        scanner.instrument_cache["600498.SH"] = provider.normalized_instrument("600498.SH")
        scanner.history_service.initialize(scanner.universe)
        scan = scanner.scan()
        report["scanner"] = dict(rows=scan.rows, diagnostics=scan.diagnostics.to_dict())
        report["fact_bundle"] = build_fact_bundle("600498.SH", provider, scanner)
        states = provider.provider_status()
        report["provider_states"] = states
        report.update(realtime_provider=full.source, history_provider=history.source,
                      universe_provider=states["universe"], index_provider=states["index"])
        report["official_events"] = provider.official.client.events
        report["pass"] = bool(report["quote"]["price"] and len(history.bars) == 120 and scan.rows
                              and full.source == "tencent" and full.coverage_ratio >= .95
                              and report["full_snapshot"]["official_requests_during_poll"] == 0
                              and all(q["price"] for q in report["indices"].values()))
    finally:
        provider.close()
    def scalar(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "item"):
            return value.item()
        raise TypeError(type(value).__name__)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(provider.official.client.redact(report), ensure_ascii=False,
                                      indent=2, default=scalar, allow_nan=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("pass", "realtime_provider", "history_provider", "universe_provider", "index_provider")}))


if __name__ == "__main__":
    main()
