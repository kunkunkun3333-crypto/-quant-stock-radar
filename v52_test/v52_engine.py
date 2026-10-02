"""V5.2 純計算函式：不下載資料、不改動 V5.1 全域設定。"""
import math
import numpy as np
import pandas as pd
from v52_config import DEFAULT

def number(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError): return None

def scale(x,lo,hi):
    v=number(x)
    return None if v is None else float(np.clip((v-lo)/(hi-lo)*100,0,100))

def blend(values,weights):
    valid={k:number(v) for k,v in values.items() if number(v) is not None and weights.get(k,0)>0}
    denominator=sum(weights[k] for k in valid)
    score=sum(valid[k]*weights[k] for k in valid)/denominator if denominator else None
    detail=[{'項目':k,'原始權重':w,'有效權重%':100*w/denominator if k in valid else 0,'分數':valid.get(k),'貢獻分':valid[k]*w/denominator if k in valid else None} for k,w in weights.items()]
    return score,100*denominator/sum(weights.values()),detail

def clean_history(hist):
    if hist is None or hist.empty or 'Close' not in hist: raise ValueError('缺少行情或收盤價')
    h=hist.copy(); h.index=pd.to_datetime(h.index)
    if h.index.tz is not None: h.index=h.index.tz_localize(None)
    h=h[~h.index.duplicated(keep='last')].sort_index()
    for k in ['Open','High','Low','Close','Volume']:
        h[k]=pd.to_numeric(h[k],errors='coerce') if k in h else np.nan
    h=h.replace([np.inf,-np.inf],np.nan)
    # 缺損日不刪掉，避免把5交易日偷偷變成5個非缺值資料點。
    invalid=h.Close.isna()|(h.Close<=0)
    if invalid.any():
        dates=', '.join(str(d.date()) for d in h.index[invalid][:5])
        raise ValueError(f'收盤序列含缺值或非正數，共{int(invalid.sum())}/{len(h)}筆；日期示例：{dates}；不能可靠計算交易日期，未補值或刪除缺損日')
    h.loc[h.Volume<0,'Volume']=np.nan
    for col in ['Open','High','Low']: h.loc[h[col]<=0,col]=np.nan
    bad=(h.High<h.Low)|(h.High<h.Close)|(h.Low>h.Close)
    h.loc[bad,['High','Low']]=np.nan
    return h

def wilder_rsi(close,period=14):
    delta=close.diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    # 用首14日簡單均值初始化 Wilder 遞迴平均。
    def smooth(s):
        seed=s.copy(); seed.iloc[:period]=np.nan
        if len(s)>period: seed.iloc[period]=s.iloc[1:period+1].mean()
        return seed.ewm(alpha=1/period,adjust=False,min_periods=1).mean()
    g,l=smooth(gain),smooth(loss)
    result=100-100/(1+g/l.replace(0,np.nan))
    result=result.mask((l==0)&(g>0),100).mask((l>0)&(g==0),0).mask((l==0)&(g==0),50)
    return result

def features(hist):
    h=clean_history(hist); c=h.Close; f=pd.DataFrame(index=h.index)
    f['Latest Price']=c;f['Open']=h.Open
    for n in (20,60,200): f[f'MA{n}']=c.rolling(n,min_periods=n).mean()
    f['MA20 Slope']=f.MA20.pct_change(5,fill_method=None)*100
    f['MA60 Slope']=f.MA60.pct_change(5,fill_method=None)*100
    f['RSI14']=wilder_rsi(c);f['BIAS20']=(c/f.MA20-1)*100
    f['Avg Volume20']=h.Volume.rolling(20).mean()
    f['Volume Ratio']=h.Volume/f['Avg Volume20'].replace(0,np.nan)
    for n in (5,20,60): f[f'Return{n}']=c.pct_change(n,fill_method=None)*100
    f['Volatility']=c.pct_change(fill_method=None).rolling(60).std()*np.sqrt(252)*100
    prev=c.shift(1)
    tr=pd.concat([h.High-h.Low,(h.High-prev).abs(),(h.Low-prev).abs()],axis=1).max(axis=1).where(h.High.notna()&h.Low.notna())
    f['ATR14']=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean().where(h.High.notna()&h.Low.notna())
    f['ATR%']=f.ATR14/c*100
    # 過去252個交易日內的最大峰谷回撤（不是全歷史高點）。
    f['Max Drawdown']=c.rolling(252,min_periods=60).apply(lambda a:float(np.min(a/np.maximum.accumulate(a)-1)),raw=True)*100
    f['MACD']=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    f['MACD Signal']=f.MACD.ewm(span=9,adjust=False).mean();f['MACD Hist']=f.MACD-f['MACD Signal']
    f['BB Mid']=f.MA20; sd=c.rolling(20).std()
    f['BB Upper']=f.MA20+2*sd;f['BB Lower']=f.MA20-2*sd
    f['Golden Cross']=(f.MA20.shift(1)<=f.MA60.shift(1))&(f.MA20>f.MA60)
    return f.replace([np.inf,-np.inf],np.nan)

