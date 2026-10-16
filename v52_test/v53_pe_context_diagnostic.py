"""Read-only diagnostic: stdlib only; never imports application/data modules."""
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

MISSING = '未保存'
DEFAULT_RUN_ID = '4b93534b80e145cebd72daad0b008844'
FIELDS = ('status', 'fetched_at', 'last_attempt_at', 'http_request_count', 'http_status', 'error')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def field(obj, name):
    return obj[name] if isinstance(obj, dict) and name in obj else MISSING


def read_json(path):
    try:
        with path.open('r', encoding='utf-8') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            return True, None, 'JSON root is not an object'
        return True, value, None
    except FileNotFoundError:
        return False, None, MISSING
    except Exception as exc:
        return None, None, type(exc).__name__


def source_summary(source):
    out = {key: field(source, key) for key in FIELDS}
    records = source.get('records') if isinstance(source, dict) else None
    if isinstance(records, dict):
        counts = Counter()
        for record in records.values():
            value = field(record, 'source_date')
            # JSON keys preserve null/missing distinction and tolerate malformed values.
            label = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            counts[label] += 1
        out['source_date'] = dict(sorted(counts.items()))
        out['record_count'] = len(records)
    else:
        out['source_date'] = MISSING
        out['record_count'] = MISSING
    return out


def context_summary(context):
    if not isinstance(context, dict):
        return {'as_of': MISSING, 'context_version': MISSING,
                'twse_pe': source_summary(None), 'pe_snapshot_version': MISSING}
    official = context.get('official')
    sources = official.get('sources') if isinstance(official, dict) else None
    pe = sources.get('twse_pe') if isinstance(sources, dict) else None
    return {'as_of': field(context, 'as_of'),
            'context_version': field(context, 'context_version'),
            'twse_pe': source_summary(pe),
            'pe_snapshot_version': field(context.get('pe_snapshot_versions'), 'twse_pe')}


def inspect_run(run_id, session_state, *, module_dir=None, environ=None):
    if not isinstance(run_id, str) or re.fullmatch(r'[0-9a-f]{32}', run_id) is None:
        return {'run_id': run_id, 'diagnostic_error': 'Invalid run ID; no files read'}
    env = os.environ if environ is None else environ
    directory = Path(module_dir) if module_dir is not None else Path(__file__).parent
    root = Path(env.get('V53_QUALITY_CONTEXT_DIR', str(directory / '.v53_quality'))).resolve()
    sidecar = root / 'contexts' / (hashlib.sha256(run_id.encode()).hexdigest() + '.json')
    disk = root / 'official' / 'twse_pe.json'
    key = 'v53_quality_context:' + run_id
    present = key in session_state
    session_context = session_state.get(key) if present else None
    exists, payload, error = read_json(sidecar)
    hash_ok = MISSING
    side_context = None
    if isinstance(payload, dict) and 'context' in payload:
        side_context = payload['context']
        if 'sha256' in payload:
            try:
                hash_ok = isinstance(payload['sha256'], str) and payload['sha256'] == digest(side_context)
            except Exception:
                hash_ok = False
    session_version = field(session_context, 'context_version')
    side_version = field(side_context, 'context_version')
    same = (session_version == side_version) if (hash_ok is True and
        isinstance(session_version, str) and session_version != MISSING and
        isinstance(side_version, str) and side_version != MISSING) else MISSING
    disk_exists, disk_payload, disk_error = read_json(disk)
    return {
        'run_id': run_id,
        'session_context_key_present': present,
        'session_context_exists': isinstance(session_context, dict),
        'session_context_is_null': present and session_context is None,
        'sidecar_exists': exists, 'sidecar_read_error': error,
        'sidecar_hash_pass': hash_ok,
        'sidecar_context_exists': isinstance(side_context, dict),
        'sidecar_context_is_null': isinstance(payload, dict) and 'context' in payload and side_context is None,
        'session_and_sidecar_same_version': same,
        'resolved_context_root': str(root), 'resolved_official_cache_dir': str(root / 'official'),
        'sidecar_path': str(sidecar),
        'path_note': '目前設定解析路徑；不代表已證明歷史建立路徑。',
        'session_context': context_summary(session_context),
        'sidecar_context': context_summary(side_context),
        'sidecar_note': 'hash 非 true 時，內容僅供診斷，不視為可信 context。',
        'disk_twse_pe': dict(exists=disk_exists, path=str(disk), read_error=disk_error,
                             **source_summary(disk_payload)),
        'field_note': '未保存代表欄位不存在；null 是原檔明確保存的空值。source_date 為日期與筆數分布。',
    }


def main(st):
    st.title('V5.3 P/E Context 唯讀診斷')
    st.caption('不掃描、不刷新、不建立 context、不重新計分；僅讀取指定任務的既有資料。')
    run_id = st.query_params.get('scan_task', DEFAULT_RUN_ID)
    try:
        st.json(inspect_run(run_id, st.session_state))
    except Exception as exc:
        st.json({'run_id': run_id, 'diagnostic_error': type(exc).__name__})
    st.stop()
