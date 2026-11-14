"""Isolated provider files and loopback HTTP; no real QQ or private credentials."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qq_backend as backend


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = {'dataDirName': 'fixture', 'onebotPort': 8721,
            'qqBackend': {'provider': 'napcat'}}
        self.write('napcat/framework/config/onebot11.json', {'network': {
            'httpServers': [{'host': '127.0.0.1', 'port': 8721, 'token': 'fixture-napcat'}],
            'websocketClients': [{'url': 'ws://127.0.0.1:8644?access_token=fixture-secret', 'token': 'fixture-ws'}]}})
        self.write('snowluma/config/onebot.json', {'networks': {
            'httpServers': [{'name': 'http-default', 'host': '127.0.0.1', 'port': 8730, 'accessToken': 'fixture-snow'}],
            'wsClients': [{'name': 'dsh', 'url': 'ws://127.0.0.1:8644', 'accessToken': 'fixture-ws'}]}})

    def write(self, relative, value):
        path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8'); return path

    def resolve(self, **options):
        return backend.resolve(self.project, self.root, **options)

    def test_shared_configuration_vectors(self):
        vectors = json.loads((Path(__file__).parent / 'fixtures/qq_backend/contracts.json').read_text(encoding='utf-8'))
        for case in vectors['cases']:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = dict(vectors['project'], **case.get('project', {}), qqBackend={
                    'provider': case['provider'], 'providers': {case['provider']: case.get('settings', {})}})
                files = dict(vectors['files'], **case.get('files', {}))
                if 'row' in case:
                    row = dict(files['snowluma/config/onebot.json']['networks']['httpServers'][0], **case['row'])
                    files['snowluma/config/onebot.json'] = {'networks': {'httpServers': [row]}}
                for name, value in files.items():
                    if value is None: continue
                    path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(value), encoding='utf-8')
                if case.get('error'):
                    with self.assertRaises(backend.BackendError): backend.resolve(project, root)
                else:
                    value = backend.resolve(project, root)
                    self.assertEqual({'base': value.base_url, 'token': value.token}, case['expected'])

    def test_default_napcat_and_explicit_snowluma_use_only_their_own_credential(self):
        napcat = self.resolve(); self.assertEqual(napcat.base_url, 'http://127.0.0.1:8721')
        self.assertEqual(napcat.token, 'fixture-napcat')
        self.project['qqBackend']['provider'] = 'snowluma'
        snow = self.resolve(); self.assertEqual(snow.base_url, 'http://127.0.0.1:8730')
        self.assertEqual(snow.token, 'fixture-snow'); self.assertEqual(snow.webui_url, 'http://127.0.0.1:5099')
        self.assertEqual(self.resolve(provider='napcat').provider, 'napcat')
        self.assertEqual(self.project['qqBackend']['provider'], 'snowluma')

    def test_napcat_public_endpoints_respect_official_flags_and_reject_conflicts(self):
        self.write('napcat/framework/config/onebot11.json', {'network': {
            'httpServers': [{'enable': True, 'port': 8721}],
            'websocketClients': [{'enable': False, 'url': 'ws://127.0.0.1:8644'},
                {'enable': True, 'url': 'ws://127.0.0.1:8645'}]}})
        value = self.resolve().public()
        self.assertEqual([row['url'] for row in value['wsClient']], ['ws://127.0.0.1:8645'])
        self.write('napcat/framework/config/onebot11.json', {'network': {
            'httpServers': [{'enable': True, 'port': 8721}],
            'websocketClients': [{'enable': False, 'enabled': True}]}})
        with self.assertRaises(backend.BackendError): self.resolve().public()

    def test_unknown_invalid_missing_and_corrupt_selected_config_never_fall_back(self):
        for value in ['unknown', '', False, None, ['snowluma']]:
            with self.subTest(value=value):
                self.project['qqBackend']['provider'] = value
                with self.assertRaises(backend.BackendError): self.resolve()
        self.project['qqBackend']['provider'] = 'snowluma'
        path = self.root / 'snowluma/config/onebot.json'
        for raw in ['{', '[]', 'null']:
            path.write_text(raw, encoding='utf-8')
            with self.assertRaises(backend.BackendError): self.resolve()
        path.unlink()
        with self.assertRaises(backend.BackendError): self.resolve()

    def test_account_overlay_replaces_whole_adapter_and_snapshot_excludes_global(self):
        self.project['qqBackend']['provider'] = 'snowluma'
        self.write('fixture/lanya-config.json', {'botQQ': '303'})
        self.write('snowluma/config/onebot_303.json', {'networks': {'httpServers': [
            {'name': 'http-default', 'port': 8732, 'path': '/onebot', 'accessToken': 'fixture-account'}]}})
        connection = self.resolve(); self.assertEqual(connection.token, 'fixture-account')
        self.assertEqual(connection.base_url, 'http://127.0.0.1:8732/onebot')
        self.assertEqual(len(connection.config_files), 2)
        self.write('snowluma/config/onebot_303.json', {'mode': 'snapshot', 'networks': {'httpServers': [
            {'name': 'different', 'port': 8733, 'accessToken': 'fixture-snapshot'}]}})
        self.assertEqual(self.resolve().token, 'fixture-snapshot')
        self.write('snowluma/config/onebot_303.json', {'networks': {'httpServers': [
            {'name': 'http-default', 'port': 8734}]}})
        self.assertEqual(self.resolve().token, '')  # never inherit the global token

    def test_corrupt_account_override_and_ambiguous_adapters_are_failures(self):
        self.project['qqBackend']['provider'] = 'snowluma'
        path = self.write('snowluma/config/onebot_303.json', {})
        path.write_text('{', encoding='utf-8')
        with self.assertRaises(backend.BackendError): self.resolve(expected_account='303')
        path.unlink()
        self.write('snowluma/config/onebot.json', {'networks': {'httpServers': [
            {'name': 'first', 'port': 8730}, {'name': 'second', 'port': 8731}]}})
        with self.assertRaises(backend.BackendError): self.resolve()
        self.project['qqBackend']['providers'] = {'snowluma': {'httpServer': 'second'}}
        self.assertEqual(self.resolve().base_url, 'http://127.0.0.1:8731')

    def test_disabled_adapters_runtime_port_and_legacy_snow_shape(self):
        self.project['qqBackend']['provider'] = 'snowluma'
        self.write('snowluma/config/onebot.json', {'httpServers': [
            {'port': 8730, 'enabled': False}, {'port': 8731, 'host': '0.0.0.0'}]})
        self.write('snowluma/config/runtime.json', {'webuiPort': 5097})
        self.assertEqual(self.resolve().base_url, 'http://127.0.0.1:8731')
        self.assertEqual(self.resolve().webui_url, 'http://127.0.0.1:5097')
        self.project['qqBackend']['providers'] = {'snowluma': {'webuiPort': 5098}}
        self.assertEqual(self.resolve().webui_url, 'http://127.0.0.1:5098')

    def test_public_metadata_and_repr_never_contain_credentials(self):
        connection = self.resolve(); public = json.dumps(connection.public())
        for secret in ['fixture-napcat', 'fixture-secret', 'fixture-ws']:
            self.assertNotIn(secret, public); self.assertNotIn(secret, repr(connection))
        self.assertIn('token_configured', public)
        self.assertEqual(connection.public()['wsClient'][0]['url'], 'ws://127.0.0.1:8644')

    def test_urls_bad_ports_and_bad_identity_do_not_escape_local_configuration(self):
        for url in ['http://example.test', 'http://127.0.0.1:0', 'file:///fixture',
                'http://secret@localhost:3000', 'http://localhost:3000?access_token=secret',
                'http://localhost:3000/../outside', 'http://localhost:3000/%2e%2e',
                'http://localhost:3000/\\other', 'http://localhost:3000/\nsecret']:
            with self.subTest(url=url):
                with self.assertRaises(backend.BackendError): backend.local_url(url)
        self.project['qqBackend']['provider'] = 'snowluma'
        for account in ['../escape', False, -1, '0', '123/other']:
            with self.subTest(account=account):
                with self.assertRaises(backend.BackendError): self.resolve(expected_account=account)

    def test_local_http_uses_selected_path_and_token_and_validates_business_result(self):
        requests = []; state = {'body': {'status': 'ok', 'retcode': 0, 'data': {'online': True}}}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append((self.path, self.headers.get('Authorization'),
                    json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                raw = json.dumps(state['body']).encode(); self.send_response(200)
                self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.project['qqBackend']['provider'] = 'snowluma'
        self.write('snowluma/config/onebot.json', {'networks': {'httpServers': [
            {'name': 'fixture', 'port': server.server_port, 'path': '/onebot', 'accessToken': 'fixture-bound-token'}]}})
        connection = self.resolve()
        self.assertTrue(backend.call('get_status', connection=connection)['data']['online'])
        self.assertEqual(requests, [('/onebot/get_status', 'Bearer fixture-bound-token', {})])
        for body in [{'status': 'failed', 'retcode': 100, 'message': 'private diagnostic'},
                {'status': 'ok', 'retcode': False}, {'status': 'ok', 'retcode': '0'}, []]:
            state['body'] = body
            with self.assertRaises(backend.BackendError) as error: backend.call('get_status', connection=connection)
            self.assertNotIn('private diagnostic', str(error.exception))
        state['body'] = {'status': 'failed', 'retcode': 100, 'wording': 'failed to resolve UID for UIN 101'}
        with self.assertRaises(backend.BackendActionRejected) as error:
            backend.call('get_friend_msg_history', {'user_id': 101}, connection=connection)
        self.assertEqual(error.exception.reason, 'peer_identity_unresolved')
        self.assertEqual(str(error.exception), 'OneBot action rejected')
        with self.assertRaises(backend.BackendActionRejected) as error:
            backend.call('get_status', connection=connection)
        self.assertEqual(error.exception.reason, 'backend_rejected')
        before = len(requests)
        with self.assertRaises(backend.BackendError): backend.call('../other', connection=connection)
        self.assertEqual(len(requests), before)


if __name__ == '__main__':
    unittest.main()
