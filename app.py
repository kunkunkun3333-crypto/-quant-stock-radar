from __future__ import annotations
import pandas as pd
import numpy as np
import streamlit as st
import quant_stock_radar as qr

st.set_page_config(page_title='台美股量化雷達 V3', page_icon='📊', layout='wide')
st.title('📊 台美股量化選股雷達 V3')
st.caption('掃描器 × 個股儀表板 × 價格/均線圖 × 基本面視覺化 × 回測｜研究工具，不構成投資建議')

with st.sidebar:
    st.header('策略設定')
    markets = st.multiselect('掃描市場', ['美股', '台股'], default=['美股', '台股'])
    qr.CFG.ma_short = st.number_input('短均線 MA', 5, 100, qr.CFG.ma_short)
    qr.CFG.ma_long = st.number_input('長均線 MA', 20, 200, qr.CFG.ma_long)
    qr.CFG.ma_trend = st.number_input('趨勢均線 MA', 100, 300, qr.CFG.ma_trend)
    qr.CFG.bias_min = st.number_input('BIAS 下限 %', -20.0, 0.0, qr.CFG.bias_min, 0.5)
    qr.CFG.bias_max = st.number_input('BIAS 上限 %', 0.0, 30.0, qr.CFG.bias_max, 0.5)
    qr.CFG.pe_max = st.number_input('P/E 上限', 1.0, 200.0, qr.CFG.pe_max, 1.0)
    qr.CFG.us_min_market_cap = st.number_input('美股最低市值（十億美元）', 0.0, 1000.0, qr.CFG.us_min_market_cap/1e9, 1.0) * 1e9
    qr.CFG.tw_min_avg_volume_lots = st.number_input('台股20日均量最低（張）', 0, 100000, qr.CFG.tw_min_avg_volume_lots, 500)
    top_n = st.slider('顯示前 N 名', 5, 50, 20)
    backtest_top = st.slider('回測前 N 名', 0, 20, 5)

st.subheader('股票池')
c1, c2 = st.columns(2)
with c1:
    us_upload = st.file_uploader('美股股票池 CSV（ticker / symbol / code）', type='csv', key='us')
    st.caption('未上傳時使用內建示範股票池。')
with c2:
    tw_upload = st.file_uploader('台股股票池 CSV（ticker / symbol / code）', type='csv', key='tw')
    st.caption('台股可填 2330，程式會嘗試 .TW / .TWO。')

def tickers_from_upload(upload, market):
    if upload is None:
        return qr.DEFAULT_US.copy() if market == 'US' else qr.DEFAULT_TW.copy()
    df = pd.read_csv(upload)
    col = next((c for c in df.columns if c.lower() in {'ticker','symbol','code'}), df.columns[0])
    vals = df[col].dropna().astype(str).str.strip().tolist()
    if market == 'US': return [v.upper() for v in vals]
    return [v.upper() if v.upper().endswith(('.TW','.TWO')) else f'{v}.TW' for v in vals]

if st.button('🚀 開始掃描', type='primary', use_container_width=True):
    if not markets:
        st.warning('請至少選擇一個市場。'); st.stop()
    progress = st.progress(0, text='正在取得基準資料…')
    try:
        benchmarks={'SPY':qr.benchmark_returns('SPY'),'QQQ':qr.benchmark_returns('QQQ'),'0050.TW':qr.benchmark_returns('0050.TW')}
        rows=[]
        if '美股' in markets:
            progress.progress(20,text='掃描美股…'); rows += qr.screen_market(tickers_from_upload(us_upload,'US'),'US',benchmarks)
        if '台股' in markets:
            progress.progress(55,text='掃描台股…'); rows += qr.screen_market(tickers_from_upload(tw_upload,'TW'),'TW',benchmarks)
        if not rows:
            st.error('沒有取得可分析結果。'); st.stop()
        df=pd.DataFrame(rows).sort_values('Total Score',ascending=False).reset_index(drop=True)
        df.insert(0,'Rank',range(1,len(df)+1))
        for c in ['Signals','5D Avg','20D Avg','60D Avg','20D WinRate','Avg MaxDD','Sharpe20']: df[c]=np.nan
        for idx in df.head(backtest_top).index:
            progress.progress(75,text=f"回測 {df.at[idx,'Ticker']}…")
            for k,v in qr.backtest_ticker(df.at[idx,'Ticker']).items():
                if k in df.columns: df.at[idx,k]=v
        progress.progress(100,text='完成'); st.session_state['results']=df
    except Exception as e: st.exception(e)

