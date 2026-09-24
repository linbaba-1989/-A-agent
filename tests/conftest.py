"""Keep offline history tests and UI previews out of the user's runtime DB."""
import pytest


@pytest.fixture(autouse=True)
def isolated_research_history_db(tmp_path, monkeypatch):
    monkeypatch.setenv('A_AGENT_RESEARCH_DB', str(tmp_path / 'research_history.sqlite3'))
