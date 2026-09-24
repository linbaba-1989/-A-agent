"""Offline persistence acceptance; all fixtures use isolated temporary SQLite DBs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from src.research_repository import CURRENT_SCHEMA_VERSION, ResearchRepository
from ui.components.ai_report import headline_values
from ui.pages.stock_research import load_example_report, _recent_records
from ui.pages import ai_research
from ui.research_history import (HistorySaveError, execute_and_save, persist_result,
                                 publish_result, recent_records)
from ui.research_state import start_run
from ui.view_models import report_view


@pytest.fixture(autouse=True)
def forbid_provider_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('real Provider/network calls forbidden')
    from src.agent import StockResearchAgent
    monkeypatch.setattr(StockResearchAgent, 'analyze', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def record(identifier='offline-fixture', symbol='600498.SH', timestamp='2026-09-24T02:30:00+08:00'):
    report = load_example_report()
    report.update(research_id=identifier, symbol=symbol, created_at=timestamp,
                  elapsed_seconds=383.81, schema_version=CURRENT_SCHEMA_VERSION,
                  few_shot_requested=False,
                  few_shot_audit={'technical_analyst': {'selected_case_ids': ['technical-05'], 'status': 'selected'}})
    for row in [*report['employees'].values(), report['chief_researcher']]:
        row.update(provider='offline', input_tokens=100, output_tokens=50, total_tokens=150,
                   usage_status='available', status='complete')
    return report


def test_full_record_survives_new_repository_and_session(monkeypatch, tmp_path):
    original = record()
    repository = ResearchRepository()
    state = {}
    saved = publish_result(state, original, repository)
    assert saved['status'] == 'complete'
    assert saved['schema_version'] == 'P1.9.1'
    assert saved['employees'] == original['employees']
    assert saved['chief_researcher'] == original['chief_researcher']
    assert saved['few_shot_audit'] == original['few_shot_audit']
    state.clear()
    del repository
    monkeypatch.chdir(tmp_path)
    loaded = ResearchRepository().recent(symbol='600498.SH')[0]
    assert loaded == saved
    assert loaded['elapsed_seconds'] == 383.81
    values = dict(headline_values(report_view(loaded)))
    assert values['短线观点'] == '偏空'
    assert values['中期观点'] == '中性偏多'
    assert not state


def test_legacy_record_preserves_missing_schema_fields():
    legacy = {'research_id': 'legacy', 'symbol': '600498.SH', 'chief_researcher': {
        'success': True, 'data': {'short_term': 'bearish', 'mid_term': 'bullish'}}}
    saved = ResearchRepository().save(legacy)
    assert saved['schema_version'] == 'legacy/unknown'
    assert saved['chief_researcher']['data'] == legacy['chief_researcher']['data']
    values = dict(headline_values(report_view(saved)))
    assert values['短线观点'] == '偏空' and values['中期观点'] == '偏多'
    assert values['风险等级'] == values['置信度'] == '--'


@pytest.mark.parametrize('all_failed', [False, True])
def test_partial_and_failed_results_keep_available_roles(all_failed):
    value = record()
    value['chief_researcher'].update(success=False, data=None, error='TimeoutError: private Provider details')
    if all_failed:
        for row in value['employees'].values():
            row.update(success=False, data=None)
    stored = ResearchRepository().save(value)
    assert stored['status'] == ('failed' if all_failed else 'partial')
    assert stored['employees'] == value['employees']
    assert stored['chief_researcher']['error'] == 'timeout'
    app = AppTest.from_string('from ui.pages.stock_research import _render_records\n_render_records("600498.SH")').run()
    assert not app.exception
    assert '查看原始分析数据' in [expander.label for expander in app.expander]


def test_rerun_and_concurrent_save_are_idempotent():
    value = record()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ResearchRepository().save(value), range(8)))
    assert all(row == results[0] for row in results)
    modified = deepcopy(value)
    modified['chief_researcher']['data']['summary'] = 'must not replace the completed report'
    assert ResearchRepository().save(modified) == results[0]
    assert len(ResearchRepository().recent()) == 1


def test_symbol_filter_limit_and_reverse_chronology():
    repository = ResearchRepository()
    repository.save(record('older', timestamp='2026-09-23T23:59:59+08:00'))
    repository.save(record('other', '000001.SZ', '2026-09-24T03:00:00+08:00'))
    repository.save(record('newer', timestamp='2026-09-24T02:30:00+08:00'))
    assert [r['research_id'] for r in repository.recent()] == ['other', 'newer', 'older']
    assert [r['research_id'] for r in repository.recent('600498.SH')] == ['newer', 'older']
    assert repository.recent('600498.SH', limit=1)[0]['research_id'] == 'newer'
    assert repository.recent('nonexistent') == []


def test_missing_optional_fields_stay_missing():
    value = {'research_id': 'minimal', 'symbol': '600498.SH',
             'employees': {'technical_analyst': None}, 'chief_researcher': None}
    saved = ResearchRepository().save(value)
    assert saved['status'] == 'failed'
    assert saved['chief_researcher'] is None
    assert all(v == '--' for _, v in headline_values(report_view(saved)))


def test_database_contains_no_credentials_or_raw_provider_errors(monkeypatch):
    secret = 'offline-configured-secret-12345'
    nested_secret = 'offline-nested-token-67890'
    monkeypatch.setenv('DEEPSEEK_API_KEY', secret)
    value = record()
    value.update(api_key=secret, headers={'Authorization': 'Bearer ' + nested_secret},
                 token=nested_secret, env='FULL_ENV_CONTENT_PRIVATE')
    row = value['chief_researcher']
    row['data']['summary'] = 'summary ' + secret + ' ' + nested_secret
    row['error'] = 'TimeoutError with raw Provider payload ' + secret
    row['fallback_reason'] = 'Authorization: Bearer ' + nested_secret
    repository = ResearchRepository()
    saved = repository.save(value)
    text = json.dumps(saved)
    assert secret not in text and nested_secret not in text
    assert 'FULL_ENV_CONTENT_PRIVATE' not in text and 'raw Provider payload' not in text
    assert saved['chief_researcher']['error'] == 'timeout'
    assert saved['chief_researcher']['total_tokens'] == 150
    with sqlite3.connect(repository.path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    for path in repository.path.parent.glob(repository.path.name + '*'):
        content = path.read_bytes()
        assert secret.encode() not in content and nested_secret.encode() not in content


def test_two_pages_load_same_saved_record_in_fresh_sessions():
    saved = publish_result({}, record())
    assert _recent_records('600498.SH') == ai_research.recent_records() == [saved]
    scripts = ('from ui.pages.stock_research import _render_records\n_render_records("600498.SH")',
               'from ui.pages.ai_research import render\nrender({})')
    for script in scripts:
        app = AppTest.from_string(script).run()
        assert not app.exception
        text = '\n'.join(str(m.value) for m in app.markdown)
        assert '短线观点' in text and '偏空' in text
        assert '中期观点' in text and '中性偏多' in text
        assert 'AI 综合研判' in text


def test_worker_saves_before_session_poll_and_captures_failure():
    repository = ResearchRepository()
    state, release = {}, Event()
    router = SimpleNamespace(last_results={}, role_states={})
    def offline_run():
        release.wait(timeout=5)
        return record()
    assert start_run(state, '600498.SH', {}, router, offline_run, repository=repository)
    pool = state['ai_research_runs']['600498.SH']['pool']
    state.clear()
    release.set()
    pool.shutdown(wait=True)
    assert len(repository.recent()) == 1
    # A raised Chief timeout still saves the freshly completed specialist.
    def interrupted():
        router.last_results['technical_analyst'] = {'success': True, 'data': {'summary': 'saved technical'}}
        raise TimeoutError('raw secret must never be persisted')
    result = execute_and_save(interrupted, symbol='600498.SH', router=router, repository=repository)
    assert result['status'] == 'partial'
    assert result['employees']['technical_analyst']['data']['summary'] == 'saved technical'
    assert result['chief_researcher']['error'] == 'timeout'


def test_storage_failure_does_not_publish_success_and_can_retry(monkeypatch):
    state = {}
    repository = ResearchRepository()
    real_save = repository.save
    def disk_full(_):
        raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(repository, 'save', disk_full)
    with pytest.raises(HistorySaveError) as failure:
        publish_result(state, record(), repository)
    assert not state
    pending = failure.value.record
    monkeypatch.setattr(repository, 'save', real_save)
    saved = publish_result(state, pending, repository)
    assert saved['research_id'] == pending['research_id']
    assert len(repository.recent()) == 1


def test_repository_uses_repo_root_default(monkeypatch, tmp_path):
    from src.research_repository import ROOT
    monkeypatch.delenv('A_AGENT_RESEARCH_DB', raising=False)
    monkeypatch.chdir(tmp_path)
    assert ResearchRepository().path == ROOT / 'data' / 'research_history.sqlite3'
