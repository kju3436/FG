"""Local annual roster import, date matching and measured table cells."""
import base64,copy,csv,datetime,hashlib,io,json,pathlib,zipfile,xml.etree.ElementTree as ET
ROOT=pathlib.Path(__file__).resolve().parents[2]
PATH=ROOT/'system'/'연간봉사대장.json'
HEADERS=['날짜','예배','설교자','통역자','봉헌기도','청소']
TEMPLATES=ROOT/'자료'/'주보_입력양식'
def _rows(data):
    name=data.get('name','');raw=base64.b64decode(data.get('content',''),validate=True)
    if len(raw)>4000000:raise ValueError('대장은 4MB 이하 파일을 사용해주세요.')
    if name.lower().endswith('.csv'):return list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    if not name.lower().endswith('.xlsx'):raise ValueError('Excel .xlsx 또는 UTF-8 CSV 파일을 선택해주세요.')
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist())>25000000:raise ValueError('압축 해제 크기가 너무 큽니다.')
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(n.itertext()) for n in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si',ns)]
        root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'));rows=[]
        for row in root.findall('.//m:sheetData/m:row',ns):
            cells={}
            for c in row.findall('m:c',ns):
                col=''.join(v for v in c.get('r','') if v.isalpha());idx=0
                for v in col:idx=idx*26+ord(v)-64
                if not 1<=idx<=7:continue
                v=c.find('m:v',ns);value=v.text if v is not None else ''
                if c.get('t')=='s':value=strings[int(value)]
                elif c.get('t')=='inlineStr':value=''.join(c.find('m:is',ns).itertext())
                if idx in (1,7) and value and c.get('t') not in ('s','inlineStr','str'):
                    book=ET.fromstring(z.read('xl/workbook.xml'));pr=book.find('m:workbookPr',ns);epoch=datetime.date(1904,1,1) if pr is not None and pr.get('date1904') in ('1','true') else datetime.date(1899,12,30)
                    try:value=(epoch+datetime.timedelta(days=int(float(value)))).isoformat()
                    except ValueError:pass
                if c.find('m:f',ns) is not None:raise ValueError('대장에는 수식 대신 담당자 이름을 직접 입력해주세요.')
                cells[idx-1]=value or ''
            row_number=int(row.get('r','0'))
            if not 1<=row_number<=2000:raise ValueError('Excel 입력 범위는 2000행 이내로 사용해주세요.')
            while len(rows)<row_number:rows.append(['']*7)
            rows[row_number-1]=[cells.get(i,'') for i in range(7)]
        # The template shares date, cleaning and cleaning-day cells across 1부/2부.
        for merged in root.findall('.//m:mergeCells/m:mergeCell',ns):
            import re
            match=re.fullmatch(r'([AFG])(\d+):\1(\d+)',merged.get('ref',''))
            if not match:continue
            col='ABCDEFG'.index(match[1]);a,b=int(match[2])-1,int(match[3])-1
            if b-a!=1 or b>=len(rows):raise ValueError('날짜·청소 병합은 1부와 2부의 두 행만 묶어주세요.')
            rows[b][col]=rows[a][col]
        return rows
