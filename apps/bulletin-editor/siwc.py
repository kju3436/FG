"""Official local Sign in with ChatGPT. Credentials never enter project files.

Only this app's PKCE/OIDC registration may use the public Responses endpoint.
No password/cookie collection, existing Codex credentials or backend-api calls.
"""
import base64,contextlib,hashlib,json,os,pathlib,secrets,sys,threading,time,uuid,webbrowser
import urllib.request,urllib.error
from urllib.parse import urlencode,urlparse
sys.path.insert(0,str(pathlib.Path(__file__).parent/'vendor'))
import jwt

ROOT=pathlib.Path(__file__).resolve().parents[2]
CONFIG=ROOT/'system'/'siwc-eligibility.json'
LOCAL=pathlib.Path(os.environ.get('LOCALAPPDATA',str(pathlib.Path.home()/'.local/share')))/'ChurchBulletinEditor'/'ChatGPT'
ISSUER='https://auth.openai.com';RESOURCE='https://api.openai.com/v1'
SCOPES='openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
LOCK=threading.RLock();PENDING={};PROGRESS={'phase':'idle','message':''}
APP_NAME='Church Bulletin Editor'

def eligibility():
    try:c=json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):c={}
    license=(ROOT/str(c.get('licenseFile',''))).resolve()
    public=(c.get('status')=='open_source_published' and str(c.get('sourceUrl','')).startswith('https://') and license.is_relative_to(ROOT) and license.is_file())
    private=(c.get('status')=='private_approved' and str(c.get('clientId','')).startswith('oaiapp_') and c.get('approvalReference'))
    return {'allowed':bool(public or private),'route':c.get('status','unverified'),'sourceUrl':c.get('sourceUrl',''),
            'clientId':c.get('clientId','') if private else '',
            'message':'공식 오픈소스 연결을 사용할 수 있습니다.' if public else '공식 허용된 연결입니다.' if private else '편집기 코드의 공개 배포 확인을 준비 중입니다. 완료되면 ChatGPT 로그인을 사용할 수 있습니다.'}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

class ConnectionError(ValueError):
    def __init__(self,message,code='',status=0,request_id=''):
        super().__init__(message);self.code=code;self.status=status;self.request_id=request_id

def _message(code,status=0):
    if code=='subscription_sharing_usage_limit_exceeded' or status==429:return 'ChatGPT 사용량 한도에 도달했습니다. 사용량을 확인하거나 기본 자동 정돈을 사용하세요.'
    if code=='subscription_sharing_user_not_eligible':return '이 계정에서는 앱의 AI 사용이 허용되지 않았습니다. 기본 자동 정돈은 계속 사용할 수 있습니다.'
    if code in ('invalid_grant','invalid_refresh_token','token_expired','refresh_token_expired','refresh_token_invalidated','refresh_token_reused'):return '로그인 연결이 만료되거나 해제되었습니다. ChatGPT로 다시 연결해주세요.'
    if status==401:return 'ChatGPT 로그인 권한을 다시 확인해주세요.'
    if status==403:return '이 앱 또는 계정의 ChatGPT 사용 권한이 허용되지 않았습니다.'
    return 'ChatGPT 연결을 완료하지 못했습니다. 잠시 후 다시 시도하거나 기본 자동 정돈을 사용하세요.'

def _open(url,payload=None,form=False,token=None):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in ('auth.openai.com','api.openai.com') or parsed.username or parsed.password:raise ValueError('공식 OpenAI 주소가 아닌 연결을 거절했습니다.')
    headers={'Accept':'application/json'};data=None
    if token:headers['Authorization']='Bearer '+token
    if payload is not None:
        headers['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
        data=urlencode(payload).encode() if form else json.dumps(payload,ensure_ascii=False).encode()
    try:return urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,data,headers),timeout=60)
    except urllib.error.HTTPError as e:
        try:
            body=json.loads(e.read(200_000));error=body.get('error',{});code=error.get('code','') if isinstance(error,dict) else error if isinstance(error,str) else ''
        except (ValueError,AttributeError):code=''
        raise ConnectionError(_message(code,e.code),code,e.code,e.headers.get('x-request-id','')) from None
    except (urllib.error.URLError,OSError,TimeoutError):raise ConnectionError('ChatGPT에 연결하지 못했습니다. 인터넷 연결을 확인해주세요.') from None

