"""Session-owned UI job state. Worker threads never render Streamlit elements."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from ui.research_view import ROLES, result_rows
from ui.research_history import HistorySaveError, execute_and_save, publish_result


def start_run(state, symbol, facts, router, call, *, analysis_mode="standard", repository=None):
    runs = state.setdefault("ai_research_runs", {})
    if any(run.get("future") is not None for run in runs.values()): return False
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-ui")
    run = {"states": {role: "idle" for role in ROLES}, "rows": {}, "facts": deepcopy(facts),
           "baseline": dict(getattr(router, "last_results", {})), "router": router,
           "result": None, "error": False, "pool": pool, "repository": repository}
    runs[symbol] = run
    run["future"] = pool.submit(execute_and_save, call, symbol=symbol, facts=facts,
                                router=router, analysis_mode=analysis_mode, repository=repository)
    return True


def poll_run(state, symbol):
    run = state.get("ai_research_runs", {}).get(symbol)
    if not run or run.get("future") is None: return run
    router = run["router"]
    # Prior-run results are ignored; a completed role never returns to waiting.
    for role in ROLES:
        row = getattr(router, "last_results", {}).get(role)
        if row is not None and row is not run["baseline"].get(role):
            run["rows"][role] = deepcopy(asdict(row) if is_dataclass(row) else dict(row))
        elif role not in run["rows"] and getattr(router, "role_states", {}).get(role) == "working":
            run["states"][role] = "working"
    if run["future"].done():
        try:
            result = run["future"].result()
            result.setdefault("fact_data", run["facts"])
            result.setdefault("symbol", symbol)
            result = publish_result(state, result, run.get("repository"))
            run["result"] = result
            run["rows"] = result_rows(result)
        except HistorySaveError as exc:
            state["pending_research_save"] = exc.record
            run["error"] = True
        except Exception:
            run["error"] = True
            for role in ROLES:
                if role not in run["rows"]: run["states"][role] = "error"
        finally:
            run["pool"].shutdown(wait=False)
            for key in ("pool", "router", "baseline"): run.pop(key, None)
            run["future"] = None
    return run
