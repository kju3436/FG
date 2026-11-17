"""Optional monthly communion order, measured from the retained Nov 1 bulletin."""
import base64,copy,datetime,hashlib,io,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[2]
BOUNDS=[(57.5,92),(92,128),(144,182),(182,225),(225,257),(257,294),(294,336),(336,374),(390,431),(431,473),(473,521),(580,628),(628,671),(671,703),(703,733),(733,768),(768,800)]
def suggested(date):
    value=datetime.date.fromisoformat(date)
    return value.weekday()==6 and value.day<=7
def background():
    from native_template import CACHE
    from pypdf import PdfReader,PdfWriter
    import pypdfium2
    source=next((ROOT/'자료'/'주보_기준원본').glob('20261101*.pdf'),None)
    if source is None:raise ValueError('11월 1일 성찬식 기준 주보가 없습니다.')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()[:16];path=CACHE/(digest+'-communion-graphics.png')
    if not path.exists():
        writer=PdfWriter();writer.add_page(PdfReader(source).pages[1]);writer.remove_text();data=io.BytesIO();writer.write(data)
        pdf=pypdfium2.PdfDocument(data.getvalue());page=pdf[0];bitmap=page.render(scale=2)
        CACHE.mkdir(parents=True,exist_ok=True);bitmap.to_pil().save(path);bitmap.close();page.close();pdf.close()
    return 'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()
def automatic(document):
    current=document['serviceData'].get('communion',{});date=document['serviceData']['date']
    if current.get('date')==date:return document
    return apply(document,suggested(date),manual=False)
def apply(document,enabled,manual=True):
    from native_template import _replace,_canonical_fields,_set,validate_document
    if type(enabled)is not bool:raise ValueError('성찬식 있음 또는 없음을 선택해주세요.')
    result=copy.deepcopy(document);data=result['serviceData'];old=data.get('communion',{})
    same=bool(old.get('enabled'))==enabled;page=result['pages'][1];registry={f['id']:f for f in result['fonts']}
    if not same and enabled:
        result['communionNormalPage']=copy.deepcopy(page)
        for block in page['blocks']:
            binding=block.get('binding','')
            if binding.startswith('service.'):
                bits=binding.split('.');row=int(bits[1]);key=bits[2]
                if row>=len(BOUNDS):raise ValueError('성찬식을 추가할 예배순서 형식이 다릅니다.')
                top,bottom=BOUNDS[row];left,right={'nameRaw':(1.92,158.4),'detailRaw':(158.4,455.76),'leader':(455.76,593.4)}.get(key,(block['x'],block['x']+block['w']))
                block.update(x=left,y=top,w=right-left,h=bottom-top,positioned=False)
            elif binding.startswith('nativeText.'):
                old_y=block['y'];target=min([(50.3,44.5),(146.5,129.46),(403.7,375.34),(574.1,565.78),(809.9,802.78),(826.8,819.7)],key=lambda p:abs(p[0]-old_y))
                if abs(target[0]-old_y)<4:block['y']=target[1];block['positioned']=False
            block.pop('previewGlyphs',None)
        for suffix,text,x,w in [('nameRaw','聖餐式 성찬식',1.92,156.48),('detailRaw','コリント人への手紙 第一 11:23-29\n韓日285章',158.4,297.36),('leader','一同 다같이',455.76,137.64)]:
            source=next(b for b in page['blocks'] if b['binding']=='service.11.'+suffix);b=copy.deepcopy(source)
            b.update(id='communion-'+suffix,binding='communion.'+suffix,x=x,y=521,w=w,h=44,positioned=False,fieldBindings=[{'field':'communion.'+suffix}]);b.pop('previewGlyphs',None)
            _replace(b,text,registry);page['blocks'].append(b)
            old[suffix]=text
        title=next(b for b in page['blocks'] if b['binding']=='serviceTitleJa');_replace(title,'聖 餐 礼 拝',registry);data['serviceTitleJa']=title['text'];data['serviceTitleKo']='성찬예배';page['backgroundData']=background()
    elif not same:
        normal=result.get('communionNormalPage')
        if not normal:raise ValueError('일반 예배 배치 정보를 찾지 못했습니다. 이전 작업으로 되돌려주세요.')
        current={b['id']:b for b in page['blocks']};restored=copy.deepcopy(normal)
        for b in restored['blocks']:
            changed=current.get(b['id'])
            if changed and b.get('binding')!='serviceTitleJa':
                b['fieldBindings']=copy.deepcopy(changed.get('fieldBindings',[]));_replace(b,changed['text'],registry)
        existing={b['id'] for b in restored['blocks']};restored['blocks'].extend(copy.deepcopy(b) for b in page['blocks'] if b['id'] not in existing and not b['id'].startswith('communion-'))
        result['pages'][1]=page=restored;title=next(b for b in page['blocks'] if b['binding']=='serviceTitleJa');data['serviceTitleJa']=title['text'];data['serviceTitleKo']='주일예배';result.pop('communionNormalPage',None)
    data['communion']=dict(old,enabled=enabled,date=data['date'],manual=manual,firstSunday=suggested(data['date']))
    _canonical_fields(data,result['pages']);result['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in result['pages'] for b in p['blocks'] for a in b.get('fieldBindings',[])]
    validate_document(result);return result
