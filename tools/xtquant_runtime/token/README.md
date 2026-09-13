# XtDataCenter token runtime

This directory is reserved for the official, isolated Token runtime. The verified POC version is `xtquant 250807.1.2` (`xtquant_250807`). Install it locally without committing its Python wrappers or native binaries:

```powershell
python -m pip install --no-deps --target tools/xtquant_runtime/token xtquant==250807.1.2 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

Set `XTDC_TOKEN` only in the ignored local `.env`. The token worker receives it over stdin and does not place it in command arguments, logs, diagnostics, or exception text.

The Token runtime runs in a dedicated Python worker process. This prevents its wrappers and native modules from being mixed with the frozen `230825b` runtime used by `QMTProvider`.