def trend(r):
    keys=['Latest Price','MA20','MA60','MA20 Slope','MA60 Slope']
    if any(number(r.get(k)) is None for k in keys): return 'N/A',None
    p,a,b,sa,sb=[r[k] for k in keys]
    checks=[p>a,p>b,a>b,sa>0,sb>0]
    points=100*sum(checks)/5
    if all(checks): return 'Bullish',points
    if p<a and p<b and a<b and sa<0 and sb<0: return 'Bearish',points
    return 'Neutral',points

def regime(r):
    t,_=trend(r)
    return {'Bullish':'Bull','Bearish':'Bear','Neutral':'Neutral'}.get(t,'N/A')

def quality(r,cfg=DEFAULT):
    values={k:scale(r.get(k),*bounds) for k,bounds in cfg.quality_ranges.items()}
    for k in ['EPS','Free Cash Flow']:
        v=number(r.get(k));values[k]=None if v is None else 100. if v>0 else 0.
    pe=number(r.get('P/E'));ref=number(r.get('Peer PE Median'))
    # 不用任意絕對本益比標籤；需要同產業正本益比中位數。
    values['P/E']=None if pe is None or pe<=0 or ref is None or ref<=0 else 100-scale(pe/ref,.5,2.)
    return blend(values,cfg.quality_weights)

def entry(r,market,cfg=DEFAULT):
    t,ts=trend(r);rsi=number(r.get('RSI14'));bias=number(r.get('BIAS20'));vr=number(r.get('Volume Ratio'))
    rs=None if rsi is None else float(np.interp(rsi,[0,20,30,40,55,65,75,100],[0,10,35,85,100,75,25,0]))
    bs=None if bias is None else float(np.interp(bias,[-30,-10,-5,-2,2,5,10,30],[0,5,30,90,100,65,15,0]))
    vs=None if vr is None else float(np.interp(vr,[0,.5,.8,1.2,1.8,3,6],[0,25,60,100,90,30,0]))
    ma=number(r.get('MA60'));price=number(r.get('Latest Price'))
    support=None if ma is None or price is None or ma<=0 else (100 if price>=ma and bias is not None and -3<=bias<=3 else 40 if price>=ma else 0)
    values={'RSI':rs,'BIAS':bs,'Trend':ts,'Support':support,'Volume':vs,'Momentum':scale(r.get('Return20'),-10,10)}
    base,cov,detail=blend(values,cfg.entry_weights)
    if base is None:return None,cov,detail
    penalty={'Bull':0,'Neutral':cfg.neutral_entry_penalty,'Bear':cfg.bear_entry_penalty}.get(market,cfg.unknown_entry_penalty)
    score=max(0,base-penalty)
    if t=='Bearish': score=min(score,cfg.bearish_entry_cap)
    if rsi is not None and rsi<30: score=min(score,cfg.oversold_entry_cap)
    detail.append({'項目':'大盤扣分／下降與超賣上限','原始權重':0,'有效權重%':0,'分數':None,'貢獻分':score-base})
    return score,cov,detail

DATA_FIELDS=['RSI14','BIAS20','Volume Ratio','MA20','MA60','ATR%','Volatility','Max Drawdown','EPS','P/E','Revenue Growth','Monthly Revenue YoY','EPS Growth','ROE','Gross Margin','Operating Margin','Free Cash Flow','Institutional Score','Industry Strength']
def completeness(r):
    available=sum(number(r.get(k)) is not None for k in DATA_FIELDS)
    available+=int(r.get('Industry') not in (None,'','其他分類','示範','N/A'))
    return available/(len(DATA_FIELDS)+1)*100

