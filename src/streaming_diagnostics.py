"""Opt-in, bounded diagnostics; never fetches or alters market quotes."""
from collections import deque
from threading import Lock
from time import monotonic, time
import tracemalloc


class StreamingDiagnostics:
    def __init__(self):
        import psutil  # Optional diagnostic runtime only; not a production dependency.
        self.process = psutil.Process()
        self.lock = Lock()
        self.started = monotonic()
        self.samples = deque(maxlen=60)
        self.owns_tracing = not tracemalloc.is_tracing()
        if self.owns_tracing:
            tracemalloc.start(1)
        self.baseline = tracemalloc.take_snapshot()
        self.process.cpu_percent()

    def sample(self, service):
        with self.lock:
            feed = service.feed
            with feed._lock:
                buffers = {
                    **feed.buffer.diagnostics(),
                    "event_buffer_length": len(feed.request_diagnostics),
                    "event_buffer_limit": feed.request_diagnostics.maxlen,
                    "latest_rows": len(feed._latest_rows),
                    "latest_ticks": len(feed._latest_ticks),
                    "instrument_details": len(feed._details),
                    "provider_init_count": feed.provider_initializations,
                    "provider_request_count": feed.provider_request_count,
                }
            current, peak = tracemalloc.get_traced_memory()
            rss = self.process.memory_info().rss
            tracer_bytes = tracemalloc.get_tracemalloc_memory()
            snapshot = tracemalloc.take_snapshot()
            growth = sorted((item for item in snapshot.compare_to(self.baseline, 'lineno')
                             if item.size_diff > 0), key=lambda item: item.size_diff, reverse=True)[:10]
            top = [{"location": str(item.traceback[0]), "bytes_diff": item.size_diff,
                    "count_diff": item.count_diff} for item in growth]
            result = {"sampled_at": time(), "elapsed": round(monotonic() - self.started, 3),
                      "rss_bytes": rss, "python_allocated_bytes": current,
                      "python_peak_bytes": peak, "tracer_overhead_bytes": tracer_bytes,
                      "cpu_percent": self.process.cpu_percent(),
                      "worker_count": sum(any(arg.endswith('xtdc_worker.py') for arg in p.cmdline())
                                          for p in self.process.children(recursive=True)),
                      "top_growth": top, **buffers, **service.connection_stats()}
            self.samples.append(result)
            return result

    def close(self):
        self.baseline = None
        if self.owns_tracing:
            tracemalloc.stop()
