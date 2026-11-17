"""Exact scripture lookup. AI may interpret references, never author Bible text."""
import json, os, pathlib, re, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'system' / 'scripture-provider.json'
SESSION_AI_MODEL = None
KO = '창세기 출애굽기 레위기 민수기 신명기 여호수아 사사기 룻기 사무엘상 사무엘하 열왕기상 열왕기하 역대상 역대하 에스라 느헤미야 에스더 욥기 시편 잠언 전도서 아가 이사야 예레미야 예레미야애가 에스겔 다니엘 호세아 요엘 아모스 오바댜 요나 미가 나훔 하박국 스바냐 학개 스가랴 말라기 마태복음 마가복음 누가복음 요한복음 사도행전 로마서 고린도전서 고린도후서 갈라디아서 에베소서 빌립보서 골로새서 데살로니가전서 데살로니가후서 디모데전서 디모데후서 디도서 빌레몬서 히브리서 야고보서 베드로전서 베드로후서 요한일서 요한이서 요한삼서 유다서 요한계시록'.split()
IDS = 'GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP HAG ZEC MAL MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV'.split()
JA = '創世記|出エジプト記|レビ記|民数記|申命記|ヨシュア記|士師記|ルツ記|サムエル記 第一|サムエル記 第二|列王記 第一|列王記 第二|歴代誌 第一|歴代誌 第二|エズラ記|ネヘミヤ記|エステル記|ヨブ記|詩篇|箴言|伝道者の書|雅歌|イザヤ書|エレミヤ書|哀歌|エゼキエル書|ダニエル書|ホセア書|ヨエル書|アモス書|オバデヤ書|ヨナ書|ミカ書|ナホム書|ハバクク書|ゼパニヤ書|ハガイ書|ゼカリヤ書|マラキ書|マタイの福音書|マルコの福音書|ルカの福音書|ヨハネの福音書|使徒の働き|ローマ人への手紙|コリント人への手紙 第一|コリント人への手紙 第二|ガラテヤ人への手紙|エペソ人への手紙|ピリピ人への手紙|コロサイ人への手紙|テサロニケ人への手紙 第一|テサロニケ人への手紙 第二|テモテへの手紙 第一|テモテへの手紙 第二|テトスへの手紙|ピレモンへの手紙|ヘブル人への手紙|ヤコブの手紙|ペテロの手紙 第一|ペテロの手紙 第二|ヨハネの手紙 第一|ヨハネの手紙 第二|ヨハネの手紙 第三|ユダの手紙|ヨハネの黙示録'.split('|')
VERSIONS = {'ko':'개역개정', 'ja':'新改訳2017'}

def config():
    value=json.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else {}
    if SESSION_AI_MODEL:value['aiModel']=SESSION_AI_MODEL
    return value

def configure_ai(key, model):
    global SESSION_AI_MODEL
    if not isinstance(key,str) or not key.startswith('sk-') or not 20<=len(key)<=512 or any(c.isspace() for c in key):raise ValueError('OpenAI API 키 형식을 확인해주세요.')
    if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',model):raise ValueError('사용할 모델 ID를 입력해주세요.')
    os.environ['BULLETIN_AI_KEY']=key;SESSION_AI_MODEL=model
    return {'configured':True,'message':'이 실행 세션에 설정했습니다. AI 검토를 실행해 연결을 확인하세요. 서버를 재시작하면 이 설정은 사라집니다.'}

def status():
    c = config()
    import ai_client
    return {'ai': bool(ai_client.current() or (os.environ.get('BULLETIN_AI_KEY') and c.get('aiModel'))),
            'officialSources': json.loads((ROOT/'system'/'scripture-official-sources.json').read_text(encoding='utf-8')),
            'languages': {lang: bool(c.get(lang, {}).get('file') or c.get(lang, {}).get('url')) for lang in VERSIONS}}

def compact(s):
    return re.sub(r'\s+', '', s).lower()

