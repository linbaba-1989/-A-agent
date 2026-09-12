import os
from datetime import datetime
from .models import ProviderStatus

class QMTProvider:
    """只读取用户本机 QMT 行情；未连接时拒绝产出行情。"""

    def status(self) -> ProviderStatus:
        try:
            from xtquant import xtdata  # type: ignore
            return ProviderStatus(True, "xtquant 可用")
        except Exception as exc:
            return ProviderStatus(False, f"xtquant 不可用：{exc}")

    def get_quote(self, symbol: str):
        try:
            from xtquant import xtdata  # type: ignore
            data = xtdata.get_full_tick([symbol])
            tick = data.get(symbol)
            if not tick:
                return None
            return {
                "symbol": symbol,
                "last": tick.get("lastPrice"),
                "open": tick.get("open"),
                "high": tick.get("high"),
                "low": tick.get("low"),
                "volume": tick.get("volume"),
                "amount": tick.get("amount"),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "source": "QMT xtquant get_full_tick",
            }
        except Exception:
            return None
