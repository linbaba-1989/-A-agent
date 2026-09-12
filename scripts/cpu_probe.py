"""Measure cached scanner CPU and memory once per second for 30 seconds."""
import json
from pathlib import Path
import statistics
import sys
import threading
import time

from dotenv import load_dotenv
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.qmt_provider import QMTProvider  # noqa: E402
from src.scanner import MarketScanner  # noqa: E402


def summary(values):
    ordered = sorted(values)
    return {"average": statistics.mean(values), "max": max(values),
            "p95": ordered[max(0, int(len(ordered) * 0.95 + 0.999999) - 1)]}


if __name__ == "__main__":
    load_dotenv()
    scanner = MarketScanner(QMTProvider())
    scanner.scan(len(scanner.universe) or 6000)  # warm static/history caches
    process = psutil.Process()
    samples = []
    stop = threading.Event()

    def sample():
        process.cpu_percent(None)
        psutil.cpu_percent(None)
        while not stop.wait(1):
            samples.append({"system_cpu_percent": psutil.cpu_percent(None),
                            "python_process_cpu_percent": process.cpu_percent(None),
                            "process_memory_mb": process.memory_info().rss / 1024 / 1024})

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    scans, scan_seconds = 0, []
    started = time.monotonic()
    while time.monotonic() - started < 30:
        result = scanner.scan(len(scanner.universe))
        scans += 1
        scan_seconds.append(result.diagnostics.elapsed_seconds)
    stop.set()
    thread.join()
    report = {"duration_seconds": time.monotonic() - started, "samples": len(samples), "scans": scans,
              "system_cpu_percent": summary([row["system_cpu_percent"] for row in samples]),
              "python_process_cpu_percent": summary([row["python_process_cpu_percent"] for row in samples]),
              "process_memory_mb": summary([row["process_memory_mb"] for row in samples]),
              "scan_seconds": summary(scan_seconds)}
    print(json.dumps(report, indent=2))
