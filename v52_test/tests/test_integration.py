import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from v52_config import DEFAULT
from v52_engine import features,phase_a,finalize,number
import v52_data as data
from v52_backtest import backtest

TICKERS=['2330.TW','2317.TW','2454.TW','2408.TW','2344.TW','2337.TW','6488.TWO','8299.TWO','2881.TW','2412.TW']
def synthetic(i=0,n=800):
    rng=np.random.default_rng(100+i);returns=rng.normal((i-4)*.00035,.004+i*.002,n)
    c=100*np.exp(np.cumsum(returns));o=np.r_[c[0],c[:-1]]
    return pd.DataFrame({'Open':o,'Close':c,'High':np.maximum(c,o)*1.01,'Low':np.minimum(c,o)*.99,'Volume':rng.integers(500000,2000000,n)},index=pd.bdate_range(end=pd.Timestamp.now().normalize(),periods=n))
def metadata():return {t:{'Market':'TW','Industry':'半導體業' if i<8 else '其他測試分類','Company Name':t} for i,t in enumerate(TICKERS)}
def fundamental(t):
    i=TICKERS.index(t) if t in TICKERS else 0
    return {'P/E':12+i*4,'EPS':float(i-2),'Revenue Growth':(i-4)*.05,'EPS Growth':(i-5)*.07,'ROE':i*.025,'Gross Margin':i*.05,'Operating Margin':(i-2)*.03,'Free Cash Flow':1e6*(i-4),'Market Cap':1e11}

class Integration(unittest.TestCase):
    def setUp(self):
        import streamlit as st
        st.cache_data.clear()
    def test_ten_stock_fixture(self):
        def loader(ts,*args):return {t:synthetic(TICKERS.index(t)) for t in ts}
        with patch.object(data,'history_batch',loader),patch.object(data,'fundamentals',fundamental):
            rows,audit,hist,expected=data.scan(TICKERS,metadata(),{'TW':synthetic(7)})
        df=finalize(rows,expected)
        self.assertEqual(len(df),10);self.assertEqual(len(audit),10)
        for k in ['Total Score','Quality Score','Entry Score','Risk Score','RSI14','BIAS20','Volume Ratio']:
            self.assertTrue(np.isfinite(df[k].astype(float)).all(),k)
        self.assertGreater(df['Total Score'].max()-df['Total Score'].min(),15)
        self.assertTrue(df['Total Score'].between(0,100).all())
        self.assertFalse(df['High Confidence'].any())
        for i in (0,4,7,9):
            bt=backtest(hist[TICKERS[i]],synthetic(7))
            for s in bt['summary'].values():
                if s['N']:self.assertTrue(0<=s['Win Rate']<=1)
    def test_single_failure_keeps_rest(self):
        def loader(ts,*args):return {t:synthetic() for t in ts if t!=TICKERS[0]}
        with patch.object(data,'history_batch',loader),patch.object(data,'fundamentals',side_effect=RuntimeError('fundamental unavailable')):
            rows,audit,_,expected=data.scan(TICKERS[:2],metadata(),{})
        self.assertEqual(len(rows),1);self.assertEqual(audit['狀態'].tolist(),['資料失敗','成功分析'])
        df=finalize(rows,expected);self.assertIsNone(number(df['Quality Score'].iloc[0]));self.assertFalse(df['High Confidence'].iloc[0])
    def test_ui_start_scan_detail_bt(self):
        from streamlit.testing.v1 import AppTest
        cat=pd.DataFrame([{'Ticker':t,'公司名稱':t,'上市櫃':'上市' if t.endswith('.TW') else '上櫃','產業代碼':'24','產業':'半導體業','清單日期':'TEST'} for t in TICKERS])
        def loader(ts,*args):return {t:synthetic(TICKERS.index(t) if t in TICKERS else 7) for t in ts}
        with patch.object(data,'catalog',return_value=cat),patch.object(data,'history_batch',side_effect=loader),patch.object(data,'fundamentals',side_effect=fundamental):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app_v52_test.py'),default_timeout=60).run()
            self.assertEqual(len(at.exception),0)
            next(s for s in at.selectbox if s.label=='台股股票池').select('上市櫃科技股（完整清單）').run()
            next(s for s in at.slider if s.label=='掃描後回測前N名').set_value(0).run()
            next(b for b in at.button if b.label=='🚀 開始掃描').click().run()
            self.assertEqual(len(at.exception),0)
            self.assertEqual(len(at.session_state['v52_result']),10)
            next(s for s in at.selectbox if s.label=='選擇股票').select('2408.TW').run()
            self.assertEqual(len(at.exception),0)
            next(b for b in at.button if b.label=='計算／查看所選股票歷史驗證').click().run()
            self.assertEqual(len(at.exception),0)
            self.assertIn('2408.TW',at.session_state['v52_bt'])
