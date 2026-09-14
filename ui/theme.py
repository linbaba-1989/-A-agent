import streamlit as st


CSS = """
<style>
:root { --cn-red:#d92d20; --cn-green:#079455; --ink:#17202e; --muted:#667085; --line:#e4e7ec; }
.stApp { background:#f5f6f8; color:var(--ink); }
.block-container { padding:2.8rem .8rem 2.5rem; max-width:none; }
h1,h2,h3 { letter-spacing:-.02em; color:var(--ink); }
h1 { font-size:1.42rem !important; margin:.05rem 0 .25rem !important; }
h2 { font-size:1.18rem !important; }
[data-testid="stSidebar"] { background:#fff; border-right:1px solid var(--line); width:180px !important; min-width:180px !important; }
[data-testid="stSidebar"] > div:first-child { width:180px !important; padding:.55rem .45rem; }
[data-testid="stSidebar"] * { color:var(--ink); }
[data-testid="stSidebar"] [role="radiogroup"] { gap:2px; }
[data-testid="stSidebar"] label { min-height:38px; padding:4px 7px; border-radius:2px; font-size:13px; }
[data-testid="stSidebar"] label:has(input:checked) { background:#fff1f0; border-left:3px solid var(--cn-red); }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { font-size:12px; }
[data-testid="stMetric"] { background:#fff; border:1px solid var(--line); border-radius:4px; padding:.55rem .75rem; }
[data-testid="stMetricLabel"] { color:var(--muted); font-size:.75rem; }
[data-testid="stDataFrame"] { border:1px solid var(--line); }
[data-testid="stNumberInput"] button { display:none !important; }
[data-testid="stNumberInput"] input { padding-right:.5rem !important; }
.terminal-bar { position:fixed; left:180px; right:0; bottom:0; height:32px; z-index:999; background:#fff; border-top:1px solid #d0d5dd; padding:6px 14px; font-size:13px; color:#344054; }
.quote-up { color:var(--cn-red); } .quote-down { color:var(--cn-green); } .quote-flat { color:var(--muted); }
.panel { background:#fff; border:1px solid var(--line); border-radius:3px; padding:9px 11px; margin-bottom:8px; }
.eyebrow { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
.price { font-size:30px; font-weight:700; line-height:1.1; }
.quote-grid { display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:7px; margin:7px 0; }
.quote-grid div { background:#fff; border:1px solid var(--line); padding:8px 10px; min-width:0; }
.quote-grid span { display:block; color:var(--muted); font-size:11px; }
.quote-grid b { display:block; margin-top:3px; font-size:14px; white-space:nowrap; }
.tech-panel { background:#fff; border:1px solid var(--line); padding:9px 11px; font-size:12px; }
.tech-row { display:flex; justify-content:space-between; gap:8px; padding:4px 0; border-bottom:1px dotted #eaecf0; }
.tech-row span { color:var(--muted); } .tech-row b { text-align:right; }
.tech-note { color:var(--muted); font-size:10px; margin:3px 0 5px; }
.tech-section { margin-top:9px; }
.top-tool { background:#fff; border:1px solid var(--line); border-radius:3px; padding:6px 10px; margin-bottom:7px; }
.index-strip,.system-strip { display:flex; align-items:center; gap:22px; min-height:36px; padding:7px 11px; background:#fff; border:1px solid var(--line); font-size:13px; }
.system-strip { min-height:52px; justify-content:space-between; margin:7px 0; }
.system-item b { font-size:16px; margin-left:5px; }
.dot-ok { color:#12b76a; } .dot-warn { color:#f79009; } .dot-off { color:#98a2b3; }
.price-flash-up { animation:flash-up .5s ease-out; }
.price-flash-down { animation:flash-down .5s ease-out; }
@keyframes flash-up { 0% { background:#fecdca; color:var(--cn-red); } 100% { background:transparent; } }
@keyframes flash-down { 0% { background:#a6f4c5; color:var(--cn-green); } 100% { background:transparent; } }
.live-badge { font-weight:700; color:var(--cn-red); } .stale-badge { color:#f79009; font-weight:700; }
.terminal-table { width:100%; border-collapse:collapse; background:#fff; font-size:12px; }
.terminal-table th { background:#f2f4f7; color:#475467; text-align:right; padding:6px 7px; border:1px solid var(--line); font-weight:500; }
.terminal-table td { text-align:right; padding:6px 7px; border:1px solid #eef0f3; height:27px; }
.terminal-table th:first-child,.terminal-table td:first-child,.terminal-table th:nth-child(2),.terminal-table td:nth-child(2) { text-align:left; }
.empty-row { color:#98a2b3; text-align:center !important; height:32px !important; }
.side-panel-title { font-size:14px; font-weight:650; border-bottom:1px solid var(--line); padding-bottom:6px; margin-bottom:5px; }
.ai-line { display:flex; justify-content:space-between; padding:4px 0; font-size:12px; border-bottom:1px dotted #eaecf0; }
#MainMenu, [data-testid="stHeader"], [data-testid="stToolbar"], footer { visibility:hidden; height:0; }
button { border-radius:3px !important; }
@media (max-width:1440px) {
  [data-testid="stSidebar"], [data-testid="stSidebar"] > div:first-child { width:160px !important; min-width:160px !important; }
  .terminal-bar { left:160px; }
  .block-container { padding-left:.55rem; padding-right:.55rem; }
  .system-strip { gap:8px; }
}
</style>
"""


def apply_theme() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
