from __future__ import annotations
import copy
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import streamlit as st
import quant_stock_radar_v5 as qr
from v51_support import INDUSTRIES, fetch_catalog, score_details

st.set_page_config(page_title='台美股量化雷達 V5.1',page_icon='📊',layout='wide')
st.title('📊 台美股量化選股雷達 V5.1')
st.caption('台灣上市櫃科技股 × MACD × 布林通道 × 評分明細｜研究工具，不構成投資建議')
if st.session_state.get('version')!='5.1':
    for k in ['results','audit','scan_cfg','scan_time']: st.session_state.pop(k,None)
    st.session_state.version='5.1'

@st.cache_data(ttl=21600,show_spinner=False)
def catalog():
    data,errors=fetch_catalog()
    if errors: raise RuntimeError('；'.join(errors))
    return data

@st.cache_resource
def engine_lock(): return threading.RLock()

with engine_lock():
    BASE_DOWNLOAD = qr.download_history
    BASE_FUNDAMENTALS = qr.get_fundamentals

@st.cache_data(ttl=1800,show_spinner=False)
def cached_history(ticker,period,interval,ma_trend,strict):
    with engine_lock():
        old=qr.CFG
        try:
            qr.CFG=copy.deepcopy(old)
            qr.CFG.period=period; qr.CFG.interval=interval; qr.CFG.ma_trend=ma_trend; qr.CFG.strict_exchange=strict
            return BASE_DOWNLOAD(ticker)
        finally: qr.CFG=old

@st.cache_data(ttl=3600,show_spinner=False)
def cached_fundamentals(ticker): return BASE_FUNDAMENTALS(ticker)

LABELS={'Rank':'排名','Market':'市場','Ticker':'代號','Company Name':'公司名稱','Price Date':'行情日期','Latest Price':'調整後價格','Total Score':'總分','Golden Cross':'最新日黃金交叉','BIAS20':'短均線乖離率%','RSI14':'RSI14','Volume Ratio':'量比','P/E':'本益比','Risk Flag':'風險提示','MACD':'MACD','MACD Signal':'MACD訊號線','MACD Hist':'MACD柱狀值','BB Upper':'布林上軌','BB Mid':'布林中軌','BB Lower':'布林下軌','Relative Strength 3M':'3月相對強弱%'}
def show_table(df): st.dataframe(df.rename(columns=LABELS),hide_index=True,use_container_width=True)
def csv(df): return df.to_csv(index=False).encode('utf-8-sig')
def fmt(x): return 'N/A' if pd.isna(x) else f'{x:.2f}'

cfg=qr.Config()
with st.sidebar:
    st.header('掃描設定')
    markets=st.multiselect('掃描市場',['台股','美股'],default=['台股'])
    mode=st.selectbox('台股股票池',['上市櫃科技股（完整清單）','全部上市櫃公司','示範台股10檔'])
    sectors=st.multiselect('科技股產業範圍',list(INDUSTRIES.values()),default=list(INDUSTRIES.values()),disabled=mode!='上市櫃科技股（完整清單）')
    st.caption('科技股範圍：上述八類上市櫃公司；不含興櫃、ETF。其他分類公司可用補充代號加入。')
    extra=st.text_input('補充台股代號（逗號分隔）',placeholder='例如：2408, 6488')
    us_mode=st.selectbox('美股股票池',['示範12檔','自動擴充'])
    max_us=st.slider('美股最多掃描檔數',50,1200,300,50)
    st.caption('台股完整模式沒有檔數上限；全量逐檔抓資料可能需數十分鐘。')
    cfg.tw_min_avg_volume_lots=st.number_input('台股20日均量最低（張）',0,100000,0,500,help='預設0，避免低成交量科技股被排除。資料不足仍會列入失敗清單。')
    cfg.us_min_market_cap=st.number_input('美股最低市值（十億美元）',0.,1000.,10.,1.)*1e9
    with st.expander('策略參數'):
        cfg.ma_short=st.number_input('短均線天數',5,100,20)
        cfg.ma_long=st.number_input('長均線天數',20,200,60)
        cfg.ma_trend=st.number_input('趨勢均線天數',100,300,200)
        cfg.bias_min=st.number_input('乖離率下限%',-20.,0.,-3.,.5)
        cfg.bias_max=st.number_input('乖離率上限%',0.,30.,5.,.5)
        cfg.pe_max=st.number_input('本益比上限',1.,200.,35.,1.)
    top_n=st.slider('排行榜顯示檔數',10,1000,100,10)
    backtest_n=st.slider('回測前N名',0,20,0)
    refresh=st.button('更新公司清單及行情快取')
    if refresh:
        catalog.clear(); cached_history.clear(); cached_fundamentals.clear()
        st.success('快取已清除；請重新掃描以更新結果。')

