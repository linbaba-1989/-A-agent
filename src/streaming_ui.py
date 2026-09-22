"""Local SSE adapter over the existing feed; never owns or initializes a provider.

One poller per shared feed, one latest-value bus (no unbounded subscriber queues).
Only connected, enabled viewers cause regular polling, and only in quote sessions.
Outside quote sessions, Token feeds hydrate once and retain the last valid snapshot.
The loopback-only HTTP surface is a desktop POC, not a remote deployment server.
"""
from __future__ import annotations

import atexit
import json
import os
import select
import socket
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
from threading import Condition, Event, Lock, Thread
from time import monotonic, time
from urllib.parse import parse_qs, urlsplit

from .market_clock import beijing_now, market_session, should_fetch_quotes
from .realtime_market import market_quote_timestamp, rank_rows
from ui.view_models import quote_status_display

ASSETS = Path(__file__).resolve().parents[1] / "ui" / "streaming"
FIELDS = ("symbol", "name", "lastPrice", "lastClose", "change_pct", "speed_1m",
          "speed_3m", "speed_5m", "amount", "quote_time", "quote_timestamp", "snapshot_seq")


def build_payload(feed, rows, previous_timestamp, now):
    stamp = market_quote_timestamp(rows, feed.last_quote_timestamp)
    status = quote_status_display({}, stamp, previous_timestamp, now=now,
                                  market_session_value=market_session(now), feed=feed)
    if rows and (not should_fetch_quotes(market_session(now)) or status['quote_status'] == 'UNAVAILABLE'):
        status.update(quote_status="CACHED", last_quote_timestamp=stamp,
                      last_quote_time=max(row["quote_time"] for row in rows), valid_quotes=len(rows))
    ranked = rank_rows(rows, "change_pct", 20)
    target = next((row for row in rows if row["symbol"] == "600498.SH"), None)
    def public(row):
        result = {key: row.get(key) for key in FIELDS}
        try:
            result["change"] = round(float(row["lastPrice"]) - float(row["lastClose"]), 4)
        except (KeyError, TypeError, ValueError):
            result["change"] = None
        return result
    return {"snapshot_seq": feed.snapshot_seq, "status": status,
            "target": public(target) if target else None,
            "top20": [public(row) for row in ranked], "valid_quotes": len(rows),
            "provider_init_count": feed.provider_initializations,
            "provider_request_count": feed.provider_request_count,
            "from_cache": all(row.get("from_cache", False) for row in rows)}


class SnapshotBus:
    def __init__(self):
        self.condition = Condition()
        self.version = 0
        self.latest = None
        self.published_at = None
        self.published_monotonic = None

    def publish(self, payload):
        with self.condition:
            if payload == self.latest:
                return
            self.version += 1
            self.latest = payload
            self.published_at = time() * 1000
            self.published_monotonic = monotonic()
            self.condition.notify_all()

    def read(self, after=0, timeout=1):
        return self.read_event(after, timeout)[:2]

    def read_event(self, after=0, timeout=1):
        with self.condition:
            self.condition.wait_for(lambda: self.version > after, timeout)
            return self.version, self.latest, self.published_at, self.published_monotonic


