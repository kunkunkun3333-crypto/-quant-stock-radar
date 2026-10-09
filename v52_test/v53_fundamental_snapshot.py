"""Isolated official-data snapshots. Local disk is optional, not durable cloud storage."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import RLock

try:
    from .v53_fundamental_normalize import SOURCES
except ImportError:
    from v53_fundamental_normalize import SOURCES

_MEMORY = {}
_LOCK = RLock()
SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc)


def as_datetime(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError('timezone_aware_datetime_required')
    return result


def _key(source_id, cache_dir):
    if source_id not in SOURCES:
        raise ValueError('unknown_source')
    return (str(Path(cache_dir).resolve()) if cache_dir is not None else None, source_id)


def _valid(snapshot, source_id):
    try:
        if snapshot['schema_version'] != SCHEMA_VERSION or snapshot['source_id'] != source_id:
            return False
        as_datetime(snapshot['fetched_at'])
        if not isinstance(snapshot['records'], dict) or not snapshot['records']:
            return False
        for code, row in snapshot['records'].items():
            if row['code'] != code or row['source']['source_id'] != source_id:
                return False
            if not row['period'] or not row['source_date'] or not isinstance(row['values'], dict):
                return False
        json.dumps(snapshot, allow_nan=False)
        return True
    except (KeyError, ValueError, TypeError):
        return False


def load_snapshot(source_id, *, cache_dir=None):
    key = _key(source_id, cache_dir)
    with _LOCK:
        if key in _MEMORY:
            return deepcopy(_MEMORY[key])
        if cache_dir is None:
            return None
        try:
            path = Path(cache_dir) / (source_id + '.json')
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if not _valid(snapshot, source_id):
                return None
            _MEMORY[key] = snapshot
            return deepcopy(snapshot)
        except (OSError, ValueError, TypeError):
            return None


def save_snapshot(source_id, snapshot, *, cache_dir=None):
    key = _key(source_id, cache_dir)
    if not _valid(snapshot, source_id):
        return {'saved_memory': False, 'saved_disk': False, 'error': 'invalid_snapshot'}
    with _LOCK:
        _MEMORY[key] = deepcopy(snapshot)
        if cache_dir is None:
            return {'saved_memory': True, 'saved_disk': False, 'error': None}
        temporary = None
        try:
            directory = Path(cache_dir)
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                             prefix=source_id + '.', suffix='.tmp', delete=False) as stream:
                temporary = stream.name
                json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / (source_id + '.json'))
            return {'saved_memory': True, 'saved_disk': True, 'error': None}
        except (OSError, ValueError, TypeError) as error:
            return {'saved_memory': True, 'saved_disk': False, 'error': type(error).__name__}
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def classify_snapshot(snapshot, *, now=None, max_age_seconds=86400):
    if not snapshot:
        return 'unavailable'
    try:
        age = (as_datetime(now or utc_now()) - as_datetime(snapshot['fetched_at'])).total_seconds()
        return 'fresh_cache' if 0 <= age < max_age_seconds else 'stale'
    except (ValueError, TypeError, KeyError):
        return 'stale'