def parse_reference(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 150:
        raise ValueError('성경 책·장·절을 입력해주세요.')
    value = value.strip().replace('～','-').replace('~','-').replace('–','-').replace('〜','-')
    m = re.fullmatch(r'(.+?)\s*(\d+)\s*(?:장|章)?\s*(?:(?::|의)?\s*(\d+(?:\s*[-,、]\s*\d+)*)\s*(?:절|節)?)?', value)
    if not m: raise ValueError('예: 요한복음 3:16-21 또는 요한복음 3장')
    names = {compact(n): code for names in (KO, JA, IDS) for n, code in zip(names, IDS)}
    book = names.get(compact(m[1]))
    if not book: raise ValueError('성경 책 이름을 전체 이름으로 입력해주세요.')
    chapter = int(m[2]); verses = None
    if not 1 <= chapter <= 150: raise ValueError('장 번호를 확인해주세요.')
    if m[3]:
        verses = []
        for token in re.split('[,、]',m[3]):
            ends = [int(v.strip()) for v in token.split('-')]
            a,b = (ends[0],ends[0]) if len(ends)==1 else ends
            if not 1 <= a <= b <= 176: raise ValueError('절 범위를 확인해주세요.')
            verses.extend(range(a,b+1))
        if verses != sorted(set(verses)): raise ValueError('절 범위를 순서대로 중복 없이 입력해주세요.')
    return {'book':book,'chapter':chapter,'verses':verses}

def post_json(url, payload, key=''):
    if not url.startswith('https://'): raise ValueError('외부 연결은 HTTPS 주소만 허용합니다.')
    headers = {'Content-Type':'application/json'}
    if key: headers['Authorization'] = 'Bearer '+key
    request = urllib.request.Request(url,json.dumps(payload).encode(),headers)
    try:
        with urllib.request.urlopen(request,timeout=25) as r:
            raw = r.read(2_000_001)
        if len(raw)>2_000_000: raise ValueError('조회 응답이 너무 큽니다.')
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('외부 서비스 연결에 실패했습니다. 연결 설정을 확인해주세요.') from None

def interpret(value, c):
    try: return parse_reference(value), 'rule'
    except ValueError as original:
        import ai_client
        if ai_client.current():
            result=ai_client.complete_json('Normalize only a Bible reference. Never generate Bible text. Return {"reference":"full Korean book name chapter:verses"}; ambiguous input must return {"reference":"UNKNOWN"}.',{'reference':value},300)
            return parse_reference(result.get('reference','UNKNOWN')), 'ai'
        key = os.environ.get('BULLETIN_AI_KEY','')
        if not key or not c.get('aiModel'): raise original
    response = post_json('https://api.openai.com/v1/responses', {
        'model':c['aiModel'], 'store':False,
        'instructions':'성경 구절 표기를 정규화한다. 본문을 생성하지 않는다. 책 전체 이름과 장:절만 출력한다. 불명확하거나 여러 장이면 UNKNOWN을 출력한다. 설명과 마크다운 금지.',
        'input':value, 'max_output_tokens':120}, key)
    text = ''.join(part.get('text','') for item in response.get('output',[]) for part in item.get('content',[]) if part.get('type')=='output_text')
    return parse_reference(text.strip()), 'ai'

def load_language(lang, ref, c):
    p = c.get(lang,{})
    if not p.get('file') and not p.get('url'):
        raise ValueError(VERSIONS[lang]+' 본문 연결이 아직 설정되지 않았습니다. 사용 가능한 데이터 파일 또는 공식 조회 서비스를 연결해주세요.')
    if not p.get('source') or not (p.get('permissionConfirmed') or (p.get('file') and p.get('userSupplied'))):
        raise ValueError(VERSIONS[lang]+' 데이터 출처와 사용 허락 확인이 필요합니다.')
    if p.get('file'):
        path = (ROOT / p['file']).resolve()
        if not path.is_relative_to(ROOT): raise ValueError('성경 파일은 프로젝트 폴더 안에 두세요.')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('version') != VERSIONS[lang]: raise ValueError('요청한 성경 번역본과 데이터 판본이 다릅니다.')
        chapter = data.get('books',{}).get(ref['book'],{}).get(str(ref['chapter']))
        if not chapter: raise ValueError(VERSIONS[lang]+'에 해당 장이 없습니다.')
        for entry in data.get('blockedRanges',[]):
            if entry['book']==ref['book'] and entry['chapter']==ref['chapter'] and (ref['verses'] is None or set(ref['verses']).intersection(entry['verses'])):
                raise ValueError('제공된 JSON의 해당 범위에 합쳐진 절 또는 잘못된 절 번호가 있습니다. 원문 확인이 필요합니다: '+entry['key'])
        result = {'version':data['version'],'verses':chapter,'chapterComplete':True}
    else:
        result = post_json(p['url'],{'version':VERSIONS[lang],**ref},os.environ.get(p.get('keyEnv',''),' ' ).strip())
    if result.get('version') != VERSIONS[lang]: raise ValueError('조회 결과의 성경 번역본이 다릅니다.')
    rows = result.get('verses',{})
    if not isinstance(rows,dict) or not rows: raise ValueError('조회한 성경 본문이 비어 있습니다.')
    try: numbers = sorted(int(n) for n in rows)
    except (ValueError, TypeError): raise ValueError('절 번호 형식이 올바르지 않습니다.') from None
    wanted = ref['verses']
    if wanted is None:
        if not result.get('chapterComplete') or numbers != list(range(1,max(numbers)+1)):
            raise ValueError('장 전체의 완전한 본문인지 확인할 수 없습니다.')
        wanted = numbers
    if any(str(n) not in rows or not isinstance(rows[str(n)],str) or not rows[str(n)].strip() for n in wanted):
        raise ValueError(VERSIONS[lang]+'에 요청한 절이 누락되어 있습니다. 기존 본문을 유지합니다.')
    return wanted, '\n'.join(f'{n} {rows[str(n)]}' for n in wanted), p['source']

def lookup(value, language='both'):
    c = config(); ref, method = interpret(value,c)
    if language not in ('both','ko','ja'): raise ValueError('조회 언어를 확인해주세요.')
    if language!='both':
        numbers,text,source=load_language(language,ref,c)
        i=IDS.index(ref['book']);suffix=str(ref['chapter'])
        if ref['verses'] is not None:suffix+=':'+','.join(str(n) for n in numbers)
        return {language:text, 'refKo' if language=='ko' else 'refJa':(KO if language=='ko' else JA)[i]+' '+suffix,
                'provenance':{'versions':{language:VERSIONS[language]},'sources':{language:source},'reference':ref,'interpretation':method}}
    # All-or-nothing: never return just one translation.
    nk,ko,sk = load_language('ko',ref,c); nj,ja,sj = load_language('ja',ref,c)
    if nk != nj: raise ValueError('한국어와 일본어의 절 범위가 다릅니다.')
    i = IDS.index(ref['book']); suffix = str(ref['chapter'])
    if ref['verses'] is not None:
        groups=[]; start=end=nk[0]
        for n in nk[1:]:
            if n==end+1:end=n
            else:
                groups.append(str(start) if start==end else f'{start}-{end}');start=end=n
        groups.append(str(start) if start==end else f'{start}-{end}')
        suffix += ':'+','.join(groups)
    return {'ko':ko,'ja':ja,'refKo':KO[i]+' '+suffix,'refJa':JA[i]+' '+suffix,
            'provenance':{'versions':VERSIONS,'sources':{'ko':sk,'ja':sj},'reference':ref,'interpretation':method}}
