import unittest,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from v52_cache import Store,DataUnavailable,InvalidMarketData,network_budget
import v52_data as data
from test_integration import synthetic

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'cache.sqlite3';self.now=10000
        self.store=Store(self.path,min_interval=0,clock=lambda:self.now)
    def test_per_symbol_reuse_across_pool_changes_and_restart(self):
        loader=Mock(side_effect=lambda t,p:synthetic())
        with patch.object(data,'get_store',return_value=self.store),patch.object(data,'_load_history',loader):
            data.history_batch(['A','B']);data.history_batch(['B','A','C'])
        self.assertEqual(loader.call_count,3)
        restarted=Store(self.path,min_interval=0,clock=lambda:self.now)
        with patch.object(data,'get_store',return_value=restarted),patch.object(data,'_load_history',loader):
            h=data.history_batch(['C','A'])
        self.assertEqual(loader.call_count,3);self.assertEqual(h['A'].attrs['cache_status'],'fresh')
    def test_rate_limit_shared_cooldown_and_expire_cannot_bypass(self):
        loader=Mock(side_effect=RuntimeError('429 Too Many Requests'))
        with self.assertRaises(DataUnavailable):self.store.get('yahoo','history:A',loader,60)
        self.store.expire('history:')
        restarted=Store(self.path,min_interval=0,clock=lambda:self.now)
        with self.assertRaises(DataUnavailable):restarted.get('yahoo','fund:B',loader,60)
        self.assertEqual(loader.call_count,1)
        self.now+=1801
        r=restarted.get('yahoo','fund:B',lambda:{'EPS':3},60)
        self.assertEqual(r.status,'downloaded')
    def test_stale_fallback_preserves_timestamp_and_fails_closed(self):
        self.store.put('history:A',synthetic(),fetched=self.now-100)
        r=self.store.get('yahoo','history:A',Mock(side_effect=RuntimeError('429')),ttl=60,max_stale=200)
        self.assertEqual(r.status,'stale');self.assertEqual(r.fetched_at,self.now-100)
        self.now+=201
        with self.assertRaises(DataUnavailable):self.store.get('yahoo','history:A',lambda:None,60,max_stale=200)
    def test_expired_budget_allows_cache_but_no_network(self):
        self.store.put('cached',{'x':1});loader=Mock()
        with network_budget(-1):
            self.assertEqual(self.store.get('yahoo','cached',loader,60).value,{'x':1})
            with self.assertRaises(DataUnavailable):self.store.get('yahoo','missing',loader,60)
        loader.assert_not_called()
    def test_empty_data_not_cached_and_singleflight(self):
        with self.assertRaises(DataUnavailable):self.store.get('yahoo','empty',lambda:pd.DataFrame(),60)
        self.assertIsNone(self.store.read('empty'))
        loader=Mock(return_value={'EPS':5})
        with ThreadPoolExecutor(4) as pool:
            rs=list(pool.map(lambda _:self.store.get('other','same',loader,60),range(4)))
        self.assertEqual(loader.call_count,1);self.assertEqual(len(rs),4)
    def test_non_rate_errors_bounded_and_no_infinite_retry(self):
        loader=Mock(side_effect=TimeoutError('timeout'))
        for i in range(10):
            with self.assertRaises(DataUnavailable):self.store.get('yahoo',str(i),loader,60)
        self.assertEqual(loader.call_count,3)
    def test_invalid_symbols_do_not_block_healthy_symbol_or_restart(self):
        for i in range(4):
            with self.assertRaises(DataUnavailable):
                self.store.get('yahoo',f'bad:{i}',Mock(side_effect=InvalidMarketData('missing close')),60)
        restarted=Store(self.path,min_interval=0,clock=lambda:self.now)
        self.assertEqual(restarted.get('yahoo','good',lambda:{'price':100},60).status,'downloaded')
        loader=Mock()
        with self.assertRaisesRegex(DataUnavailable,'此標的暫停重試.*missing close'):
            restarted.get('yahoo','bad:0',loader,60)
        loader.assert_not_called()
    def test_missing_close_reports_dates_without_filling(self):
        from v52_engine import clean_history
        h=synthetic();date=h.index[20];h.loc[date,'Close']=float('nan')
        with self.assertRaisesRegex(ValueError,str(date.date())):clean_history(h)
        self.assertTrue(pd.isna(h.loc[date,'Close']))
    def test_snapshot_is_separate_from_adjusted_history(self):
        from v52_official import normalize,roc_date
        r=normalize([{'Code':'2330','Date':'1150924','ClosingPrice':'1,000'}],'twse_quotes').iloc[0]
        self.assertEqual(r['官方收盤價'],1000);self.assertEqual(r['行情日期'],'2026-09-24')
        self.assertIsNone(roc_date('broken'))
        self.assertNotIn('RSI14',r.index)
