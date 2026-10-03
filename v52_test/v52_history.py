"""增量行情與可搬移的歷史庫；清洗委派既有函式，不補價、不合成還原因子。"""
import base64
import io
import json
import zipfile
import numpy as np
import pandas as pd
from v52_cache import Cached,DataUnavailable,EmptyMarketData,InvalidMarketData,decode

METRICS=('history_cache_hit','history_incremental','history_full','history_rebase')

def market_bucket(ticker,stamp):
    # 保守等候來源日線發布；週末沿用上一工作日，不假裝完整休市日曆。
    tw=ticker.endswith(('.TW','.TWO')) or ticker=='^TWII'
    now=pd.Timestamp(stamp,unit='s',tz='UTC').tz_convert('Asia/Taipei' if tw else 'America/New_York')
    date=now.normalize()
    if now.hour<(16 if tw else 18):date-=pd.Timedelta(days=1)
    while date.dayofweek>=5:date-=pd.Timedelta(days=1)
    return str(date.date())

def metric(store,name):store.metrics[name]=store.metrics.get(name,0)+1

def history(store,ticker,period,full_loader,delta_loader,prepare,clean,max_stale):
    key=f'history:v4:{ticker}:{period}'
    # 與Store共用RLock；檢查、下載、保存不可被另一session插隊。
    with store.lock:
        old=store.read(key);bucket=market_bucket(ticker,store.clock())
        if old and not store._state('expired:'+key).get('expired'):
            checked=old.value.attrs.get('history_bucket') or market_bucket(ticker,old.fetched_at)
            if checked==bucket:
                metric(store,'history_cache_hit')
                store.metrics['fresh_hits']+=1
                return Cached(old.value,'fresh',old.fetched_at)
        downloaded_delta={}
        def load():
            if old is None or old.value.empty:
                result=full_loader(ticker,period);metric(store,'history_full');mode='full'
            else:
                baseline=old.value
                start=(baseline.index[-1]-pd.Timedelta(days=14)).strftime('%Y-%m-%d')
                end=(pd.Timestamp(bucket)+pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                # 同一已成功區間不再抓；失敗仍允許Store有限retry與冷卻後重試。
                interval=(start,end)
                if interval not in downloaded_delta:
                    downloaded_delta[interval]=delta_loader(ticker,start,end)
                raw=downloaded_delta[interval]
                if raw is None or raw.empty:raise EmptyMarketData(f'{ticker}：增量空資料；待續掃')
                raw=raw.copy();idx=pd.DatetimeIndex(pd.to_datetime(raw.index))
                if ticker.endswith(('.TW','.TWO')) or ticker=='^TWII':
                    idx=idx.tz_localize('Asia/Taipei') if idx.tz is None else idx.tz_convert('Asia/Taipei')
                raw.index=idx.tz_localize(None).normalize()
                if raw.index.has_duplicates:raise InvalidMarketData(f'{ticker}：增量日期重複')
                # 用同一個latest-bar函式判斷尾列，再與舊歷史合併後驗證recent-60。
                # 不先裁去無效列；最多只允許既有prepare移除一筆最新bar。
                common=baseline.index.intersection(raw.index)
                if common.empty:raise InvalidMarketData(f'{ticker}：增量缺少重疊區間，拒絕直接拼接')
                left=baseline.loc[common,'Close'].to_numpy(dtype=float)
                right=pd.to_numeric(raw.loc[common,'Close'],errors='coerce').to_numpy(dtype=float)
                good=np.isfinite(left)&np.isfinite(right)&(left>0)&(right>0)
                rebase=bool(np.any(~np.isclose(left[good],right[good],rtol=1e-6,atol=1e-8)))
                # 先驗證合併後缺值，不能藉full refresh略過第二新日異常。
                attrs=dict(baseline.attrs)
                merged=pd.concat([baseline.loc[~baseline.index.isin(raw.index)],raw]).sort_index()
                merged.attrs=attrs
                try:
                    result=clean(prepare(merged,ticker))
                except ValueError as exc:raise InvalidMarketData(f'{ticker}：{exc}') from exc
                if rebase:
                    result=full_loader(ticker,period);metric(store,'history_full');metric(store,'history_rebase');mode='full-rebase'
                else:
                    metric(store,'history_incremental');mode='incremental'
                # latest-bar + 孤立歷史缺損提示均保留。
                warnings=[result.attrs.get('latest_bar_warning',''),result.attrs.get('data_quality_warning','')]
                result.attrs['data_quality_warning']='；'.join(dict.fromkeys(x for x in warnings if x))
            result.attrs.update(history_bucket=bucket,history_update_mode=mode)
            return result
        # TTL=0只代表檢查增量，不刪除歷史。過舊成功庫仍作為增量基底。
        return store.get('yahoo',key,load,0,max_stale)

def export_archive(store):
    # 僅行情與基本面，不包含checkpoint、登入資訊或供應商冷卻。
    with store.connect() as conn:
        rows=conn.execute("SELECT key,fetched,payload FROM cache WHERE key LIKE 'history:v4:%' OR key LIKE 'fundamental:v2:%'").fetchall()
    records=[{'key':k,'fetched':ts,'payload':base64.b64encode(blob).decode('ascii')} for k,ts,blob in rows]
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('cache.json',json.dumps({'format':'v52-history-1','entries':records}))
    return out.getvalue()

def import_archive(store,blob):
    # 僅解碼資料，不執行pickle、SQL或將ZIP路径寫入磁碟；較新快照優先。
    if len(blob)>128*1024**2:raise ValueError('備份超過128MB')
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        if z.namelist()!=['cache.json'] or z.getinfo('cache.json').file_size>256*1024**2:raise ValueError('不是支援的行情備份')
        document=json.loads(z.read('cache.json'))
    if document.get('format')!='v52-history-1':raise ValueError('備份格式不符')
    entries=document['entries']
    if len(entries)>10000:raise ValueError('備份筆數過多')
    validated=[]
    from v52_engine import clean_history
    for item in entries:
        key=item['key'];stamp=float(item['fetched'])
        if not key.startswith(('history:v4:','fundamental:v2:')) or not 0<stamp<=store.clock()+60:raise ValueError('備份key或時間不符')
        payload=base64.b64decode(item['payload'],validate=True)
        if len(payload)>2*1024**2:raise ValueError('單檔行情過大')
        # decode使用zlib+JSON；先限制解壓大小，避免異常備份耗盡記憶體。
        import zlib
        inflater=zlib.decompressobj();inflater.decompress(payload,8*1024**2+1)
        if inflater.unconsumed_tail or not inflater.eof:raise ValueError('單筆解壓資料過大或不完整')
        value=decode(payload)
        if key.startswith('history:'):
            if not isinstance(value,pd.DataFrame) or not {'Open','High','Low','Close','Volume'}.issubset(value.columns):raise ValueError('行情欄位不完整')
            clean_history(value)
        elif not isinstance(value,dict):raise ValueError('基本面格式不符')
        validated.append((key,stamp,payload))
    with store.lock,store.connect() as conn:
        conn.executemany('INSERT INTO cache(key,fetched,payload) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET fetched=excluded.fetched,payload=excluded.payload WHERE excluded.fetched>cache.fetched',validated)
    return len(validated)
