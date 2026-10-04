"""沿用 V5.1 股票池→掃描→排行榜/個股/回測的 Streamlit 流程。"""
from dataclasses import asdict
import json
import os
import math
import time
import re
import uuid
import v52_auto as auto
from v52_cache import get_store
from v52_bulk import scan_resumable,benchmark_refresh,limited_universe
from v52_official import snapshots
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
import quant_stock_radar_v5 as legacy
from v51_support import INDUSTRIES
from v52_config import DEFAULT
from v52_engine import number,features,finalize,confidence
from v52_data import catalog,history_batch,fundamentals,benchmark_state,scan

from v52_validation import cached_backtest,validate_pool,display_results,quality_reason

def pool_backtests(rows,hist,bench):
    bar=st.progress(0.,text="歷史驗證準備中（使用已清洗行情，不重新下載）")
    result=validate_pool(rows,hist,bench,lambda i,n,t:bar.progress(i/max(n,1),text=f"歷史驗證 {i}/{n}｜{t}"))
    bar.empty()
    return result

st.set_page_config(page_title='Quant Stock Radar V5.2',page_icon='📊',layout='wide')
st.title('📊 Quant Stock Radar V5.2')
if os.environ.get('V52_TEST_SITE')=='1':st.warning('🧪 V5.2 獨立測試站｜正式V5.1不受影響｜尚未通過正式部署驗收')
st.caption('V5.2 – Quant Decision System｜量化投資決策輔助系統')
st.caption(f'測試組建：{DEFAULT.model_version}｜背景自動掃描 auto-v1｜全標的歷史驗證 v2')
st.info('實驗模型：總分權重尚未完成完整多因子樣本外驗證。歷史統計只涵蓋技術＋大盤訊號；高信心標的另需完整模型驗證資格，目前不放行。')
if st.session_state.get('v52_version')!=DEFAULT.model_version:
    for key in ['v52_result','v52_audit','v52_hist','v52_rows','v52_expected','v52_bt','v52_market','v52_bench','v52_scan_time']:st.session_state.pop(key,None)
    st.session_state.v52_version=DEFAULT.model_version

LABELS={'Rank':'排名','Ticker':'代號','Market':'市場','Company Name':'公司','Industry':'產業','Latest Price':'調整後價格','Price Date':'行情日期','Total Score':'總分（實驗）','Quality Score':'Quality 品質','Entry Score':'Entry 進場','Trend Score':'Trend 趨勢分','Industry Strength':'產業強弱','Institutional Score':'法人分數','Risk Score':'Risk 風險↑','20D Win Rate':'20日歷史勝率','20D N':'20日樣本N','Trend':'趨勢','Signal':'訊號','Data Completeness':'資料完整度%','Quality Coverage':'品質覆蓋率%','Total Coverage':'總分覆蓋率%','Market Regime':'大盤環境','BIAS20':'20日乖離率%','Volume Ratio':'量比','P/E':'本益比','EPS':'每股盈餘EPS','Revenue Growth':'營收成長率','EPS Growth':'EPS成長率（未接入）','Earnings Growth':'Yahoo獲利成長率（非EPS成長）','Gross Margin':'毛利率','Operating Margin':'營益率','Monthly Revenue YoY':'月營收YoY','ATR%':'ATR波動%','Volatility':'年化波動%','Max Drawdown':'近252日最大回撤%','Foreign Net':'外資買賣超','Trust Net':'投信買賣超','Dealer Net':'自營商買賣超','Trust Buy Streak':'投信連買天數','Sell Streak':'連賣天數','Flow Date':'籌碼日期','Flow Status':'籌碼狀態','Industry Sample':'產業有效樣本','Industry Coverage':'本次產業覆蓋率%','Industry Status':'產業狀態','Industry RS20':'產業20日相對強弱百分點','Peer PE Median':'同產業正本益比中位數','Fundamental Retrieved':'基本面擷取時間','Fundamental As Of':'財報公開日期（未驗證）'}

def fmt(x,digits=1):return 'N/A' if number(x) is None else f'{float(x):.{digits}f}'
def table(df):
    if df.empty:st.info('目前沒有資料。');return
    st.dataframe(df.rename(columns=LABELS).style.format(precision=2,na_rep='N/A'),hide_index=True,use_container_width=True)
def export(df):
    clean=df.copy()
    for k in clean.columns:
        if clean[k].apply(lambda x:isinstance(x,(dict,list))).any():clean[k]=clean[k].apply(lambda x:json.dumps(x,ensure_ascii=False,default=str) if isinstance(x,(dict,list)) else x)
    return clean.to_csv(index=False,na_rep='N/A').encode('utf-8-sig')
