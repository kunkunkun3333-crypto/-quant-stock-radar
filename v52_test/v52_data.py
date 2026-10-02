"""可替換的資料層；所有模型函式與V5.1全域CFG隔離。"""
import time
from contextvars import ContextVar
from contextlib import contextmanager
from curl_cffi import requests as curl_requests
from datetime import datetime,timezone
import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf
from v52_config import DEFAULT
from v52_cache import get_store, network_budget, DataUnavailable, InvalidMarketData, EmptyMarketData
from v52_engine import features,number,phase_a,regime,institutional_flow
from v52_backtest import backtest,align_benchmark
from v51_support import fetch_catalog,INDUSTRIES

_ACTIVITY=ContextVar('v52_download_activity',default=None)
@contextmanager
def download_activity(callback):
    token=_ACTIVITY.set(callback)
    try:yield
    finally:_ACTIVITY.reset(token)
def _activity(message):
    callback=_ACTIVITY.get()
    if callback:callback(message)

class BoundedSession(curl_requests.Session):
    """每一個Yahoo HTTP请求上限12秒，包括info內部請求。"""
    def request(self,method,url,*args,**kwargs):
        timeout=kwargs.get('timeout')
        if isinstance(timeout,(tuple,list)):
            timeout=sum(float(x) for x in timeout if x is not None)
        kwargs['timeout']=min(float(timeout),12.) if timeout is not None else 12.
        return super().request(method,url,*args,**kwargs)

@st.cache_data(ttl=21600,show_spinner=False)
def catalog():
    data,errors=fetch_catalog()
    if errors:raise RuntimeError('；'.join(errors))
    # 不把非電子公司都混為同一產業；仍保留官方代碼。
    data['產業']=[INDUSTRIES.get(str(c),f'官方產業分類 {c}') for c in data['產業代碼']]
    return data

def extract_history(raw,ticker):
    if raw is None or raw.empty:return pd.DataFrame()
    if isinstance(raw.columns,pd.MultiIndex):
        for level in range(raw.columns.nlevels):
            if ticker in raw.columns.get_level_values(level):
                frame=raw.xs(ticker,axis=1,level=level).copy();break
        else:return pd.DataFrame()
    else:frame=raw.copy()
    # yf批次聯集日期會有上市前的空列；只去掉首尾空資料，不掩蓋中間資料洞。
    if 'Close' not in frame:return pd.DataFrame()
    valid=frame['Close'].dropna()
    if valid.empty:return pd.DataFrame()
    frame=frame.loc[valid.index[0]:valid.index[-1]]
    frame.index=pd.to_datetime(frame.index)
    if frame.index.tz is not None:frame.index=frame.index.tz_localize(None)
    return frame

def _load_history(ticker,period):
    # 單檔明確例外；yf.download會吞掉限流，並继续請求整批。
    if hasattr(yf,'config'):
        yf.config.network.retries=0;yf.config.debug.hide_exceptions=False
    try:
        with BoundedSession(impersonate='chrome') as session:
            raw=yf.Ticker(ticker,session=session).history(period=period,interval='1d',auto_adjust=True,repair=False,keepna=True,timeout=12)
    except Exception as exc:
        if type(exc).__name__=='YFPricesMissingError':raise EmptyMarketData(str(exc)) from exc
        raise
    h=raw.copy() if raw is not None and not isinstance(raw.columns,pd.MultiIndex) else extract_history(raw,ticker)
    if h.empty:raise EmptyMarketData(f'{ticker}：未取得有效行情')
    from v52_engine import clean_history
    try:return clean_history(h)
    except ValueError as exc:raise InvalidMarketData(f'{ticker}：{exc}') from exc

class HistoryBatch(dict):
    def __init__(self):super().__init__();self.errors={}