class StreamingUI:
    def __init__(self, feed, *, clock=beijing_now):
        self.feed = feed
        self.clock = clock
        self.bus = SnapshotBus()
        self.stop = Event()
        self.lock = Lock()
        self.clients = {}
        self.connected_total = 0
        self.disconnected_total = 0
        self.sent_events = deque(maxlen=300)
        self.probe = None
        if os.environ.get("A_AGENT_STREAM_DIAGNOSTICS") == "1":
            from .streaming_diagnostics import StreamingDiagnostics
            self.probe = StreamingDiagnostics()
        self.previous_timestamp = None
        self.last_error = None
        self.key = secrets.token_urlsafe(24)  # Local capability, never the provider Token.
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Do not log capabilities or provider credentials.

            def do_GET(self):
                url = urlsplit(self.path)
                parts = url.path.strip("/").split("/")
                if len(parts) != 2 or not secrets.compare_digest(parts[0], owner.key):
                    self.send_error(404)
                    return
                route = parts[1]
                if route in {"index.html", "stream.js", "stream.css"}:
                    content = (ASSETS / route).read_bytes()
                    self.send_response(200)
                    mime = {"index.html": "text/html", "stream.js": "text/javascript", "stream.css": "text/css"}
                    self.send_header("Content-Type", mime[route] + "; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    self.wfile.write(content)
                elif route == "diagnostics" and owner.probe is not None:
                    sample = owner.probe.sample(owner) if parse_qs(url.query).get("sample") == ["1"] else None
                    with owner.lock:
                        events = list(owner.sent_events)
                    data = json.dumps({"sample": sample, "connections": owner.connection_stats(),
                                       "sent_events": events}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                elif route == "events":
                    query = parse_qs(url.query)
                    interval = query.get("interval", ["2"])[0]
                    interval = int(interval) if interval in {"1", "2", "5"} else 2
                    enabled = query.get("enabled", ["1"])[0] == "1"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    client = owner.subscribe(enabled, interval)
                    version = 0  # Always replay latest complete snapshot on reconnect.
                    self.connection.settimeout(5)
                    last_write = monotonic()
                    try:
                        while not owner.stop.is_set():
                            next_version, payload, published_at, published_mono = owner.bus.read_event(version, timeout=1)
                            if select.select([self.connection], [], [], 0)[0]:
                                if not self.connection.recv(1, socket.MSG_PEEK):
                                    break
                            if payload is not None and next_version != version:
                                sent_at = time() * 1000
                                delay_ms = (monotonic() - published_mono) * 1000
                                envelope = {**payload, "event_id": next_version,
                                            "feed_published_at": published_at, "sse_sent_at": sent_at,
                                            "feed_to_sse_ms": delay_ms,
                                            "diagnostics_enabled": owner.probe is not None}
                                data = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
                                self.wfile.write(f"id: {next_version}\nretry: 1500\ndata: {data}\n\n".encode("utf-8"))
                                if owner.probe is not None:
                                    with owner.lock:
                                        owner.sent_events.append({"event_id": next_version,
                                            "snapshot_seq": payload["snapshot_seq"],
                                            "feed_published_at": published_at, "sse_sent_at": sent_at,
                                            "feed_to_sse_ms": delay_ms,
                                            "replay": version == 0, "trace": payload.get("trace")})
                                version = next_version
                            elif monotonic() - last_write >= 15:
                                self.wfile.write(b": heartbeat\n\n")
                            else:
                                continue
                            self.wfile.flush()
                            last_write = monotonic()
                    except (OSError, ValueError):
                        pass
                    finally:
                        owner.unsubscribe(client)
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.http_thread = Thread(target=self.server.serve_forever, daemon=True, name="streaming-ui-http")
        self.poll_thread = Thread(target=self._poll, daemon=True, name="streaming-ui-poll")
        self.http_thread.start()
        self.poll_thread.start()
        atexit.register(self.close)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/{self.key}/index.html"

    def subscribe(self, enabled=True, interval=2):
        client = object()
        with self.lock:
            self.clients[client] = (enabled, interval)
            self.connected_total += 1
        return client

    def unsubscribe(self, client):
        with self.lock:
            if self.clients.pop(client, None) is not None:
                self.disconnected_total += 1

    def connection_stats(self):
        with self.lock:
            return {"active_clients": len(self.clients), "connected_total": self.connected_total,
                    "disconnected_total": self.disconnected_total,
                    "per_client_queue_size": 0, "bus_slots": int(self.bus.latest is not None),
                    "sent_event_buffer_length": len(self.sent_events), "sent_event_buffer_limit": 300}

    def tick(self, enabled):
        now = self.clock()
        session = market_session(now)
        try:
            if not should_fetch_quotes(session):
                if self.bus.latest is None:
                    self.bus.publish(build_payload(self.feed, self.feed.cached(), self.previous_timestamp, now))
                rows = self.feed.ensure_closed_snapshot(session, now)
            elif enabled and self.feed.provider is not None:
                rows = self.feed.snapshot(market_session=session, now=now)
            else:
                rows = self.feed.cached()
            # Static names are filled only for the 21 visible symbols, using the
            # feed's existing instrument cache, without changing ranking rules.
            visible = rank_rows(rows, "change_pct", 20)
            target = next((row for row in rows if row["symbol"] == "600498.SH"), None)
            if target is not None and target not in visible:
                visible.append(target)
            if self.feed.provider is not None:
                self.feed.enrich_static(visible)
            payload = build_payload(self.feed, rows, self.previous_timestamp, self.clock())
            self.previous_timestamp = payload["status"].get("last_quote_timestamp")
            self.last_error = None
        except Exception as exc:
            self.last_error = type(exc).__name__
            payload = build_payload(self.feed, self.feed.cached(), self.previous_timestamp, now)
            payload["error"] = self.last_error
        if self.probe is not None:
            record = self.feed.last_request_diagnostic or {}
            # Only attach the diagnostic for this exact snapshot sequence.
            if record.get("feed_snapshot_seq") == payload["snapshot_seq"]:
                payload["trace"] = {key: record.get(key) for key in (
                    "request_id", "request_started_at", "request_finished_at",
                    "provider_raw_max_quote_time", "provider_raw_median_quote_time",
                    "feed_snapshot_seq", "feed_quote_time", "rows_changed_count", "from_cache")}
        self.bus.publish(payload)

    def _poll(self):
        due = 0.0
        while not self.stop.wait(0.1):
            with self.lock:
                clients = list(self.clients.values())
            if not clients:
                due = 0.0
                continue
            enabled = any(item[0] for item in clients)
            interval = min((item[1] for item in clients if item[0]), default=2)
            if monotonic() >= due:
                self.tick(enabled)
                due = monotonic() + interval

    def close(self):
        if self.stop.is_set():
            return
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.http_thread.join(timeout=2)
        self.poll_thread.join(timeout=2)
        if self.probe is not None and not self.poll_thread.is_alive():
            self.probe.close()
        atexit.unregister(self.close)