def bt_table(summary):
    out=[]
    for horizon,r in summary.items():
        out.append({'交易日':horizon,'N':r['N'],'上漲':r['Up'],'下跌':r['Down'],'持平':r['Flat'],'Win Rate%':None if r['Win Rate'] is None else r['Win Rate']*100,'平均報酬%':None if r['Average Return'] is None else r['Average Return']*100,'中位報酬%':None if r['Median Return'] is None else r['Median Return']*100,'最差事件回撤%':None if r['Maximum Drawdown'] is None else r['Maximum Drawdown']*100,'最佳%':None if r['Best'] is None else r['Best']*100,'最差%':None if r['Worst'] is None else r['Worst']*100,'隔日開盤扣成本平均%':None if r['Net Average Return'] is None else r['Net Average Return']*100,'隔日開盤扣成本勝率%':None if r['Net Win Rate'] is None else r['Net Win Rate']*100,'可執行價格樣本N':r.get('Executable N',0),'註記':r['Warning']})
    shown=pd.DataFrame(out)
    for i,r in enumerate(out):
        if r['N']<30:
            for col in shown.columns:
                if '%' in col:shown[col]=shown[col].astype(object);shown.at[i,col]=f"N/A｜樣本不足 N={r['N']}"
    table(shown)

with st.sidebar:
    st.header('掃描設定')
    markets=st.multiselect('掃描市場',['台股','美股'],default=['台股'])
    mode=st.selectbox('台股股票池',['上市櫃科技股（完整清單）','全部上市櫃公司','示範台股10檔'],index=2)
    tw_limit=st.selectbox('台股掃描上限',[10,50,100,300,500,1000,'全部'],index=0)
    sectors=st.multiselect('科技股產業範圍',list(INDUSTRIES.values()),default=list(INDUSTRIES.values()),disabled=mode!='上市櫃科技股（完整清單）')
    extra=st.text_input('補充台股代號（逗號分隔）',placeholder='2408,6488')
    st.caption('使用官方產業分類；不把AI、伺服器、記憶體等主題自行推定為產業。科技模式不含興櫃／ETF。')
    us_mode=st.selectbox('美股股票池',['示範12檔','自動擴充'])
    max_us=st.slider('美股最多掃描檔數',50,1200,300,50)
    min_tw=st.number_input('台股20日均量最低（張）',0,100000,0,500)
    min_us=st.number_input('美股最低市值（十億美元）',0.,1000.,10.,1.)*1e9
    top_n=st.slider('排行榜顯示檔數',10,2000,100,10)
    st.caption('掃描完成後驗證全部成功標的的5／20／60日歷史訊號；結果按行情內容快取，不限前N名。')
    st.caption('技術指標固定MA20/60、RSI14；所有分數權重與門檻集中於v52_config.py。健康回應時每批25→50→100檔，批間休息5秒；網路預算隨批次檔數調整、每輪最多約600秒。暫停後按續掃，已完成結果保存6小時。')
    if st.button('更新公司清單及行情快取'):
        get_store().expire('checkpoint:')
        st.session_state.pop('v52_scan_context',None)
        catalog.clear();history_batch.clear();fundamentals.clear();cached_backtest.clear()
        st.success('已標記資料待更新；保留舊快照與限流冷卻。請重新掃描，已顯示的結果仍是上次快照。')

with st.expander('指標中文解讀／模型設定與限制'):
    st.markdown('**Quality**是公司品質，**Entry**是模型進場條件，**Risk越高風險越高**。所有分數0–100；不是勝率。RSI偏低不代表低估，跌勢中超賣的Entry有上限。')
    st.markdown('台灣大盤採 **^TWII加權指數**，美股採 **SPY**。資料缺失或過舊顯示N/A，不猜測Bull。價格是調整後日線，不是即時報價；資料供應商可能提供尚未收盤的日K。')
    st.markdown('法人籌碼、月營收YoY、可驗證EPS成長率、歷史時點財報／下市股資料尚未接入。缺項不當成公司0分，僅在有效項目間重新分配權重，另顯示覆蓋率並提高資料不足風險。')
    st.json(asdict(DEFAULT))
    st.caption('所有模型參數在測試前固定，沒有為了提高勝率而調參。AI／記憶體等細分主題待可靠分類來源。')

universe=pd.DataFrame();blocked=False
if '台股' in markets and st.button('重新取得台股清單（保留成功備援）'):
    get_store().expire('universe:')
    catalog.clear()
