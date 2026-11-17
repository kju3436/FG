"""Local-only bulletin editor and vector PDF booklet renderer."""
import argparse, base64, copy, io, json, os, pathlib, re, shutil, subprocess, threading, webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
import html
import pdfplumber
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import DecodedStreamObject, NameObject, ArrayObject
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageOps
from compose import compose, initial_data
from scripture import lookup as scripture_lookup, status as scripture_status, configure_ai
from partial import apply_partial
from ai_review import review_document, apply_suggestions

ROOT=pathlib.Path(__file__).resolve().parents[2]
APP=pathlib.Path(__file__).resolve().parent
CACHE=ROOT/'tmp'/'pdfs'/'bulletin-editor'
W,H=595,842
SOURCES={}
READERS={}
RASTER_LOCK=threading.Lock()

def setup():
    CACHE.mkdir(parents=True,exist_ok=True)
    for key,date in [('four','20261108'),('eight','20261115')]:
        files=list((ROOT/'자료'/'주보_기준원본').glob(date+'*.pdf'))
        if len(files)!=1: raise RuntimeError(f'{date} 기준 주보가 필요합니다.')
        SOURCES[key]=files[0]
        READERS[key]=PdfReader(files[0]);count=len(READERS[key].pages)
        if not (CACHE/f'{key}-{count}.png').exists():
            subprocess.run(['pdftoppm','-scale-to','1190','-png',str(files[0]),str(CACHE/key)],check=True,capture_output=True)
        if not (CACHE/f'{key}-print-1.png').exists():
            subprocess.run(['pdftoppm','-f','1','-l','1','-r','300','-png',str(files[0]),str(CACHE/(key+'-print'))],check=True,capture_output=True)
    fonts=pathlib.Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    pdfmetrics.registerFont(TTFont('BulletinKO',str(fonts/'malgun.ttf')))
    pdfmetrics.registerFont(TTFont('BulletinJA',str(fonts/'meiryo.ttc'),subfontIndex=0))

def text_crop(page,box):
    return (page.crop(tuple(box)).extract_text(x_tolerance=2,y_tolerance=3) or '').replace('\x00',' ')

def template(key):
    roles=['표지','예배순서','말씀','교회소식'] if key=='four' else ['특별 표지','예배순서','말씀','신앙고백·주기도','감사찬송 악보','임명 선서문','봉헌찬송 악보','교회소식']
    cuts=([0,240,402,635,842],[0,42,470,565,824,842],[0,31,214,257,823,842],[0,31,226,665,693,824,842]) if key=='four' else ([0,240,440,600,842],[0,42,444,534,824,842],[0,31,116,144,824,842],[0,440,824,842],[0,842],[0,35,824,842],[0,842],[0,45,239,665,693,824,842])
    labels=(['교회 정보·날짜','연간 표어','연간 목표','예배안내·연락처'],['예배 제목·담당','예배순서 앞부분','설교 제목','예배순서 뒷부분','쪽번호'],['사죄확인 제목','사죄확인 한일 본문','설교본문 제목','설교 한일 본문','쪽번호'],['교회소식 제목','강조 공지','일반 공지','봉사표 제목','예배봉사표','쪽번호'])
    pages=[]
    with pdfplumber.open(SOURCES[key]) as pdf:
        for i,p in enumerate(pdf.pages):
            blocks=[]
            for j,(a,b) in enumerate(zip(cuts[i],cuts[i][1:])):
                if key=='four': name=labels[i][j]
                else: name=f'{roles[i]} 영역 {j+1}'
                crop=[0,a,W,b]
                blocks.append({'id':f'{key}-{i}-{j}','label':name,'type':'source','source':key,'sourcePage':i,'crop':crop,'x':0,'y':a,'w':W,'h':b-a,'text':text_crop(p,crop),'fontSize':15,'lineHeight':1.5,'font':'ko','align':'left','color':'#111111','fill':'#ffffff','padding':8})
            pages.append({'name':roles[i],'blocks':blocks})
    return {'schemaVersion':1,'template':key,'title':'주일주보','pageWidth':W,'pageHeight':H,'pages':pages}

