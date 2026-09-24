"""One history source for every research page; session state is only a cache."""
from dataclasses import asdict, is_dataclass
from datetime import datetime
import sqlite3
from time import perf_counter
from uuid import uuid4

from src.research_repository import (CURRENT_SCHEMA_VERSION, ResearchRepository,
                                     error_category, sanitize_record)
from ui.research_view import workforce_data

STATUS_LABELS = {'complete': '完成', 'partial': '部分完成', 'failed': '失败'}


class HistorySaveError(Exception):
    def __init__(self, record):
        super().__init__('研究结果尚未保存，请重试保存，无需重新调用 AI。')
        self.record = record


def recent_records(symbol=None, limit=100):
    return ResearchRepository().recent(symbol=symbol, limit=limit)


def persist_result(result, repository=None):
    # Assign once before attempting the transaction, so a storage retry has the
    # same identity. No model fields are filled in or inferred here.
    record = sanitize_record(result)
    record.setdefault('research_id', str(uuid4()))
    record.setdefault('schema_version', CURRENT_SCHEMA_VERSION)
    record.setdefault('created_at', datetime.now().astimezone().isoformat(timespec='seconds'))
    record.setdefault('data_status_by_role', workforce_data(record))
    try:
        return (repository or ResearchRepository()).save(record)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        raise HistorySaveError(record) from exc


def publish_result(state, result, repository=None):
    repository = repository or ResearchRepository()
    saved = persist_result(result, repository)
    # Publish only after commit. Session loss never removes the database row.
    state['last_research'] = saved
    state['research_history'] = repository.recent()
    return saved


def execute_and_save(call, *, symbol, analysis_mode='standard', facts=None, router=None,
                     few_shot_requested=None, repository=None, research_id=None, retrieval_audit=None):
    """Run the existing callable, then commit in the worker before notifying UI."""
    research_id = research_id or str(uuid4())
    baseline = dict(getattr(router, 'last_results', {}))
    started = perf_counter()
    try:
        result = call()
    except Exception as exc:
        rows = {}
        for role, row in getattr(router, 'last_results', {}).items():
            if row is not baseline.get(role):
                rows[role] = asdict(row) if is_dataclass(row) else dict(row)
        chief = rows.pop('chief_researcher', None) or {
            'success': False, 'data': None, 'error': error_category(type(exc).__name__)}
        result = {'employees': rows, 'chief_researcher': chief,
                  'error': error_category(type(exc).__name__)}
    result = {**result, 'research_id': research_id, 'schema_version': CURRENT_SCHEMA_VERSION,
              'symbol': symbol, 'analysis_mode': analysis_mode,
              'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
              'elapsed_seconds': perf_counter() - started}
    result.setdefault('fact_data', facts)
    if retrieval_audit is not None:
        result.setdefault('few_shot_audit', retrieval_audit())
    if few_shot_requested is not None:
        result['few_shot_requested'] = few_shot_requested
    return persist_result(result, repository)