if '台股' in markets and not (st.session_state.get('v52_resume_button',False) and st.session_state.get('v52_scan_context')):
    try:
        with st.spinner('讀取上市櫃清單…'):full=catalog()
        for warning in full.attrs.get('universe_warnings',[]):st.warning(warning)
        if full.attrs.get('universe_sources'):
            with st.expander('台股清單來源與版本時間',expanded=bool(full.attrs.get('universe_warnings'))):
                st.dataframe(pd.DataFrame(full.attrs['universe_sources']),hide_index=True)
        universe=full.copy()
        if mode=='上市櫃科技股（完整清單）':universe=full[full['產業'].isin(sectors)].copy()
        if mode=='示範台股10檔':universe=full[full.Ticker.isin(legacy.DEFAULT_TW)].copy()
        for code in [c.strip().upper() for c in extra.replace('，',',').split(',') if c.strip()]:
            selected=full[full.Ticker.eq(code)|full.Ticker.str.split('.').str[0].eq(code)]
            if selected.empty:blocked=True;st.error(f'找不到上市櫃公司代號：{code}')
            else:universe=pd.concat([universe,selected]).drop_duplicates('Ticker')
        pool_total=len(universe)
        universe=limited_universe(universe,tw_limit)
        st.caption(f'原股票池 {pool_total} 檔；本次上限 {tw_limit}；按代號排序取樣')
        st.subheader(f'台股待掃描清單：{len(universe)} 檔')
        with st.expander('查看完整公司名單與產業分布'):
            table(universe)
            if not universe.empty:table(universe.groupby(['上市櫃','產業']).size().rename('檔數').reset_index())
        st.download_button('下載台股待掃描清單',export(universe),'tw_universe_v52.csv','text/csv')
    except Exception as exc:blocked=True;st.error(f'無法確認完整台股清單：{exc}。請稍後更新快取重試。')

with st.expander('官方收盤／本益比備援查詢（不計算技術分數）'):
    st.caption('官方單日、未還原快照，日期以來源為準；不取代歷史日線，不混入Yahoo回測。尚未取得或無有效正本益比時顯示N/A。')
    if st.button('讀取官方行情快照'):
        with st.spinner('讀取四個官方全市場端點…'):
            st.session_state.v52_official=snapshots()
    if 'v52_official' in st.session_state:
        snap,source_audit=st.session_state.v52_official
        if not universe.empty and not snap.empty:snap=universe[['Ticker','公司名稱']].merge(snap,on='Ticker',how='left')
        table(snap);table(source_audit)
with st.expander('資料快取與來源狀態'):
    st.caption('歷史庫不因TTL刪除；按市場收盤後更新時段增量下載。同時段成功取得後沿用快取。基本面24小時；真正限流冷卻30分鐘。Cloud本機檔案不保證永久保存，請保留備份；V52_CACHE_DIR可指向持久磁碟。')
    st.json(get_store().status())
    from v52_history import export_archive,import_archive
    metrics=get_store().metrics
    st.write({label:metrics.get(key,0) for label,key in [('行情快取命中','history_cache_hit'),('增量更新','history_incremental'),('完整下載','history_full'),('還原基準重抓','history_rebase')]})
    st.caption('統計為本程序累計；每批統計另見下方。備份只含行情與基本面，不含續掃任務。')
    if st.button('準備下載行情備份'):
        st.session_state.v52_archive=export_archive(get_store())
    if 'v52_archive' in st.session_state:
        st.download_button('下載行情備份 ZIP',st.session_state.v52_archive,'v52_history_backup.zip','application/zip')
    backup=st.file_uploader('還原先前下載的行情備份',type=['zip'])
    if backup is not None and st.button('匯入行情備份'):
        try:st.success(f'已檢查匯入 {import_archive(get_store(),backup.getvalue())} 筆快取，保留較新的既有資料。')
        except Exception as exc:st.error(f'備份匯入失敗：{exc}')


if st.button('單獨更新大盤'):
    bench,status=benchmark_refresh(['TW' if m=='台股' else 'US' for m in markets])
    st.session_state.update(v52_market=status,v52_bench=bench)

@st.fragment(run_every='1s')
def resume_cooldown():
    saved_audit=st.session_state.get('v52_audit')
    if saved_audit is None or not saved_audit.attrs.get('pending',0):return
    store=get_store()
    seconds=max(0,math.ceil(store._state('yahoo').get('until',0)-store.clock()))
    if seconds:
        st.warning(f'Yahoo冷卻中：還需 {seconds//60:02d}:{seconds%60:02d}；已完成結果保留，倒數結束後按「接續上次掃描」。')
    else:
        st.info('可以接續上次掃描；將從第一檔未完成股票繼續，不重跑已完成股票。')
resume_cooldown()

auto_token=st.query_params.get('scan_task') or st.session_state.get('v52_auto_token')
active_task=auto.restore(auto_token) if auto_token else None
auto_busy=bool(active_task and active_task.view()[0]['status'] in ('running','cooldown'))
resume_clicked=st.button('▶ 接續上次掃描',key='v52_resume_button')
if resume_clicked and active_task:
    auto.command(auto_token,'resume');auto.ensure(auto_token);st.rerun()
