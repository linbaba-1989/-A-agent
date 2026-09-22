from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THEME = (ROOT / "ui" / "theme.py").read_text(encoding="utf-8")
SIDEBAR = (ROOT / "ui" / "components" / "sidebar.py").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
REALTIME = (ROOT / "ui" / "pages" / "realtime_market.py").read_text(encoding="utf-8")


def test_desktop_sidebar_css_is_visible_at_desktop_widths():
    assert '[data-testid="stSidebar"] { background:#fff' in THEME
    assert 'width:180px !important' in THEME
    assert '@media (max-width:1440px)' in THEME
    assert 'width:160px !important' in THEME
    assert '[data-testid="stSidebar"] { visibility:hidden' not in THEME
    assert '[data-testid="stSidebar"] { display:none' not in THEME


def test_active_navigation_is_rendered_by_sidebar_and_called_before_router():
    expected = ("总览", "实时行情", "全A扫描", "自选股", "个股研究", "AI研究院", "策略回测", "设置")
    for item in expected:
        assert item in SIDEBAR
    assert "key=\"main_navigation\"" in SIDEBAR
    assert '[data-testid="stSidebar"] label:has(input:checked) { background:#fff1f0; border-left:3px solid var(--cn-red); }' in THEME
    assert "page = render_sidebar()" in APP


def test_realtime_fragment_does_not_hide_sidebar():
    assert '@st.fragment(run_every=interval)' in REALTIME
    assert "st.sidebar" not in REALTIME
    assert '[data-testid="stSidebar"]' not in REALTIME


def test_collapsed_sidebar_keeps_streamlit_restore_control_visible():
    assert '[data-testid="stHeader"] { visibility:visible' in THEME
    assert '[data-testid="stToolbar"] { visibility:visible' in THEME
    assert '[data-testid="stExpandSidebarButton"] { visibility:visible' in THEME
    assert '[data-testid="stHeader"], [data-testid="stToolbar"]' not in THEME


def test_streamlit_material_icons_keep_their_icon_font():
    assert '[data-testid="stIconMaterial"] { font-family:"Material Symbols Rounded" !important; }' in THEME