with st.expander('指標怎麼看？'):
    st.markdown('**總分不是上漲機率。** MACD反映動能，柱狀值為MACD減訊號線；布林通道為20日均線加減兩倍標準差。價格靠近下軌不代表一定反彈。RSI不是估值，負乖離不代表低估。')
    st.caption('V5.1沿用V5評分：趨勢25、動能20、估值15、基本面20、成交量10、買點10。核心候選仍為黃金交叉＋乖離率＋本益比；回測不是MACD／布林策略回測。')

universe=pd.DataFrame(); blocked=False
if '台股' in markets:
    try:
        if mode=='示範台股10檔' and not extra.strip():
            universe=pd.DataFrame({'Ticker':qr.DEFAULT_TW,'公司名稱':qr.DEFAULT_TW,'產業':'示範','上市櫃':'上市'})
        else:
            with st.spinner('讀取上市、上櫃公司清單…'): full=catalog()
            universe=full.copy()
            if mode=='上市櫃科技股（完整清單）': universe=full[full['產業'].isin(sectors)].copy()
            elif mode=='示範台股10檔': universe=full[full.Ticker.isin(qr.DEFAULT_TW)].copy()
            extras=[x.strip().upper() for x in extra.replace('，',',').split(',') if x.strip()]
            for code in extras:
                selected=full[full.Ticker.eq(code) | full.Ticker.str.split('.').str[0].eq(code)]
                if selected.empty: st.error(f'補充代號 {code} 不在上市櫃公司清單中。'); blocked=True
                else: universe=pd.concat([universe,selected]).drop_duplicates('Ticker')
        st.subheader(f'台股待掃描清單：{len(universe)} 檔')
        st.caption('清單內所有代號均會嘗試分析。上市與上櫃代號分開處理，不互換交易所；新上市或不足趨勢均線所需天數者會列入資料失敗。')
        with st.expander('查看完整公司名單與產業分布'):
            show_table(universe)
            if not universe.empty: st.dataframe(universe.groupby(['上市櫃','產業']).size().rename('檔數').reset_index(),hide_index=True)
        st.download_button('下載台股待掃描清單',csv(universe),'tw_universe_v51.csv','text/csv')
    except Exception as exc:
        st.error(f'無法建立完整股票池：{exc}。請稍後按「更新公司清單及行情快取」重試。'); blocked=True

