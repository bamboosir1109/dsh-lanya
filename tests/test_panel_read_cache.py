"""Bounded metadata cache and source access regressions; synthetic files only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from panel_read_cache import ReadCache
import panel_activity as activity
import test_panel_activity as fixture


class ReadCacheTests(unittest.TestCase):
    def test_single_flight_results_are_isolated_and_expire(self):
        stamp = [1]; cache = ReadCache(clock=lambda:stamp[0]); entered=threading.Event(); release=threading.Event(); calls=[]
        def build():
            calls.append(1);entered.set();self.assertTrue(release.wait(3));return {'rows':[{'n':1}]}
        with ThreadPoolExecutor(max_workers=3) as pool:
            one=pool.submit(cache.get,'fixture',build);self.assertTrue(entered.wait(3))
            two=pool.submit(cache.get,'fixture',build);release.set();a,b=one.result(),two.result()
        a['rows'][0]['n']=99;self.assertEqual(b['rows'][0]['n'],1);self.assertEqual(len(calls),1)
        stamp[0]+=6;cache.get('fixture',build);self.assertEqual(len(calls),2)

    def test_errors_and_oversized_values_never_leave_cached_success(self):
        cache=ReadCache(max_bytes=20)
        with self.assertRaises(ValueError):cache.get('fixture',lambda:(_ for _ in ()).throw(ValueError('fixture')))
        self.assertEqual(cache.get('fixture',lambda:{'ok':True}),{'ok':True})
        calls=[]
        def large():calls.append(1);return {'large':'x'*100}
        cache.get('large',large);cache.get('large',large);self.assertEqual(len(calls),2)

    def test_keys_and_entry_count_bound_eviction(self):
        cache=ReadCache(entries=1);calls=[]
        for key in ['one','two','one']:cache.get(key,lambda:calls.append(key) or {'key':key})
        self.assertEqual(calls,['one','two','one'])

    def test_waiter_timeout_does_not_cancel_or_duplicate_the_current_builder(self):
        cache=ReadCache(max_wait=0);entered=threading.Event();release=threading.Event();calls=[]
        def build():calls.append(1);entered.set();self.assertTrue(release.wait(3));return {'ok':True}
        with ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(cache.get,'fixture',build);self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(TimeoutError):cache.get('fixture',build)
            finally:release.set()
            self.assertEqual(first.result(),{'ok':True})
        self.assertEqual(len(calls),1);self.assertEqual(cache.get('fixture',build),{'ok':True})


class NativeProjectionCacheTests(unittest.TestCase):
    def setUp(self):
        self.base=fixture.PanelActivityTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.call('bili_video_info',{'bvid':fixture.BV,'title':'Public fixture'},args={'bvid':fixture.BV})
        self.base.write();self.cache=mock.patch.object(activity,'_TOOL_CACHE',ReadCache());self.cache.start();self.addCleanup(self.cache.stop)

    def read(self,cutoff=0,**kwargs):
        return activity._native(self.base.path,'fixture-session',str(self.base.work),'Fixture','primary',cutoff,fixture.NOW,**kwargs)

    def test_shared_projection_reuses_scan_across_windows_and_revalidates_append(self):
        with mock.patch.object(activity,'_native_events',wraps=activity._native_events) as scan:
            first=self.read();second=self.read(cutoff=fixture.NOW-500)
            self.assertEqual(len(first),1);self.assertEqual(second,[]);self.assertEqual(scan.call_count,1)
            first[0]['video']['title']='changed caller'
            self.assertEqual(self.read()[0]['video']['title'],'Public fixture')
            with self.base.path.open('ab') as stream:stream.write(b'broken\n')
            with self.assertRaises(activity.ActivityUnavailable):self.read()
            self.assertEqual(scan.call_count,2)

    def test_changed_owner_and_lost_read_access_do_not_reuse_cache(self):
        self.read()
        with self.assertRaises(activity.ActivityUnavailable):
            activity._native(self.base.path,'foreign-session',str(self.base.work),'Fixture','primary',0,fixture.NOW)
        original=Path.open
        def deny(path,*args,**kwargs):
            if path==self.base.path:raise PermissionError('synthetic revoked access')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'open',deny):
            with self.assertRaises(PermissionError):self.read()


if __name__=='__main__':unittest.main()
