# coding: gbk
"""Paste/run in the QMT model editor. Read-only; it does not place orders."""
import json

CODES = ["000001.SZ", "600000.SH", "600519.SH"]


def _probe(ContextInfo):
    result = {}
    for code in CODES:
        try:
            detail = ContextInfo.get_instrument_detail(code)
            result[code] = dict((key, detail.get(key)) for key in (
                "InstrumentName", "PreClose", "FloatVolumn", "TotalVolumn",
                "FloatVolume", "TotalVolume", "VolumeMultiple", "InstrumentStatus",
                "IsTrading", "OpenDate"))
        except Exception as exc:
            result[code] = {"error": type(exc).__name__ + ": " + str(exc)}
    print("QMT_INTERNAL_CAPITAL_PROBE " + json.dumps(result, ensure_ascii=True, default=str))


def init(ContextInfo):
    ContextInfo.set_universe(CODES)


def handlebar(ContextInfo):
    _probe(ContextInfo)