def validate(doc):
    if doc.get('engine')=='native-v2':
        from weekly import validate as validate_week
        return validate_week(doc)
    if doc.get('schemaVersion')!=1 or len(doc.get('pages',[])) not in (4,8): raise ValueError('4페이지 또는 8페이지 문서만 지원합니다.')
    for page in doc['pages']:
        if len(page.get('blocks',[]))>120: raise ValueError('페이지당 영역은 최대 120개입니다.')
        for b in page['blocks']:
            for k in ['x','y','w','h']:
                v=b.get(k)
                if not isinstance(v,(int,float)) or not (-1000<v<3000):raise ValueError('영역 좌표가 올바르지 않습니다.')
            minimum=.1 if b.get('type')=='source' else 4
            if b['w']<minimum or b['h']<minimum or b['x']<0 or b['y']<0 or b['x']+b['w']>W+.1 or b['y']+b['h']>H+.1:raise ValueError(f"{b.get('label','영역')}: 페이지 밖으로 벗어났습니다.")
            if b.get('type') not in ('source','text','image'):raise ValueError('지원하지 않는 영역 형식입니다.')
            if b['type']=='source':
                if b.get('source') not in SOURCES:raise ValueError('기준 PDF가 올바르지 않습니다.')
                if not isinstance(b.get('sourcePage'),int) or not 0<=b['sourcePage']<len(READERS[b['source']].pages):raise ValueError('원본 페이지가 올바르지 않습니다.')
                crop=b.get('crop',[])
                if len(crop)!=4 or not all(isinstance(x,(int,float)) for x in crop) or not (0<=crop[0]<crop[2]<=W and 0<=crop[1]<crop[3]<=H):raise ValueError('원본 영역이 올바르지 않습니다.')
            elif b['type']=='image':load_image(b)
            else:
                if len(b.get('text',''))>40000:raise ValueError('본문이 너무 깁니다.')
                if not 6<=float(b.get('fontSize',15))<=72:raise ValueError('글자 크기는 6~72pt입니다.')

def load_image(b):
    value=b.get('imageData','')
    if not isinstance(value,str) or not re.match(r'^data:image/(png|jpeg);base64,',value):raise ValueError('PNG 또는 JPG 이미지가 필요합니다.')
    try:
        raw=base64.b64decode(value.split(',',1)[1],validate=True)
        if len(raw)>2_000_000:raise ValueError('이미지는 2MB 이하입니다.')
        img=Image.open(io.BytesIO(raw))
        if img.format not in ('PNG','JPEG') or img.width*img.height>20_000_000:raise ValueError('이미지 크기 또는 형식을 확인해주세요.')
        img.load();return ImageOps.exif_transpose(img)
    except Exception:raise ValueError('이미지를 읽지 못했습니다. PNG/JPG 크기와 형식을 확인해주세요.') from None

def image_layer(b):
    img=load_image(b);ratio=min(b['w']/img.width,b['h']/img.height);w,h=img.width*ratio,img.height*ratio
    mem=io.BytesIO();c=canvas.Canvas(mem,pagesize=(W,H));c.drawImage(ImageReader(img),b['x']+(b['w']-w)/2,H-b['y']-(b['h']+h)/2,w,h,mask='auto');c.save()
    return PdfReader(mem).pages[0]

def wrap(text,font,size,width):
    if width<=0:raise ValueError('텍스트 영역의 안쪽 여백을 줄여주세요.')
    lines=[]
    for para in text.replace('\r','').split('\n'):
        line=''
        for char in para:
            if line and measure_text(line+char,font,size)>width:
                # Prefer Korean word boundaries; Japanese may break between characters.
                space=line.rfind(' ')
                if space>len(line)*.45:
                    lines.append(line[:space]);line=line[space+1:]+char
                else:
                    # Japanese kinsoku: avoid closing punctuation at line start
                    # and opening brackets at line end without altering scripture.
                    carry=''
                    forbidden_start='、。，．？！：；)]}）］｝」』】〉》ー々ぁぃぅぇぉっゃゅょァィゥェォッャュョ'
                    forbidden_end='([{（［｛「『【〈《'
                    if font=='BulletinJA':
                        while len(line)>1 and (line[-1] in forbidden_end or (not carry and char in forbidden_start)):
                            carry=line[-1]+carry;line=line[:-1]
                    lines.append(line);line=carry+char
            else:line+=char
        lines.append(line)
    return lines

def character_font(char,preferred):
    other='BulletinJA' if preferred=='BulletinKO' else 'BulletinKO'
    return preferred if ord(char) in pdfmetrics.getFont(preferred).face.charToGlyph else other

def measure_text(text,font,size):
    return sum(pdfmetrics.stringWidth(ch,character_font(ch,font),size) for ch in text)

