"""Nested availability acceptance; no real local server or private payload."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import panel_health as health


class PanelHealthTests(unittest.TestCase):
    def budget(self):
        base=dict(schema='unified-budget/v2',availability='available',currency='CNY',numeric_scale=1,
                  basis='owner_confirmed_numeric_cny_estimate')
        daily=dict(base,schema='daily-spending/v2',timezone='Asia/Shanghai',today='2026-11-04',
                   coverage=dict(owner_verified=True,owner_verified_scopes=2,
                                 scope='current_mapped_main_agent_native_turns',reasons=[]),
                   days=[dict(date='2026-11-04',status='partial',total=0.9,known_total_cny=0.9)])
        return dict(ok=True,budget=dict(base,daily_spending=daily))

    def test_top_level_success_cannot_mask_unavailable_daily_statistics(self):
        value=self.budget();self.assertEqual(health.validate('/budget',value),'healthy')
        value['budget']['daily_spending']['availability']='unavailable'
        with self.assertRaises(ValueError):health.validate('/budget',value)
        self.assertEqual(health.validate('/budget/summary',value),'healthy')

    def test_state_accepts_the_producers_timezone_aware_iso_timestamp(self):
        value=dict(at='2026-11-04T11:27:43.8352322+08:00',components=[],plugins=[],memory={})
        self.assertEqual(health.validate('/state',value),'healthy')
        for stamp in [None,'invalid','2026-11-04T11:27:43']:
            with self.assertRaises(ValueError):health.validate('/state',dict(value,at=stamp))

    def test_unknown_amounts_are_degraded_and_unit_conversion_is_refused(self):
        value=self.budget();value['budget']['daily_spending']['days'][0]['total']=None
        self.assertEqual(health.validate('/budget',value),'degraded')
        for patch in [dict(currency='USD'),dict(numeric_scale=7.2),dict(numeric_scale=True),dict(basis='invoice')]:
            wrong=copy.deepcopy(value);wrong['budget']['daily_spending'].update(patch)
            with self.assertRaises(ValueError):health.validate('/budget',wrong)

    def test_verified_empty_browsing_is_valid_but_unavailable_owner_is_not(self):
        value=dict(ok=True,bili=dict(mainAgent=dict(status='ok',recent_videos=[],replies=[],coverage=dict(owner_verified_scopes=2))))
        self.assertEqual(health.validate('/bili',value),'healthy')
        value['bili']['mainAgent']['status']='unavailable'
        with self.assertRaises(ValueError):health.validate('/bili',value)

    def test_empty_diary_and_degraded_facts_are_not_false_failures(self):
        self.assertEqual(health.validate('/memory/diary',dict(ok=True,diary=dict(status='empty'))),'healthy')
        self.assertEqual(health.validate('/activity/journal',dict(status='ok',days=[],freshness=dict(status='degraded'))),'degraded')
        with self.assertRaises(ValueError):health.validate('/memory',dict(ok=True,memory=dict(entries=[],availability=dict(status='error'))))

    def test_backend_and_profile_contracts_do_not_accept_only_a_top_level_success(self):
        for route in ['/qq/backend', '/qq/backend/status', '/qq/backend/health', '/qq/profile', '/qq/friends', '/qq/groups']:
            with self.subTest(route=route), self.assertRaises(ValueError): health.validate(route, {'ok': True})
        status={'ok':True,'backend':{'provider':'snowluma','verified':False,'status':'unconfirmed'}}
        self.assertEqual(health.validate('/qq/backend/status',status),'degraded')
        status['backend'].update(verified=True,status='online')
        self.assertEqual(health.validate('/qq/backend/status',status),'healthy')
        profile={'ok':True,'profile':{'user_id':303,'nickname':'fixture'}}
        self.assertEqual(health.validate('/qq/profile',profile),'healthy')

    def test_origins_reject_external_redirect_targets_credentials_and_paths(self):
        for origin in ['http://127.0.0.1:19388','http://localhost:19389','http://[::1]:19388']:
            self.assertEqual(health.local_origin(origin),origin)
        for origin in ['https://localhost:19388','http://example.invalid:80','http://127.0.0.1:19388/api',
                       'http://user:password@localhost:19388','http://127.0.0.1:19388?token=fixture']:
            with self.assertRaises(ValueError):health.local_origin(origin)

    def test_diagnostics_never_echo_private_bodies_or_exception_text(self):
        opener=mock.Mock();opener.open.side_effect=ValueError('PRIVATE ACCOUNT SECRET')
        with mock.patch.object(health.urllib.request,'build_opener',return_value=opener):
            result=health.check('http://127.0.0.1:19388',routes=['/bili'])
        self.assertFalse(result['ok']);self.assertEqual(result['checks'][0]['state'],'unavailable')
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(opener.open.call_count,1)


if __name__=='__main__':unittest.main()
