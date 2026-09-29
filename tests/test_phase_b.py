import unittest
from v52_engine import *

class PhaseB(unittest.TestCase):
    def rows(self,n):return [{'Market':'TW','Industry':'半導體業','P/E':20+i,'Return20':i,'Return60':2*i,'Benchmark Return20':1,'Trend':'Bullish','Stale':False} for i in range(n)]
    def test_industry_threshold(self):
        self.assertIsNone(number(enrich_industries(self.rows(4),{('TW','半導體業'):4})[0]['Industry Strength']))
        r=enrich_industries(self.rows(10),{('TW','半導體業'):10})[0]
        self.assertTrue(0<=r['Industry Strength']<=100)
        self.assertIsNone(number(enrich_industries(self.rows(5),{('TW','半導體業'):20})[0]['Industry Strength']))
    def test_risk_direction(self):
        a={'Quality Score':80,'Entry Score':80,'Trend Score':80,'Industry Strength':80,'Institutional Score':None,'Risk Score':10}
        b=dict(a,**{'Risk Score':90})
        self.assertGreater(total_score(a)[0],total_score(b)[0]);self.assertEqual(total_score(a)[1],90)
    def test_confidence_never_technical_proxy(self):
        r={'Quality Score':95,'Entry Score':95,'Trend':'Bullish','Market Regime':'Bull','Industry Strength':90,'Risk Score':10,'Data Completeness':95,'Quality Coverage':90,'Stale':False}
        ok,checks=confidence(r,{'N':100,'Win Rate':.9,'Scope':'technical-only'})
        self.assertFalse(ok);self.assertFalse(checks['完整模型已驗證'])
    def test_institution_missing(self):self.assertIsNone(institutional_flow('2330.TW')['Institutional Score'])