def draw_bilingual(c,text,x,y,font,size):
    for ch in text:
        actual=character_font(ch,font);c.setFont(actual,size);c.drawString(x,y,ch);x+=pdfmetrics.stringWidth(ch,actual,size)

def text_layer(b):
    mem=io.BytesIO(); c=canvas.Canvas(mem,pagesize=(W,H))
    size=float(b.get('fontSize',15)); pad=max(0,float(b.get('padding',8)))
    font='BulletinJA' if b.get('font')=='ja' else 'BulletinKO'
    leading=size*float(b.get('lineHeight',1.5))
    lines=wrap(b.get('text',''),font,size,b['w']-pad*2)
    if size+(len(lines)-1)*leading>b['h']-pad*2:raise ValueError(f"{b.get('label','본문')}: 글자가 영역 높이를 넘습니다. 영역을 늘리거나 본문을 다음 페이지로 나눠주세요.")
    from reportlab.lib.colors import HexColor
    c.setFillColor(HexColor(b.get('fill','#ffffff')));c.rect(b['x'],H-b['y']-b['h'],b['w'],b['h'],fill=1,stroke=0)
    c.setFillColor(HexColor(b.get('color','#111111')));c.setFont(font,size)
    if b.get('border'):
        c.setStrokeColor(HexColor('#777777'));c.setLineWidth(.4);c.rect(b['x'],H-b['y']-b['h'],b['w'],b['h'],stroke=1,fill=0)
    for j,line in enumerate(lines):
        x=b['x']+pad;y=H-b['y']-pad-size-j*leading
        width=measure_text(line,font,size)
        if b.get('align')=='center':x=b['x']+(b['w']-width)/2
        elif b.get('align')=='right':x=b['x']+b['w']-pad-width
        draw_bilingual(c,line,x,y,font,size)
    c.save();return PdfReader(mem).pages[0]

def clipped_source(b,readers):
    if b.get('rasterize'):
        # Partial edits can retain fragments from any page, not just the cover.
        number=b['sourcePage']+1
        raster=CACHE/f'{b["source"]}-print-{number}.png'
        with RASTER_LOCK:
            if not raster.exists():
                subprocess.run(['pdftoppm','-f',str(number),'-l',str(number),'-singlefile','-r','300','-png',str(SOURCES[b['source']]),str(raster.with_suffix(''))],check=True,capture_output=True)
        img=Image.open(raster)
        x0,y0,x1,y1=b['crop'];sx=img.width/W;sy=img.height/H
        crop=img.crop((round(x0*sx),round(y0*sy),round(x1*sx),round(y1*sy)))
        mem=io.BytesIO();c=canvas.Canvas(mem,pagesize=(W,H));c.drawImage(ImageReader(crop),b['x'],H-b['y']-b['h'],b['w'],b['h']);c.save()
        return PdfReader(mem).pages[0]
    src=copy.deepcopy(readers[b['source']].pages[b['sourcePage']])
    x0,y0,x1,y1=b['crop']; content=src.get_contents().get_data()
    stream=DecodedStreamObject();stream.set_data(f'q {x0} {H-y1} {x1-x0} {y1-y0} re W n\n'.encode()+content+b'\nQ')
    src[NameObject('/Contents')]=stream
    sx=b['w']/(x1-x0);sy=b['h']/(y1-y0)
    tx=b['x']-sx*x0;ty=H-b['y']-b['h']-sy*(H-y1)
    src.add_transformation(Transformation((sx,0,0,sy,tx,ty)))
    return src

