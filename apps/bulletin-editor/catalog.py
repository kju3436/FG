"""Click selectors: metadata from local sources, exact text from existing providers.

No AI, online scripture collection or PPT source modifications. Catalog replies
contain titles/numbers only, never source file paths or an entire Bible corpus.
"""
import hashlib,json,pathlib
import scripture
ROOT=pathlib.Path(__file__).resolve().parents[2]

def _bible():
    c=scripture.config();value=c.get('ko',{}).get('file')
    if not value:return {},[]
    path=(ROOT/value).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():return {},[]
    d=json.loads(path.read_text(encoding='utf-8-sig'))
    if d.get('version')!='개역개정':return {},[]
    return d.get('books',{}),d.get('blockedRanges',[])

def songs():
    p=ROOT/'system'/'library-index.json'
    index=json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
    hymns={}
    for entry in index.get('hymns',[]):
        number=str(entry.get('number',''))
        if not number.isdigit():continue
        number=int(number)
        if number not in hymns or entry.get('preferred'):
            hymns[number]={'id':f'hanil-{number:03}','kind':'hymn','number':number,'title':entry.get('title',''),'preferred':bool(entry.get('preferred'))}
    from hymn_metadata import records,format_song
    metadata=records();result=[hymns[n] for n in sorted(hymns)]
    for song in result:
        song.update(metadata.get(song['number'],{}))
        if song.get('titleKo'):song['title']=song['titleKo']
        song['bulletinText']=format_song(song)
        song['metadataComplete']=bool(song.get('titleJa') and song.get('newKoreanStatus')!='확인 필요')
    return result

def catalog():
    books,blocked=_bible();result=[]
    for i,(code,ko,ja) in enumerate(zip(scripture.IDS,scripture.KO,scripture.JA)):
        chapters=[]
        for number,verses in sorted(books.get(code,{}).items(),key=lambda item:int(item[0])):
            available={int(n) for n in verses if str(n).isdigit() and 1<=int(n)<=176}
            problem={n for r in blocked if r.get('book')==code and r.get('chapter')==int(number) for n in r.get('verses',[])}
            chapters.append({'number':int(number),'verses':sorted(available|problem),'blocked':sorted(problem)})
        result.append({'id':code,'ko':ko,'ja':ja,'testament':'old' if i<39 else 'new','chapters':chapters})
    return {'books':result,'songs':songs(),'japaneseConnected':bool(scripture.config().get('ja',{}).get('file') or scripture.config().get('ja',{}).get('url')),
            'numbering':'한일찬송가 번호','note':'장·절 목록은 등록된 개역개정 데이터 기준입니다. 판본별 절 차이는 원문으로 확인해주세요.'}

def _reference(data):
    if data.get('reference'):
        ref=scripture.parse_reference(data['reference'])
    else:
        code=data.get('book');chapter=data.get('chapter');start=data.get('start');end=data.get('end')
        if code not in scripture.IDS or any(type(v)is not int for v in (chapter,start,end)) or not 1<=start<=end<=176:
            raise ValueError('성경 책·장·시작 절·마지막 절을 순서대로 선택해주세요.')
        ref={'book':code,'chapter':chapter,'verses':list(range(start,end+1))}
    books,blocked=_bible();chapter=books.get(ref['book'],{}).get(str(ref['chapter']))
    if not chapter:raise ValueError('등록된 성경에 해당 장이 없습니다.')
    wanted=ref['verses']
    if wanted and any(str(n) not in chapter for n in wanted):
        if any(r.get('book')==ref['book'] and r.get('chapter')==ref['chapter'] and set(wanted).intersection(r.get('verses',[])) for r in blocked):
            raise ValueError('해당 범위는 제공된 성경 데이터의 절 번호를 먼저 확인해야 합니다. 기존 말씀은 유지했습니다.')
        raise ValueError('해당 장에 없는 절 번호입니다. 기존 말씀은 유지했습니다.')
    return ref

