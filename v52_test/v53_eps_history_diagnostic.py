"""Opt-in disk-only EPS archive diagnostic; no download, save or refresh.

Set V53_EPS_HISTORY_ARCHIVE_DIR to the existing archive root, or enter it on
this diagnostic page. Default is archive beside this file, only a path proposal;
it is never created. The reader revalidates stored raw evidence and hashes.
"""
import os
from pathlib import Path

FIELDS = ('snapshot_version', 'validation_status', 'total_rows', 'unique_codes',
          'manifest_filename_count', 'csv_file_count', 'fetched_at')
SAMPLES = {'TWSE': ('2330', '2303', '2454', '3711'), 'TPEX': ('6488', '3293')}


def inspect_archive(archive_dir):
    # This dependency has no import-time network or filesystem writes.
    from v53_eps_history import load_historical_eps_snapshot
    root = Path(archive_dir).expanduser().resolve()
    report = {'archive_dir': str(root), 'archive_root_exists': root.is_dir(),
              'fiscal_year': 2025, 'quarter': 2, 'markets': {}}
    for market, codes in SAMPLES.items():
        item = {'validated_snapshot_exists': False, 'error': None,
                **{key: '未保存' for key in FIELDS},
                'samples': {code: {'basic_eps': None, 'status': 'snapshot_unavailable'} for code in codes}}
        report['markets'][market] = item
        if not root.is_dir():
            item['error'] = 'archive_root_not_found'
            continue
        try:
            snapshot = load_historical_eps_snapshot(root, market, 2025, 2)
            if snapshot is None:
                item['error'] = 'snapshot_not_found'
                continue
            metadata = snapshot['metadata']
            if metadata.get('validation_status') != 'validated':
                item['error'] = 'snapshot_not_validated'
                continue
            item['validated_snapshot_exists'] = True
            for key in FIELDS:
                item[key] = metadata.get(key, '未保存')
            for code in codes:
                record = snapshot['records'].get(code)
                item['samples'][code] = {
                    'basic_eps': None if record is None else record.get('basic_eps'),
                    'status': ('record_missing' if record is None else
                               'N/A' if record.get('basic_eps') is None else 'available'),
                    'basis': '未保存' if record is None else record.get('basis', '未保存'),
                    'eps_type': '未保存' if record is None else record.get('eps_type', '未保存'),
                }
        except Exception as exc:
            # One invalid market must not hide the other market's diagnostic.
            item['error'] = f'{type(exc).__name__}: {exc}'
    return report


def main(st):
    st.title('V5.3 Historical EPS Archive Diagnostic')
    st.caption('唯讀既有 archive｜2025 Q2｜TWSE / TPEX｜不下載、不刷新、不計分')
    default = os.environ.get('V53_EPS_HISTORY_ARCHIVE_DIR') or str(Path(__file__).resolve().parent / 'archive')
    path = st.text_input('既有 archive 根目錄（內含 TWSE / TPEX）', value=default,
                         key='v53_eps_history_diagnostic_path')
    st.caption('預設路徑僅為候選位置；上傳程式檔不會自動建立歷史 snapshot。')
    if not path.strip():
        st.warning('請指定既有 archive 路徑。')
        st.stop()
        return
    try:
        report = inspect_archive(path.strip())
        st.json(report)
    except Exception as exc:
        st.error(f'診斷無法讀取：{type(exc).__name__}: {exc}')
    st.stop()
