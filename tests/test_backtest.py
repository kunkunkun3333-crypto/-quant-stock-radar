import unittest
import numpy as np
import pandas as pd
from v52_backtest import *

class Backtest(unittest.TestCase):
    def frame(self,n=300):
        return pd.DataFrame({'Latest Price':np.arange(100.,100+n),'Open':np.arange(100.,100+n)},index=pd.bdate_range('2020-01-01',periods=n))
    def test_exact_horizon_counts_and_no_overlap(self):
        f=self.frame(81);s=pd.Series(True,index=f.index)
        e=events_for_horizon(f,s,20)
        self.assertEqual(len(e),3)
        self.assertAlmostEqual(e.Return.iloc[0],.2)
        self.assertAlmostEqual(e['Executable Net Return'].iloc[0],120/101-1-.005)
        self.assertEqual(summarize(e)['Win Rate'],1)
    def test_win_not_intraperiod_high(self):
        f=self.frame(6);f['Latest Price']=[100,200,180,150,120,90]
        e=events_for_horizon(f,pd.Series([True]+[False]*5,index=f.index),5)
        self.assertEqual(summarize(e)['Win Rate'],0)
        self.assertAlmostEqual(summarize(e)['Maximum Drawdown'],-.55)
    def test_purge_split(self):
        f=self.frame(300);s=pd.Series(True,index=f.index)
        train=events_for_horizon(f,s,60,end=210);test=events_for_horizon(f,s,60,start=210)
        self.assertTrue((pd.to_datetime(train['Exit Date'])<f.index[210]).all())
        self.assertTrue((pd.to_datetime(test['Signal Date'])>=f.index[210]).all())
    def test_no_benchmark_future_fill(self):
        f=self.frame();b=pd.DataFrame({'Latest Price':[123]},index=[f.index[100]])
        a=align_benchmark(f.index,b)
        self.assertTrue(a.iloc[:100]['Latest Price'].isna().all())
        self.assertTrue(a.iloc[150:]['Latest Price'].isna().all())
    def test_horizons_have_own_sample(self):
        f=self.frame(30);s=pd.Series(True,index=f.index)
        self.assertGreater(len(events_for_horizon(f,s,5)),0)
        self.assertEqual(len(events_for_horizon(f,s,60)),0)