def risk(r,cfg=DEFAULT):
    t,_=trend(r);growth=number(r.get('EPS Growth'));pe=number(r.get('P/E'));ref=number(r.get('Peer PE Median'))
    dd=number(r.get('Max Drawdown'))
    vals={'Volatility':scale(r.get('Volatility'),10,65),'ATR':scale(r.get('ATR%'),1,7),'Drawdown':scale(-dd if dd is not None else None,5,50),'Surge':scale(r.get('Return20'),5,40),'Volume':scale(r.get('Volume Ratio'),1.5,5),'Trend':{'Bullish':0,'Neutral':40,'Bearish':100}.get(t),'Fundamentals':scale(-growth if growth is not None else None,0,.5),'Valuation':scale(pe/ref if pe is not None and ref is not None and pe>0 and ref>0 else None,1,3),'Missing':100-completeness(r)}
    return blend(vals,cfg.risk_weights)

def phase_a(r,market='N/A',cfg=DEFAULT):
    r=dict(r);r['Trend'],r['Trend Score']=trend(r);r['Market Regime']=market
    r['Quality Score'],r['Quality Coverage'],r['Quality Details']=quality(r,cfg)
    r['Entry Score'],r['Entry Coverage'],r['Entry Details']=entry(r,market,cfg)
    r['Data Completeness']=completeness(r)
    r['Risk Score'],r['Risk Coverage'],r['Risk Details']=risk(r,cfg)
    return r

def institutional_flow(ticker):
    """預留可靠、具日期之資料介面。尚未接穩定來源，所有欄位明示缺值。"""
    return {'Institutional Score':None,'Foreign Net':None,'Trust Net':None,'Dealer Net':None,'Trust Buy Streak':None,'Sell Streak':None,'Flow Date':None,'Flow Status':'N/A：尚未接入可驗證法人資料'}

def enrich_industries(rows,expected,cfg=DEFAULT):
    """expected 為本次股票池各市場/產業預期數量，不冒充整體產業母體。"""
    df=pd.DataFrame(rows).copy()
    if df.empty:return []
    for k in ['Peer PE Median','Industry Strength','Industry Return20','Industry RS20','Industry Strong Ratio','Industry Sample','Industry Coverage']:
        df[k]=np.nan
    df['Industry Status']='N/A：分類、樣本或覆蓋率不足'
    for (market,industry),g in df.groupby(['Market','Industry'],dropna=False):
        if industry in ('其他分類','示範','N/A','') or pd.isna(industry):continue
        pe=pd.to_numeric(g['P/E'],errors='coerce');positive=pe[pe>0]
        if len(positive)>=cfg.min_peers:df.loc[g.index,'Peer PE Median']=positive.median()
        valid=g[g['Return20'].apply(number).notna() & g['Return60'].apply(number).notna() & g['Benchmark Return20'].apply(number).notna() & g['Trend'].ne('N/A') & ~g['Stale']]
        n=len(valid);coverage=n/max(expected.get((market,industry),len(g)),1)
        df.loc[g.index,'Industry Sample']=n;df.loc[g.index,'Industry Coverage']=coverage*100
        if n<cfg.min_peers or coverage<cfg.min_industry_coverage:continue
        r20=valid.Return20.mean();r60=valid.Return60.mean()
        rs=(valid.Return20-valid['Benchmark Return20']).mean();ratio=valid.Trend.eq('Bullish').mean()
        score,_,_=blend({'Return20':scale(r20,-10,15),'Return60':scale(r60,-20,30),'Relative20':scale(rs,-10,10),'StrongRatio':ratio*100},cfg.industry_weights)
        df.loc[g.index,'Industry Strength']=score;df.loc[g.index,'Industry Return20']=r20;df.loc[g.index,'Industry RS20']=rs;df.loc[g.index,'Industry Strong Ratio']=ratio
        df.loc[g.index,'Industry Status']='強勢' if score>=60 else '弱勢' if score<40 else '中性'
    return df.to_dict('records')

def total_score(r,cfg=DEFAULT):
    vals={k:r.get(k) for k in cfg.total_weights}
    v=number(vals['Risk Score']);vals['Risk Score']=100-v if v is not None else None
    return blend(vals,cfg.total_weights)

