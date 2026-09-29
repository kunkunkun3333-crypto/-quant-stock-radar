"""每標的持久快取；成功資料與錯誤冷卻分離，不繞過供應商限流。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO
from pathlib import Path
import json
import os
import sqlite3
import threading
import time
import zlib
import pandas as pd

_DEADLINE=ContextVar('v52_network_deadline',default=None)
@contextmanager
def network_budget(seconds):
    token=_DEADLINE.set(time.monotonic()+seconds)
    try:yield
    finally:_DEADLINE.reset(token)

class DataUnavailable(RuntimeError):pass
@dataclass
class Cached:
    value: object
    status: str
    fetched_at: float
    warning: str = ''

def encode(value):
    payload={'kind':'frame','data':value.to_json(orient='split',date_format='iso'),'attrs':value.attrs} if isinstance(value,pd.DataFrame) else {'kind':'json','data':value}
    return zlib.compress(json.dumps(payload,ensure_ascii=False,allow_nan=False).encode())
def decode(blob):
    p=json.loads(zlib.decompress(blob))
    if p['kind']=='json':return p['data']
    f=pd.read_json(StringIO(p['data']),orient='split');f.index=pd.to_datetime(f.index)
    f.attrs.update(p.get('attrs',{}));return f

def is_rate_limit(exc):
    text=(type(exc).__name__+' '+str(exc)).lower()
    return any(s in text for s in ('ratelimit','rate limit','too many requests','429'))

class Store:
    def __init__(self,path,min_interval=1.5,cooldown=1800,clock=time.time,sleep=time.sleep):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.min_interval=min_interval;self.cooldown=cooldown;self.clock=clock;self.sleep=sleep
        self.lock=threading.RLock();self.metrics={'network_calls':0,'fresh_hits':0,'stale_hits':0,'blocked':0,'errors':0}
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, fetched REAL, payload BLOB)')
            c.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15)
        c.execute('PRAGMA busy_timeout=15000')
        try:
            with c:yield c
        finally:c.close()
    def _state(self,key):
        with self.connect() as c:r=c.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else {}
    def _set_state(self,key,value):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value)))
    def read(self,key):
        with self.connect() as c:r=c.execute('SELECT fetched,payload FROM cache WHERE key=?',(key,)).fetchone()
        if not r:return None
        try:return Cached(decode(r[1]),'disk',r[0])
        except (ValueError,KeyError,TypeError,zlib.error):return None
    def put(self,key,value,fetched=None):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(key,self.clock() if fetched is None else fetched,encode(value)))
    def expire(self,prefix):
        # 保留成功快照及其真實取得時間；標記需更新，不抹除冷卻或舊資料。
        with self.connect() as c:
            keys=c.execute('SELECT key FROM cache WHERE key LIKE ?',(prefix+'%',)).fetchall()
        for (key,) in keys:self._set_state('expired:'+key,{'expired':True})
    def _fallback(self,cached,max_stale,reason):
        if cached is not None and 0<=self.clock()-cached.fetched_at<=max_stale:
            self.metrics['stale_hits']+=1
            return Cached(cached.value,'stale',cached.fetched_at,reason)
        self.metrics['blocked']+=1
        raise DataUnavailable(reason)
    def get(self,provider,key,loader,ttl,max_stale=604800):
        # 同程序所有Streamlit使用者共用一把鎖，避免重複冷啟動請求。
        with self.lock:
            cached=self.read(key);now=self.clock()
            if cached and 0<=now-cached.fetched_at<ttl and not self._state('expired:'+key).get('expired'):
                self.metrics['fresh_hits']+=1
                return Cached(cached.value,'fresh',cached.fetched_at)
            state=self._state(provider);failure=self._state('failure:'+key)
            until=max(state.get('until',0),failure.get('until',0))
            if until>now:
                return self._fallback(cached,max_stale,f"來源冷卻中，約{int(until-now)}秒後可重試：{state.get('reason') or failure.get('reason') or '暫時失敗'}")
            deadline=_DEADLINE.get()
            if deadline is not None and time.monotonic()>=deadline:
                return self._fallback(cached,max_stale,'本輪網路時間預算已用完；再次掃描會沿用快取並續抓缺項')
            wait=max(0,self.min_interval-(now-state.get('last_request',0)))
            if deadline is not None and time.monotonic()+wait>=deadline:
                return self._fallback(cached,max_stale,'本輪網路時間預算已用完；請稍後續掃')
            if wait:self.sleep(wait)
            state['last_request']=self.clock();self._set_state(provider,state)
            self.metrics['network_calls']+=1
            try:
                value=loader()
                if value is None or (isinstance(value,(dict,list,pd.DataFrame)) and len(value)==0):raise ValueError('來源未提供有效資料')
                self.put(key,value)
            except Exception as exc:
                self.metrics['errors']+=1
                reason=type(exc).__name__+': '+str(exc)
                count=state.get('failures',0)+1
                cooldown=self.cooldown if is_rate_limit(exc) else 300 if count>=3 else 0
                self._set_state(provider,{**state,'failures':count,'until':self.clock()+cooldown,'reason':reason})
                self._set_state('failure:'+key,{'until':self.clock()+300,'reason':reason})
                return self._fallback(cached,max_stale,reason)
            self._set_state(provider,{**state,'failures':0,'until':0,'reason':''})
            self._set_state('failure:'+key,{})
            self._set_state('expired:'+key,{})
            return Cached(value,'downloaded',self.clock())
    def status(self):
        with self.connect() as c:
            count=c.execute('SELECT COUNT(*) FROM cache').fetchone()[0]
            states={k:json.loads(v) for k,v in c.execute("SELECT key,value FROM state WHERE key NOT LIKE 'failure:%' AND key NOT LIKE 'expired:%'")}
        return {'cache_entries':count,'disk_mb':round(self.path.stat().st_size/1024**2,2),'metrics':dict(self.metrics),'providers':states}

@lru_cache(maxsize=1)
def get_store():
    root=Path(os.environ.get('V52_CACHE_DIR',str(Path(__file__).parent/'.cache'/'v52')))
    return Store(root/'market.sqlite3')