def _json(url,payload=None,form=False,token=None):
    with _open(url,payload,form,token) as response:raw=response.read(1_000_001)
    if len(raw)>1_000_000:raise ValueError('ChatGPT 응답이 너무 큽니다.')
    try:return json.loads(raw)
    except ValueError:raise ValueError('ChatGPT 응답 형식을 확인하지 못했습니다.') from None

def _discovery():
    d=_json(ISSUER+'/.well-known/openid-configuration')
    if d.get('issuer')!=ISSUER:raise ValueError('OpenAI 로그인 발급자 검증에 실패했습니다.')
    for key in ('authorization_endpoint','token_endpoint','jwks_uri','revocation_endpoint'):
        if not str(d.get(key,'')).startswith(ISSUER+'/'):raise ValueError('OpenAI 로그인 주소 검증에 실패했습니다.')
    return d

def _atomic(path,text):
    path.parent.mkdir(parents=True,exist_ok=True);stage=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    stage.write_text(text,encoding='utf-8');stage.replace(path)

def _host():
    path=LOCAL/'host.json'
    try:
        value=json.loads(path.read_text())['id'];uuid.UUID(value.removeprefix('urn:uuid:'));return value
    except (ValueError,OSError,KeyError):pass
    value='urn:uuid:'+str(uuid.uuid4());_atomic(path,json.dumps({'id':value}));return value

def _read():
    path=LOCAL/'accounts.enc'
    if not path.exists():return {'accounts':{},'active':None}
    from ai_client import _crypt
    try:return json.loads(_crypt(base64.b64decode(path.read_text()),True))
    except (ValueError,OSError,TypeError):raise ValueError('이 PC의 로그인 정보를 읽지 못했습니다. ChatGPT로 다시 연결해주세요.') from None

def _write(data):
    from ai_client import _crypt
    _atomic(LOCAL/'accounts.enc',base64.b64encode(_crypt(json.dumps(data,ensure_ascii=False).encode())).decode())

@contextlib.contextmanager
def _storage_lock():
    with LOCK:
        LOCAL.mkdir(parents=True,exist_ok=True)
        with (LOCAL/'account.lock').open('a+b') as handle:
            if handle.tell()==0:handle.write(b'0');handle.flush()
            if os.name=='nt':
                import msvcrt
                for i in range(100):
                    try:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1);break
                    except OSError:
                        if i==99:raise ValueError('로그인 정보를 갱신 중입니다. 잠시 후 다시 시도해주세요.')
                        time.sleep(.05)
            try:yield
            finally:
                if os.name=='nt':handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)

def current():
    if not eligibility()['allowed']:return None
    with LOCK:
        data=_read();record=data['accounts'].get(data.get('active'))
        if not record or 'chatgpt.tokens.use.direct' not in record.get('scopes',[]):return None
        return {'provider':'chatgpt','protocol':'siwc','model':record.get('model',''),'email':record.get('email',''),
                'accountId':data['active'],'remembered':True,'baseUrl':RESOURCE}

def status():
    with LOCK:
        try:data=_read();active=current()
        except ValueError:data={'accounts':{},'active':None};active=None
        accounts=[{'id':aid,'label':(r.get('email') or 'ChatGPT 계정')+' · '+r['client_id'][-6:],
                   'planEnabled':'chatgpt.tokens.use.direct' in r.get('scopes',[])} for aid,r in data['accounts'].items()]
        progress=dict(PROGRESS)
    record=data['accounts'].get(data.get('active'),{})
    return {'eligibility':eligibility(),'connected':bool(active),'active':active,'accounts':accounts,'progress':progress,'showWelcome':bool(active and not record.get('welcomed')),'usageUrl':'https://chatgpt.com/settings/usage'}

