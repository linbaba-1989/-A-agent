"""Transactional local research history. No Provider calls or schema migration."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_VERSION = "P1.9.1"
SPECIALISTS = ("technical_analyst", "fundamental_event_analyst", "sentiment_analyst", "risk_officer")
_PRIVATE_KEY = re.compile(r"api.?key|secret|password|authorization|credential|private.?key|access.?token|refresh.?token|(^|_)token$|^headers?$|^\.?env$|^environment$", re.I)
_ERROR_KEYS = {"error", "error_message", "raw_error", "exception", "fallback_reason", "error_type", "traceback"}


def error_category(error):
    """Persist a bounded error category, never a Provider's raw error body."""
    text = str(error or "").lower()
    if not text:
        return None
    for fragments, category in ((('timeout', 'timed out'), 'timeout'),
                                (('authentication', 'unauthorized', '401', '403'), 'authentication_error'),
                                (('rate_limit', 'rate limit', '429'), 'rate_limit'),
                                (('schema', 'validation', 'json'), 'schema_error'),
                                (('insufficient_specialist_results',), 'insufficient_specialist_results'),
                                (('connection', 'connecterror'), 'connection_error')):
        if any(fragment in text for fragment in fragments):
            return category
    return 'provider_error'


def sanitize_record(value):
    """Remove credential fields and redact configured credentials in all text."""
    configured = dict(dotenv_values(ROOT / '.env'))
    configured.update(os.environ)
    secrets = {str(v) for k, v in configured.items() if v and _PRIVATE_KEY.search(k)}

    def collect(item):
        if isinstance(item, dict):
            for key, val in item.items():
                if _PRIVATE_KEY.search(str(key)) and isinstance(val, str) and val:
                    secrets.add(val)
                    if val.lower().startswith(('bearer ', 'basic ')):
                        secrets.add(val.split(None, 1)[1])
                else:
                    collect(val)
        elif isinstance(item, (list, tuple)):
            for val in item:
                collect(val)
    collect(value)

    def clean(item):
        if isinstance(item, dict):
            return {str(key): error_category(val) if str(key).lower() in _ERROR_KEYS else clean(val)
                    for key, val in item.items() if not _PRIVATE_KEY.search(str(key))}
        if isinstance(item, (list, tuple)):
            return [clean(val) for val in item]
        if isinstance(item, str):
            for secret in sorted(secrets, key=len, reverse=True):
                item = item.replace(secret, '[REDACTED]')
            item = re.sub(r'(?i)\bBearer\s+[^\s,;\"\']+', 'Bearer [REDACTED]', item)
            item = re.sub(r'(?im)^.*\b[\w]*(?:API_KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)\s*[:=].*$', '[REDACTED]', item)
            item = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', item)
            return item
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if item is None or isinstance(item, (bool, int, float)):
            return item
        # Reports normally contain JSON values; do not stringify arbitrary objects
        # (client/config reprs can contain credentials).
        if hasattr(item, 'item'):
            return clean(item.item())
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError('research_history_non_json_value')
    return clean(value)


def research_status(record):
    employees = record.get('employees') or {}
    chief = record.get('chief_researcher') or {}
    rows = [employees.get(role) or {} for role in SPECIALISTS] + [chief]
    successes = sum(row.get('success') is True for row in rows)
    if successes == 5 and chief.get('status') != 'degraded' and (chief.get('data') or {}).get('status') != 'degraded':
        return 'complete'
    return 'partial' if successes else 'failed'


def _sort_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


class ResearchRepository:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('A_AGENT_RESEARCH_DB') or ROOT / 'data' / 'research_history.sqlite3').resolve()

    @contextmanager
    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            # SQLite's rollback journal and FULL sync make each insert atomic;
            # no journal-mode switch is needed during concurrent first use.
            db.execute('PRAGMA synchronous=FULL')
            with db:
                db.execute('''CREATE TABLE IF NOT EXISTS research_history (
                    research_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    created_at TEXT, created_ts REAL NOT NULL, analysis_mode TEXT,
                    status TEXT NOT NULL, overall_view TEXT, confidence REAL,
                    elapsed_seconds REAL, schema_version TEXT NOT NULL,
                    saved_at TEXT NOT NULL, payload TEXT NOT NULL)''')
                db.execute('CREATE INDEX IF NOT EXISTS research_by_symbol_time ON research_history(symbol, created_ts DESC)')
                db.execute('CREATE INDEX IF NOT EXISTS research_by_time ON research_history(created_ts DESC)')
            yield db
        finally:
            db.close()

    def save(self, result):
        record = sanitize_record(result)
        if not record.get('research_id') or not record.get('symbol'):
            raise ValueError('research_id_and_symbol_required')
        record['schema_version'] = record.get('schema_version') or 'legacy/unknown'
        record['status'] = research_status(record)
        chief = (record.get('chief_researcher') or {}).get('data') or {}
        record['overall_view'] = chief.get('overall_view')
        record['confidence'] = chief.get('confidence')
        # Store the whole sanitized record: facts, five reports, model/provider,
        # timing, data status, retrieval audit/IDs and usage metadata stay intact.
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with self._connection() as db, db:
            db.execute('''INSERT INTO research_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(research_id) DO NOTHING''', (
                record['research_id'], record['symbol'], record.get('created_at'),
                _sort_timestamp(record.get('created_at')), record.get('analysis_mode'),
                record['status'], record['overall_view'], record['confidence'],
                record.get('elapsed_seconds'), record['schema_version'],
                datetime.now(timezone.utc).isoformat(), payload))
            stored = db.execute('SELECT payload FROM research_history WHERE research_id=?',
                                (record['research_id'],)).fetchone()
        return json.loads(stored[0])

    def recent(self, symbol=None, limit=100):
        if not isinstance(limit, int) or limit < 1:
            raise ValueError('positive_history_limit_required')
        where, params = ('WHERE symbol=?', [symbol]) if symbol is not None else ('', [])
        with self._connection() as db:
            rows = db.execute(f'SELECT payload FROM research_history {where} ORDER BY created_ts DESC, rowid DESC LIMIT ?',
                              [*params, limit]).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, research_id):
        with self._connection() as db:
            row = db.execute('SELECT payload FROM research_history WHERE research_id=?', (research_id,)).fetchone()
        return json.loads(row[0]) if row else None
