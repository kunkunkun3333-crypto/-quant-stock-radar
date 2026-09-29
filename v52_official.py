"""官方每日快照供查詢／驗收；不混入Yahoo還原歷史或回測。"""
from datetime import datetime
import requests
import pandas as pd
from v52_cache import get_store
from v52_engine import number
ENDPOINTS={
 'twse_quotes':'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
 'twse_pe':'https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL',
 'tpex_quotes':'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
 'tpex_pe':'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis',
}
def fetch_rows(key):
    response=requests.get(ENDPOINTS[key],timeout=(5,20));response.raise_for_status()
    rows=response.json()
    if not isinstance(rows,list) or not rows:raise ValueError('官方來源未回傳有效列表')
    return rows

def roc_date(value):
    s=str(value or '').replace('/','').replace('-','')
    try:
        if len(s)==7:s=str(int(s[:3])+1911)+s[3:]
        return datetime.strptime(s,'%Y%m%d').date().isoformat()
    except ValueError:return None

def numeric(value):
    return number(str(value).replace(',',''))

def normalize(rows,key):
    out=[];is_tw=key.startswith('twse');suffix='.TW' if is_tw else '.TWO'
    for r in rows:
        code=str(r.get('Code') if is_tw else r.get('SecuritiesCompanyCode') or '').strip()
        if not code:continue
        row={'Ticker':code+suffix}
        if key.endswith('quotes'):
            row.update({'官方收盤價':numeric(r.get('ClosingPrice') if is_tw else r.get('Close')),'行情日期':roc_date(r.get('Date')),'行情來源':key})
        else:
            pe=numeric(r.get('PEratio') if is_tw else r.get('PriceEarningRatio'))
            row.update({'官方本益比':pe if pe is not None and pe>0 else None,'本益比日期':roc_date(r.get('Date')),'本益比來源':key})
        out.append(row)
    return pd.DataFrame(out)

def snapshots():
    frames=[];audit=[]
    for key in ENDPOINTS:
        try:
            cached=get_store().get(key,'official:'+key,lambda key=key:fetch_rows(key),21600,604800)
            frame=normalize(cached.value,key)
            if frame.empty:raise ValueError('無法辨識欄位')
            frames.append(frame)
            audit.append({'來源':key,'資料筆數':len(frame),'快取':cached.status,'擷取時間UTC':datetime.fromtimestamp(cached.fetched_at,__import__('datetime').timezone.utc).isoformat(),'原因':cached.warning})
        except Exception as exc:audit.append({'來源':key,'資料筆數':0,'快取':'N/A','原因':str(exc)})
    if not frames:return pd.DataFrame(),pd.DataFrame(audit)
    quote=pd.concat([f for f in frames if '官方收盤價' in f],ignore_index=True) if any('官方收盤價' in f for f in frames) else pd.DataFrame(columns=['Ticker','官方收盤價','行情日期'])
    pe=pd.concat([f for f in frames if '官方本益比' in f],ignore_index=True) if any('官方本益比' in f for f in frames) else pd.DataFrame(columns=['Ticker','官方本益比','本益比日期'])
    return quote.merge(pe,on='Ticker',how='outer'),pd.DataFrame(audit)
