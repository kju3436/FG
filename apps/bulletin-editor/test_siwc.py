"""Local fixtures only: never opens a browser, contacts OpenAI or reads real tokens."""
import base64,copy,hashlib,io,json,pathlib,tempfile,time,unittest
from urllib.parse import parse_qs,urlparse
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric import rsa
import siwc as s
import ai_client

class SignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.jwk=json.loads(s.jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()));cls.jwk.update(kid='test-key',alg='RS256')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for name,value in [('LOCAL',pathlib.Path(self.tmp.name)),('PENDING',{}),('PROGRESS',{'phase':'idle','message':''})]:
            p=patch.object(s,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(s,'eligibility',return_value={'allowed':True,'clientId':'','route':'open_source_published','sourceUrl':'https://example.invalid/source'});p.start();self.addCleanup(p.stop)
        self.discovery={'issuer':s.ISSUER,**{k:s.ISSUER+'/'+k for k in ('authorization_endpoint','token_endpoint','jwks_uri','revocation_endpoint')}}

    def authorize(self,scope=s.SCOPES,client='oaiapp_test',subject='test-subject'):
        with patch.object(s,'_discovery',return_value=self.discovery),patch.object(s.webbrowser,'open',return_value=True) as opened:
            s.begin(8766)
        params={k:v[0] for k,v in parse_qs(urlparse(opened.call_args.args[0]).query).items()};state=params['state']
        claims={'iss':s.ISSUER,'aud':client,'sub':subject,'iat':int(time.time()),'exp':int(time.time())+3600,'nonce':params['nonce'],'email':'test@example.invalid'}
        token=s.jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'test-key'})
        tokens={'id_token':token,'scope':scope,'access_token':'fixture-access','refresh_token':'fixture-refresh','expires_in':3600,'token_type':'Bearer'}
        def reply(url,payload=None,form=False,token=None):
            if url==self.discovery['token_endpoint']:
                self.assertEqual(payload['client_id'],client);self.assertEqual(payload['redirect_uri'],params['redirect_uri'])
                self.assertEqual(payload['resource'],s.RESOURCE)
                challenge=base64.urlsafe_b64encode(hashlib.sha256(payload['code_verifier'].encode()).digest()).decode().rstrip('=')
                self.assertEqual(challenge,params['code_challenge']);return tokens
            if url==self.discovery['jwks_uri']:return {'keys':[self.jwk]}
            if url==s.RESOURCE+'/models':return {'models':[{'slug':'fixture-model','display_name':'Fixture','visibility':'list'}]}
            raise AssertionError(url)
        with patch.object(s,'_json',side_effect=reply):result=s.finish({'state':state,'code':'one-use-code','client_id':client})
        return result,params

    def test_real_signature_pkce_registration_and_encrypted_storage(self):
        result,params=self.authorize();self.assertTrue(result['connected'])
        self.assertEqual(params['client_id'],'dynamic_agent_client');self.assertEqual(params['code_challenge_method'],'S256')
        self.assertNotIn('fixture-access',(s.LOCAL/'accounts.enc').read_text())
        self.assertEqual(s.current()['model'],'fixture-model');self.assertNotIn('fixture-refresh',json.dumps(s.status()))
        with self.assertRaises(ValueError):s.finish({'state':params['state'],'code':'replay','client_id':'oaiapp_test'})

    def test_identity_only_never_enables_inference(self):
        r,_=self.authorize(scope='openid profile email');self.assertFalse(r['connected']);self.assertIsNone(s.current())
        self.assertEqual(s.status()['progress']['phase'],'needsPlan')
        with self.assertRaises(ValueError):s._access()

    def test_invalid_state_and_signature_preserve_active_account(self):
        self.authorize();before=copy.deepcopy(s._read())
        with self.assertRaises(ValueError):s.finish({'state':'not-issued','code':'bad'})
        self.assertEqual(s._read(),before)
        claims={'iss':s.ISSUER,'aud':'oaiapp_test','sub':'s','exp':int(time.time())+200,'iat':int(time.time()),'nonce':'wrong'}
        token=s.jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'test-key'})
        with patch.object(s,'_json',return_value={'keys':[self.jwk]}):
            with self.assertRaises(ValueError):s._verify(token,'oaiapp_test','expected',self.discovery)

    def test_persisted_host_and_returning_client_are_reused(self):
        _,params=self.authorize();aid=s.status()['active']['accountId'];host=params['ext_agent_host_id']
        with patch.object(s,'_discovery',return_value=self.discovery),patch.object(s.webbrowser,'open',return_value=True) as opened:s.begin(8767,aid)
        p=parse_qs(urlparse(opened.call_args.args[0]).query)
        self.assertEqual(p['client_id'],['oaiapp_test']);self.assertEqual(p['ext_agent_host_id'],[host]);self.assertNotIn('agent_name_hint',p)
        self.assertIn('id_token_hint',p)

    def test_refresh_rotation_uses_issued_client_and_serialized_store(self):
        self.authorize();data=s._read();r=data['accounts'][data['active']];r['expires_at']=0;s._write(data)
        with patch.object(s,'_discovery',return_value=self.discovery),patch.object(s,'_json',return_value={'access_token':'new-access','refresh_token':'new-refresh','expires_in':3600}) as request:
            access,_=s._access()
        self.assertEqual(access,'new-access');self.assertEqual(request.call_args.args[1]['client_id'],'oaiapp_test')
        self.assertEqual(s._read()['accounts'][data['active']]['refresh_token'],'new-refresh')

    def test_complete_requires_terminal_event_and_uses_public_streaming_contract(self):
        chunks=b'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\ndata: {"type":"response.completed"}\n\n'
        with patch.object(s,'_access',return_value=('secret','fixture-model')),patch.object(s,'_open',return_value=io.BytesIO(chunks)) as call:
            self.assertEqual(s.complete('instruction','input'),'{"ok":true}')
        url,payload=call.call_args.args;self.assertEqual(url,s.RESOURCE+'/responses');self.assertTrue(payload['stream']);self.assertFalse(payload['store'])
        self.assertIsInstance(payload['input'],list);self.assertNotIn('max_output_tokens',payload)
        for bad in [b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',b'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n']:
            with patch.object(s,'_access',return_value=('secret','fixture-model')),patch.object(s,'_open',return_value=io.BytesIO(bad)):
                with self.assertRaises(ValueError):s.complete('i','t')

    def test_logout_revokes_and_keeps_only_account_mapping(self):
        self.authorize()
        with patch.object(s,'_discovery',return_value=self.discovery),patch.object(s,'_open',return_value=io.BytesIO(b'')) as request:r=s.signout()
        self.assertTrue(r['revoked']);self.assertIsNone(s.current());self.assertTrue(s.prefers_chatgpt())
        self.assertEqual(request.call_args.args[1]['token_type_hint'],'refresh_token')
        record=next(iter(s._read()['accounts'].values()));self.assertEqual(record['refresh_token'],'');self.assertEqual(record['client_id'],'oaiapp_test')

    def test_unpublished_app_never_opens_browser(self):
        with patch.object(s,'eligibility',return_value={'allowed':False,'message':'not published'}),patch.object(s.webbrowser,'open') as opened:
            with self.assertRaises(ValueError):s.begin(8766)
        opened.assert_not_called()

    def test_provider_bridge_uses_oauth_without_api_key_or_fallback(self):
        self.authorize()
        with patch.object(s,'complete',return_value='{"ok":true}') as call,patch.object(ai_client,'_text') as api:
            self.assertEqual(ai_client.complete_json('i',{}),{'ok':True})
        api.assert_not_called();call.assert_called_once()
        with patch.object(s,'complete',side_effect=ValueError('quota')),patch.object(ai_client,'_text') as api:
            with self.assertRaises(ValueError):ai_client.complete_json('i',{})
        api.assert_not_called()

if __name__=='__main__':unittest.main()
