"""有界批次、磁碟checkpoint及優先大盤；沿用既有清洗與評分。"""
from dataclasses import asdict,replace
import hashlib,json,time,math
import pandas as pd
from v52_cache import get_store,network_budget
from v52_config import DEFAULT
from v52_data import scan,benchmark_state
import v52_data as data

BATCH_SIZE=25
BATCH_COOLDOWN=5
ROUND_SECONDS=120
CHECKPOINT_TTL=21600

def limited_universe(frame,limit):
    ordered=frame.sort_values('Ticker').drop_duplicates('Ticker')
    return ordered if limit=='全部' else ordered.head(int(limit))

def _plain(value):
    if isinstance(value,dict):return {str(k):_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [_plain(v) for v in value]
    if hasattr(value,'item'):return _plain(value.item())
    if isinstance(value,float) and not math.isfinite(value):return None
    return value

def benchmark_refresh(markets,cfg=DEFAULT):
    # 獨立操作、獨立行情key，優先讀自己的快取；不繞過同一Yahoo來源的429。
    bench={};states={}
    for market,ticker in [('TW','^TWII'),('US','SPY')]:
        if market not in markets:continue
        try:
            with network_budget(30):bench[market]=data.history_batch([ticker],cfg.period)[ticker]
            states[market]=benchmark_state(bench[market],cfg)
        except Exception as exc:
            bench[market]=pd.DataFrame();states[market]={'Regime':'N/A','Date':None,'Reason':str(exc)}
    return bench,states

def scan_resumable(tickers,metadata,benchmarks,progress=None,min_tw_lots=0,min_us_cap=0,
                   cfg=DEFAULT,batch_report=None,round_seconds=ROUND_SECONDS,sleep=time.sleep):
    tickers=list(dict.fromkeys(tickers));store=get_store();start=time.monotonic()
    benchmark_id={m:(str(h.index[-1]),float(h.Close.iloc[-1]),h.attrs.get('cache_status')=='stale') if h is not None and not h.empty else None for m,h in benchmarks.items()}
    identity=[tickers,metadata,benchmark_id,min_tw_lots,min_us_cap,asdict(cfg),'bulk-v1']
    job=hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()
    key='checkpoint:'+job;cached=store.read(key)
    valid=cached and not store._state('expired:'+key).get('expired') and time.time()-cached.value.get('created_at',0)<CHECKPOINT_TTL
    state=cached.value if valid else {'created_at':time.time(),'rows':{},'audit':{},'done':[],'batches':[]}
    rows=dict(state['rows']);audit=dict(state['audit']);done=set(state['done']);histories={}
    # 每批checkpoint包含成功行情，重新開頁或程序重啟可恢復；不從第1檔下載。
    for t in list(rows):
        h=store.read('checkpoint-history:'+job+':'+t)
        if h:histories[t]=h.value
        else:done.discard(t);rows.pop(t,None)
    pending=[t for t in tickers if t not in done]
    expected={}
    for t in tickers:
        m=metadata.get(t,{});k=(m.get('Market','TW' if t.endswith(('.TW','.TWO')) else 'US'),m.get('Industry','N/A'))
        expected[k]=expected.get(k,0)+1
    for offset in range(0,len(pending),BATCH_SIZE):
        if time.monotonic()-start>=round_seconds:break
        provider=store._state('yahoo')
        if provider.get('until',0)>store.clock():break
        batch=pending[offset:offset+BATCH_SIZE];before=dict(store.metrics)
        remaining=max(.001,round_seconds-(time.monotonic()-start))
        local_cfg=replace(cfg,batch_size=BATCH_SIZE,scan_network_budget_seconds=remaining)
        def update(i,n,t):
            if progress:progress(len(done)+i,len(tickers),t)
        r,a,h,_=scan(batch,metadata,benchmarks,update,min_tw_lots,min_us_cap,local_cfg)
        for item in r:
            t=item['Ticker'];rows[t]=_plain(item)
            if t in h:store.put('checkpoint-history:'+job+':'+t,h[t]);histories[t]=h[t]
        for item in a.to_dict('records'):
            t=item['Ticker'];audit[t]=_plain(item)
            if item['狀態']!='待續掃':done.add(t)
        counts=a['狀態'].value_counts()
        summary={'批次':len(state['batches'])+1,'本批檔數':len(batch),'成功':int(counts.get('成功分析',0)),
                 '失敗':int(counts.get('資料失敗',0)),'排除':int(counts.get('條件排除',0)),
                 '待續掃':int(counts.get('待續掃',0)),
                 '限流回應':store.metrics['rate_limits']-before['rate_limits'],
                 '空回應':store.metrics['empty_responses']-before['empty_responses'],
                 '重試':store.metrics['retries']-before['retries']}
        state['batches'].append(summary)
        state.update(rows=rows,audit=audit,done=sorted(done));store.put(key,state);store._set_state('expired:'+key,{})
        if batch_report:batch_report(pd.DataFrame(state['batches']))
        if summary['待續掃'] or store._state('yahoo').get('until',0)>store.clock():break
        if offset+BATCH_SIZE<len(pending):
            if time.monotonic()-start+BATCH_COOLDOWN>=round_seconds:break
            sleep(BATCH_COOLDOWN)
    for t in tickers:
        if t not in done:audit[t]={'Ticker':t,'狀態':'待續掃','原因':audit.get(t,{}).get('原因','本輪暫停；保留已完成結果，稍後按續掃')}
    result_audit=pd.DataFrame([audit[t] for t in tickers])
    result_audit.attrs['batches']=state['batches']
    result_audit.attrs['pending']=len(tickers)-len(done)
    result_audit.attrs['checkpoint']=job
    return list(rows.values()),result_audit,histories,expected
