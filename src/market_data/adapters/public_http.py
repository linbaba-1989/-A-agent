"""Bounded public HTTP only: no key, identity rotation, or implicit retries."""
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import json

MAX_BYTES = 16 * 1024 * 1024


def get_text(url, headers=None):
    request = Request(url, headers={"User-Agent": "A-Agent-market-research/1.0", **(headers or {})})
    with urlopen(request, timeout=12) as response:
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("response_too_large")
        # Ranking JSON is UTF-8; legacy quote text is GBK/GB18030. Some public
        # responses omit charset, so never decode JSON with the quote fallback.
        charset = response.headers.get_content_charset() or (
            "utf-8" if data.lstrip().startswith((b"{", b"[")) else "gb18030")
        return data.decode(charset)


def tencent_universe(transport=get_text):
    """Dated dynamic list, fetched only by the universe cache, never each quote."""
    from .parsers import from_wire
    from ..contracts import Security
    from ..universe import board_for
    rows, total = {}, None
    for page in range(100):
        params = {"_appver": "11.17.0", "board_code": "aStock", "sort_type": "price",
                  "direct": "down", "offset": str(page * 200), "count": "200"}
        payload = json.loads(transport(
            "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?" + urlencode(params)))
        data = payload["data"]
        total = int(data["total"])
        if not 1 <= total <= 20000:
            raise ValueError("invalid_universe_total")
        for row in data["rank_list"]:
            symbol = from_wire(row["code"])
            rows[symbol] = Security(symbol, row.get("name") or None, symbol[-2:], board_for(symbol))
        if (page + 1) * 200 >= total:
            if len(rows) < total * .98:
                raise ValueError("incomplete_universe")
            return list(rows.values())
    raise ValueError("universe_pagination_limit")
