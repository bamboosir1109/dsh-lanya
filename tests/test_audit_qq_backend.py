"""Read-only diagnostic routes with synthetic files; no network or QQ sends."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))


class AuditBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qq-audit-fixture-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.data=self.root/'fixture';self.data.mkdir()
        self.write('config/project.json',{'dataDirName':'fixture','qqBackend':{'provider':'snowluma'}})
        self.write('snowluma/config/onebot.json',{'networks':{
            'httpServers':[{'name':'http','port':8730,'accessToken':'fixture-http-auth'}],
            'wsClients':[{'name':'dsh','url':'ws://127.0.0.1:8644','accessToken':'fixture-ws-auth'}]}})
        self.write('fixture/lanya-config.json',{'botQQ':303,'fanGroupQQ':202,'adminQQ':[101]})
        self.write('fixture/lanya-tunables.json',{'GroupId':202})
        environment=mock.patch.dict(os.environ,{'LANYA_WORKDIR':str(self.root),'LANYA_PROJECT_CONFIG':str(self.root/'config/project.json')})
        environment.start();self.addCleanup(environment.stop)
        import project_conf
        project_conf.load(reload=True)
        spec=importlib.util.spec_from_file_location('audit_qq_isolated',SCRIPTS/'audit_qq.py')
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        patch=mock.patch.object(self.module,'plugin_cfg',return_value={'accessToken':'fixture-ws-auth','dmPolicy':'open',
            'allowAllUsers':True,'adminUsers':['101'],'interimMessages':False})
        self.plugin=patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(self.module,'port_up',return_value=True);patch.start();self.addCleanup(patch.stop)

    def write(self,name,value):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value),encoding='utf-8')

    def run_audit(self):
        status=dict(online=True,good=True,logged_in=True,provider_matches=True,account_matches=True)
        output=io.StringIO()
        with mock.patch.object(self.module.qq_backend,'health',return_value=status) as health, \
                mock.patch.object(self.module.qq_backend,'call',return_value={'data':[{'group_id':202}]}) as transport,redirect_stdout(output):
            result=self.module.main()
        return result,output.getvalue(),health,transport

    def test_selected_provider_and_separate_http_ws_credentials_are_valid_without_echoing_secrets_or_ids(self):
        _,text,health,transport=self.run_audit()
        self.assertEqual(health.call_args.args[0].provider,'snowluma')
        self.assertEqual(transport.call_args.kwargs['connection'].provider,'snowluma')
        self.assertIn('SnowLuma',text)
        for private in ['fixture-http-auth','fixture-ws-auth','303','202','101',str(self.root)]: self.assertNotIn(private,text)
        self.assertTrue(next(ok for name,ok,_ in self.module.ROWS if name=='反向 WS 与插件凭据'))

    def test_admin_only_open_policy_is_not_reported_as_all_friends_allowed(self):
        self.plugin.return_value['allowAllUsers']=False
        self.run_audit()
        self.assertFalse(next(ok for name,ok,_ in self.module.ROWS if name=='私聊: 所有好友都能聊'))

    def test_missing_selected_configuration_never_probes_a_different_installed_backend(self):
        (self.root/'snowluma/config/onebot.json').unlink()
        result,_,health,transport=self.run_audit()
        self.assertEqual(result,1);health.assert_not_called();transport.assert_not_called()

    def test_malformed_reverse_endpoint_reports_failure_without_crashing_or_echoing_it(self):
        for url in ['ws://[invalid', 'ws://127.0.0.1:notaport', None,
                'ws://fixture-private-user@127.0.0.1:8644', 'ws://example.test:8644']:
            with self.subTest(kind=type(url).__name__):
                self.write('snowluma/config/onebot.json', {'networks': {
                    'httpServers':[{'name':'http','port':8730,'accessToken':'fixture-http-auth'}],
                    'wsClients':[{'name':'dsh','url':url,'accessToken':'fixture-ws-auth'}]}})
                _,text,_,_=self.run_audit()
                self.assertNotIn('fixture-private-user',text)
                self.assertFalse(next(ok for name,ok,_ in self.module.ROWS if name=='反向 WS 与插件凭据'))
                self.assertFalse(next(ok for name,ok,_ in self.module.ROWS if name=='反向 WS 端口'))

    def test_reverse_default_port_is_resolved(self):
        self.write('snowluma/config/onebot.json', {'networks': {
            'httpServers':[{'name':'http','port':8730}],
            'wsClients':[{'name':'dsh','url':'wss://localhost','accessToken':'fixture-ws-auth'}]}})
        with mock.patch.object(self.module,'port_up',return_value=True) as probe:
            self.run_audit()
        self.assertIn(mock.call(443,'localhost'),probe.call_args_list)


if __name__=='__main__':unittest.main()
