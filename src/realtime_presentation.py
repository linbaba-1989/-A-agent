"""UI consumption boundary: provider identity and trade-date isolation, no source policy."""
from .market_clock import market_session, should_fetch_quotes, timestamp_to_beijing, to_beijing


def supports_realtime(provider):
    return (callable(getattr(provider, "get_stock_universe", None)) and
            (callable(getattr(provider, "snapshot", None)) or
             all(callable(getattr(provider, name, None)) for name in
                 ("get_full_ticks", "tick_timestamp", "_valid_tick"))))


def feed_mode(provider):
    if getattr(provider, "hybrid", False):
        return "hybrid"
    if callable(getattr(provider, "snapshot", None)) and not callable(getattr(provider, "get_full_ticks", None)):
        return "free"
    return "legacy"


def provenance(provider, source, timestamp):
    mode = feed_mode(provider)
    name = getattr(provider, "provider_name", type(provider).__name__)
    realtime = getattr(provider, "realtime", provider)
    day = timestamp_to_beijing(timestamp)
    return dict(market_data_mode=mode, provider=name, realtime_provider=getattr(realtime, "provider_name", name),
                source=source, trade_date=day.date().isoformat() if day else None)


def presentation_rows(feed, rows, now=None):
    """Never project another chain/date into Hybrid's current market table."""
    provider = feed.provider
    if feed_mode(provider) == "legacy":
        return rows, {}
    current = to_beijing(now)
    chain = getattr(provider, "realtime", provider)
    sources = {getattr(getattr(chain, name, None), "source", None) for name in ("primary", "fallback")}
    sources.discard(None)
    calendar = getattr(getattr(provider, "official", None), "calendar", None)
    session = market_session(current, calendar) if calendar is not None else market_session(current)
    accepted, rejected = [], []
    for row in rows:
        identity = provenance(provider, row.get("source"), row.get("quote_timestamp"))
        reason = None
        if (row.get("market_data_mode") != identity["market_data_mode"] or
                row.get("provider") != identity["provider"] or row.get("source") not in sources):
            reason = "provider_provenance_mismatch"
        elif row.get("trade_date") != identity["trade_date"]:
            reason = "trade_date_mismatch"
        elif should_fetch_quotes(session) and identity["trade_date"] != current.date().isoformat():
            reason = "not_current_trade_date"
        if reason:
            rejected.append(dict(source=row.get("source"), trade_date=identity["trade_date"], reason=reason))
        else:
            accepted.append(row)
    source = ",".join(sorted({r["source"] for r in accepted})) or getattr(chain, "active_source", "unavailable")
    status = dict(market_data_mode=feed_mode(provider), provider=getattr(provider, "provider_name", type(provider).__name__),
                  source=source, rejected_rows=len(rejected), rejected_provenance=rejected[:5])
    if not accepted:
        status.update(quote_status="UNAVAILABLE", last_quote_timestamp=None, last_quote_time=None, valid_quotes=0)
    elif all(r.get("quote_status") in {"STALE", "UNAVAILABLE"} for r in accepted):
        status["quote_status"] = "STALE"
    return accepted, status


def beta_unavailable_message(ctx):
    provider = ctx.get("provider") or getattr(ctx.get("selection"), "provider", None)
    if feed_mode(provider) == "hybrid" or getattr(ctx.get("selection"), "name", "") == "Hybrid":
        realtime = getattr(provider, "realtime", None)
        health = getattr(realtime, "health", {})
        detail = " · ".join(f"{name}: {value.state}" for name, value in health.items())
        return "Hybrid实时行情异常 / UNAVAILABLE" + (" · " + detail if detail else "")
    return "当前实时行情 feed 不可用 / UNAVAILABLE"