rescan_clicked=st.button('🔄 重新掃描同一股票池（保留行情快取）',disabled=auto_busy)
start_clicked=st.button('🚀 開始掃描',type='primary',disabled=blocked or auto_busy)
if start_clicked or resume_clicked or rescan_clicked:
    checkpoint_id=None
    if resume_clicked or rescan_clicked:
        context=st.session_state.get('v52_scan_context')
        previous=st.session_state.get('v52_audit')
        if previous is None or not previous.attrs.get('checkpoint'):
            st.warning('目前沒有可接續的任務，請先按開始掃描。');st.stop()
        if resume_clicked and not previous.attrs.get('pending',0):
            st.success(f'已完成 {len(previous)}/{len(previous)}、待續掃 0。');st.stop()
        # 相容修補前仍留在session中的任務；順序取自舊audit，而非重新選股。
        if context is None:
            tickers=previous.Ticker.tolist()
            old_rows=st.session_state.get('v52_rows',[])
            old_meta={r['Ticker']:{k:r.get(k) for k in ['Market','Company Name','Industry','Industry Code']} for r in old_rows}
            if 'full' in globals():
                for _,r in full[full.Ticker.isin(tickers)].iterrows():
                    old_meta.setdefault(r.Ticker,{'Market':'TW','Company Name':r['公司名稱'],'Industry':r['產業'],'Industry Code':r['產業代碼']})
            context={'tickers':tickers,'meta':old_meta,'bench':st.session_state.get('v52_bench',{}),
                     'status':st.session_state.get('v52_market',{}),'min_tw':min_tw,'min_us':min_us}
        checkpoint_id=previous.attrs['checkpoint'] if resume_clicked else None
        tickers=context['tickers'];meta=context['meta'];bench=context['bench'];status=context['status']
        scan_min_tw=context['min_tw'];scan_min_us=context['min_us']
        if rescan_clicked:
            # 僅使這一個任務checkpoint失效；不清行情、基本面、來源冷卻或其他任務。
            get_store()._set_state('expired:checkpoint:'+previous.attrs['checkpoint'],{'expired':True})
            for key in ['v52_result','v52_audit','v52_hist','v52_rows','v52_expected','v52_bt','v52_scan_time','v52_run_stats']:
                st.session_state.pop(key,None)
            st.info(f'重新分析原股票池 {len(tickers)} 檔；保留歷史行情及基本面快取、大盤快照，重設本任務結果與批次統計。')
        else:
            st.info(f"接續原任務：已處理 {len(tickers)-previous.attrs['pending']}/{len(tickers)}；沿用原股票池、大盤快照及篩選條件。")
    else:
        if not markets:st.warning('請選擇至少一個市場。');st.stop()
        if '台股' in markets and universe.empty:st.warning('請選擇至少一個產業。');st.stop()
        tickers=universe.Ticker.tolist() if '台股' in markets else [];meta={}
        if '台股' in markets:
            meta={r.Ticker:{'Market':'TW','Company Name':r['公司名稱'],'Industry':r['產業'],'Industry Code':r['產業代碼']} for _,r in universe.iterrows()}
        if '美股' in markets:
            us=legacy.get_universe('US','demo' if us_mode=='示範12檔' else 'auto',max_us)
            if us_mode=='自動擴充' and us==legacy.DEFAULT_US:st.warning('美股來源可能不可用，已退回示範12檔。')
            for t in us:meta[t]={'Market':'US','Industry':'N/A'}
            tickers+=us
        tickers=list(dict.fromkeys(tickers))
        bench,status=benchmark_refresh(['TW' if m=='台股' else 'US' for m in markets])
        scan_min_tw=min_tw;scan_min_us=min_us
        context={'tickers':tickers,'meta':meta,'bench':bench,'status':status,'min_tw':min_tw,'min_us':min_us}
    st.session_state.v52_scan_context=context
    if not resume_clicked:
        token=auto.create(context)
        st.session_state.v52_auto_token=token;st.query_params['scan_task']=token
        auto.ensure(token);st.rerun()
    bar=st.progress(0,text='開始分批下載…')
    batch_display=st.empty()
    phase_display=st.empty()
    run_started=time.perf_counter();metrics_before=dict(get_store().metrics)
    analyzed=[0];first_batch=[None]
    run_id=uuid.uuid4().hex[:12]
    def update_run(i,n,t):
        analyzed[0]+=1
        bar.progress(i/max(n,1),text=f'{i}/{n}：{t}')
    def update_phase(message):
        match=re.match(r'第 (\d+) 批開始',message)
        if match and first_batch[0] is None:first_batch[0]=int(match.group(1))
        phase_display.info(message)
    try:
        rows,audit,hist,expected=scan_resumable(tickers,meta,bench,update_run,scan_min_tw,scan_min_us,batch_report=lambda frame:batch_display.dataframe(frame,hide_index=True),status=update_phase,checkpoint_id=checkpoint_id)
        bt={};result=finalize(rows,expected,bt)
        if not result.empty:
            bt=pool_backtests(rows,hist,bench)
            result=finalize(rows,expected,bt)
        st.session_state.update(v52_result=result,v52_audit=audit,v52_hist=hist,v52_rows=rows,v52_expected=expected,v52_bt=bt,v52_market=status,v52_bench=bench,v52_scan_time=datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S'))
        st.session_state.v52_run_stats={'schema':2,'執行ID':run_id,'模式':'重新分析（保留行情快取）' if rescan_clicked else '接續' if resume_clicked else '開始掃描',
            '本輪耗時秒':round(time.perf_counter()-run_started,2),'本輪實際分析檔數':analyzed[0],
            **{label:get_store().metrics.get(key,0)-metrics_before.get(key,0) for label,key in [('Cache hit','history_cache_hit'),('Incremental update','history_incremental'),('Full download','history_full'),('Download errors','errors'),('Retry','retries')]},
            'batches':audit.attrs.get('batches',[])[first_batch[0]-1:] if first_batch[0] is not None else []}
        remaining=audit.attrs.get('pending',0)
        bar.progress((len(tickers)-remaining)/max(1,len(tickers)),text=f'本輪結束：已處理 {len(tickers)-remaining}/{len(tickers)}、待續掃 {remaining}')
        st.success(f'已處理 {len(tickers)-remaining}/{len(tickers)}、待續掃 {remaining}')
        if remaining:resume_cooldown()
    except Exception as exc:st.error(f'掃描未完成：{exc}')

@st.fragment(run_every='1s')
def automatic_progress(token):
    task=auto.ensure(token)
    if task is None:
        st.warning('此自動任務已無磁碟紀錄，請重新開始。');return
    state,result=task.view()
    total=state['total'];done=state['processed'];remaining=max(0,total-done)
    phase={'running':'掃描中','cooldown':'冷卻中','paused':'已暫停','cancelled':'已取消','completed':'已完成','failed':'已停止，需檢查原因'}.get(state['status'],state['status'])
    st.subheader('自動掃描進度')
    st.progress(min(1.,state['progress']/max(1,total)),text=f"{state['progress']} / {total}｜{phase}")
    seconds=max(0,math.ceil(state.get('next_retry_at',0)-task.store.clock()))
    st.write(f"目前批次：{state['batch']} 批已保存｜{state['message']}")
    if state['status']=='cooldown':st.info(f'冷卻中／剩餘 {seconds} 秒；結束後自動恢復，不需按續掃。')
    stats=state['metrics']
    for col,label,value in zip(st.columns(4),['成功','失敗','快取命中','完整下載'],[state['success'],state['failed'],stats['history_cache_hit'],stats['history_full']]):col.metric(label,value)
    eta=(state['active_seconds']/done*remaining+seconds) if done else None
    st.caption(f"預估剩餘：{eta/60:.1f} 分鐘（不含未來未知限流）" if eta is not None else '預估剩餘：累積第一批資料後顯示')
    st.caption('成功／失敗以已保存批次為準。暫停與取消會先完成並保存目前批次；關閉頁面後背景仍可能繼續，須使用暫停或取消。Cloud程序或磁碟被清除時，無法保證持續執行。請保留帶scan_task的網址供重新連線恢復。')
    a,b,c=st.columns(3)
    if a.button('暫停掃描',disabled=state['status'] not in ('running','cooldown')):
        auto.command(token,'paused');st.rerun()
    if b.button('取消本輪掃描',disabled=state['status'] in auto.TERMINAL):
        auto.command(token,'cancelled');st.rerun()
    if c.button('恢復自動掃描',disabled=state['status'] not in ('paused','failed')):
        auto.command(token,'resume');auto.ensure(token);st.rerun()
    marker=(token,state['batch'],state['status'])
    if result is not None and state['status'] in ('paused','cancelled','completed','failed') and st.session_state.get('v52_auto_delivered')!=marker:
        rows,audit,hist,expected=result;context=auto.context_for(task)
        bt={};ranked=finalize(rows,expected,bt)
        if state['status']=='completed' and not ranked.empty:
            bt=pool_backtests(rows,hist,context['bench'])
            ranked=finalize(rows,expected,bt)
        st.session_state.update(v52_result=ranked,v52_audit=audit,v52_hist=hist,v52_rows=rows,v52_expected=expected,v52_bt=bt,v52_market=context['status'],v52_bench=context['bench'],v52_scan_context=context,v52_scan_time=datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S'))
        st.session_state.v52_run_stats={'schema':2,'執行ID':token[:12],'模式':'自動掃描任務（跨批次累計）','本輪耗時秒':round(task.store.clock()-state['created_at'],2),'本輪實際分析檔數':done,'Cache hit':stats['history_cache_hit'],'Incremental update':stats['history_incremental'],'Full download':stats['history_full'],'Download errors':stats['errors'],'Retry':stats['retries'],'batches':state['batches']}
        st.session_state.v52_auto_delivered=marker
        st.rerun()

if auto_token:automatic_progress(auto_token)

st.subheader('Market Status｜大盤環境')
if 'v52_market' not in st.session_state:st.info('尚未掃描；大盤環境 N/A。')
else:
    for market,r in st.session_state.v52_market.items():
        icon={'Bull':'🟢','Neutral':'🟡','Bear':'🔴'}.get(r['Regime'],'⚪')
        st.write(f"{market} {icon} {r['Regime']}｜行情日期：{r['Date'] or 'N/A'}｜MA20：{fmt(r.get('MA20'))}｜MA60：{fmt(r.get('MA60'))} {r['Reason']}")

if 'v52_result' in st.session_state:
    df=st.session_state.v52_result;audit=st.session_state.v52_audit
    counts=audit['狀態'].value_counts() if not audit.empty else {}
    for col,label,val in zip(st.columns(4),['已處理','成功分析','條件排除','資料失敗'],[len(audit)-counts.get('待續掃',0),counts.get('成功分析',0),counts.get('條件排除',0),counts.get('資料失敗',0)]):col.metric(label,int(val))
    st.metric('待續掃（非資料失敗）',int(counts.get('待續掃',0)))
    if audit.attrs.get('boundary_trace'):
        with st.expander('批次切換紀錄'):st.dataframe(pd.DataFrame(audit.attrs['boundary_trace']),hide_index=True)
    st.subheader('本輪效能統計')
    stats=st.session_state.get('v52_run_stats')
    current=stats if stats and stats.get('schema')==2 else {}
    hits=current.get('Cache hit');incremental=current.get('Incremental update');full_count=current.get('Full download')
    denominator=sum(current.get(k,0) for k in ['Cache hit','Incremental update','Full download'])
    hit_rate=f'{100*hits/denominator:.1f}%' if current and denominator else 'N/A'
    labels=['本輪總耗時','快取命中數','增量更新數','完整下載數','下載錯誤數','Retry 次數','Cache hit rate']
    values=[f"{current['本輪耗時秒']:.2f} 秒" if current else 'N/A',hits,incremental,full_count,current.get('Download errors'),current.get('Retry'),hit_rate]
    for col,label,value in zip(st.columns(4),labels[:4],values[:4]):col.metric(label,'N/A' if value is None else value)
    for col,label,value in zip(st.columns(3),labels[4:],values[4:]):col.metric(label,'N/A' if value is None else value)
    columns=['批次','本批檔數','耗時秒','快取命中','增量更新','完整下載','下載錯誤','重試']
    st.markdown('**每批處理統計（僅本輪）**')
    st.dataframe(pd.DataFrame(current.get('batches',[])).reindex(columns=columns),hide_index=True)
    if current:
        st.caption(f"執行 ID：{current['執行ID']}｜{current['模式']}｜本輪實際分析 {current['本輪實際分析檔數']} 檔")
        if current['本輪實際分析檔數']==0:
            st.info('本輪未執行股票分析（可能僅讀取checkpoint或處於冷卻）。不沿用先前批次統計；請用「重新掃描同一股票池（保留行情快取）」驗證快取。')
    else:
        st.info('目前結果沒有新版的本輪量測資料，故顯示N/A。請按「重新掃描同一股票池（保留行情快取）」。')
    st.caption('快取命中率＝行情快取命中÷（行情快取命中＋增量更新＋完整下載）；分母為0時顯示N/A。下載錯誤與Retry包含行情及基本面。本輪總耗時包含掃描、排行及自動回測，不含先前取得的大盤／股票清單。統計取本次執行前後差額，不沿用checkpoint歷史批次；頁面重繪只保留本次量測。')
    st.caption(f'掃描快照（台北）：{st.session_state.v52_scan_time}｜切换頁面不會重新下載股票池行情；修改設定後需重新掃描。')
    with st.expander('逐檔掃描紀錄／N/A原因'):
        table(audit);st.download_button('下載掃描紀錄',export(audit),'audit_v52.csv','text/csv')
    if df.empty:st.warning('沒有可分析結果，請查看失敗原因。')
    else:
        st.subheader('High Confidence｜高信心標的')
        high=df[df['High Confidence']]
        if high.empty:st.info('今日沒有符合高信心條件的標的。完整多因子模型尚未驗證，系統不降低門檻湊名單。')
        else:table(high[['Ticker','Company Name','Total Score','Risk Score']])
        tabs=st.tabs(['🏆 完整排行','🔎 個股儀表板','🧪 歷史驗證','📋 資料品質'])
        cols=['Rank','Ticker','Company Name','Industry','Latest Price','Price Date','Total Score','Quality Score','Entry Score','Risk Score','20D Win Rate','20D N','Trend','Signal']
        with tabs[0]:
            shown=display_results(df,st.session_state.v52_bt,audit)
            table(shown[cols+['20D 狀態','Quality 原因']].head(top_n))
            if st.button('驗證目前全部成功標的（保留掃描與行情快取）'):
                st.session_state.v52_bt=pool_backtests(st.session_state.v52_rows,st.session_state.v52_hist,st.session_state.v52_bench)
                st.session_state.v52_result=finalize(st.session_state.v52_rows,st.session_state.v52_expected,st.session_state.v52_bt)
                st.rerun()
            with st.expander('全部標的5／20／60日驗證摘要'):
                table(shown[['Ticker']+[f'{h}D {k}' for h in DEFAULT.horizons for k in ['N','Win Rate','Average Return','Median Return','Maximum Drawdown']]])
            st.caption('勝率以百分比顯示；N<30不展示績效，並列出實際樣本數。總分同分以股票代號排序。產業比較限本次股票池成功樣本，並非全市場排名。')
        with tabs[1]:
            t=st.selectbox('選擇股票',df.Ticker.tolist(),format_func=lambda t:f"{t}｜{df.loc[df.Ticker==t,'Company Name'].iloc[0]}")
            row=df[df.Ticker==t].iloc[0].to_dict()
            st.subheader(f"{row['Company Name']}｜{row['Industry']}")
            st.caption(f"行情日期 {row['Price Date']}｜市場 {row['Market']}｜調整後價格 {fmt(row['Latest Price'],2)}（台股TWD／美股USD）")
            scores=['Total Score','Quality Score','Entry Score','Industry Strength','Institutional Score','Risk Score']
            for col,k in zip(st.columns(6),scores):col.metric(LABELS[k],fmt(row.get(k)))
            st.caption(quality_reason(row, next((str(r.get('原因','')) for r in audit.to_dict('records') if r.get('Ticker')==t),'')))
            st.caption('Risk：0–33 🟢、34–66 🟡、67–100 🔴，越高代表模型風險越高。')
            st.write('趨勢：'+{'Bullish':'🟢 上升趨勢','Neutral':'🟡 盤整','Bearish':'🔴 下降趨勢'}.get(row['Trend'],'N/A'))
            rs=number(row.get('RSI14'));bias=number(row.get('BIAS20'));vr=number(row.get('Volume Ratio'));pe=number(row.get('P/E'));ref=number(row.get('Peer PE Median'))
            readings=[('RSI14',fmt(rs),'N/A' if rs is None else '偏熱' if rs>70 else '偏低／須搭配趨勢' if rs<30 else '中間區域'),('BIAS20',fmt(bias),'N/A' if bias is None else '低於20日均線' if bias<0 else '不低於20日均線'),('量比',fmt(vr),'N/A' if vr is None else '成交量明顯放大' if vr>=1.5 else '成交量縮小' if vr<.8 else '接近近期均量'),('P/E',fmt(pe),'無產業比較，不判定便宜昂貴' if pe is None or ref is None else '低於本次同產業正本益比中位數' if pe<ref else '不低於本次同產業正本益比中位數')]
            table(pd.DataFrame(readings,columns=['指標','數值','中文解讀']))
            table(pd.DataFrame([{k:row.get(k) for k in ['MA20','MA60','MA20 Slope','MA60 Slope','MACD','MACD Signal','MACD Hist','BB Upper','BB Mid','BB Lower','ATR%','Volatility','Max Drawdown']}]))
            h=st.session_state.v52_hist[t];f=features(h)
            st.subheader('價格、均線與布林通道')
            st.line_chart(f[['Latest Price','MA20','MA60','MA200','BB Upper','BB Mid','BB Lower']].tail(252))
            st.subheader('MACD（12、26、9）');st.line_chart(f[['MACD','MACD Signal']].tail(120));st.bar_chart(f[['MACD Hist']].tail(120))
            if 'Volume' in h:st.subheader('成交量');st.bar_chart(h[['Volume']].tail(90))
            st.subheader('Why Ranked Here｜為什麼排在這裡？')
            st.write(f"本次排名第 {row['Rank']}，實驗總分 {fmt(row['Total Score'])}，有效總分權重覆蓋率 {fmt(row['Total Coverage'])}%。")
            ex=row['Explanation']
            st.markdown('**優勢**');st.write('；'.join(ex['Strengths']) or '沒有明顯高分優勢，請逐項查看。')
            st.markdown('**風險與限制**');st.write('；'.join(ex['Risks']));st.write(ex['Conclusion'])
            st.subheader('有效權重與貢獻分')
            table(pd.DataFrame(row['Total Details']).replace({'Risk Score':'風險安全分＝100－Risk'}))
            for key,title in [('Quality Details','品質'),('Entry Details','進場'),('Risk Details','風險（越高越危險）')]:
                with st.expander(title+'計算明細'):table(pd.DataFrame(row[key]))
            st.subheader('基本面與法人籌碼')
            fields=['P/E','EPS','Revenue Growth','Monthly Revenue YoY','EPS Growth','Earnings Growth','ROE','Gross Margin','Operating Margin','Free Cash Flow','Market Cap','Fundamental As Of','Fundamental Retrieved','Foreign Net','Trust Net','Dealer Net','Trust Buy Streak','Sell Streak','Flow Date','Flow Status']
            table(pd.DataFrame({'指標':[LABELS.get(k,k) for k in fields],'數值':['N/A' if row.get(k) is None or (isinstance(row.get(k),float) and pd.isna(row[k])) else str(row[k]) for k in fields]}))
            st.caption('成長率／ROE／毛利率／營益率採小數，0.10＝10%。EPS為來源的TTM每股盈餘；非台股月營收YoY。')
            st.subheader('高信心資格逐項檢查')
            table(pd.DataFrame({'條件':list(row['Confidence Checks']),'通過':list(row['Confidence Checks'].values())}))
        with tabs[2]:
            st.warning('以下是固定技術＋大盤訊號的歷史事件統計，不是完整Quality／產業／法人／總分策略的績效驗證。使用目前公司名單，仍存在存活者與選樣偏誤。')
            st.markdown('**勝率定義：** 第N個交易日收盤價高於訊號日收盤價的事件比例；不是期間曾上漲。每個持有期各自剔除未成熟與重疊樣本。訊號收盤後形成，因此另列隔日開盤進場、固定總成本50基點的統計；未處理漲跌停成交限制與額外流動性滑價。')
            st.caption('歷史訊號：Entry≥65、上升趨勢、大盤Bull或Neutral、量比有效。價格／量與大盤只使用訊號當時及以前的值；不將今日基本面或產業分類套到歷史。')
            bt=st.session_state.v52_bt.get(t)
            if st.button('計算／查看所選股票歷史驗證'):
                try:
                    with st.spinner('計算固定訊號歷史統計…'):
                        bt=cached_backtest(t,st.session_state.v52_hist[t],st.session_state.v52_bench.get(row['Market']))
                    st.session_state.v52_bt[t]=bt
                    st.session_state.v52_result=finalize(st.session_state.v52_rows,st.session_state.v52_expected,st.session_state.v52_bt)
                    st.rerun()
                except Exception as exc:st.error(f'回測N/A：{exc}')
            if bt:
                st.write(f'{t}｜{bt["status"]}');bt_table(bt.get('summary',{}))
                if bt.get('diagnostics'):
                    st.write('訊號排除診斷（原因可重疊）');st.json(bt['diagnostics']);st.json(bt.get('exclusions',{}))
                if bt['split_date']:
                    st.subheader('Out-of-Sample｜保留樣本檢查')
                    st.caption(f'切分日 {bt["split_date"]}：較早70%為研究區段，較新30%為保留區段；沒有用任何一段調參。訓練期的退出日期必須早於切分日，跨界樣本剔除。不是完整多因子樣本外證明。')
                    st.markdown('**較早區段**');bt_table(bt['train']);st.markdown('**較新保留區段**');bt_table(bt['test'])
                else:st.info('歷史少於504筆，不提供樣本外切分統計。')
                if bt.get('events') is not None:
                    with st.expander('逐筆歷史事件'):table(bt['events'])
                    st.download_button('下載所選股票回測事件',export(bt['events']),f'{t}_events_v52.csv','text/csv')
            else:st.info('此股票尚未回測，數據為N/A。可按上方按鈕，不必重跑全市場掃描。')
        with tabs[3]:
            table(df[['Ticker','Data Completeness','Quality Coverage','Total Coverage','Price Date','Industry Sample','Industry Coverage','Industry Status']])
            st.caption('完整度檢查20項：技術8項、基本面9項、法人分数、產業強弱與產業分類。低於70%標示不足。數值缺失允許N/A，但不允許Infinity或將缺失偽裝0。')
        st.download_button('下載完整結果 CSV',export(display_results(df,st.session_state.v52_bt,audit)),'quant_stock_v52_results.csv','text/csv')
        st.download_button('下載高信心標的 CSV',export(high),'quant_stock_v52_high_confidence.csv','text/csv')
        legacy_picks=df[(df['Golden Cross']==True)&df.BIAS20.between(-3,5)&(df['P/E'].isna()|df['P/E'].between(0,35,inclusive='neither'))]
        with st.expander('V5.1相容核心候選（不是V5.2高信心）'):
            table(legacy_picks[cols]);st.download_button('下載舊規則核心候選 CSV',export(legacy_picks),'legacy_core_candidates.csv','text/csv')

st.divider()
st.caption('V5.2 – Multi-Factor Scoring · Quality · Entry · Risk · Trend · Market Regime · Industry · Institutional Flow（N/A） · Backtesting · High Confidence Gate · Ranking Explanation · Data Quality')
