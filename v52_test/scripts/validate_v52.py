"""可重現驗收；synthetic與live結果隔離，合成快取只放臨時目錄。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse,json,time,tempfile,resource
from datetime import datetime,timezone
from dataclasses import replace
from unittest.mock import patch
import pandas as pd
import numpy as np
from v52_cache import Store,get_store
import v52_data as data
from v52_engine import finalize
from v52_config import DEFAULT
from v51_support import INDUSTRIES

TEN=['2330.TW','2317.TW','2454.TW','2408.TW','2344.TW','2337.TW','6488.TWO','8299.TWO','2881.TW','2412.TW']
def fixture(i,n=2520):
    rng=np.random.default_rng(i+1000);c=100*np.exp(np.cumsum(rng.normal(.0001,.006+(i%10)*.002,n)))
    o=np.r_[c[0],c[:-1]]
    return pd.DataFrame({'Open':o,'High':np.maximum(o,c)*1.01,'Low':np.minimum(o,c)*.99,'Close':c,'Volume':rng.integers(500000,2000000,n)},index=pd.bdate_range(end=pd.Timestamp.now().normalize(),periods=n))

def measure(tickers,meta,bench,cfg):
    started=time.perf_counter();rows,audit,hist,expected=data.scan(tickers,meta,bench,cfg=cfg);df=finalize(rows,expected)
    return {'elapsed_seconds':round(time.perf_counter()-started,3),'attempted':len(audit),'analyzed':len(df),'failed':int(audit['狀態'].eq('資料失敗').sum()),'peak_rss_mb':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2),'history_bytes_mb':round(sum(h.memory_usage(deep=True).sum() for h in hist.values())/1024**2,2),'score_min':float(df['Total Score'].min()) if len(df) else None,'score_max':float(df['Total Score'].max()) if len(df) else None,'all_scores_finite':bool(np.isfinite(df[['Total Score','Entry Score','Risk Score']].to_numpy(float)).all()) if len(df) else None,'failure_examples':audit[audit['狀態'].eq('資料失敗')].head(2).to_dict('records')}

def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['live','synthetic'],required=True);p.add_argument('--limit',type=int,default=10);p.add_argument('--out',required=True);args=p.parse_args()
    report={'observed_at_utc':datetime.now(timezone.utc).isoformat(),'mode':args.mode,'real_market_data':args.mode=='live','cloud_measurement':False}
    if args.mode=='synthetic':
        tickers=[f'TEST{i:04d}.TW' for i in range(args.limit)];meta={t:{'Market':'TW','Company Name':'SYNTHETIC','Industry':'SYNTHETIC_'+str(i%8)} for i,t in enumerate(tickers)}
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'fixture.sqlite3',min_interval=0)
            started=time.perf_counter()
            for i,t in enumerate(tickers):
                store.put(f'history:v2:{t}:10y',fixture(i))
                store.put(f'fundamental:v2:{t}',{'P/E':10+i%30,'EPS':2,'Revenue Growth':(i%15-5)/100,'ROE':.12})
            report['fixture_prepare_seconds']=round(time.perf_counter()-started,3)
            with patch.object(data,'get_store',return_value=store):
                report['warm_scan']=measure(tickers,meta,{'TW':fixture(42)},replace(DEFAULT,request_pause=0))
            report['cache']=store.status()
    else:
        cat=data.catalog();tech=cat[cat['產業'].isin(INDUSTRIES.values())]
        tickers=TEN if args.limit==10 else tech.Ticker.head(args.limit).tolist()
        meta={r.Ticker:{'Market':'TW','Company Name':r['公司名稱'],'Industry':r['產業']} for _,r in cat.iterrows()}
        report.update(roster_count=len(cat),tech_count=len(tech))
        before=dict(get_store().metrics)
        try:bench={'TW':data.history_batch(['^TWII'])['^TWII']}
        except Exception as exc:bench={};report['benchmark_error']=str(exc)
        report['scan']=measure(tickers,meta,bench,DEFAULT)
        report['network_calls_this_run']=get_store().metrics['network_calls']-before['network_calls']
        report['cache']=get_store().status()
        report['accepted']=report['scan']['analyzed']==len(tickers) and bool(bench)
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.out).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
