"""背景自動接續控制；每批完整保存checkpoint才接受暫停／取消。"""
import threading,time,uuid
from copy import deepcopy
from v52_cache import get_store
from v52_bulk import scan_resumable,BATCH_COOLDOWN
from v52_config import DEFAULT

_TASKS={}
_LOCK=threading.RLock()
TERMINAL={'completed','cancelled','failed'}
METRICS=['history_cache_hit','history_incremental','history_full','errors','retries']

class Task:
    def __init__(self,token,state,store):
        self.token=token;self.state=state;self.store=store
        self.lock=threading.RLock();self.wake=threading.Event();self.thread=None;self.result=None
    def save(self):self.store.put('auto-task:'+self.token,self.state)
    def view(self):
        with self.lock:return deepcopy(self.state),self.result

def create(context):
    store=get_store();token=uuid.uuid4().hex
    state={'context':{k:v for k,v in context.items() if k!='bench'},'markets':list(context['bench']),
           'status':'running','requested':None,'checkpoint':None,'created_at':store.clock(),
           'processed':0,'success':0,'failed':0,'progress':0,'total':len(context['tickers']),
           'batch':0,'message':'準備自動掃描','next_retry_at':0,'stalls':0,'active_seconds':0,
           'metrics':{k:0 for k in METRICS},'batches':[]}
    values={'auto-task:'+token:state}
    for market,h in context['bench'].items():values['auto-benchmark:'+token+':'+market]=h
    store.put_many(values)
    return token

def restore(token):
    if not isinstance(token,str) or len(token)!=32 or any(c not in '0123456789abcdef' for c in token):return None
    with _LOCK:
        if token not in _TASKS:
            store=get_store();saved=store.read('auto-task:'+token)
            if saved is None:return None
            task=Task(token,saved.value,store)
            if task.state.get('requested') in ('paused','cancelled'):
                task.state['status']=task.state['requested'];task.state['requested']=None;task.save()
            _TASKS[token]=task
        return _TASKS[token]

def context_for(task):
    context=dict(task.state['context']);context['bench']={}
    for market in task.state['markets']:
        saved=task.store.read('auto-benchmark:'+task.token+':'+market)
        if saved is None:raise ValueError('原任務大盤快照已遺失，請建立新任務')
        context['bench'][market]=saved.value
    return context

def command(token,action):
    task=restore(token)
    if task is None:return
    with task.lock:
        if action=='resume' and task.state['status'] not in ('completed','cancelled'):
            task.state.update(status='running',requested=None,stalls=0)
        elif action in ('paused','cancelled') and task.state['status'] not in TERMINAL:
            task.state['requested']=action
            if task.thread is None or not task.thread.is_alive():
                task.state.update(status=action,requested=None)
            task.state['message']='要求已收到；本批保存後'+('暫停' if action=='paused' else '取消')
        task.save();task.wake.set()

def ensure(token):
    task=restore(token)
    if task is None:return None
    with task.lock:
        if task.thread is None or not task.thread.is_alive():
            # 已完成任務在程序重啟後只讀既有checkpoint還原結果。
            if task.state['status'] in ('running','cooldown') or (task.state['status']=='completed' and task.result is None):
                task.thread=threading.Thread(target=_worker,args=(task,),daemon=True,name='v52-auto-'+token[:8])
                task.thread.start()
    return task

def _worker(task):
    try:
        context=context_for(task);store=task.store
        while True:
            with task.lock:
                if task.state.get('requested'):
                    task.state.update(status=task.state['requested'],requested=None);task.save();return
                if task.state['status'] in ('paused','cancelled','failed'):return
                wait_until=max(task.state.get('next_retry_at',0),store._state('yahoo').get('until',0))
                if wait_until>store.clock():
                    task.state.update(status='cooldown',next_retry_at=wait_until,message='冷卻中；時間到後自動恢復')
                    wait=min(1.,wait_until-store.clock())
                else:wait=0;task.state['status']='running'
            if wait:
                task.wake.wait(wait);task.wake.clear();continue
            before=dict(store.metrics);started=time.perf_counter()
            def progress(i,n,t):
                with task.lock:task.state.update(progress=i,message=f'分析 {t}')
            def status(message):
                with task.lock:task.state['message']=message
            result=scan_resumable(context['tickers'],context['meta'],context['bench'],progress,
                    context['min_tw'],context['min_us'],checkpoint_id=task.state['checkpoint'],
                    max_batches=1,job_namespace=task.token,status=status)
            rows,audit,_,_=result
            with task.lock:
                previous=task.state['processed'];processed=len(audit)-audit.attrs['pending']
                counts=audit['狀態'].value_counts();task.result=result
                for k in METRICS:task.state['metrics'][k]+=store.metrics.get(k,0)-before.get(k,0)
                task.state.update(checkpoint=audit.attrs['checkpoint'],processed=processed,progress=processed,
                    success=int(counts.get('成功分析',0)),failed=int(counts.get('資料失敗',0)),
                    batches=audit.attrs['batches'],batch=len(audit.attrs['batches']),
                    active_seconds=task.state['active_seconds']+time.perf_counter()-started)
                task.state['stalls']=0 if processed>previous else task.state['stalls']+1
                if not audit.attrs['pending']:
                    task.state.update(status='completed',message='全部目標處理完成',next_retry_at=0)
                elif task.state['stalls']>=8:
                    task.state.update(status='failed',message='連續8輪沒有進展，保留checkpoint；請檢查逐檔原因後再恢復')
                else:
                    until=max(store.clock()+BATCH_COOLDOWN,store._state('yahoo').get('until',0))
                    if audit.attrs['pending']:
                        failures=[store._state(f'failure:history:v4:{t}:{DEFAULT.period}').get('until',0) for t in audit.attrs['remaining_tickers']]
                        if failures and all(x>store.clock() for x in failures):until=max(until,min(failures))
                    if processed==previous:until=max(until,store.clock()+min(300,15*2**min(task.state['stalls'],4)))
                    task.state.update(status='cooldown',next_retry_at=until)
                task.save()
                if task.state['status'] in TERMINAL:return
    except Exception as exc:
        with task.lock:
            task.state.update(status='failed',message=f'{type(exc).__name__}: {exc}')
            task.save()