def _suffix(chapter,verses):
    if verses is None:return str(chapter)
    groups=[];start=end=verses[0]
    for n in verses[1:]:
        if n==end+1:end=n
        else:groups.append(str(start) if start==end else f'{start}-{end}');start=end=n
    groups.append(str(start) if start==end else f'{start}-{end}')
    return str(chapter)+':'+','.join(groups)

def preview(data):
    ref=_reference(data);config=scripture.config()
    numbers,ko,source=scripture.load_language('ko',ref,config)
    i=scripture.IDS.index(ref['book']);suffix=_suffix(ref['chapter'],numbers if ref['verses'] is not None else None)
    result={'reference':ref,'refKo':scripture.KO[i]+' '+suffix,'refJa':scripture.JA[i]+' '+suffix,'ko':ko,'ja':None,'source':source,
            'japaneseMessage':'新改訳2017은 원문을 붙여넣어주세요. 자동 번역하거나 본문을 생성하지 않습니다.'}
    if config.get('ja',{}).get('file') or config.get('ja',{}).get('url'):
        try:
            jnumbers,ja,_=scripture.load_language('ja',ref,config)
            if jnumbers!=numbers:raise ValueError('한국어와 일본어의 절 범위가 다릅니다.')
            result.update(ja=ja,japaneseMessage='등록된 일본어 원문을 불러왔습니다.')
        except ValueError as e:result['japaneseMessage']=str(e)
    result['sameCurrent']=same_reference(data.get('currentReference',''),result['refKo'])
    return result

def same_reference(a,b):
    try:return scripture.parse_reference(a)==scripture.parse_reference(b)
    except ValueError:return False

def apply_scripture(document,data):
    from native_template import update_document
    kind=data.get('kind')
    if kind=='call':
        value=preview(data)
        if 'expectedKo' in data and value['ko']!=data['expectedKo']:raise ValueError('성경 데이터가 바뀌었습니다. 구절을 다시 확인해주세요.')
        rows=document['serviceData'].get('service',[])
        row=next((i for i,r in enumerate(rows) if str(r.get('ko','')).replace(' ','')=='예배로의부르심'),None)
        if row is None:raise ValueError('예배로의 부르심 영역을 찾지 못했습니다.')
        return update_document(document,{f'service.{row}.detailRaw':'…………………… '+value['refJa']+' ……………………'})
    if kind not in ('sermon','absolution'):raise ValueError('적용할 말씀 항목을 선택해주세요.')
    value=preview(data);existing=document['serviceData']['scripture'][kind]
    if 'expectedKo' in data and value['ko']!=data['expectedKo']:raise ValueError('성경 데이터가 바뀌었습니다. 본문을 다시 확인해주세요.')
    japanese=data.get('japanese')
    if not isinstance(japanese,str):
        japanese=value['ja'] if value['ja'] is not None else existing.get('ja','') if same_reference(existing.get('refKo',''),value['refKo']) else ''
    if not japanese.strip() and data.get('clearJapanese') is not True:
        raise ValueError('일본어 원문을 입력하거나 일본어 나중에 입력을 선택해주세요.')
    base='scripture.'+kind+'.'
    return update_document(document,{base+'refKo':value['refKo'],base+'refJa':value['refJa'],base+'ko':value['ko'],base+'ja':japanese})

def apply_song(document,data):
    from native_template import update_document
    row=data.get('row');rows=document['serviceData'].get('service',[])
    if type(row)is not int or not 0<=row<len(rows) or rows[row].get('ko') not in ('모임찬송','찬송','감사찬송','봉헌','찬양'):
        raise ValueError('찬송가·찬양 항목을 선택해주세요.')
    selected=next((song for song in songs() if song['id']==data.get('id')),None)
    if selected is None:raise ValueError('승인 목록에서 곡을 선택해주세요.')
    if 'expectedTitle' in data and data['expectedTitle']!=selected['title']:raise ValueError('곡 목록이 바뀌었습니다. 다시 선택해주세요.')
    if 'expectedText' in data and data['expectedText']!=selected['bulletinText']:raise ValueError('찬송가 대장이 바뀌었습니다. 다시 선택해주세요.')
    text=selected['bulletinText']
    return update_document(document,{f'service.{row}.detailRaw':text})