def confidence(r,bt,cfg=DEFAULT):
    h=cfg.high
    def ge(k,t):return number(r.get(k)) is not None and r[k]>=t
    def le(k,t):return number(r.get(k)) is not None and r[k]<=t
    checks={f"Quality≥{h['quality']}":ge('Quality Score',h['quality']),f"Entry≥{h['entry']}":ge('Entry Score',h['entry']),'非下降趨勢':r.get('Trend') in ('Bullish','Neutral'),'大盤非Bear且有資料':r.get('Market Regime') in ('Bull','Neutral'),f"產業≥{h['industry']}":ge('Industry Strength',h['industry']),f"Risk≤{h['risk']}":le('Risk Score',h['risk']),'完整度足夠':ge('Data Completeness',cfg.min_data_coverage),'品質覆蓋率足夠':ge('Quality Coverage',h['quality_coverage']),'行情新鮮':not r.get('Stale',True),f"20D樣本≥{h['samples']}":number(bt.get('N')) is not None and bt['N']>=h['samples'],f"20D歷史勝率≥{h['winrate']:.0%}":number(bt.get('Win Rate')) is not None and bt['Win Rate']>=h['winrate'],'完整模型已驗證':cfg.full_model_validated and bt.get('Scope')=='full-model'}
    return all(checks.values()),checks

def explain(r):
    strengths=[];risks=[]
    for k,label in [('Quality Score','公司品質'),('Entry Score','進場條件'),('Industry Strength','產業強弱')]:
        v=number(r.get(k))
        if v is None:risks.append(f'{label} N/A')
        elif v>=70:strengths.append(f'{label} {v:.1f}分')
        elif v<40:risks.append(f'{label}僅 {v:.1f}分')
    if r.get('Trend')=='Bullish':strengths.append('價格在MA20／MA60之上，兩條均線向上')
    if r.get('Trend')=='Bearish':risks.append('價格與均線呈下降排列，低RSI不視為便宜')
    if r.get('Market Regime')=='Bear':risks.append('大盤Bear，進場分數已扣分')
    if r.get('Market Regime')=='N/A':risks.append('大盤資料不足，進場分數採保守扣分')
    if number(r.get('BIAS20')) is not None and r['BIAS20']>5:risks.append(f"高於MA20 {r['BIAS20']:.1f}%，有追高風險")
    if number(r.get('Risk Score')) is not None:risks.append(f"Risk {r['Risk Score']:.1f}分（越高風險越高）")
    if r.get('Data Completeness',0)<70:risks.append('資料不足，評分可信度較低')
    if r.get('Stale'):risks.append('行情日期過舊，不列高信心')
    if number(r.get('Institutional Score')) is None:risks.append('法人籌碼尚無可驗證資料')
    pe=number(r.get('P/E'));ref=number(r.get('Peer PE Median'))
    if pe is not None and pe>0 and ref is not None:
        (strengths if pe<ref else risks).append(f'本益比{pe:.1f}，'+('低於' if pe<ref else '不低於')+f'本次同產業正本益比中位數{ref:.1f}；非歷史估值比較')
    return {'Strengths':strengths,'Risks':risks,'Conclusion':'排名反映本次可用資料與固定實驗權重的綜合結果；各維度可能矛盾，歷史技術統計不代表完整模型或未來報酬。'}

def finalize(rows,expected,backtests=None,cfg=DEFAULT):
    out=[];backtests=backtests or {}
    for r in enrich_industries(rows,expected,cfg):
        r=phase_a(r,r.get('Market Regime','N/A'),cfg)
        r['Total Score'],r['Total Coverage'],r['Total Details']=total_score(r,cfg)
        bt=backtests.get(r['Ticker'],{}).get('summary',{}).get(20,{})
        r['20D Win Rate']=bt.get('Win Rate');r['20D N']=bt.get('N')
        r['High Confidence'],r['Confidence Checks']=confidence(r,bt,cfg)
        r['Signal']='高信心條件通過' if r['High Confidence'] else '研究觀察／未通過高信心'
        r['Explanation']=explain(r);out.append(r)
    if not out:return pd.DataFrame()
    result=pd.DataFrame(out).sort_values(['Total Score','Ticker'],ascending=[False,True],na_position='last').reset_index(drop=True)
    result.insert(0,'Rank',range(1,len(result)+1))
    return result