def history_batch(tickers,period='10y'):
    result=HistoryBatch();errors=[]
    for number_,ticker in enumerate(dict.fromkeys(tickers),1):
        _activity(f'下載行情 {number_}/{len(tickers)}：{ticker}')
        try:
            cached=get_store().get('yahoo',f'history:v3:{ticker}:{period}',lambda:_load_history(ticker,period),DEFAULT.history_ttl,DEFAULT.fallback_max_age)
            h=cached.value
            h.attrs.update({'cache_status':cached.status,'fetched_at':cached.fetched_at,'warning':'；'.join(x for x in [cached.warning,h.attrs.get('data_quality_warning','')] if x),'source':'Yahoo adjusted daily'})
            result[ticker]=h
        except Exception as exc:
            result.errors[ticker]=str(exc);errors.append(f'{ticker}: {exc}')
    if not result:
        exc=DataUnavailable('；'.join(errors[:2]) or '未提供股票代號');exc.ticker_errors=result.errors;raise exc
    return result

def _load_fundamentals(ticker):
    if hasattr(yf,'config'):yf.config.network.retries=0;yf.config.debug.hide_exceptions=False
    with BoundedSession(impersonate='chrome') as session:
        info=yf.Ticker(ticker,session=session).info or {}
    if not info:raise EmptyMarketData(f'{ticker}：基本面空資料')
    if not any(k in info for k in ['trailingEps','trailingPE','returnOnEquity','revenueGrowth','marketCap']):raise InvalidMarketData(f'{ticker}：基本面未回傳有效欄位')
    mapping={'EPS':'trailingEps','P/E':'trailingPE','Revenue Growth':'revenueGrowth','Earnings Growth':'earningsGrowth','ROE':'returnOnEquity','Gross Margin':'grossMargins','Operating Margin':'operatingMargins','Free Cash Flow':'freeCashflow','Market Cap':'marketCap','Debt To Equity':'debtToEquity'}
    out={k:number(info.get(v)) for k,v in mapping.items()}
    if out['P/E'] is not None and out['P/E']<=0:out['P/E']=None
    out.update({'Company Name':info.get('shortName') or ticker,'Yahoo Industry':info.get('industry'),'Monthly Revenue YoY':None,'EPS Growth':None,'Fundamental As Of':None,'Fundamental Retrieved':datetime.now(timezone.utc).isoformat(),'Fundamental Status':'最新快照；發布時點未驗證'})
    return out

def fundamentals(ticker):
    cached=get_store().get('yahoo',f'fundamental:v2:{ticker}',lambda:_load_fundamentals(ticker),DEFAULT.fundamental_ttl,DEFAULT.fallback_max_age)
    out=dict(cached.value)
    out.update({'Fundamental Cache':cached.status,'Fundamental Warning':cached.warning})
    return out

# 相容原UI清理介面；僅標記需更新，不刪除備援快照或來源冷卻。
history_batch.clear=lambda:get_store().expire('history:')
fundamentals.clear=lambda:get_store().expire('fundamental:')

@st.cache_data(ttl=1800,show_spinner=False,max_entries=120)
def cached_backtest(ticker,hist,benchmark_hist,cfg=DEFAULT):
    return backtest(hist,benchmark_hist,cfg)

def benchmark_state(hist,cfg=DEFAULT):
    if hist is None or hist.empty:return {'Regime':'N/A','Date':None,'Reason':'基準行情缺失'}
    try:
        f=features(hist);date=f.index[-1]
        stale=hist.attrs.get('cache_status')=='stale' or (pd.Timestamp.now().normalize()-date.normalize()).days>cfg.stale_days
        return {'Regime':'N/A' if stale else regime(f.iloc[-1]),'Date':str(date.date()),'Price':float(f['Latest Price'].iloc[-1]),'MA20':number(f.MA20.iloc[-1]),'MA60':number(f.MA60.iloc[-1]),'Reason':'行情過舊' if stale else ''}
    except Exception as exc:return {'Regime':'N/A','Date':None,'Reason':str(exc)}

def scan(tickers,metadata,benchmarks,progress=None,min_tw_lots=0,min_us_cap=0,cfg=DEFAULT):
    with network_budget(cfg.scan_network_budget_seconds):
        return _scan(tickers,metadata,benchmarks,progress,min_tw_lots,min_us_cap,cfg)

