"""所有股票共用的歷史驗證與缺值說明；不下載行情、不改評分。"""
import hashlib,pickle
import pandas as pd
from v52_backtest import backtest,MIN_SAMPLES
from v52_config import DEFAULT
from v52_engine import number,quality
from v52_cache import get_store
VERSION='historical-validation-diagnostics-v3'

def cached_backtest(ticker,hist,benchmark_hist,cfg=DEFAULT):
    # 完整資料及attrs入指紋：缺口、日期、行情修訂或設定變更均重算。
    digest=hashlib.sha256(pickle.dumps((VERSION,ticker,hist,benchmark_hist,cfg),protocol=5)).hexdigest()
    store=get_store();key='backtest:'+digest
    saved=store.read(key)
    if saved is not None and not store._state('expired:'+key).get('expired'):
        value=saved.value
        for section in ('summary','train','test','exclusions'):
            value[section]={int(k):v for k,v in value.get(section,{}).items()}
        value['events']=pd.DataFrame(value['events'],columns=['Signal Date','Exit Date','Horizon','Return','Drawdown','Executable Net Return'])
        return value
    result=backtest(hist,benchmark_hist,cfg)
    payload=dict(result);payload['events']=result['events'].astype(object).where(result['events'].notna(),None).to_dict('records')
    store.put(key,payload);store._set_state('expired:'+key,{})
    return result
cached_backtest.clear=lambda:get_store().expire('backtest:')

def validate_pool(rows,histories,benchmarks,progress=None):
    results={}
    for i,row in enumerate(rows,1):
        ticker=row['Ticker']
        try:results[ticker]=cached_backtest(ticker,histories[ticker],benchmarks.get(row['Market']))
        except Exception as exc:
            results[ticker]={'summary':{},'status':f'N/A｜回測失敗：{type(exc).__name__}: {exc}','split_date':None,'events':None}
        if progress:progress(i,len(rows),ticker)
    return results

def horizon_status(bt,h):
    if not bt:return 'N/A｜尚未回測'
    r=bt.get('summary',{}).get(h,bt.get('summary',{}).get(str(h)))
    if not r:return bt.get('status','N/A｜回測失敗')
    n=r['N']
    return f'N/A｜樣本不足 N={n}' if n<MIN_SAMPLES else f'歷史統計 N={n}'

def quality_reason(row,audit_reason=''):
    score,coverage,details=quality(row)
    missing=[d['項目'] for d in details if d.get('分數') is None]
    if number(row.get('Quality Score')) is None:
        reason='N/A｜無可用基本面品質指標' if score is None else 'N/A｜計算結果缺失（請重新掃描）'
    else:reason=f'有效權重覆蓋率 {coverage:.0f}%'
    if missing:reason+='；缺項：'+', '.join(missing)
    if number(row.get('P/E')) is not None and number(row.get('Peer PE Median')) is None:reason+='；P/E缺同產業有效比較基準'
    if '基本面' in str(audit_reason):reason+='；'+str(audit_reason)
    return reason

def display_results(df,backtests,audit=None):
    out=df.copy();reasons={}
    if audit is not None and not audit.empty:reasons=dict(zip(audit.Ticker,audit['原因']))
    out['Quality 原因']=[quality_reason(r,reasons.get(r['Ticker'],'')) for r in df.to_dict('records')]
    out['Quality Score']=[f'{v:.1f}' if (v:=number(x)) is not None else 'N/A｜基本面不足' for x in df['Quality Score']]
    for h in DEFAULT.horizons:
        statuses=[];ns=[];values={k:[] for k in ['Win Rate','Average Return','Median Return','Maximum Drawdown']}
        for t in df.Ticker:
            bt=backtests.get(t);status=horizon_status(bt,h);statuses.append(status)
            summaries=(bt or {}).get('summary',{});r=summaries.get(h,summaries.get(str(h),{}))
            ns.append(str(r['N']) if r and r['N']>=MIN_SAMPLES else status)
            for k in values:
                v=number(r.get(k));values[k].append(f'{v:.2%}' if v is not None and r.get('N',0)>=MIN_SAMPLES else status)
        out[f'{h}D N']=ns;out[f'{h}D 狀態']=statuses
        for k,v in values.items():out[f'{h}D {k}']=v
    return out
