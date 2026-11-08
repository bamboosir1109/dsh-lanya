"""Panel contract for both backends; transport is inert and identities synthetic."""
import json
from unittest import mock
import unittest
import test_panel_autonomy as fixture


class PanelBackendTests(unittest.TestCase):
    setUp = fixture.PanelAutonomyTests.setUp
    write = fixture.PanelAutonomyTests.write
    route = fixture.PanelAutonomyTests.route

    def configure(self, provider):
        if not hasattr(self, 'guard_patch'):
            self.guard_patch = mock.patch.object(self.module.qq_backend_selection, 'runtime_guard', return_value={'ready': False, 'state': 'fixture'})
            self.guard_patch.start(); self.addCleanup(self.guard_patch.stop)
        self.write(self.config, {'dataDirName': 'fixture', 'onebotPort': 8721,
            'qqBackend': {'provider': provider}})
        self.write(self.data / 'lanya-config.json', {'botQQ': 303})
        path = self.napcat / 'framework/config'; path.mkdir(parents=True, exist_ok=True)
        self.write(path / 'onebot11.json', {'network': {'httpServers': [
            {'port': 8721, 'token': 'fixture-napcat-only'}]}})
        path = self.root / 'snowluma/config'; path.mkdir(parents=True, exist_ok=True)
        self.write(path / 'onebot.json', {'networks': {'httpServers': [
            {'name': 'dsh', 'port': 8730, 'accessToken': 'fixture-snow-only'}]}})

    def test_selected_connection_and_catalog_are_secret_free_and_not_runtime_claims(self):
        for provider, port in [('napcat', 8721), ('snowluma', 8730)]:
            self.configure(provider)
            result = self.route('/api/qq/connection')[1]
            self.assertTrue(result['ok']); self.assertEqual(result['backend']['provider'], provider)
            self.assertEqual(result['backend']['base_url'], 'http://127.0.0.1:' + str(port))
            catalog = self.route('/api/qq/backend')[1]['backend']
            self.assertEqual(catalog['selected'], provider); self.assertFalse(catalog['runtime_verified'])
            self.assertEqual(len(catalog['providers']), 2)
            public = json.dumps(result) + json.dumps(catalog)
            self.assertNotIn('fixture-napcat-only', public); self.assertNotIn('fixture-snow-only', public)

    def test_contacts_and_profile_resolve_current_provider_on_each_request(self):
        for provider in ['napcat', 'snowluma']:
            self.configure(provider); seen = []
            def call(action, payload=None, **options):
                connection = options['connection']; seen.append((action, connection.provider, connection.token))
                data = {'user_id': 303, 'nickname': 'fixture'} if action == 'get_login_info' else []
                return {'status': 'ok', 'retcode': 0, 'data': data}
            with mock.patch.object(self.module.qq_backend, 'call', side_effect=call):
                self.assertTrue(self.route('/api/qq/friends')[1]['ok'])
                self.assertTrue(self.route('/api/qq/groups')[1]['ok'])
                self.assertTrue(self.route('/api/qq/profile')[1]['ok'])
            self.assertEqual([x[0] for x in seen], ['get_friend_list', 'get_group_list', 'get_login_info'])
            self.assertTrue(all(x[1] == provider for x in seen))
            self.assertTrue(all(x[2] == ('fixture-napcat-only' if provider == 'napcat' else 'fixture-snow-only') for x in seen))

    def test_missing_snow_config_does_not_route_contacts_via_installed_napcat(self):
        self.configure('snowluma'); (self.root / 'snowluma/config/onebot.json').unlink()
        with mock.patch.object(self.module.qq_backend, 'call') as transport:
            for route in ['/api/qq/friends', '/api/qq/groups', '/api/qq/profile', '/api/qq/connection']:
                self.assertFalse(self.route(route)[1]['ok'])
            transport.assert_not_called()

    def test_invalid_contact_result_is_not_reported_as_an_empty_success(self):
        self.configure('snowluma')
        with mock.patch.object(self.module, '_onebot_call', return_value={'data': {'invalid': True}}):
            self.assertFalse(self.route('/api/qq/friends')[1]['ok'])
            self.assertFalse(self.route('/api/qq/groups')[1]['ok'])

    def test_compatibility_routes_still_use_selected_provider_without_token_prefix(self):
        self.configure('snowluma')
        old = self.route('/api/napcat')[1]
        self.assertEqual(old['napcat']['provider'], 'snowluma')
        self.assertTrue(old['ok']); self.assertNotIn('fixture-snow', json.dumps(old))

    def test_background_status_requires_matching_provider_generation_and_fresh_health(self):
        self.configure('napcat')
        connection = self.module._qq_connection()
        observed = dict(provider='napcat', observed_at=1000000, config_fingerprint=connection.fingerprint,
            verified=True, online=True, good=True, logged_in=True, provider_matches=True, account_matches=True)
        self.assertEqual(self.module.selected_backend_component(observed)['count'], 1)
        for key, value in [('observed_at', 800000), ('config_fingerprint', 'old'), ('provider_matches', False), ('verified', 'true')]:
            with self.subTest(key=key):
                changed = dict(observed); changed[key] = value
                self.assertIsNone(self.module.selected_backend_component(changed)['count'])
        self.configure('snowluma')
        row = self.module.selected_backend_component(observed)
        self.assertEqual(row['backend']['provider'], 'snowluma'); self.assertIsNone(row['count'])

    def test_failed_profile_write_preserves_the_cached_values_and_has_no_false_success(self):
        self.configure('snowluma')
        file = self.napcat / 'qq-profile.json'; self.write(file, {'nickname': 'original'})
        before = file.read_bytes()
        with mock.patch.object(self.module.qq_backend, 'call', side_effect=self.module.qq_backend.BackendError('fixture rejection')):
            result = self.route('/api/qq/profile', {'nickname': 'new name'})[1]
        self.assertFalse(result['ok']); self.assertEqual(result['did'], [])
        self.assertEqual(file.read_bytes(), before)

    def test_invalid_avatar_and_stale_backend_are_refused_before_any_remote_edit(self):
        self.configure('snowluma')
        for body in [{'nickname': 'new name', 'avatarBase64': 'invalid base64!'},
                {'nickname': 'new name', 'backend_fingerprint': 'previous-backend'}, {'nickname': ['invalid']}]:
            with mock.patch.object(self.module.qq_backend, 'call') as transport:
                self.assertFalse(self.route('/api/qq/profile', body)[1]['ok'])
                transport.assert_not_called()

    def test_partial_profile_acceptance_keeps_the_successful_field_and_reports_avatar_failure(self):
        import base64
        self.configure('snowluma')
        def call(action, *args, **kwargs):
            if action == 'set_qq_avatar': raise self.module.qq_backend.BackendError('fixture rejection')
            return {'status': 'ok', 'retcode': 0, 'data': {
                'get_status': {'online': True, 'good': True}, 'get_login_info': {'user_id': 303},
                'get_version_info': {'app_name': 'SnowLuma'}}.get(action, {})}
        with mock.patch.object(self.module.qq_backend, 'call', side_effect=call):
            result = self.route('/api/qq/profile', {'nickname': 'accepted name',
                'avatarBase64': base64.b64encode(b'inert fixture').decode()})[1]
        self.assertTrue(result['ok']); self.assertEqual(result['did'], ['nickname'])
        self.assertEqual(len(result['warn']), 1)
        cache, avatar = self.module._qq_profile_paths(self.module._qq_connection(), 303)
        self.assertEqual(json.loads(cache.read_bytes())['nickname'], 'accepted name')
        self.assertFalse(avatar.exists())
        self.assertFalse((self.napcat / 'qq-avatar.png').exists())

    def test_wrong_provider_or_account_cannot_modify_the_profile(self):
        self.configure('snowluma')
        for wrong in ['provider', 'account']:
            seen = []
            def call(action, *args, **kwargs):
                seen.append(action)
                data = {'get_status': {'online': True, 'good': True},
                    'get_login_info': {'user_id': 404 if wrong == 'account' else 303},
                    'get_version_info': {'app_name': 'NapCat.Onebot' if wrong == 'provider' else 'SnowLuma'}}
                return {'status': 'ok', 'retcode': 0, 'data': data.get(action, {})}
            with mock.patch.object(self.module.qq_backend, 'call', side_effect=call):
                result = self.route('/api/qq/profile', {'nickname': 'must not change'})[1]
            self.assertFalse(result['ok']); self.assertEqual(result['did'], [])
            self.assertFalse(any(action.startswith('set_') for action in seen))

    def test_profile_cache_is_bound_to_provider_and_actual_account(self):
        self.configure('snowluma')
        original = self.napcat / 'qq-profile.json'; self.write(original, {'signature': 'unbound fixture'})
        raw = original.read_bytes()
        cache, avatar = self.module._qq_profile_paths(self.module._qq_connection(), 303)
        cache.parent.mkdir(); self.write(cache, {'signature': 'accepted Snow value'})
        avatar.write_bytes(b'inert cache fixture')
        def read(account=303):
            with mock.patch.object(self.module.qq_backend, 'call', return_value={
                    'status': 'ok', 'retcode': 0, 'data': {'user_id': account, 'nickname': 'fixture'}}):
                return self.route('/api/qq/profile')[1]['profile']
        self.assertEqual(read()['signature'], 'accepted Snow value'); self.assertTrue(read()['hasAvatar'])
        self.assertEqual(read(404)['signature'], ''); self.assertFalse(read(404)['hasAvatar'])
        self.configure('napcat')
        self.assertEqual(read()['signature'], ''); self.assertFalse(read()['hasAvatar'])
        self.assertEqual(original.read_bytes(), raw)

    def test_changed_generation_after_profile_verification_refuses_mutation(self):
        self.configure('snowluma')
        def verified(*args, **kwargs):
            self.configure('napcat')
            return dict(online=True, good=True, logged_in=True, provider_matches=True, account_matches=True)
        with mock.patch.object(self.module.qq_backend, 'health', side_effect=verified), \
                mock.patch.object(self.module.qq_backend, 'call') as transport:
            result = self.route('/api/qq/profile', {'nickname': 'stale edit'})[1]
        self.assertFalse(result['ok']); transport.assert_not_called()

    def test_profile_edit_without_trusted_account_has_no_remote_side_effect(self):
        self.configure('snowluma'); (self.data / 'lanya-config.json').unlink()
        with mock.patch.object(self.module.qq_backend, 'call') as transport:
            result = self.route('/api/qq/profile', {'nickname': 'unbound edit'})[1]
        self.assertFalse(result['ok']); transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