def import_records(data):
    rows=_rows(data);start=next((i for i,r in enumerate(rows) if r[:6]==HEADERS),None)
    if start is None:raise ValueError('대장 열은 날짜·예배·설교자·통역자·봉헌기도·청소 순서여야 합니다.')
    records=[];seen=set();has_clean_date=len(rows[start])>6 and rows[start][6]=='청소일'
    for line,row in enumerate(rows[start+1:],start+2):
        row=(row+['']*7)[:7]
        if not any(row):continue
        try:date=datetime.date.fromisoformat(str(row[0])[:10]).isoformat()
        except ValueError:raise ValueError(str(line)+'행 날짜를 YYYY-MM-DD로 입력해주세요.')
        service=str(row[1]).replace('부','部').replace(' ','').strip()
        if service not in ('1部','2部'):raise ValueError(date+': 예배를 1부 또는 2부로 입력해주세요.')
        key=(date,service)
        if key in seen:raise ValueError(date+' '+service+' 담당이 중복됩니다.')
        seen.add(key)
        if any(len(str(v))>300 for v in row):raise ValueError('각 담당 칸은 300자 이내로 입력해주세요.')
        record=dict(zip(HEADERS,[date,service,*[str(v).strip() for v in row[2:6]]]))
        if has_clean_date and row[6]:
            try:record['청소일']=datetime.date.fromisoformat(str(row[6])[:10]).isoformat()
            except ValueError:raise ValueError(str(line)+'행 청소일을 YYYY-MM-DD로 입력해주세요.')
        records.append(record)
    if not records:raise ValueError('등록할 날짜와 예배가 없습니다. 기존 대장은 유지했습니다.')
    if len(records)>1000:raise ValueError('대장은 1000행 이내로 입력해주세요.')
    by_date={}
    for r in records:
        bucket=by_date.setdefault(r['날짜'],{})
        for key in ('청소','청소일'):
            if r.get(key) and bucket.get(key) and r[key]!=bucket[key]:raise ValueError(r['날짜']+': 1부·2부의 공통 '+key+'이 다릅니다.')
            if r.get(key):bucket[key]=r[key]
    for r in records:
        for key,value in by_date[r['날짜']].items():r[key]=value
    imported_count=len(records)
    if PATH.exists():
        old=json.loads(PATH.read_text(encoding='utf-8'));incoming={(r['날짜'],r['예배']) for r in records}
        records=[r for r in old if (r['날짜'],r['예배']) not in incoming]+records
        records.sort(key=lambda r:(r['날짜'],r['예배']))
        PATH.with_suffix('.previous.json').write_bytes(PATH.read_bytes())
    PATH.parent.mkdir(parents=True,exist_ok=True);stage=PATH.with_suffix('.tmp');stage.write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8');stage.replace(PATH)
    return {'count':len(records),'imported':imported_count,'years':sorted({r['날짜'][:4] for r in records}),'message':str(imported_count)+'개 예배 일정을 반영했습니다. 다른 날짜의 기존 일정은 유지하며, 이번 파일의 빈 담당 칸은 미정으로 처리합니다.'}
def proposal(document):
    records=json.loads(PATH.read_text(encoding='utf-8')) if PATH.exists() else []
    index={(r['날짜'],r['예배']):r for r in records};date=datetime.date.fromisoformat(document['serviceData']['date']);current=[index.get((date.isoformat(),s),{}) for s in ('1部','2部')]
    future=[]
    for delta in (7,14):
        day=date+datetime.timedelta(days=delta)
        for service in ('1部','2部'):
            r=index.get((day.isoformat(),service),{});future.append(dict(date=day.isoformat(),service=service,prayer=r.get('봉헌기도','') or '미정',interpreter=r.get('통역자','') or '미정',cleaning=r.get('청소','') or '미정',cleaningDate=r.get('청소일','')))
    changes={};warnings=[];preachers=list(dict.fromkeys(r.get('설교자','') for r in current if r.get('설교자')))
    if len(preachers)==1 and all(r.get('설교자') for r in current):changes['preacher']='説教 '+preachers[0]
    elif preachers:
        changes['preacher']='説教 '+', '.join(s+' '+(r.get('설교자') or '미정') for s,r in zip(('1部','2部'),current))
        if any(not r.get('설교자') for r in current):warnings.append('이번 주 설교 담당에 미정인 예배가 있습니다.')
    else:changes['preacher']='説教 미정';warnings.append('이번 주 설교 담당이 대장에 없습니다.')
    changes['interpreters']='(通訳 '+', '.join(s+' '+(r.get('통역자') or '미정') for s,r in zip(('1部','2部'),current))+')'
    if any(not r.get('통역자') for r in current):warnings.append('이번 주 통역 담당에 미정인 칸이 있습니다.')
    for i,row in enumerate(document['serviceData'].get('service',[])):
        if row.get('ko')=='봉헌기도':changes[f'service.{i}.leader']='\n'.join(s+' '+(r.get('봉헌기도') or '미정') for s,r in zip(('1部','2部'),current))
    if any('미정'==r[k] for r in future for k in ('prayer','interpreter','cleaning')):warnings.append('다음 두 주의 빈 담당 칸은 미정으로 표시합니다.')
    for i in (0,2):
        if future[i]['cleaning']=='미정' and future[i+1]['cleaning']!='미정':future[i]['cleaning']=future[i+1]['cleaning']
        elif future[i+1]['cleaning']=='미정' and future[i]['cleaning']!='미정':future[i+1]['cleaning']=future[i]['cleaning']
        elif future[i]['cleaning']!=future[i+1]['cleaning']:raise ValueError(future[i]['date']+': 청소 담당이 1부·2부에서 다릅니다. 공통 담당을 확인해주세요.')
        dates={r['cleaningDate'] for r in future[i:i+2] if r['cleaningDate']}
        if len(dates)>1:raise ValueError(future[i]['date']+': 공통 청소일이 서로 다릅니다.')
        for r in future[i:i+2]:r['cleaningDate']=next(iter(dates),'')
    years=sorted({r['날짜'][:4] for r in records})
    if records and str(date.year) not in years:warnings.insert(0,str(date.year)+'년 일정이 등록된 대장에 없습니다. 해당 연도의 대장을 불러와주세요.')
    plan={'date':date.isoformat(),'changes':changes if records else {},'roster':future if records else [],'warnings':warnings if records else [],'registered':len(records),'years':years}
    plan['fingerprint']=hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest();return plan
