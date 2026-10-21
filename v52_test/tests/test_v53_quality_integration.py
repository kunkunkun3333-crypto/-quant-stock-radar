"""Offline deterministic integration tests; fixture prices are not market claims."""
import ast
import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from v52_config import DEFAULT
import v52_engine as engine
from v53_quality_adapter import context_for_run,prepare_quality_inputs


def rows(n=5):
    return [dict(Ticker=f'{i}.TW',Market='TW',Industry='測試產業',**{
        'P/E':100.+i,'EPS':2.,'EPS Growth':None,'Revenue Growth':.1,
        'Monthly Revenue YoY':None,'Free Cash Flow':1.,
        'Latest Price':110.,'MA20':105.,'MA60':100.,'MA20 Slope':1.,'MA60 Slope':1.,
        'RSI14':55.,'BIAS20':2.,'Volume Ratio':1.,'Return20':5.,'Return60':10.,
        'Benchmark Return20':2.,'Market Regime':'Bull','Trend':'Bullish','Stale':False}) for i in range(n)]


def context_without_eps(n=5):
    return dict(as_of='2026-10-11T12:00:00+08:00',context_version='frozen-v1',
        official={'sources':{'twse_pe':{'status':'downloaded','fetched_at':'2026-10-10T00:00:00+08:00',
        'source':'TWSE','records':{str(i):{'source_date':'2026-10-08','values':{'official_pe':10.+i,'official_basic_eps_ytd':999}} for i in range(n)}}}},
        selection={'status':'available','selected_period':'2026-08','monthly_revenue_yoy_eligible':True,
        'audits':[dict(market=m,period='2026-08',revenue_coverage=1.,yoy_coverage=1.,revenue_coverage_pass=True,yoy_coverage_pass=True) for m in ('TWSE','TPEX')]})


def context(n=5):
    c = context_without_eps(n)
    c['official']['sources']['twse_eps'] = {
        'status': 'downloaded', 'source': {'institution': 'TWSE'},
        'fetched_at': '2026-10-10T00:00:00+08:00',
        'records': {str(i): {'source_date': '2026-10-09',
            'period': {'fiscal_year': 2026, 'fiscal_year_roc': 115,
                       'quarter': 2, 'basis': 'cumulative_ytd'},
            'values': {'official_basic_eps_ytd': 2.}} for i in range(n)}}
    return c


def monthly(selection,ticker):
    return {'monthly_revenue_yoy_eligible':True,'eligible_monthly_revenue_yoy_ratio':.2,
            'raw_monthly_revenue_yoy_ratio':999,'monthly_revenue_yoy_pct':999}


def finalize(r,c=None):
    return engine.finalize(r,{('TW','測試產業'):len(r)},official_quality_context=c).set_index('Ticker').sort_index()