def begin(port,account=None):
    eligible=eligibility()
    if not eligible['allowed']:raise ValueError(eligible['message'])
    if type(port)is not int or not 1024<=port<=65535:raise ValueError('로컬 서버 포트를 확인해주세요.')
    d=_discovery()
    with _storage_lock():
        data=_read();old=data['accounts'].get(account) if account else None
        if account and not old:raise ValueError('저장된 계정을 다시 선택해주세요.')
        host=_host()
    client=old['client_id'] if old else eligible['clientId'] or 'dynamic_agent_client'
    state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
    redirect=f'http://127.0.0.1:{port}/auth/chatgpt/callback'
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    transaction={'client':client,'nonce':nonce,'verifier':verifier,'redirect':redirect,'expires':time.time()+600,'discovery':d,'subject':old.get('subject') if old else None,'host':host}
    with LOCK:PENDING.clear();PENDING[state]=transaction;PROGRESS.update(phase='waiting',message='기본 브라우저에서 ChatGPT 로그인을 완료해주세요.')
    params={'client_id':client,'response_type':'code','redirect_uri':redirect,'scope':SCOPES,'resource':RESOURCE,
            'state':state,'nonce':nonce,'code_challenge_method':'S256','code_challenge':challenge,'ext_agent_host_id':host}
    if client=='dynamic_agent_client':params['agent_name_hint']=APP_NAME
    if old and old.get('id_token'):params['id_token_hint']=old['id_token']
    if old and old.get('email'):params['login_hint']=old['email']
    if not webbrowser.open(d['authorization_endpoint']+'?'+urlencode(params),new=1):cancel();raise ValueError('기본 브라우저를 열지 못했습니다. PC의 브라우저 설정을 확인해주세요.')
    return {'started':True,'message':'기본 브라우저에서 로그인하고 사용 권한을 확인해주세요.'}

def _verify(token,client,nonce,d):
    try:
        h=jwt.get_unverified_header(token)
        if h.get('alg') not in ('RS256','ES256') or not isinstance(h.get('kid'),str):raise ValueError()
        keys=[k for k in _json(d['jwks_uri']).get('keys',[]) if k.get('kid')==h['kid']]
        if len(keys)!=1:raise ValueError()
        key=jwt.PyJWK.from_dict(keys[0],algorithm=h['alg']).key
        claims=jwt.decode(token,key,algorithms=[h['alg']],issuer=ISSUER,audience=client,leeway=5,
                          options={'require':['iss','aud','sub','exp','iat']+(['nonce'] if nonce else [])})
        if nonce and claims['nonce']!=nonce:raise ValueError()
        if not isinstance(claims['sub'],str) or not claims['sub']:raise ValueError()
        return claims
    except (jwt.PyJWTError,ValueError,KeyError,TypeError):raise ValueError('ChatGPT 로그인 서명과 계정을 확인하지 못했습니다.') from None

