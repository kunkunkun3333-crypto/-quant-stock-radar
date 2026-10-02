"""固定規則、無調參的價格/成交量/大盤技術訊號事件研究。"""
import numpy as np
import pandas as pd
from v52_config import DEFAULT
from v52_engine import features,entry,trend,regime,number

SCOPE='technical-only'

def align_benchmark(stock_index,benchmark_features):
    if benchmark_features is None or benchmark_features.empty:return pd.DataFrame(index=stock_index)
    b=benchmark_features.copy(); b['Benchmark Date']=b.index
    aligned=b.reindex(stock_index,method='ffill')
    # 只往過去找，超過7個日曆日的基準不沿用。
    ages=(pd.Series(stock_index,index=stock_index)-aligned['Benchmark Date']).dt.days
    for col in aligned.select_dtypes(include='bool').columns:
        aligned[col]=aligned[col].astype('boolean')
    aligned.loc[ages>7,:]=np.nan
    # 大盤缺損當日不得以前一日訊號取代。
    gaps=pd.to_datetime(benchmark_features.attrs.get('missing_close_dates',[]))
    aligned.loc[aligned.index.normalize().isin(gaps),:]=np.nan
    return aligned

def technical_signals(f,benchmark_features,cfg=DEFAULT):
    aligned=align_benchmark(f.index,benchmark_features)
    signals=[]
    for date,r in f.iterrows():
        market=regime(aligned.loc[date]) if not aligned.empty else 'N/A'
        e,_,_=entry(r,market,cfg);t,_=trend(r)
        signals.append(e is not None and e>=cfg.historical_entry_threshold and t=='Bullish' and market in ('Bull','Neutral') and number(r.get('Volume Ratio')) is not None)
    return pd.Series(signals,index=f.index,dtype=bool)

def summarize(events):
    if events.empty:return {'N':0,'Up':0,'Down':0,'Flat':0,'Win Rate':None,'Average Return':None,'Median Return':None,'Maximum Drawdown':None,'Best':None,'Worst':None,'Net Average Return':None,'Net Win Rate':None,'Scope':SCOPE,'Warning':'⚠️ 樣本不足'}
    r=events['Return'];net=events['Executable Net Return'].dropna()
    return {'N':len(events),'Up':int((r>0).sum()),'Down':int((r<0).sum()),'Flat':int((r==0).sum()),'Win Rate':float((r>0).mean()),'Average Return':float(r.mean()),'Median Return':float(r.median()),'Maximum Drawdown':float(events.Drawdown.min()),'Best':float(r.max()),'Worst':float(r.min()),'Net Average Return':float(net.mean()) if len(net) else None,'Net Win Rate':float((net>0).mean()) if len(net) else None,'Executable N':len(net),'Scope':SCOPE,'Warning':'⚠️ 樣本不足' if len(events)<30 else '技術訊號樣本；非完整模型驗證'}

def events_for_horizon(f,signal,horizon,start=0,end=None,cfg=DEFAULT):
    end=len(f) if end is None else end
    last_exit=-1;out=[]
    gaps=pd.to_datetime(f.attrs.get('missing_close_dates',[]))
    for i in np.flatnonzero(signal.to_numpy()):
        if i<start or i+horizon>=end or i<=last_exit:continue
        exit_i=i+horizon
        if len(gaps) and ((gaps>f.index[i])&(gaps<=f.index[exit_i])).any():continue
        path=f['Latest Price'].iloc[i:exit_i+1].to_numpy(float)
        base=path[0];last=path[-1]
        open_next=number(f.Open.iloc[i+1]);cost=cfg.roundtrip_cost_bps/10000
        net=last/open_next-1-cost if open_next is not None and open_next>0 else None
        out.append({'Signal Date':str(f.index[i].date()),'Exit Date':str(f.index[exit_i].date()),'Horizon':horizon,'Return':last/base-1,'Drawdown':float(np.min(path/np.maximum.accumulate(path)-1)),'Executable Net Return':net})
        last_exit=exit_i
    return pd.DataFrame(out,columns=['Signal Date','Exit Date','Horizon','Return','Drawdown','Executable Net Return'])

def backtest(hist,benchmark_hist,cfg=DEFAULT):
    f=features(hist);b=features(benchmark_hist) if benchmark_hist is not None and not benchmark_hist.empty else None
    signal=technical_signals(f,b,cfg)
    split=int(len(f)*(1-cfg.oos_fraction));enough=len(f)>=504
    summaries={};train={};test={};all_events=[]
    for h in cfg.horizons:
        e=events_for_horizon(f,signal,h,cfg=cfg);summaries[h]=summarize(e);all_events.append(e)
        # 訓練標籤必須在切分點前已成熟。跨切分樣本完全排除。
        train[h]=summarize(events_for_horizon(f,signal,h,end=split,cfg=cfg)) if enough else summarize(pd.DataFrame())
        test[h]=summarize(events_for_horizon(f,signal,h,start=split,cfg=cfg)) if enough else summarize(pd.DataFrame())
    return {'summary':summaries,'train':train,'test':test,'events':pd.concat(all_events,ignore_index=True),'split_date':str(f.index[split].date()) if enough else None,'scope':SCOPE,'status':'完成' if b is not None else '大盤資料缺失；無法建立訊號','full_model_validated':False,'cost_bps':cfg.roundtrip_cost_bps}