def factor(frame,name):
    return next(d for d in frame.loc['0.TW','Quality Details'] if d['項目']==name)

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.mock=patch('v53_monthly_revenue_common.get_monthly_record',side_effect=monthly);self.mock.start()
        self.addCleanup(self.mock.stop)
    def test_no_context_exact_legacy(self):
        # Original finalize algorithm using unchanged primitives, independent of new branch.
        original=[];r=rows()
        for x in engine.enrich_industries(r,{('TW','測試產業'):5}):
            x=engine.phase_a(x,x['Market Regime']);x['Total Score'],x['Total Coverage'],x['Total Details']=engine.total_score(x)
            x['20D Win Rate']=None;x['20D N']=None
            x['High Confidence'],x['Confidence Checks']=engine.confidence(x,{})
            x['Signal']='高信心條件通過' if x['High Confidence'] else '研究觀察／未通過高信心'
            x['Explanation']=engine.explain(x);original.append(x)
        df=pd.DataFrame(original).sort_values(['Total Score','Ticker'],ascending=[False,True],na_position='last').reset_index(drop=True)
        df.insert(0,'Rank',range(1,len(df)+1))
        pd.testing.assert_frame_equal(finalize(r),df.set_index('Ticker').sort_index())
    def test_failed_context_falls_back_once(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[]
            def fail():calls.append(1);raise RuntimeError()
            c=context_for_run('run',root,create=True,builder=fail)
            self.assertIsNone(c);self.assertIsNone(context_for_run('run',root,create=True,builder=fail))
            self.assertEqual(len(calls),1)
            pd.testing.assert_frame_equal(finalize(rows(),c),finalize(rows()))
    def test_context_reload_frozen(self):
        with tempfile.TemporaryDirectory() as root:
            c=context_for_run('run',root,create=True,builder=context)
            d=context_for_run('run',root,create=True,builder=lambda:self.fail('redownload'))
            self.assertEqual(c,d)
            pd.testing.assert_frame_equal(finalize(rows(),c),finalize(rows(),d))
    def test_official_pe_and_median(self):
        result=finalize(rows(),context())
        self.assertEqual(result.loc['0.TW','quality_pe'],10)
        self.assertEqual(result.loc['0.TW','quality_peer_pe_median'],12)
        self.assertEqual(result.loc['0.TW','quality_peer_sample_count'],5)
        self.assertAlmostEqual(factor(result,'P/E')['分數'],100-(10/12-.5)/1.5*100)
    def test_shared_and_other_scores_unchanged(self):
        r=rows();before=copy.deepcopy(r);a=finalize(r);b=finalize(r,context())
        cols=['P/E','Peer PE Median','Revenue Growth','Monthly Revenue YoY','EPS','EPS Growth','Risk Score','Risk Details','Entry Score','Entry Details','Trend','Trend Score','Data Completeness','20D N','20D Win Rate']
        pd.testing.assert_frame_equal(a[cols],b[cols]);self.assertEqual(r,before)
    def test_pe_missing(self):
        c=context();c['official']['sources']['twse_pe']['records']['0']['values']['official_pe']=None
        self.assertIsNone(factor(finalize(rows(),c),'P/E')['分數'])
    def test_peer_under_five(self):
        self.assertIsNone(factor(finalize(rows(4),context(4)),'P/E')['分數'])
    def test_yoy_weight_and_ratio(self):
        d=factor(finalize(rows(),context()),'Monthly Revenue YoY')
        self.assertEqual(d['原始權重'],10);self.assertAlmostEqual(d['分數'],75.)
    def test_yoy_coverage_failed(self):
        c=context();c['selection']['audits'][0]['yoy_coverage']=.97
        self.assertIsNone(factor(finalize(rows(),c),'Monthly Revenue YoY')['分數'])
    def test_common_unavailable(self):
        c=context();c['selection']['status']='unavailable'
        self.assertIsNone(factor(finalize(rows(),c),'Monthly Revenue YoY')['分數'])
    def test_individual_missing(self):
        with patch('v53_monthly_revenue_common.get_monthly_record',return_value={'monthly_revenue_yoy_eligible':False,'eligible_monthly_revenue_yoy_ratio':None}):
            self.assertIsNone(factor(finalize(rows(),context()),'Monthly Revenue YoY')['分數'])
    def test_zero_yoy(self):
        with patch('v53_monthly_revenue_common.get_monthly_record',return_value={'monthly_revenue_yoy_eligible':True,'eligible_monthly_revenue_yoy_ratio':0.}):
            self.assertEqual(factor(finalize(rows(),context()),'Monthly Revenue YoY')['分數'],25.)
    def test_coverage_increase(self):
        self.assertEqual(finalize(rows(),context()).loc['0.TW','Quality Coverage']-finalize(rows()).loc['0.TW','Quality Coverage'],10.)
    def test_eps_unchanged(self):
        a=finalize(rows());b=finalize(rows(),context())
        for k in ('EPS','EPS Growth'):self.assertEqual(factor(a,k),factor(b,k)) if k=='EPS Growth' else self.assertEqual(factor(a,k)['分數'],factor(b,k)['分數'])
    def test_lag_exceeded(self):
        c=context();c['as_of']='2026-11-11T00:00:00+08:00'
        self.assertIsNone(factor(finalize(rows(),c),'Monthly Revenue YoY')['分數'])
    def test_unavailable_pe_source(self):
        c=context();c['official']['sources']['twse_pe']['status']='unavailable'
        self.assertIsNone(factor(finalize(rows(),c),'P/E')['分數'])
    def test_no_yahoo_peer_mixing(self):
        r=rows();r[0]['P/E']=1.e9
        self.assertEqual(finalize(r,context()).loc['0.TW','quality_peer_pe_median'],12.)
    def test_missing_context_not_created_on_resume(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(context_for_run('old',root,builder=lambda:self.fail('called')))
    def test_revenue_coverage_failed(self):
        c=context();c['selection']['audits'][1]['revenue_coverage']=.97
        self.assertIsNone(factor(finalize(rows(),c),'Monthly Revenue YoY')['分數'])
    def test_us_unchanged(self):
        r=rows();r[0]['Ticker']='AAPL';r[0]['Market']='US'
        a=finalize(r);b=finalize(r,context());self.assertEqual(a.loc['AAPL','Quality Score'],b.loc['AAPL','Quality Score'])


class AppContextTests(unittest.TestCase):
    def helpers(self):
        from types import SimpleNamespace
        source=Path(engine.__file__).resolve().parent/'app_v52.py'
        tree=ast.parse(source.read_text())
        funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_official_quality_for_run','finalize')]
        calls=[]
        namespace={'st':SimpleNamespace(session_state={}), '__file__':str(source),'os':__import__('os'),
                   '_v52_finalize':lambda *a,**kw:calls.append(kw['official_quality_context'])}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),str(source),'exec'),namespace)
        return namespace,calls,tree
    def test_app_rerun_recompute_same_context(self):
        ns,calls,tree=self.helpers();c=context()
        with patch('v53_quality_adapter.context_for_run',return_value=c) as builder:
            ns['_official_quality_for_run']('token',True)
            ns['finalize']([],{},{});ns['finalize']([],{}, {})
            ns['_official_quality_for_run']('token',False)
            ns['finalize']([],{}, {})
            self.assertEqual(builder.call_count,1)
            self.assertTrue(all(x is c for x in calls))
        # Every application finalize call resolves to the context wrapper.
        direct=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_v52_finalize']
        self.assertEqual(len(direct),1)
    def test_app_builder_exception_fallback(self):
        ns,calls,_=self.helpers()
        with patch('v53_quality_adapter.context_for_run',side_effect=RuntimeError()) as builder:
            ns['_official_quality_for_run']('token',True)
            ns['_official_quality_for_run']('token',False)
            ns['finalize']([],{}, {})
            self.assertEqual(builder.call_count,1);self.assertEqual(calls,[None])
    def test_app_new_task_new_context(self):
        ns,calls,_=self.helpers()
        with patch('v53_quality_adapter.context_for_run',side_effect=[{'context_version':'one'},{'context_version':'two'}]):
            ns['_official_quality_for_run']('one',True);ns['finalize']([],{}, {})
            ns['_official_quality_for_run']('two',True);ns['finalize']([],{}, {})
        self.assertEqual([c['context_version'] for c in calls],['one','two'])

class EPSIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.mock = patch('v53_monthly_revenue_common.get_monthly_record', side_effect=monthly)
        self.mock.start(); self.addCleanup(self.mock.stop)
    def sample(self, value):
        c=context();c['official']['sources']['twse_eps']['records']['0']['values']['official_basic_eps_ytd']=value
        return c
    def test_positive_zero_negative(self):
        for value, score in [(2.,100.),(0.,0.),(-2.,0.)]:
            with self.subTest(value=value):
                f=finalize(rows(),self.sample(value))
                self.assertEqual(factor(f,'EPS')['分數'],score)
                self.assertEqual(f.loc['0.TW','quality_eps'],value)
    def test_missing_nonfinite_no_yahoo_fallback(self):
        for value in (None,float('nan'),float('inf'),-float('inf'),'2',True):
            with self.subTest(value=value):
                f=finalize(rows(),self.sample(value));self.assertIsNone(factor(f,'EPS')['分數'])
                self.assertEqual(f.loc['0.TW','EPS'],2.)
    def test_opposite_sign_and_input_immutable(self):
        for yahoo, official in [(2.,-1.),(-2.,1.)]:
            r=rows();r[0]['EPS']=yahoo;old=copy.deepcopy(r);c=self.sample(official);before=copy.deepcopy(c)
            f=finalize(r,c)
            self.assertEqual(f.loc['0.TW','EPS'],yahoo)
            self.assertEqual(factor(f,'EPS')['分數'],100. if official>0 else 0.)
            self.assertEqual(r,old);self.assertEqual(c,before)
    def test_market_routing(self):
        r=rows();r.append(dict(r[0],Ticker='0.TWO'))
        c=self.sample(-1.);c['official']['sources']['tpex_eps']=copy.deepcopy(c['official']['sources']['twse_eps'])
        c['official']['sources']['tpex_eps']['source']={'institution':'TPEX'}
        c['official']['sources']['tpex_eps']['records']['0']['values']['official_basic_eps_ytd']=3.
        f=finalize(r,c);self.assertEqual(f.loc['0.TW','quality_eps'],-1.)
        self.assertEqual(f.loc['0.TWO','quality_eps'],3.)
        self.assertEqual(f.loc['0.TWO','quality_eps_source'],{'institution':'TPEX'})
    def test_missing_record(self):
        c=context();del c['official']['sources']['twse_eps']['records']['0']
        self.assertIsNone(factor(finalize(rows(),c),'EPS')['分數'])
    def test_invalid_periods(self):
        valid=context()['official']['sources']['twse_eps']['records']['0']['period']
        cases=[None,{},dict(valid,fiscal_year=None),dict(valid,fiscal_year=True),dict(valid,fiscal_year='2026'),dict(valid,quarter=0),dict(valid,quarter=5),dict(valid,quarter=True),dict(valid,basis='trailing'),dict(valid,basis=None),dict(valid,fiscal_year_roc=114),dict(valid,quarter=4)]
        for p in cases:
            with self.subTest(period=p):
                c=context();c['official']['sources']['twse_eps']['records']['0']['period']=p
                self.assertIsNone(factor(finalize(rows(),c),'EPS')['分數'])
    def test_unavailable_and_missing_source(self):
        for missing in (False,True):
            c=context()
            if missing: del c['official']['sources']['twse_eps']
            else:c['official']['sources']['twse_eps']['status']='unavailable'
            self.assertIsNone(factor(finalize(rows(),c),'EPS')['分數'])
    def test_coverage_and_redistribution(self):
        a=finalize(rows(),self.sample(None));b=finalize(rows(),self.sample(1.))
        self.assertEqual(b.loc['0.TW','Quality Coverage']-a.loc['0.TW','Quality Coverage'],10.)
        detail=factor(b,'EPS');self.assertEqual(detail['原始權重'],10.)
        self.assertAlmostEqual(detail['有效權重%'],100*10/b.loc['0.TW','Quality Coverage'])
        self.assertAlmostEqual(b.loc['0.TW','Quality Score'],(a.loc['0.TW','Quality Score']*a.loc['0.TW','Quality Coverage']+100*10)/b.loc['0.TW','Quality Coverage'])
    def test_other_factors_and_scores_unchanged(self):
        a=finalize(rows(),self.sample(2.));b=finalize(rows(),self.sample(-2.))
        for k in ('EPS Growth','P/E','Monthly Revenue YoY'):
            self.assertEqual(factor(a,k),factor(b,k))
        columns=[k for k in a.columns if k.startswith(('Risk','Entry','Trend','Industry','20D'))]+['EPS','EPS Growth','P/E','Monthly Revenue YoY']
        pd.testing.assert_frame_equal(a[columns],b[columns])
    def test_frozen_eps_reload(self):
        with tempfile.TemporaryDirectory() as root:
            c=context_for_run('eps-run',root,create=True,builder=lambda:self.sample(-2.))
            d=context_for_run('eps-run',root,create=True,builder=lambda:self.fail('refresh forbidden'))
            pd.testing.assert_frame_equal(finalize(rows(),c),finalize(rows(),d))
    def test_metadata_and_fallback_preserved(self):
        c=context();source=c['official']['sources']['twse_eps'];source['status']='stale_fallback'
        f=finalize(rows(),c);row=f.loc['0.TW']
        self.assertEqual(row['quality_eps_basis'],'cumulative_ytd')
        self.assertEqual(row['quality_eps_period'],source['records']['0']['period'])
        self.assertEqual(row['quality_eps_fetched_at'],source['fetched_at'])
        self.assertEqual(row['quality_eps_source_date'],'2026-10-09')
        self.assertEqual(row['quality_eps_status'],'stale_fallback')
    def test_us_ignores_official_eps(self):
        r=rows();r[0]['Ticker']='AAPL';r[0]['Market']='US';r[0]['EPS']=-3.
        a=finalize(r);b=finalize(r,self.sample(10.))
        self.assertEqual(a.loc['AAPL','Quality Score'],b.loc['AAPL','Quality Score'])

if __name__=='__main__':unittest.main()