def finish(query):
    if not eligibility()['allowed']:raise ValueError('코드 공개 배포를 먼저 확인해주세요.')
    with LOCK:t=PENDING.pop(query.get('state',''),None)
    if not t or t['expires']<time.time():raise ValueError('로그인 요청이 만료되었거나 일치하지 않습니다. 다시 연결해주세요.')
    try:
        if query.get('error'):raise ValueError('로그인 연결이 취소되거나 허용되지 않았습니다. 기존 연결은 유지했습니다.')
        client=query.get('client_id') or t['client'];code=query.get('code')
        if not code or not client.startswith('oaiapp_'):raise ValueError('앱 등록이 완료되지 않았습니다.')
        if t['client']!='dynamic_agent_client' and t['client']!=client:raise ValueError('다른 앱의 로그인 응답을 거절했습니다.')
        token=_json(t['discovery']['token_endpoint'],{'grant_type':'authorization_code','client_id':client,'code':code,'code_verifier':t['verifier'],'redirect_uri':t['redirect'],'resource':RESOURCE},True)
        claims=_verify(token.get('id_token',''),client,t['nonce'],t['discovery'])
        if t['subject'] and claims['sub']!=t['subject']:raise ValueError('선택한 계정과 다릅니다. 다른 계정으로 연결을 선택해주세요.')
        if not isinstance(token.get('scope'),str):raise ValueError('사용 권한 응답이 없습니다.')
        scopes=token['scope'].split();enabled='chatgpt.tokens.use.direct' in scopes
        record={'client_id':client,'issuer':ISSUER,'subject':claims['sub'],'email':claims.get('email',''),'host':t['host'],'scopes':scopes,
                'id_token':token['id_token'],'access_token':token.get('access_token',''),'refresh_token':token.get('refresh_token',''),
                'expires_at':time.time()+float(token.get('expires_in',3600)),'earliest_refresh_at':token.get('earliest_refresh_at',0)}
        if enabled:
            if token.get('token_type','').lower()!='bearer' or not record['access_token']:raise ValueError('ChatGPT 사용 토큰이 없습니다.')
            catalog=_json(RESOURCE+'/models',token=record['access_token']);models=[m for m in catalog.get('models',[]) if m.get('visibility')=='list' and isinstance(m.get('slug'),str)]
            if not models:raise ValueError('이 계정에서 사용할 수 있는 모델이 없습니다. 기본 자동 정돈을 사용하세요.')
            record['model']=models[0]['slug']
        aid=hashlib.sha256((ISSUER+'|'+client+'|'+claims['sub']).encode()).hexdigest()[:24]
        with _storage_lock():
            data=_read();data['accounts'][aid]=record
            if enabled:data['active']=aid;data['preferred']='chatgpt'
            _write(data)
        with LOCK:PROGRESS.update(phase='connected' if enabled else 'needsPlan',message='ChatGPT 계정으로 연결했습니다.' if enabled else '계정 로그인은 완료했지만 AI 사용 권한이 없습니다. 기본 자동 정돈은 계속 사용할 수 있습니다.')
        return {'connected':enabled,'message':PROGRESS['message']}
    except (ValueError,OSError,KeyError,TypeError) as e:
        message=str(e) if isinstance(e,ValueError) else 'ChatGPT 연결을 완료하지 못했습니다. 다시 연결해주세요.'
        with LOCK:PROGRESS.update(phase='error',message=message)
        raise ValueError(message) from None

def cancel():
    with LOCK:PENDING.clear();PROGRESS.update(phase='idle',message='로그인 연결을 취소했습니다.')
    return {'cancelled':True}