def _scan(tickers,metadata,benchmarks,progress=None,min_tw_lots=0,min_us_cap=0,cfg=DEFAULT):
    rows=[];audit=[];histories={};expected={};done=0
    for t in tickers:
        m=metadata.get(t,{});key=(m.get('Market','TW' if t.endswith(('.TW','.TWO')) else 'US'),m.get('Industry','N/A'));expected[key]=expected.get(key,0)+1
    bf={};states={};failed_batches=0
    for market,h in benchmarks.items():
        states[market]=benchmark_state(h,cfg)
        try:bf[market]=features(h)
        except Exception:bf[market]=None
    for start in range(0,len(tickers),cfg.batch_size):
        batch=tickers[start:start+cfg.batch_size]
        try:downloaded=history_batch(tuple(batch),cfg.period);batch_error='未取得行情／來源冷卻或時間預算用盡'
        except Exception as exc:
            downloaded=HistoryBatch();downloaded.errors=getattr(exc,'ticker_errors',{});batch_error=str(exc)
        for t in batch:
            meta=metadata.get(t,{});market=meta.get('Market','TW' if t.endswith(('.TW','.TWO')) else 'US');notes=[]
            try:
                if t not in downloaded:raise ValueError(getattr(downloaded,'errors',{}).get(t,batch_error))
                h=downloaded[t];f=features(h)
                if h.attrs.get('warning'):notes.append('行情提示：'+h.attrs['warning'])
                if len(f)<cfg.minimum_bars:raise ValueError(f'歷史僅{len(f)}筆，至少需要{cfg.minimum_bars}筆')
                r=f.iloc[-1].to_dict();histories[t]=h
                _activity(f'讀取基本面：{t}')
                try:fund=fundamentals(t)
                except Exception as exc:fund={};notes.append('基本面N/A：'+str(exc))
                if fund.get('Fundamental Warning'):notes.append('基本面備援：'+fund['Fundamental Warning'])
                r.update(fund);r.update(meta);r.update(institutional_flow(t));r['Ticker']=t;r['Market']=market;r.setdefault('Company Name',t);r.setdefault('Industry','N/A');r.setdefault('P/E',None)
                r['History Cache']=h.attrs.get('cache_status','unknown');r['History Retrieved']=h.attrs.get('fetched_at')
                r['Price Date']=str(f.index[-1].date());r['Stale']=h.attrs.get('cache_status')=='stale' or fund.get('Fundamental Cache')=='stale' or (pd.Timestamp.now().normalize()-f.index[-1].normalize()).days>cfg.stale_days
                b=align_benchmark(f.index,bf.get(market));br=b.iloc[-1] if not b.empty else {}
                reg=regime(br)
                if states.get(market,{}).get('Regime','N/A')=='N/A':reg='N/A'
                r['Benchmark Return20']=number(br.get('Return20'));r['Benchmark Date']=str(br.get('Benchmark Date','N/A'))
                if market=='TW' and number(r.get('Avg Volume20')) is not None and r['Avg Volume20']<min_tw_lots*1000:
                    audit.append({'Ticker':t,'狀態':'條件排除','原因':'成交量門檻'});continue
                if market=='US' and number(r.get('Market Cap')) is not None and r['Market Cap']<min_us_cap:
                    audit.append({'Ticker':t,'狀態':'條件排除','原因':'市值門檻'});continue
                r=phase_a(r,reg,cfg);rows.append(r)
                audit.append({'Ticker':t,'狀態':'成功分析','原因':'；'.join(notes),'行情日期':r['Price Date']})
            except Exception as exc:
                reason=str(exc)
                pending=any(x in reason.lower() for x in ['rate','429','冷卻','預算','timeout','empty','未取得有效行情','暫停重試'])
                audit.append({'Ticker':t,'狀態':'待續掃' if pending else '資料失敗','原因':reason})
            finally:
                done+=1
                if progress:progress(done,len(tickers),t)
        time.sleep(cfg.request_pause)
    return rows,pd.DataFrame(audit),histories,expected
