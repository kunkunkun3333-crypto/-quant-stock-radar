import unittest
import numpy as np
import pandas as pd
from v52_engine import *

class PhaseA(unittest.TestCase):
    def hist(self,c):return pd.DataFrame({'Close':c,'Open':c,'High':c*1.01,'Low':c*.99,'Volume':1000},index=pd.bdate_range('2020-01-01',periods=len(c)))
    def test_rsi_boundaries(self):
        for close,want in [(np.arange(1.,301),100),(np.arange(301.,1.,-1),0),(np.full(300,100.),50)]:
            self.assertEqual(features(self.hist(close)).RSI14.iloc[-1],want)
    def test_bias_volume(self):
        f=features(self.hist(np.arange(1.,301)))
        self.assertAlmostEqual(f.BIAS20.iloc[-1],(300/np.mean(np.arange(281,301))-1)*100)
        self.assertEqual(f['Volume Ratio'].iloc[-1],1)
    def test_missing_quality(self):
        self.assertIsNone(quality({})[0]); self.assertEqual(quality({'EPS':2})[0],100)
        self.assertEqual(quality({'EPS':2})[1],10)
        self.assertIsNone(quality({'P/E':-2,'Peer PE Median':20})[0])
    def test_falling_not_bargain(self):
        r=features(self.hist(np.linspace(300,20,300))).iloc[-1].to_dict()
        a=phase_a(r,'Bull');b=phase_a(r,'Bear')
        self.assertEqual(a['Trend'],'Bearish');self.assertLessEqual(a['Entry Score'],35)
        self.assertLessEqual(b['Entry Score'],a['Entry Score'])
    def test_no_future_features(self):
        h=self.hist(np.linspace(50,80,300)+np.sin(np.arange(300)))
        pd.testing.assert_frame_equal(features(h).iloc[:200],features(h.iloc[:200]))
    def test_bad_history(self):
        h=self.hist(np.ones(100));h.iloc[50,h.columns.get_loc('Close')]=np.nan
        with self.assertRaises(ValueError):features(h)