if st.button('🚀 開始掃描',type='primary',disabled=blocked):
    if not markets: st.warning('請至少選擇一個市場。'); st.stop()
    if '台股' in markets and universe.empty: st.warning('台股清單為空，請選擇產業。'); st.stop()
    progress=st.progress(0,text='準備掃描…')
    audit=[]; rows=[]
    # 共用核心的可變設定與替換函式均受鎖保護，避免多位使用者互相改動。
    with engine_lock():
        old_cfg=qr.CFG; original_history=qr.download_history; original_fund=qr.get_fundamentals
        qr.CFG=copy.deepcopy(cfg); qr.CFG.strict_exchange=True
        qr.download_history=lambda ticker:cached_history(ticker,cfg.period,cfg.interval,cfg.ma_trend,True)
        qr.get_fundamentals=cached_fundamentals
        try:
            us=qr.get_universe('US','demo' if us_mode=='示範12檔' else 'auto',max_us) if '美股' in markets else []
            tw=universe.Ticker.tolist() if '台股' in markets else []
            total=len(us)+len(tw); done=0
            benchmarks={}
            for b in (['SPY'] if us else [])+(['0050.TW'] if tw else []): benchmarks[b]=qr.benchmark_returns(b)
            for b,values in benchmarks.items():
                if all(pd.isna(x) for x in values.values()): st.warning(f'{b} 基準行情不足，相對強弱將缺值，相關分數不加分。')
            for market,tickers in [('TW',tw),('US',us)]:
                offset=done
                rows+=qr.screen_market(tickers,market,benchmarks,audit=audit,progress=lambda i,n,t:progress.progress((offset+i)/max(total,1),text=f'{offset+i}/{total}：{t}'))
                done+=len(tickers)
            result=pd.DataFrame(rows)
            if not result.empty:
                result=result.sort_values(['Total Score','Ticker'],ascending=[False,True]).reset_index(drop=True)
                result.insert(0,'Rank',range(1,len(result)+1))
                if not universe.empty:
                    names=universe.set_index('Ticker')['公司名稱'].to_dict()
                    result['Company Name']=[names.get(t,n) for t,n in zip(result.Ticker,result['Company Name'])]
                for idx in result.head(backtest_n).index:
                    for k,v in qr.backtest_ticker(result.at[idx,'Ticker']).items(): result.at[idx,k]=v
            st.session_state.update(results=result,audit=pd.DataFrame(audit),scan_cfg=copy.deepcopy(cfg),scan_time=datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S'),backtest_n=backtest_n)
            progress.progress(1.,text='掃描完成')
        except Exception as exc: st.exception(exc)
        finally: qr.CFG=old_cfg; qr.download_history=original_history; qr.get_fundamentals=original_fund

if 'audit' in st.session_state:
    audit=st.session_state.audit
    counts=audit['狀態'].value_counts() if not audit.empty else pd.Series(dtype=int)
    cols=st.columns(4)
    for c,label,value in zip(cols,['已嘗試','成功分析','條件排除','資料失敗'],[len(audit),counts.get('成功分析',0),counts.get('條件排除',0),counts.get('資料失敗',0)]): c.metric(label,int(value))
    st.caption(f'掃描完成時間（台北）：{st.session_state.scan_time}。行情日期見各股票欄位；清單與行情快取分別保留6小時、30分鐘。基本面可能缺漏，成功分析不代表所有欄位齊全。')
    with st.expander('逐檔掃描紀錄／失敗原因'):
        show_table(audit)
        st.download_button('下載掃描紀錄',csv(audit),'scan_audit_v51.csv','text/csv')

if 'results' in st.session_state and not st.session_state.results.empty:
    df=st.session_state.results; saved=st.session_state.scan_cfg
    st.caption('以下結果使用上次掃描的參數；更改左側設定後請重新掃描。價格採還原權息日線，美股美元、台股新台幣；不是即時報價。')
    tabs=st.tabs(['🏆 排行榜','🎯 核心候選','🔎 個股儀表板','🧪 回測'])
    display=['Rank','Market','Ticker','Company Name','Price Date','Latest Price','Total Score','BIAS20','RSI14','MACD','MACD Signal','MACD Hist','BB Upper','BB Mid','BB Lower','Volume Ratio','P/E','Golden Cross','Risk Flag']
    with tabs[0]: show_table(df[display].head(top_n).round(3))
    with tabs[1]:
        picks=df[df['Core Candidate']]
        if picks.empty: st.info('沒有同時符合核心候選條件的股票；不是系統錯誤。')
        else: show_table(picks[display].round(3))
    with tabs[2]:
        ticker=st.selectbox('選擇股票',df.Ticker.tolist(),format_func=lambda t:f"{t}｜{df.loc[df.Ticker==t,'Company Name'].iloc[0]}")
        row=df[df.Ticker==ticker].iloc[0]
        st.caption(f"掃描行情日期：{row['Price Date']}｜風險提示：{row['Risk Flag']}（－不代表無風險）")
        cs=st.columns(4)
        for c,k in zip(cs,['Latest Price','Total Score','RSI14','BIAS20']): c.metric(LABELS[k],fmt(row[k]))
        st.subheader('MACD 與布林通道數值')
        show_table(pd.DataFrame([{k:row[k] for k in ['MACD','MACD Signal','MACD Hist','BB Upper','BB Mid','BB Lower']}]).round(4))
        st.caption('MACD > 訊號線與柱狀值 > 0 是同一方向訊號，在目前策略分別加5分與3分。')
        try:
            with st.spinner('讀取個股圖表…'): _,h=cached_history(ticker,saved.period,saved.interval,saved.ma_trend,True)
            close=h['Close']; mid=close.rolling(20).mean(); std=close.rolling(20).std()
            st.caption(f'圖表行情日期：{h.index[-1].date()}。若晚於掃描日期，請重新掃描更新分數。')
            st.subheader('股價、均線與布林通道')
            chart=pd.DataFrame({'調整後收盤':close,f'MA{saved.ma_short}':close.rolling(saved.ma_short).mean(),f'MA{saved.ma_long}':close.rolling(saved.ma_long).mean(),'布林上軌':mid+2*std,'布林中軌':mid,'布林下軌':mid-2*std})
            st.line_chart(chart.tail(252))
            macd=close.ewm(span=12,adjust=False).mean()-close.ewm(span=26,adjust=False).mean(); signal=macd.ewm(span=9,adjust=False).mean()
            st.subheader('MACD（12、26、9）')
            st.line_chart(pd.DataFrame({'MACD':macd,'訊號線':signal}).tail(120))
            st.bar_chart(pd.DataFrame({'MACD柱狀值':macd-signal}).tail(120))
            if 'Volume' in h: st.subheader('成交量'); st.bar_chart(h[['Volume']].tail(90))
        except Exception as exc: st.warning(f'圖表讀取失敗：{exc}')
        st.subheader('每一分怎麼來？')
        detail=score_details(row,saved)
        st.dataframe(detail,hide_index=True,use_container_width=True)
        st.caption(f"明細加總：{detail['得分'].sum():.0f}／100｜掃描總分：{row['Total Score']:.0f}。缺值通常不加分；本益比缺值沿用V5給5分。")
        st.subheader('基本面快照')
        st.dataframe(pd.DataFrame({'指標':['本益比','營收成長率','獲利成長率','ROE','自由現金流','負債權益比'],'數值':[row.get(k) for k in ['P/E','Revenue Growth','EPS Growth','ROE','Free Cash Flow','Debt To Equity']]}),hide_index=True)
        st.caption('成長率與ROE採小數表示，0.10代表10%；基本面來源為Yahoo Finance最新可用快照，未提供財報日期。')
    with tabs[3]:
        st.caption('沿用原事件回測：MA20上穿MA60並符合乖離率；不是V5.1總分的績效驗證。回測均線固定20/60日。')
        if st.session_state.backtest_n==0: st.info('此次未執行回測。可在左側設定回測前N名後重新掃描。')
        else: show_table(df[[c for c in ['Ticker','Signals','5D Avg','20D Avg','60D Avg','20D WinRate','Avg MaxDD','Sharpe20'] if c in df]].head(st.session_state.backtest_n))
    st.download_button('下載完整結果 CSV',csv(df),'quant_stock_all_results.csv','text/csv')
    st.download_button('下載核心候選 CSV',csv(df[df['Core Candidate']]),'quant_stock_picks.csv','text/csv')
elif 'results' in st.session_state: st.info('這次沒有保留的分析結果，請查看逐檔掃描紀錄。')