def render(doc,mode='booklet'):
    validate(doc);readers=READERS;logical=PdfWriter()
    for pg in doc['pages']:
        page=logical.add_blank_page(width=W,height=H)
        for b in pg['blocks']:
            layer=clipped_source(b,readers) if b['type']=='source' else image_layer(b) if b['type']=='image' else text_layer(b)
            page.merge_page(layer)
    if mode=='logical':writer=logical
    elif mode=='booklet':
        writer=PdfWriter(); n=len(logical.pages); aw,ah=841.8897637795277,595.2755905511812
        # One centered, uniformly scaled pair per side, in outer-to-inner sheet order.
        scale=min(aw/2/W,ah/H)
        for sheet in range(n//4):
            for pair in [(n-1-sheet*2,sheet*2),(sheet*2+1,n-2-sheet*2)]:
                side=writer.add_blank_page(width=aw,height=ah)
                for col,idx in enumerate(pair):
                    x=col*aw/2+(aw/2-W*scale)/2;y=(ah-H*scale)/2
                    side.merge_transformed_page(logical.pages[idx],Transformation().scale(scale).translate(x,y))
    else:raise ValueError('출력 모드가 올바르지 않습니다.')
    data=io.BytesIO();writer.write(data);return data.getvalue()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(APP/'web'),**kw)
    def log_message(self,*a):pass
    def reply(self,data,kind='application/json',status=200):
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}',f'[::1]:{self.server.server_port}'):return self.send_error(403)
        path=urlparse(self.path).path
        if path=='/auth/chatgpt/callback':
            import siwc
            try:
                values=parse_qs(urlparse(self.path).query,keep_blank_values=True)
                if any(len(v)!=1 for v in values.values()):raise ValueError('중복된 로그인 응답을 거절했습니다.')
                result=siwc.finish({k:v[0] for k,v in values.items()});message=result['message']
            except Exception as e:message=str(e) if isinstance(e,ValueError) else '로그인 연결을 완료하지 못했습니다. 편집기에서 다시 연결해주세요.'
            return self.reply(('<!doctype html><html lang="ko"><meta charset="utf-8"><title>ChatGPT 연결</title><body style="font:20px sans-serif;padding:48px;line-height:1.8"><h1>ChatGPT 연결 확인</h1><p>'+html.escape(message)+'</p><p>이 창을 닫고 주보 편집기로 돌아가세요.</p></body></html>').encode(),'text/html; charset=utf-8')
        if path=='/api/chatgpt/status':
            import siwc
            return self.reply(json.dumps(siwc.status(),ensure_ascii=False).encode())
        if path=='/api/chatgpt/eligibility':
            from chatgpt_eligibility import status
            return self.reply(json.dumps(status(),ensure_ascii=False).encode())
        if path=='/api/ai/status':
            import ai_client
            return self.reply(json.dumps(ai_client.status(),ensure_ascii=False).encode())
        if path.startswith('/api/weekly/'):
            try:
                import weekly
                if path=='/api/weekly/current':return self.reply(json.dumps(weekly.initial(),ensure_ascii=False).encode())
                if path=='/api/weekly/archives':return self.reply(json.dumps(weekly.archives(),ensure_ascii=False).encode())
                if path=='/api/weekly/catalog':
                    from catalog import catalog
                    return self.reply(json.dumps(catalog(),ensure_ascii=False).encode())
                if path=='/api/weekly/hymn-metadata':
                    from hymn_metadata import PATH
                    return self.reply(PATH.read_bytes(),'text/csv; charset=utf-8')
                if path=='/api/weekly/roster-template':
                    from annual_roster import TEMPLATES
                    year=parse_qs(urlparse(self.path).query).get('year',['2027'])[0]
                    if year not in ('2026','2027'):raise ValueError('준비된 연도는 2026년·2027년입니다.')
                    template=TEMPLATES/(year+'_연간봉사대장.xlsx')
                    if not template.exists():raise ValueError('연간 봉사 대장 양식 파일이 없습니다.')
                    return self.reply(template.read_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                if path.startswith('/api/weekly/background/'):
                    from native_template import background_bytes
                    return self.reply(background_bytes(int(path.rsplit('/',1)[-1])),'image/png')
                if path.startswith('/api/weekly/font/'):
                    from native_template import font_bytes
                    return self.reply(font_bytes(path.rsplit('/',1)[-1]),'font/otf')
                return self.send_error(404)
            except Exception as e:return self.reply(json.dumps({'error':str(e)},ensure_ascii=False).encode(),status=400)
        if path=='/api/templates':
            return self.reply(json.dumps({k:template(k) for k in SOURCES},ensure_ascii=False).encode())
        if path=='/api/service-defaults':return self.reply(json.dumps(initial_data(),ensure_ascii=False).encode())
        if path=='/api/scripture/status':return self.reply(json.dumps(scripture_status(),ensure_ascii=False).encode())
        if path.startswith('/source/'):
            name=path.split('/')[-1]
            if not re.fullmatch(r'(four|eight)-\d+\.png',name):return self.send_error(404)
            f=CACHE/name
            if f.exists():return self.reply(f.read_bytes(),'image/png')
            return self.send_error(404)
        if path.startswith('/font/'):
            names={'ko':'malgun.ttf','ja':'meiryo.ttc'};name=names.get(path.split('/')[-1])
            if not name:return self.send_error(404)
            return self.reply((pathlib.Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name).read_bytes(),'font/ttf')
        return super().do_GET()
    def do_POST(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}',f'[::1]:{self.server.server_port}'):return self.send_error(403)
        # External services are configured server-side, never by request payload.
        if self.headers.get('Origin') and self.headers['Origin']!=f'http://{self.headers.get("Host")}':return self.send_error(403)
        if not (self.path.startswith('/api/pdf/') or self.path.startswith('/api/weekly/') or self.path in ('/api/source-text','/api/compose','/api/scripture','/api/region/check','/api/review','/api/review/apply','/api/worship/plan','/api/worship/build','/api/ai/configure','/api/ai/connect','/api/ai/models','/api/ai/disconnect','/api/chatgpt/login','/api/chatgpt/cancel','/api/chatgpt/logout','/api/chatgpt/acknowledge')):return self.send_error(404)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8_000_000:raise ValueError('문서 크기를 확인해주세요.')
            doc=json.loads(self.rfile.read(length))
            if self.path.startswith('/api/chatgpt/'):
                import siwc
                action=self.path.rsplit('/',1)[-1]
                result=siwc.begin(self.server.server_port,doc.get('account')) if action=='login' else siwc.cancel() if action=='cancel' else siwc.acknowledge() if action=='acknowledge' else siwc.signout(doc.get('account'))
                return self.reply(json.dumps(result,ensure_ascii=False).encode())
            if self.path.startswith('/api/weekly/'):
                import weekly
                from native_template import update_document,edit_block,render_document
                action=self.path.rsplit('/',1)[-1]
                if action=='load':result=weekly.load_archive(doc.get('id',''))
                elif action=='validate':weekly.validate(doc['document']);result={'valid':True}
                elif action=='save':result=weekly.save_current(doc['document'])
                elif action=='archive':result=weekly.archive(doc['document'])
                elif action=='next':
                    from annual_roster import apply
                    from communion import automatic
                    result=weekly.preview(automatic(apply(weekly.next_week(doc['document']))))
                elif action=='update':
                    weekly.validate(doc['document']);result=update_document(doc['document'],doc['changes'])
                    if 'date' in doc['changes']:
                        from annual_roster import apply
                        result=apply(result)
                        from communion import automatic
                        result=automatic(result)
                    result=weekly.preview(result)
                elif action=='edit':
                    weekly.validate(doc['document']);result=edit_block(doc['document'],doc['id'],doc['text'])
                    if result['serviceData']['date']!=doc['document']['serviceData']['date']:
                        from annual_roster import apply
                        result=apply(result)
                        from communion import automatic
                        result=automatic(result)
                    result=weekly.preview(result)
                elif action=='scripture-preview':
                    from catalog import preview
                    result=preview(doc)
                elif action=='translate-title':
                    from title_translation import translate
                    result=translate(doc)
                elif action=='communion':
                    import communion
                    weekly.validate(doc['document'])
                    result=weekly.preview(communion.automatic(doc['document']) if doc.get('mode')=='auto' else communion.apply(doc['document'],doc.get('enabled')))
                elif action=='roster-import':
                    from annual_roster import import_records
                    result=import_records(doc)
                elif action=='roster-preview':
                    from annual_roster import proposal
                    result=proposal(doc['document'])
                elif action=='roster-apply':
                    from annual_roster import apply
                    weekly.validate(doc['document'])
                    result=weekly.preview(apply(doc['document'],doc.get('expected')))
                elif action in ('select-scripture','select-song'):
                    from catalog import apply_scripture,apply_song
                    weekly.validate(doc['document'])
                    result=weekly.preview((apply_scripture if action=='select-scripture' else apply_song)(doc['document'],doc))
                elif action=='addition-propose':
                    weekly.validate(doc['document'])
                    if doc.get('mode') in ('ai','whole','manual'):
                        from reflow import propose
                        if doc.get('mode')=='manual' and not doc.get('selection'):raise ValueError('외부 AI에서 받은 JSON 결과를 붙여넣어주세요.')
                        result=propose(doc['document'],doc.get('text',''),doc.get('page',3),doc.get('zone','bottom'),use_ai=doc.get('mode')=='ai',selection=doc.get('selection') if doc.get('mode')=='manual' else None)
                    else:
                        from additions import propose
                        result=propose(doc['document'],doc.get('text',''),doc.get('page',3),doc.get('zone','bottom'))
                    previous={b['id'] for p in doc['document']['pages'] for b in p['blocks']}
                    for p in result['document']['pages']:
                        for b in p['blocks']:
                            if b['id'] not in previous:
                                b['aiEditable']=doc.get('kind')=='notice'
                                b['role']='general' if b['aiEditable'] else 'quoted'
                    result['document']=weekly.preview(result['document'])
                elif action=='layout-prompt':
                    from reflow import prompt
                    weekly.validate(doc['document'])
                    result=prompt(doc['document'],doc.get('text',''),doc.get('page',3),doc.get('zone','bottom'))
                elif action=='review-apply':
                    from ai_review import _validate,_index
                    original=doc['document'];weekly.validate(original);_validate(doc['suggestions'],_index(original));result=original
                    for suggestion in doc['suggestions']:result=edit_block(result,suggestion['id'],suggestion['after'])
                    result=weekly.preview(result)
                elif action=='pdf':return self.reply(render_document(doc['document'],doc.get('mode','booklet')),'application/pdf')
                else:return self.send_error(404)
                return self.reply(json.dumps(result,ensure_ascii=False).encode())
            if self.path=='/api/ai/configure':
                return self.reply(json.dumps(configure_ai(doc.get('key'),doc.get('model')),ensure_ascii=False).encode())
            if self.path in ('/api/ai/connect','/api/ai/models','/api/ai/disconnect'):
                import ai_client
                result=ai_client.connect(doc) if self.path.endswith('/connect') else ai_client.models(doc) if self.path.endswith('/models') else ai_client.disconnect()
                return self.reply(json.dumps(result,ensure_ascii=False).encode())
            if self.path=='/api/region/check':
                validate({'schemaVersion':1,'pages':[{'blocks':[doc]},{'blocks':[]},{'blocks':[]},{'blocks':[]}]})
                if doc['type']=='text':text_layer(doc)
                return self.reply(b'{"ok":true}')
            if self.path=='/api/review':
                validate(doc);result=review_document(doc)
                result['issues']=(doc.get('warnings',[])+result.get('issues',[]))
                return self.reply(json.dumps(result,ensure_ascii=False).encode())
            if self.path=='/api/review/apply':
                result=apply_suggestions(doc['document'],doc['suggestions']);validate(result)
                for pg in result['pages']:
                    for b in pg['blocks']:
                        if b['type']=='text':text_layer(b)
                return self.reply(json.dumps(result,ensure_ascii=False).encode())
            if self.path.startswith('/api/worship/'):
                from worship import plan_worship, build_worship
                validate(doc)
                if self.path.endswith('/plan'):return self.reply(json.dumps(plan_worship(doc),ensure_ascii=False).encode())
                return self.reply(build_worship(doc),'application/vnd.openxmlformats-officedocument.presentationml.presentation')
            if self.path=='/api/scripture':
                return self.reply(json.dumps(scripture_lookup(doc.get('reference',''),doc.get('language','both')),ensure_ascii=False).encode())
            if self.path=='/api/compose':
                base=doc['document'];validate(base)
                result=(apply_partial if doc.get('partial') else compose)(base,doc['data'],wrap,measure_text);validate(result)
                # Measure every newly composed text block before replacing the visible document.
                for pg in result['pages']:
                    for b in pg['blocks']:
                        if b['type']=='text':text_layer(b)
                return self.reply(json.dumps(result,ensure_ascii=False).encode())
            if self.path=='/api/source-text':
                validate({'schemaVersion':1,'pages':[{'blocks':[doc]},{'blocks':[]},{'blocks':[]},{'blocks':[]}]})
                with pdfplumber.open(SOURCES[doc['source']]) as pdf:text=text_crop(pdf.pages[doc['sourcePage']],doc['crop'])
                return self.reply(json.dumps({'text':text},ensure_ascii=False).encode())
            data=render(doc,self.path.rsplit('/',1)[-1]);return self.reply(data,'application/pdf')
        except Exception as e:return self.reply(json.dumps({'error':str(e)},ensure_ascii=False).encode(),status=400)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8765);p.add_argument('--open',action='store_true');args=p.parse_args()
    setup();server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    url=f'http://127.0.0.1:{server.server_port}';print('주보 편집기 '+url,flush=True)
    if args.open:webbrowser.open(url)
    server.serve_forever()