def apply(document,expected=None):
    from native_template import update_document,_replace,validate_document
    plan=proposal(document)
    if expected and plan['fingerprint']!=expected:raise ValueError('대장 또는 날짜가 바뀌었습니다. 다시 확인해주세요.')
    result=update_document(document,plan['changes']);registry={f['id']:f for f in result['fonts']}
    if plan['roster']:
        page=result['pages'][3];source=next((b for b in page['blocks'] if b.get('binding')=='rosterRaw.0'),None) or next((b for b in page['blocks'] if b['id'].startswith('annual-roster-')),None)
        if source is None:raise ValueError('봉사표 기준 영역을 찾지 못했습니다.')
        page['blocks']=[b for b in page['blocks'] if b.get('binding') not in ('rosterRaw.0','rosterRaw.1') and not b['id'].startswith('annual-roster-')]
        result['serviceData']['roster']=[]
        for i,r in enumerate(plan['roster']):
            day=datetime.date.fromisoformat(r['date']);value=dict(r,date=f'{day.month}/{day.day}');result['serviceData']['roster'].append(value);top=718+(i//2)*52+(i%2)*26
            cells=[('service',86,48,top,25),('prayer',137,137,top,25),('interpreter',280,142,top,25)]
            if i%2==0:cells += [('date',36,47,top,51),('cleaning',428,163,top,51)]
            for key,x,w,y,h in cells:
                b=copy.deepcopy(source);b.update(id=f'annual-roster-{i}-{key}',binding=f'roster.{i}.{key}',x=x,y=y,w=w,h=h,fieldBindings=[{'field':f'roster.{i}.{key}'}]);b.pop('previewGlyphs',None);b.pop('format',None)
                text=value[key]
                if key=='cleaning' and r.get('cleaningDate'):
                    cd=datetime.date.fromisoformat(r['cleaningDate']);text+='\n'+f'{cd.month}/{cd.day}'
                # Make even identical old/new text use the measured cell geometry.
                b['positioned']=False;_replace(b,text,registry);page['blocks'].append(b)
    result['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in result['pages'] for b in p['blocks'] for a in b.get('fieldBindings',[])]
    result['annualRoster']={'date':plan['date'],'fingerprint':plan['fingerprint'],'warnings':plan['warnings'],'years':plan['years']}
    validate_document(result);return result
