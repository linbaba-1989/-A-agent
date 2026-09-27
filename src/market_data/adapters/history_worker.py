"""Isolated SDK worker: timeout/termination cannot poison application sockets."""
import contextlib
import json
import sys


def fetch(task):
    symbol, adjustment = task["symbol"], task["adjustment"]
    start, end = task["start"], task["end"]
    wire = symbol[-2:].lower() + symbol[:6]
    if task["provider"] == "akshare":
        import akshare as ak
        if task["kind"] == "index":
            frame = ak.stock_zh_index_daily(symbol=wire)
            flavor = "akshare_index_sina"
        elif task.get("endpoint") == "sina":
            frame = ak.stock_zh_a_daily(symbol=wire, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""),
                                      adjust="" if adjustment == "raw" else adjustment)
            flavor = "akshare_sina"
        else:
            frame = ak.stock_zh_a_hist(symbol=symbol[:6], period="daily",
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                adjust="" if adjustment == "raw" else adjustment, timeout=12)
            flavor = "akshare_em"
        return {"rows": json.loads(frame.to_json(orient="records", date_format="iso")),
                "flavor": flavor}
    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError("baostock_login:" + login.error_code)
    try:
        fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
        if task["kind"] != "index":
            fields += ",adjustflag"
        rs = bs.query_history_k_data_plus(symbol[-2:].lower() + "." + symbol[:6], fields,
            start_date=start, end_date=end, frequency="d",
            adjustflag={"raw": "3", "qfq": "2", "hfq": "1"}[adjustment])
        rows = []
        if rs.error_code != "0":
            raise RuntimeError("baostock_query:" + rs.error_code)
        while rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        if rs.error_code != "0":
            raise RuntimeError("baostock_iteration:" + rs.error_code)
        return {"rows": rows, "flavor": "baostock"}
    finally:
        bs.logout()


if __name__ == "__main__":
    task = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            payload = fetch(task)
        print(json.dumps(payload, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__ + ":" + str(exc)[:180]}))
        sys.exit(1)
