# A-agent xtquant runtime

Production runtime: `official_230825b`.

The vendor package is intentionally excluded from Git because it contains official `.dll` and `.pyd` binaries whose redistribution terms are not established. Install it locally without modifying the QMT installation:

1. Download `xtquant_0825b_2023-09-20.rar` from the [ThinkTrader official download page](https://dict.thinktrader.net/nativeApi/download_xtquant.html).
2. Extract the archive outside the QMT installation.
3. Copy the complete, unchanged `xtquant` directory into this directory so that these paths exist:

   ```text
   tools/xtquant_runtime/230825b/xtquant/__init__.py
   tools/xtquant_runtime/230825b/xtquant/xtdata.py
   tools/xtquant_runtime/230825b/xtquant/IPythonApiClient.cp311-win_amd64.pyd
   tools/xtquant_runtime/230825b/xtquant/xtpythonclient.cp311-win_amd64.pyd
   ```

   Use the native files matching the active Python version. Do not combine wrappers or native modules from another xtquant installation.

4. Start the local QMT client, then run:

   ```powershell
   python scripts/qmt_diagnostics.py
   ```

`QMTProvider` loads this directory exclusively. It raises `xtquant_runtime_missing` when the package has not been installed, `xtquant_runtime_incomplete` when required files are absent, and `xtquant_component_mixed` if the process has already loaded any xtquant component from another directory. It never silently falls back to the broker-bundled legacy package.
