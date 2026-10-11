"""Run: python -m unittest discover -s v52_test/tests -p test_v53_monthly_revenue_common.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from v53_monthly_revenue_snapshot import SnapshotStore, build_snapshot
from v53_monthly_revenue_common import audit_month, select_common_complete_month, get_monthly_record
from v53_monthly_revenue_history import import_official_rows, fetch_historical_month, number, parse_historical_html

POOL = [{'Ticker': f'{i}.TW'} for i in range(100)] + [{'Ticker': f'{i}.TWO'} for i in range(100)]
NOW = date(2026,10,11)
def snap(market='TWSE', period='2026-08', n=100, yoy=100, stamp='2026-10-01T00:00:00+00:00'):
    rows=[dict(market=market,period=period,code=str(i),monthly_revenue=0, monthly_revenue_yoy_pct=-2 if i<yoy else None) for i in range(n)]
    return build_snapshot(market,period,rows,source='synthetic_test_only',source_date=None,fetched_at=stamp)
def choose(rows):return select_common_complete_month(rows,POOL,now=NOW)

class Tests(unittest.TestCase):
    def test_august(self):
        self.assertEqual(choose([snap(),snap('TPEX'),snap('TPEX','2026-09',84)])['selected_period'],'2026-08')
    def test_september(self):
        self.assertEqual(choose([snap(),snap('TPEX'),snap(period='2026-09'),snap('TPEX','2026-09')])['selected_period'],'2026-09')
    def test_boundary(self):
        self.assertFalse(audit_month(snap(n=97),POOL)['revenue_coverage_pass'])
        self.assertTrue(audit_month(snap(n=98),POOL)['revenue_coverage_pass'])
    def test_yoy_separate(self):
        result=choose([snap(yoy=97),snap('TPEX')]);self.assertEqual(result['status'],'available');self.assertFalse(result['monthly_revenue_yoy_eligible'])
    def test_age(self):
        result=choose([snap(period='2026-07'),snap('TPEX','2026-07')]);self.assertEqual(result['reason'],'common_month_too_old')
    def test_conflict(self):
        s=snap();r=s['records']+[dict(s['records'][0],monthly_revenue=8)]
        s=build_snapshot('TWSE','2026-08',r,source='test',source_date=None,fetched_at=s['fetched_at'])
        self.assertFalse(audit_month(s,POOL)['revenue_coverage_pass'])
    def test_no_cross_month(self):
        selected=choose([snap(n=98),snap('TPEX'),snap(period='2026-07')]);self.assertIsNone(get_monthly_record(selected,'99.TW')['monthly_revenue'])
    def test_reload(self):
        with tempfile.TemporaryDirectory() as root:
            s=snap();SnapshotStore(root).save(s);loaded=SnapshotStore(root).load_all()[0]
            self.assertEqual(s,loaded)
    def test_interrupted_write(self):
        with tempfile.TemporaryDirectory() as root:
            store=SnapshotStore(root);s=snap();store.save(s)
            with patch('v53_monthly_revenue_snapshot.os.replace',side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):store.save(snap(period='2026-09'))
            self.assertEqual(SnapshotStore(root).load_all(),[s])
    def test_versions(self):
        store=SnapshotStore();store.save(snap());store.save(snap(stamp='2026-10-02T00:00:00+00:00'));store.save(snap(period='2026-09'))
        self.assertEqual(len(store.load_all()),3)
    def test_latest_revision_not_best_coverage(self):
        result=choose([snap(),snap(n=90,stamp='2026-10-02T00:00:00+00:00'),snap('TPEX')]);self.assertEqual(result['status'],'unavailable')
    def test_recent_listing_lookup(self):
        universe=[{'Ticker':'7812.TW'},{'Ticker':'7855.TW'},{'Ticker':'7856.TWO'}];ss=[]
        for market,codes in [('TWSE',['7812','7855']),('TPEX',['7856'])]:
            rows=[dict(market=market,period='2026-08',code=c,monthly_revenue=1,monthly_revenue_yoy_pct=2) for c in codes]
            ss.append(build_snapshot(market,'2026-08',rows,source='synthetic_test_only',source_date=None,fetched_at='2026-10-01T00:00:00+00:00'))
        selected=select_common_complete_month(ss,universe,now=NOW)
        for row in universe:self.assertEqual(get_monthly_record(selected,row['Ticker'])['period'],'2026-08')
    def test_no_reconstruction(self):
        row={'資料年月':'11509','公司代號':'7856','營業收入-當月營收':'1','營業收入-去年同月增減(%)':'2'}
        with self.assertRaises(ValueError):import_official_rows('TPEX','2026-08',[row],fetched_at='2026-10-01T00:00:00+00:00',source='test')
    def test_history_unavailable(self):
        def offline(*a,**kw):raise TimeoutError()
        self.assertEqual(fetch_historical_month('TPEX','2026-08',opener=offline)['status'],'historical_snapshot_unavailable')
    def test_numeric(self):
        for v in ['--','','NaN','Infinity','abc']:self.assertIsNone(number(v))
        self.assertEqual(number('0'),0);self.assertEqual(number('-2'),-2)
    def test_future_excluded(self):
        self.assertEqual(choose([snap(period='2026-10'),snap('TPEX','2026-10')])['status'],'unavailable')


class HistoricalTests(unittest.TestCase):
    def html(self):
        headings=['公司代號','公司名稱','當月營收','上月營收','去年當月營收','上月比較增減(%)','去年同月增減(%)','當月累計營收','去年累計營收','前期比較增減(%)']
        data=['7856','測試公司','100','90','80','11.11','25.00','900','700','28.57','-']
        return ('115年8月份<table><tr>'+''.join('<th>'+v+'</th>' for v in headings)+'</tr><tr>'+''.join('<td>'+v+'</td>' for v in data)+'</tr></table>').encode('cp950')
    def test_historical_html(self):
        rows=parse_historical_html(self.html(),'2026-08')
        s=import_official_rows('TPEX','2026-08',rows,source='test',fetched_at='2026-10-11T00:00:00+00:00')
        self.assertEqual(s['records'][0]['monthly_revenue_yoy_ratio'],0.25)
        self.assertIsNone(s['source_date'])
    def test_wrong_month(self):
        with self.assertRaises(ValueError):parse_historical_html(self.html(),'2026-09')
    def test_changed_header(self):
        b=self.html().decode('cp950').replace('當月营收','nochange').replace('上月營收','unknown').encode('cp950')
        with self.assertRaises(ValueError):parse_historical_html(b,'2026-08')
    def test_fixed_real_denominators(self):
        pools=[{'Ticker':f'{i}.TW'} for i in range(1095)]+[{'Ticker':f'{i}.TWO'} for i in range(893)]
        self.assertFalse(audit_month(snap(n=1073),pools)['revenue_coverage_pass'])
        self.assertTrue(audit_month(snap(n=1074),pools)['revenue_coverage_pass'])
        self.assertFalse(audit_month(snap('TPEX',n=875),pools)['revenue_coverage_pass'])
        self.assertTrue(audit_month(snap('TPEX',n=876),pools)['revenue_coverage_pass'])
    def test_second_page_failure_saves_nothing(self):
        class Response:
            status=200
            def __enter__(this):return this
            def __exit__(this,*args):pass
            def read(this):return self.html()
        calls=[]
        def opener(url,**kw):
            calls.append(url)
            if len(calls)==2:raise TimeoutError()
            return Response()
        store=SnapshotStore()
        result=fetch_historical_month('TPEX','2026-08',opener=opener,store=store)
        self.assertEqual(result['status'],'historical_snapshot_unavailable')
        self.assertEqual(store.load_all(),[])
        self.assertEqual(len(calls),2)


class SafeAccessorTests(unittest.TestCase):
    def selection(self, yoy):
        s=snap(yoy=yoy)
        for r in s['records']:
            r['monthly_revenue_yoy_ratio']=r['monthly_revenue_yoy_pct']/100 if r['monthly_revenue_yoy_pct'] is not None else None
        s=build_snapshot('TWSE','2026-08',s['records'],source='synthetic_test_only',source_date=None,fetched_at=s['fetched_at'])
        return choose([s,snap('TPEX')])
    def assert_no_score(self,row):
        self.assertFalse(row['monthly_revenue_yoy_eligible'])
        for key in ('monthly_revenue_yoy_pct','monthly_revenue_yoy_ratio','eligible_monthly_revenue_yoy_pct','eligible_monthly_revenue_yoy_ratio'):
            self.assertIsNone(row[key])
        self.assertEqual(row['monthly_revenue_yoy_display'],'N/A')
    def test_coverage_failed_raw_preserved_no_score(self):
        import copy
        selection=self.selection(97);before=copy.deepcopy(selection)
        row=get_monthly_record(selection,'0.TW')
        self.assert_no_score(row)
        self.assertEqual(row['raw_monthly_revenue_yoy_pct'],-2)
        self.assertEqual(row['raw_monthly_revenue_yoy_ratio'],-0.02)
        self.assertEqual(selection,before)
        self.assertEqual(audit_month(selection['selected_snapshots']['TWSE'],POOL)['yoy_valid_count'],97)
    def test_coverage_passed_individual_value(self):
        row=get_monthly_record(self.selection(99),'0.TW')
        self.assertTrue(row['monthly_revenue_yoy_eligible'])
        self.assertEqual(row['eligible_monthly_revenue_yoy_pct'],-2)
        self.assertEqual(row['eligible_monthly_revenue_yoy_ratio'],-0.02)
        self.assertEqual(row['monthly_revenue_yoy_pct'],-2)
    def test_coverage_passed_individual_missing(self):
        selection=self.selection(99)
        self.assertTrue(selection['monthly_revenue_yoy_eligible'])
        row=get_monthly_record(selection,'99.TW');self.assert_no_score(row)
        self.assertIsNone(row['raw_monthly_revenue_yoy_pct'])
    def test_unavailable_selection(self):
        self.assert_no_score(get_monthly_record(choose([]),'0.TW'))

if __name__=='__main__':unittest.main()
