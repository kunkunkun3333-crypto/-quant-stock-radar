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
ROUND_SECONDS=600
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
                   cfg=DEFAULT,batch_report=None,round_seconds=ROUND_SECONDS,sleep=time.sleep,status=None,checkpoint_id=None):
    tickers=list(dict.fromkeys(tickers));store=get_store();start=time.monotonic()
    trace=[]
    def announce(message):
        trace.append({'秒':round(time.monotonic()-start,3),'事件':message})
        if status:status(message)
    announce('讀取續掃紀錄')
    benchmark_id={m:(str(h.index[-1]),float(h.Close.iloc[-1]),h.attrs.get('cache_status')=='stale') if h is not None and not h.empty else None for m,h in benchmarks.items()}
    identity=[tickers,metadata,benchmark_id,min_tw_lots,min_us_cap,asdict(cfg),'bulk-v1']
    job=checkpoint_id or hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()
    key='checkpoint:'+job;cached=store.read(key)
    valid=cached and not store._state('expired:'+key).get('expired') and time.time()-cached.value.get('created_at',0)<CHECKPOINT_TTL
    if checkpoint_id and not valid:
        raise ValueError('上次checkpoint已過期或已清除；未重新掃描任何股票。請明確按開始掃描建立任務。')
    state=cached.value if valid else {'created_at':time.time(),'rows':{},'audit':{},'done':[],'batches':[]}
    context=state.get('context')
    if checkpoint_id and context:
        tickers=context['tickers'];metadata=context['metadata']
        min_tw_lots=context['min_tw_lots'];min_us_cap=context['min_us_cap']
        benchmarks={}
        for market in context['markets']:
            saved=store.read('checkpoint-benchmark:'+job+':'+market)
            if saved is None:raise ValueError('續掃大盤快照遺失；未重新開始任務，請按開始掃描。')
            benchmarks[market]=saved.value
    state['context']={'tickers':tickers,'metadata':metadata,'min_tw_lots':min_tw_lots,
                      'min_us_cap':min_us_cap,'markets':list(benchmarks)}
    def save_position():
        remaining=[t for t in tickers if t not in done]
        state.update(rows=rows,audit=audit,done=[t for t in tickers if t in done],
                     remaining_tickers=remaining,processed_count=len(done),
                     next_index=next((i for i,t in enumerate(tickers) if t not in done),len(tickers)))
    rows=dict(state['rows']);audit=dict(state['audit']);done=set(state['done']);histories={}
    # 每批checkpoint包含成功行情，重新開頁或程序重啟可恢復；不從第1檔下載。
    for t in list(rows):
        h=store.read('checkpoint-history:'+job+':'+t)
        if h:histories[t]=h.value
        elif checkpoint_id:raise ValueError(f'{t}已完成行情快照遺失；為避免重跑，已停止續掃。請按開始掃描。')
        else:done.discard(t);rows.pop(t,None)
    pending=[t for t in tickers if t not in done]
    save_position()
    initial={key:state}
    for market,h in benchmarks.items():initial['checkpoint-benchmark:'+job+':'+market]=h
    store.put_many(initial)
    store._set_state('expired:'+key,{})
    expected={}
    for t in tickers:
        m=metadata.get(t,{});k=(m.get('Market','TW' if t.endswith(('.TW','.TWO')) else 'US'),m.get('Industry','N/A'))
        expected[k]=expected.get(k,0)+1
    for offset in range(0,len(pending),BATCH_SIZE):
        if time.monotonic()-start>=round_seconds:
            announce('本輪時間預算已到，保留結果待續掃');break
        provider=store._state('yahoo')
        if provider.get('until',0)>store.clock():
            announce(f"Yahoo冷卻中，約{int(provider['until']-store.clock())}秒後可續掃");break
        batch=pending[offset:offset+BATCH_SIZE];before=dict(store.metrics)
        remaining=max(.001,round_seconds-(time.monotonic()-start))
        local_cfg=replace(cfg,batch_size=BATCH_SIZE,scan_network_budget_seconds=min(cfg.scan_network_budget_seconds,remaining))
        batch_number=len(state['batches'])+1
        announce(f'第 {batch_number} 批開始：本批 {len(batch)} 檔，累計已完成 {len(done)}/{len(tickers)}')
        def update(i,n,t):
            if progress:progress(len(done)+i,len(tickers),t)
        with data.download_activity(lambda message:announce(f'第 {batch_number} 批｜{message}')):
            r,a,h,_=scan(batch,metadata,benchmarks,update,min_tw_lots,min_us_cap,local_cfg)
        announce(f'第 {batch_number} 批分析完成，正在寫入checkpoint')
        checkpoint_values={}
        for item in r:
            t=item['Ticker'];rows[t]=_plain(item)
            if t in h:checkpoint_values['checkpoint-history:'+job+':'+t]=h[t];histories[t]=h[t]
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
        save_position()
        checkpoint_values[key]=state
        store.put_many(checkpoint_values);store._set_state('expired:'+key,{})
        announce(f'第 {batch_number} 批checkpoint已保存')
        if batch_report:batch_report(pd.DataFrame(state['batches']))
        if summary['待續掃'] or store._state('yahoo').get('until',0)>store.clock():
            announce('本批有待續掃或Yahoo冷卻，已暫停；請稍後按接續上次掃描');break
        if offset+BATCH_SIZE<len(pending):
            if time.monotonic()-start+BATCH_COOLDOWN>=round_seconds:
                announce('本輪時間預算不足，已保存；請按接續上次掃描');break
            for seconds in range(BATCH_COOLDOWN,0,-1):
                announce(f'第 {batch_number} 批完成；{seconds} 秒後開始第 {batch_number+1} 批')
                sleep(1)
    for t in tickers:
        if t not in done:audit[t]={'Ticker':t,'狀態':'待續掃','原因':audit.get(t,{}).get('原因','本輪暫停；保留已完成結果，稍後按續掃')}
    result_audit=pd.DataFrame([audit[t] for t in tickers])
    result_audit.attrs['batches']=state['batches']
    result_audit.attrs['pending']=len(tickers)-len(done)
    result_audit.attrs['checkpoint']=job
    result_audit.attrs['processed_count']=len(done)
    result_audit.attrs['next_index']=state['next_index']
    result_audit.attrs['remaining_tickers']=state['remaining_tickers']
    result_audit.attrs['cooldown_until']=store._state('yahoo').get('until',0)
    announce(f'本輪結束：已處理 {len(done)}/{len(tickers)}，待續掃 {len(tickers)-len(done)}')
    result_audit.attrs['boundary_trace']=trace
    return list(rows.values()),result_audit,histories,expected
