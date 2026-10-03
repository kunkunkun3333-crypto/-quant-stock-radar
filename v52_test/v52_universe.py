"""TWSE/TPEx獨立成功快照；來源故障不污染行情或掃描checkpoint。"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from v51_support import SOURCES,normalize,INDUSTRIES
from v52_cache import get_store

TTL=21600
COLUMNS=['Ticker','公司名稱','上市櫃','產業代碼','產業','清單日期']

def _download(board,url,suffix,sleep):
    for attempt in range(3):
        try:
            response=requests.get(url,timeout=(5,15),headers={'User-Agent':'Mozilla/5.0'})
            response.raise_for_status()
            rows=response.json()
            if not isinstance(rows,list) or not rows:raise ValueError('来源未提供有效清單')
            parsed=normalize(rows,board,suffix)
            if not parsed:raise ValueError('無法辨識公司代號欄位')
            return parsed
        except Exception as exc:
            status=getattr(getattr(exc,'response',None),'status_code',None)
            retry=isinstance(exc,(requests.Timeout,requests.ConnectionError)) or (isinstance(status,int) and (status>=500 or status==429))
            if not retry or attempt==2:raise
            sleep(2**attempt)

def fetch_universe(store=None,sleep=time.sleep):
    store=store if store is not None else get_store()
    rows=[];states=[];warnings=[]
    # 使用各來源自己的快取；不將TPEx失敗傳遞成TWSE/Yahoo來源冷卻。
    for board,url,suffix in SOURCES:
        source='TWSE' if suffix=='.TW' else 'TPEx';key='universe:v1:'+source
        with store.lock:
            cached=store.read(key);now=store.clock();error='';mode='成功快取'
            value=cached.value if cached else None
            fresh=cached and 0<=now-cached.fetched_at<TTL and not store._state('expired:'+key).get('expired')
            if not fresh:
                failure=store._state('failure:'+key)
                try:
                    if failure.get('until',0)>now:raise RuntimeError(failure.get('reason','來源暫時失敗'))
                    parsed=_download(board,url,suffix,sleep)
                    stamp=datetime.fromtimestamp(store.clock(),ZoneInfo('Asia/Taipei')).isoformat(timespec='seconds')
                    value={'rows':parsed,'fetched_at_taipei':stamp}
                    store.put(key,value)
                    store._set_state('expired:'+key,{})
                    store._set_state('failure:'+key,{})
                    mode='最新取得'
                except Exception as exc:
                    error=f'{type(exc).__name__}: {exc}'
                    if failure.get('until',0)<=now:store._set_state('failure:'+key,{'until':store.clock()+60,'reason':error})
                    mode='上次成功清單' if value else '缺漏'
                    warnings.append(f'{source} 暫時失敗，使用上次成功清單' if value else f'{source} 暫時失敗，且無成功清單快取；本次股票池不含{board}公司，不是完整上市櫃股票池。')
            if value:rows.extend(value['rows'])
            dates=sorted({str(r.get('清單日期','')) for r in value['rows'] if r.get('清單日期')}) if value else []
            states.append({'來源':source,'狀態':mode,'檔數':len(value['rows']) if value else 0,
                           '來源清單日期':'、'.join(dates) or 'N/A',
                           '成功保存時間（台北）':value['fetched_at_taipei'] if value else 'N/A','錯誤':error})
    frame=pd.DataFrame(rows,columns=COLUMNS).drop_duplicates('Ticker')
    frame['產業']=[INDUSTRIES.get(str(c),f'官方產業分類 {c}') for c in frame['產業代碼']]
    frame.attrs.update(universe_sources=states,universe_warnings=warnings,universe_complete=all(s['檔數']>0 for s in states))
    if frame.empty:raise RuntimeError('TWSE與TPEx均無可用清單或成功快取：'+'；'.join(warnings))
    return frame
