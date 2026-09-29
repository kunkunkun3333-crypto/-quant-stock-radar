import unittest
import tempfile
from pathlib import Path
from v52_cache import Store
from unittest.mock import patch,Mock
from dataclasses import replace
import pandas as pd
import numpy as np
import v52_data as data
from v52_config import DEFAULT
from v52_backtest import technical_signals
from v52_engine import features
from test_integration import synthetic

class DataSafety(unittest.TestCase):
    def test_scan_records_all_failed_batches(self):
        tickers=[str(i)+'.TW' for i in range(7)]
        with patch.object(data,'history_batch',side_effect=RuntimeError('rate limit')) as download:
            rows,audit,_,_=data.scan(tickers,{}, {},cfg=replace(DEFAULT,batch_size=2,max_failed_batches=2,request_pause=0))
        self.assertEqual(download.call_count,4)
        self.assertEqual(len(audit),7)
        self.assertEqual(rows,[])
    def test_fundamental_no_forward_pe_or_infinity(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        store=Store(Path(tmp.name)/'cache.sqlite3',min_interval=0)
        with patch.object(data,'get_store',return_value=store),patch.object(data.yf,'Ticker',return_value=Mock(info={'trailingPE':-5,'forwardPE':12,'trailingEps':float('inf'),'marketCap':1e10})):
            f=data.fundamentals('TEST.TW')
        self.assertIsNone(f['P/E']);self.assertIsNone(f['EPS'])
        self.assertIsNone(f['Monthly Revenue YoY']);self.assertIsNone(f['Fundamental As Of'])
    def test_batch_retains_internal_gaps(self):
        h=synthetic(n=100);h.loc[h.index[50],'Close']=np.nan
        raw=pd.concat({'TEST.TW':h},axis=1)
        self.assertTrue(pd.isna(data.extract_history(raw,'TEST.TW').Close.iloc[50]))
    def test_future_cannot_change_past_signals(self):
        h=synthetic(7,n=300);b=synthetic(8,n=300)
        full=technical_signals(features(h),features(b))
        past=technical_signals(features(h.iloc[:220]),features(b.iloc[:220]))
        pd.testing.assert_series_equal(full.iloc[:220],past)
