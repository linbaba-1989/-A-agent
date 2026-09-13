"""Private JSON-lines worker owning the isolated xtquant Token runtime."""
from __future__ import annotations

import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
from typing import Any


def _configure_json_streams() -> None:
    """Keep the private JSON-lines protocol independent of the Windows code page."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="strict")


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    try:
        return value.item()
    except AttributeError:
        return value


def _reply(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(_plain(payload), ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main(runtime_root: str) -> None:
    sys.path.insert(0, str(Path(runtime_root).resolve()))
    with redirect_stdout(sys.stderr):
        from xtquant import xtdatacenter as xtdc
        from xtquant import xtdata

    initialized = False
    for line in sys.stdin:
        request = json.loads(line)
        request_id = request.get("id")
        command = request.get("command")
        try:
            if command == "init":
                token = request.pop("token", "")
                if not token:
                    raise RuntimeError("xtdc_token_missing")
                with redirect_stdout(sys.stderr):
                    if request.get("data_home"):
                        xtdc.set_data_home_dir(request["data_home"])
                    xtdc.set_token(token)
                    xtdc.init()
                token = ""
                initialized = True
                result = {"version": request.get("version"), "xtdata_path": xtdata.__file__,
                          "xtdatacenter_path": xtdc.__file__}
            elif not initialized:
                raise RuntimeError("xtdc_not_initialized")
            elif command == "get_sector_list":
                with redirect_stdout(sys.stderr):
                    result = xtdata.get_sector_list()
            elif command == "get_stock_list_in_sector":
                with redirect_stdout(sys.stderr):
                    result = xtdata.get_stock_list_in_sector(request["sector"])
            elif command == "get_all_sector_stocks":
                with redirect_stdout(sys.stderr):
                    sector_names = xtdata.get_sector_list() or []
                    stock_set = set()
                    for sector_name in sector_names:
                        try:
                            stock_set.update(xtdata.get_stock_list_in_sector(sector_name) or [])
                        except Exception:
                            continue
                result = sorted(stock_set)
            elif command == "download_sector_data":
                with redirect_stdout(sys.stderr):
                    result = xtdata.download_sector_data()
            elif command == "download_history_data":
                result = {}
                for code in request["symbols"]:
                    with redirect_stdout(sys.stderr):
                        result[code] = xtdata.download_history_data(code, request["period"])
            elif command == "get_full_tick":
                with redirect_stdout(sys.stderr):
                    result = xtdata.get_full_tick(request["symbols"])
            elif command == "get_quote_server_status":
                with redirect_stdout(sys.stderr):
                    status = xtdata.get_quote_server_status()
                result = dict((key, str(value)) for key, value in status.items())
            elif command == "get_instrument_detail":
                with redirect_stdout(sys.stderr):
                    result = xtdata.get_instrument_detail(request["symbol"], request.get("complete", False))
            elif command == "get_local_data":
                with redirect_stdout(sys.stderr):
                    frames = xtdata.get_local_data(field_list=request["fields"], stock_list=request["symbols"],
                                                   period=request["period"], count=request["count"],
                                                   dividend_type="none", fill_data=False) or {}
                result = {code: {"columns": list(frame.columns), "index": list(frame.index),
                                 "data": frame.values.tolist()} for code, frame in frames.items()}
            elif command == "close":
                try:
                    xtdc.shutdown()
                finally:
                    _reply({"id": request_id, "success": True, "result": True})
                    return
            else:
                raise RuntimeError("xtdc_command_unknown")
            _reply({"id": request_id, "success": True, "result": result})
        except Exception as exc:
            message = type(exc).__name__ + ": " + str(exc)
            if command == "init" and token:
                message = message.replace(token, "***")
            _reply({"id": request_id, "success": False, "error": message})


if __name__ == "__main__":
    _configure_json_streams()
    main(sys.argv[1])
