"""Immutable monthly archives. Standard library only; no scanning/model hooks."""
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate(snapshot):
    if snapshot.get('market') not in ('TWSE', 'TPEX'):
        raise ValueError('invalid_market')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', snapshot.get('period', '')):
        raise ValueError('invalid_period')
    if not isinstance(snapshot.get('records'), list):
        raise ValueError('invalid_records')
    stamp = datetime.fromisoformat(snapshot['fetched_at'])
    if stamp.tzinfo is None:
        raise ValueError('fetched_at_requires_timezone')
    for row in snapshot['records']:
        if row.get('market') != snapshot['market'] or row.get('period') != snapshot['period']:
            raise ValueError('record_market_or_period_mismatch')
        if not re.fullmatch(r'[A-Za-z0-9]+', row.get('code', '')):
            raise ValueError('invalid_code')
    canonical(snapshot)


def build_snapshot(market, period, records, *, source, source_date, fetched_at, raw_rows=None, raw_metadata=None):
    payload = dict(schema_version=1, market=market, period=period, records=copy.deepcopy(records),
                   source=source, source_date=source_date, fetched_at=fetched_at,
                   raw_rows=copy.deepcopy(raw_rows or []), raw_metadata=copy.deepcopy(raw_metadata or {}))
    validate(payload)
    digest = hashlib.sha256(canonical(payload)).hexdigest()
    payload.update(content_sha256=digest, snapshot_version=digest,
                   archived_at=datetime.now(timezone.utc).isoformat())
    return payload


def verify(snapshot):
    validate(snapshot)
    payload = {k: v for k, v in snapshot.items() if k not in ('content_sha256', 'snapshot_version', 'archived_at')}
    digest = hashlib.sha256(canonical(payload)).hexdigest()
    if snapshot.get('content_sha256') != digest or snapshot.get('snapshot_version') != digest:
        raise ValueError('snapshot_hash_mismatch')
    return snapshot


class SnapshotStore:
    """Optional filesystem archive plus isolated in-memory copies."""
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.memory = {}

    def save(self, snapshot):
        verify(snapshot)
        key = (snapshot['market'], snapshot['period'], snapshot['snapshot_version'])
        if self.root:
            directory = self.root / key[0] / key[1]
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (key[2] + '.json')
            if not path.exists():
                fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(canonical(snapshot))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        self.memory[key] = copy.deepcopy(snapshot)
        return key[2]

    def load_all(self):
        values = copy.deepcopy(self.memory)
        if self.root:
            for path in sorted(self.root.glob('*/*/*.json')):
                snapshot = verify(json.loads(path.read_text(encoding='utf-8')))
                key = (snapshot['market'], snapshot['period'], snapshot['snapshot_version'])
                values[key] = snapshot
        return list(values.values())
