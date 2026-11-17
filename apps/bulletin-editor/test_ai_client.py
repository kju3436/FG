import copy,json,pathlib,tempfile,unittest
from unittest.mock import patch
import ai_client as client

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        import siwc
        isolated=patch.object(siwc,'LOCAL',pathlib.Path(self.folder.name)/'chatgpt');isolated.start();self.addCleanup(isolated.stop)
        self.state=patch.object(client,'SESSION',None);self.state.start();self.addCleanup(self.state.stop)
        self.path=patch.object(client,'PROFILE',pathlib.Path(self.folder.name)/'ai.json');self.path.start();self.addCleanup(self.path.stop)

    def test_provider_protocols_and_non_openai_keys(self):
        responses={'openai':{'output':[{'content':[{'type':'output_text','text':'{"ok":true}'}]}]},
          'claude':{'content':[{'type':'text','text':'{"ok":true}'}],'stop_reason':'end_turn'},
          'gemini':{'candidates':[{'content':{'parts':[{'text':'{"ok":true}'}]},'finishReason':'STOP'}]},
          'ollama':{'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]},
          'custom':{'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}}
        for provider,response in responses.items():
            with self.subTest(provider=provider),patch.object(client,'_request',return_value=response) as request:
                data=dict(provider=provider,key='different-provider-key',model='my-model',baseUrl='https://example.com/v1')
                r=client.connect(data);self.assertTrue(r['connected']);self.assertEqual(r['provider'],provider)
                url,payload,headers=request.call_args.args
                self.assertNotIn(data['key'],url);self.assertNotIn(data['key'],json.dumps(r))
                self.assertNotIn('주보',json.dumps(payload,ensure_ascii=False))
                if provider=='claude':self.assertEqual(headers['x-api-key'],data['key'])
                if provider=='gemini':self.assertEqual(headers['x-goog-api-key'],data['key'])

    def test_failed_connection_does_not_replace_working_connection(self):
        working={'provider':'ollama','protocol':'openai','baseUrl':'http://localhost:11434/v1','key':'','model':'existing'}
        client.SESSION=copy.deepcopy(working)
        with patch.object(client,'_request',side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):client.connect({'provider':'gemini','key':'key','model':'new'})
        self.assertEqual(client.current(),working)

    def test_auto_models_and_json_response_validation(self):
        with patch.object(client,'_request',side_effect=[{'data':[{'id':'large'},{'id':'mini-test'},{'id':'text-embedding'}]}, {'choices':[{'message':{'content':'{"ok":true}'}}]}]):
            r=client.connect({'provider':'ollama','key':''})
        self.assertEqual(r['model'],'mini-test')
        for raw in ['{"a":1,"a":2}','{"x":NaN}','not json']:
            with self.assertRaises(ValueError):client._parse(raw)

    def test_endpoints_and_secret_persistence(self):
        with self.assertRaises(ValueError):client._validated({'provider':'custom','baseUrl':'http://remote.example/v1','key':'secret'})
        with self.assertRaises(ValueError):client._validated({'provider':'custom','baseUrl':'https://user:secret@example.com/v1','key':'secret'})
        secret='private-test-value'
        with patch.object(client,'_request',return_value={'choices':[{'message':{'content':'{"ok":true}'}}]}):
            result=client.connect({'provider':'ollama','key':secret,'model':'test','remember':True})
        self.assertNotIn(secret,client.PROFILE.read_text());self.assertNotIn(secret,json.dumps(result))
        client.SESSION=None;self.assertEqual(client.current()['key'],secret)
        client.disconnect();self.assertFalse(client.PROFILE.exists());self.assertIsNone(client.current())

if __name__=='__main__':unittest.main()