if 'results' in st.session_state:
    df=st.session_state['results']; picks=df[df['Core Candidate']].copy()
    a,b,c,d=st.columns(4)
    a.metric('掃描標的',len(df)); b.metric('核心候選',len(picks)); c.metric('最高分',f"{df['Total Score'].max():.0f}"); d.metric('黃金交叉',int(df['Golden Cross'].sum()))
    tab1,tab2,tab3,tab4=st.tabs(['🏆 排行榜','🎯 核心候選','🔎 個股儀表板','🧪 回測'])
    display=['Rank','Market','Ticker','Company Name','Latest Price','Total Score','Golden Cross','BIAS20','RSI14','Volume Ratio','P/E','Relative Strength 3M','Risk Flag']
    with tab1: st.dataframe(df[display].head(top_n).round(2),use_container_width=True,hide_index=True)
    with tab2:
        if picks.empty: st.info('目前沒有同時符合黃金交叉、BIAS 與 P/E 條件的核心候選。')
        else: st.dataframe(picks[display].round(2),use_container_width=True,hide_index=True)
    with tab3:
        ticker=st.selectbox('選擇股票',df['Ticker'].tolist(),format_func=lambda x: f"{x}｜{df.loc[df['Ticker']==x,'Company Name'].iloc[0]}")
        row=df.loc[df['Ticker']==ticker].iloc[0]
        m1,m2,m3,m4,m5=st.columns(5)
        m1.metric('最新價',f"{row['Latest Price']:.2f}"); m2.metric('總分',f"{row['Total Score']:.0f}/100"); m3.metric('RSI14',f"{row['RSI14']:.1f}"); m4.metric('BIAS20',f"{row['BIAS20']:.2f}%"); m5.metric('3M相對強弱',f"{row['Relative Strength 3M']:.2f}%")
        try:
            _,hist=qr.download_history(ticker)
            chart=hist[['Close']].copy(); chart['MA20']=chart['Close'].rolling(qr.CFG.ma_short).mean(); chart['MA60']=chart['Close'].rolling(qr.CFG.ma_long).mean(); chart['MA200']=chart['Close'].rolling(qr.CFG.ma_trend).mean()
            st.subheader('價格與均線')
            st.line_chart(chart[['Close','MA20','MA60','MA200']].tail(252),use_container_width=True)
            st.subheader('成交量')
            st.bar_chart(hist[['Volume']].tail(90),use_container_width=True)
        except Exception as e: st.warning(f'圖表資料取得失敗：{e}')
        left,right=st.columns(2)
        with left:
            st.subheader('策略分數')
            score_df=pd.DataFrame({'分數':[row.get('Trend Score',np.nan),row.get('Momentum Score',np.nan),row.get('Valuation Score',np.nan),row.get('Fundamental Score',np.nan),row.get('Volume Score',np.nan),row.get('Entry Score',np.nan)]},index=['趨勢','動能','估值','基本面','成交量','買點'])
            st.bar_chart(score_df,use_container_width=True)
        with right:
            st.subheader('基本面快照')
            fundamentals=pd.DataFrame({'指標':['P/E','營收成長','EPS成長','ROE','自由現金流','負債權益比'],'數值':[row.get('P/E'),row.get('Revenue Growth'),row.get('EPS Growth'),row.get('ROE'),row.get('Free Cash Flow'),row.get('Debt To Equity')]})
            st.dataframe(fundamentals,use_container_width=True,hide_index=True)
        st.info(f"風險提示：{row['Risk Flag']}｜核心候選：{'是' if row['Core Candidate'] else '否'}。分數代表符合策略規則程度，不是買賣建議。")
    with tab4:
        btshow=['Rank','Ticker','Total Score','Signals','5D Avg','20D Avg','60D Avg','20D WinRate','Avg MaxDD','Sharpe20']
        st.dataframe(df[btshow].head(max(backtest_top,1)).round(4),use_container_width=True,hide_index=True)
        st.caption('回測僅使用歷史價格訊號，不把今天的基本面數字套用到過去，以降低 look-ahead bias。')
    d1,d2=st.columns(2)
    d1.download_button('⬇️ 下載完整結果 CSV',df.to_csv(index=False).encode('utf-8-sig'),'quant_stock_all_results.csv','text/csv',use_container_width=True)
    d2.download_button('⬇️ 下載核心候選 CSV',picks.to_csv(index=False).encode('utf-8-sig'),'quant_stock_picks.csv','text/csv',use_container_width=True)

st.divider(); st.caption('V3｜yfinance 資料可能延遲、缺漏或受來源限制；歷史績效不代表未來結果。')