def _access():
    if not eligibility()['allowed']:raise ValueError('코드 공개 배포를 먼저 확인해주세요.')
    with _storage_lock():
        data=_read();aid=data.get('active');r=data['accounts'].get(aid)
        if not r or 'chatgpt.tokens.use.direct' not in r.get('scopes',[]):raise ValueError('본인 ChatGPT 계정으로 연결해주세요.')
        if r.get('expires_at',0)<time.time()+45 and float(r.get('earliest_refresh_at',0))<=time.time():
            if not r.get('refresh_token'):raise ValueError('ChatGPT로 다시 로그인해주세요.')
            d=_discovery()
            try:token=_json(d['token_endpoint'],{'grant_type':'refresh_token','client_id':r['client_id'],'refresh_token':r['refresh_token'],'resource':RESOURCE},True)
            except ConnectionError as e:
                if e.code in ('invalid_grant','invalid_refresh_token','token_expired','refresh_token_expired','refresh_token_invalidated','refresh_token_reused'):
                    r.update(access_token='',refresh_token='',id_token='');data['active']=None;_write(data)
                raise
            if not token.get('access_token') or not token.get('refresh_token'):raise ValueError('ChatGPT 연결 갱신을 완료하지 못했습니다. 다시 로그인해주세요.')
            if token.get('id_token'):
                claims=_verify(token['id_token'],r['client_id'],None,d)
                if claims['sub']!=r['subject']:raise ValueError('계정이 다른 로그인 갱신을 거절했습니다.')
                r['id_token']=token['id_token']
            scopes=token.get('scope',' '.join(r['scopes'])).split()
            r.update(access_token=token['access_token'],refresh_token=token['refresh_token'],scopes=scopes,
                     expires_at=time.time()+float(token.get('expires_in',3600)),earliest_refresh_at=token.get('earliest_refresh_at',0))
            if 'chatgpt.tokens.use.direct' not in scopes:data['active']=None
            _write(data)
            if not data.get('active'):raise ValueError('ChatGPT AI 사용 권한을 다시 확인해주세요.')
        if r['expires_at']<=time.time():raise ValueError('로그인 갱신을 기다리고 있습니다. 잠시 후 다시 시도해주세요.')
        return r['access_token'],r['model']

def complete(instructions,text):
    token,model=_access();payload={'model':model,'instructions':instructions,'input':[{'role':'user','content':text}],'store':False,'stream':True}
    result=[];lines=[];size=0;completed=False
    with _open(RESOURCE+'/responses',payload,token=token) as response:
        for raw in response:
            size+=len(raw)
            if size>2_000_000 or len(raw)>200_000:raise ValueError('ChatGPT 응답이 너무 큽니다.')
            line=raw.decode('utf-8').rstrip('\r\n')
            if line:
                if line.startswith('data:'):lines.append(line[5:].lstrip())
                continue
            if not lines:continue
            data='\n'.join(lines);lines=[]
            if data=='[DONE]':continue
            try:e=json.loads(data)
            except ValueError:raise ValueError('ChatGPT 응답 스트림을 확인하지 못했습니다.') from None
            kind=e.get('type')
            if kind=='response.output_text.delta':result.append(e.get('delta',''))
            elif kind=='response.completed':completed=True
            elif kind in ('error','response.failed','response.incomplete'):
                error=e.get('response',{}).get('error') or e.get('error') or {};code=error.get('code','') if isinstance(error,dict) else ''
                raise ConnectionError(_message(code),code)
    if not completed:raise ValueError('ChatGPT 응답이 끝나기 전에 연결이 끊겼습니다. 기존 주보는 유지했습니다.')
    return ''.join(result)

def deactivate():
    if not (LOCAL/'accounts.enc').exists():return
    with _storage_lock():data=_read();data['active']=None;data['preferred']='api';_write(data)

def prefers_chatgpt():
    with LOCK:return _read().get('preferred')=='chatgpt'

def acknowledge():
    with _storage_lock():
        data=_read();r=data['accounts'].get(data.get('active'))
        if r:r['welcomed']=True;_write(data)
    return {'acknowledged':True}

def signout(account=None):
    revoked=True
    with _storage_lock():
        data=_read();aid=account or data.get('active');r=data['accounts'].get(aid)
        if r and r.get('refresh_token'):
            try:
                d=_discovery()
                with _open(d['revocation_endpoint'],{'token':r['refresh_token'],'token_type_hint':'refresh_token','client_id':r['client_id']},True) as response:response.read(1000)
            except (ValueError,OSError):revoked=False
        if r:r.update(access_token='',refresh_token='',id_token='',scopes=[],model='')
        if data.get('active')==aid:data['active']=None
        _write(data)
    cancel()
    return {'connected':False,'revoked':revoked,'message':'ChatGPT 연결을 해제했습니다.' if revoked else '이 PC에서는 연결을 해제했습니다. ChatGPT 설정에서도 앱 연결을 해제해주세요.'}
