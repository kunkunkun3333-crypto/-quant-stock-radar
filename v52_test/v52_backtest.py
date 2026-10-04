"""固定規則、無調參的價格/成交量/大盤技術訊號事件研究。"""
import numpy as np
import pandas as pd
from v52_config import DEFAULT
from v52_engine import features,entry,trend,regime,number

SCOPE='technical-only'
MIN_SAMPLES=30

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
    signals=[];diagnostics={'歷史筆數':len(f),'大盤不可用':0,'大盤Bear':0,'非上升趨勢':0,'Entry不足':0,'量比缺失':0}
    for date,r in f.iterrows():
        market=regime(aligned.loc[date]) if not aligned.empty else 'N/A'
        e,_,_=entry(r,market,cfg);t,_=trend(r)
        diagnostics['大盤不可用']+=int(market=='N/A')
        diagnostics['大盤Bear']+=int(market=='Bear')
        diagnostics['非上升趨勢']+=int(t!='Bullish')
        diagnostics['Entry不足']+=int(e is None or e<cfg.historical_entry_threshold)
        diagnostics['量比缺失']+=int(number(r.get('Volume Ratio')) is None)
        signals.append(e is not None and e>=cfg.historical_entry_threshold and t=='Bullish' and market in ('Bull','Neutral') and number(r.get('Volume Ratio')) is not None)
    result=pd.Series(signals,index=f.index,dtype=bool)
    diagnostics['符合訊號日數']=int(result.sum());result.attrs['diagnostics']=diagnostics
    return result

def summarize(events):
    if events.empty:return {'N':0,'Up':0,'Down':0,'Flat':0,'Win Rate':None,'Average Return':None,'Median Return':None,'Maximum Drawdown':None,'Best':None,'Worst':None,'Net Average Return':None,'Net Win Rate':None,'Scope':SCOPE,'Warning':'⚠️ 樣本不足'}
    r=events['Return'];net=events['Executable Net Return'].dropna()
    result={'N':len(events),'Up':int((r>0).sum()),'Down':int((r<0).sum()),'Flat':int((r==0).sum()),'Win Rate':float((r>0).mean()),'Average Return':float(r.mean()),'Median Return':float(r.median()),'Maximum Drawdown':float(events.Drawdown.min()),'Best':float(r.max()),'Worst':float(r.min()),'Net Average Return':float(net.mean()) if len(net) else None,'Net Win Rate':float((net>0).mean()) if len(net) else None,'Executable N':len(net),'Scope':SCOPE,'Warning':'⚠️ 樣本不足' if len(events)<30 else '技術訊號樣本；非完整模型驗證'}
    if len(events)<MIN_SAMPLES:
        for k in ['Win Rate','Average Return','Median Return','Maximum Drawdown','Best','Worst','Net Average Return','Net Win Rate']:result[k]=None
    if len(net)<MIN_SAMPLES:
        result['Net Average Return']=None;result['Net Win Rate']=None
    return result


def events_for_horizon(f,signal,horizon,start=0,end=None,cfg=DEFAULT):
    end=len(f) if end is None else end
    last_exit=-1;out=[];reasons={'區段外':0,'尚未滿持有期':0,'重疊樣本':0,'跨歷史缺口':0}
    gaps=pd.to_datetime(f.attrs.get('missing_close_dates',[]))
    for i in np.flatnonzero(signal.to_numpy()):
        if i<start:reasons['區段外']+=1;continue
        if i+horizon>=end:reasons['尚未滿持有期']+=1;continue
        if i<=last_exit:reasons['重疊樣本']+=1;continue
        exit_i=i+horizon
        if len(gaps) and ((gaps>f.index[i])&(gaps<=f.index[exit_i])).any():reasons['跨歷史缺口']+=1;continue
        path=f['Latest Price'].iloc[i:exit_i+1].to_numpy(float)
        base=path[0];last=path[-1]
        open_next=number(f.Open.iloc[i+1]);cost=cfg.roundtrip_cost_bps/10000
        net=last/open_next-1-cost if open_next is not None and open_next>0 else None
        out.append({'Signal Date':str(f.index[i].date()),'Exit Date':str(f.index[exit_i].date()),'Horizon':horizon,'Return':last/base-1,'Drawdown':float(np.min(path/np.maximum.accumulate(path)-1)),'Executable Net Return':net})
        last_exit=exit_i
    result=pd.DataFrame(out,columns=['Signal Date','Exit Date','Horizon','Return','Drawdown','Executable Net Return'])
    result.attrs['exclusions']=reasons
    return result

def backtest(hist,benchmark_hist,cfg=DEFAULT):
    f=features(hist);b=features(benchmark_hist) if benchmark_hist is not None and not benchmark_hist.empty else None
    signal=technical_signals(f,b,cfg)
    split=int(len(f)*(1-cfg.oos_fraction));enough=len(f)>=504
    summaries={};train={};test={};all_events=[];excluded={}
    for h in cfg.horizons:
        e=events_for_horizon(f,signal,h,cfg=cfg);summaries[h]=summarize(e);all_events.append(e);excluded[h]=e.attrs['exclusions']
        # 訓練標籤必須在切分點前已成熟。跨切分樣本完全排除。
        train[h]=summarize(events_for_horizon(f,signal,h,end=split,cfg=cfg)) if enough else summarize(pd.DataFrame())
        test[h]=summarize(events_for_horizon(f,signal,h,start=split,cfg=cfg)) if enough else summarize(pd.DataFrame())
    return {'summary':summaries,'train':train,'test':test,'events':pd.concat(all_events,ignore_index=True),'split_date':str(f.index[split].date()) if enough else None,'scope':SCOPE,'diagnostics':signal.attrs.get('diagnostics',{}),'exclusions':excluded,'status':('大盤資料缺失；無法建立訊號' if b is None else '無符合既有條件的歷史訊號' if not signal.any() else '回測完成；各持有期N<30的統計為N/A'),'full_model_validated':False,'cost_bps':cfg.roundtrip_cost_bps}
