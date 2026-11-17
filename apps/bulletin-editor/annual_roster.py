"""Local annual roster import, date matching and measured table cells."""
import base64,copy,csv,datetime,hashlib,io,json,pathlib,zipfile,xml.etree.ElementTree as ET
ROOT=pathlib.Path(__file__).resolve().parents[2]
PATH=ROOT/'system'/'연간봉사대장.json'
HEADERS=['날짜','예배','설교자','통역자','봉헌기도','청소']
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
                if not 1<=idx<=6:continue
                v=c.find('m:v',ns);value=v.text if v is not None else ''
                if c.get('t')=='s':value=strings[int(value)]
                elif c.get('t')=='inlineStr':value=''.join(c.find('m:is',ns).itertext())
                if idx==1 and value and c.get('t') not in ('s','inlineStr','str'):
                    try:value=(datetime.date(1899,12,30)+datetime.timedelta(days=int(float(value)))).isoformat()
                    except ValueError:pass
                if c.find('m:f',ns) is not None:raise ValueError('대장에는 수식 대신 담당자 이름을 직접 입력해주세요.')
                cells[idx-1]=value or ''
            rows.append([cells.get(i,'') for i in range(6)])
        return rows
def import_records(data):
    rows=_rows(data);start=next((i for i,r in enumerate(rows) if r[:6]==HEADERS),None)
    if start is None:raise ValueError('대장 열은 날짜·예배·설교자·통역자·봉헌기도·청소 순서여야 합니다.')
    records=[];seen=set()
    for row in rows[start+1:]:
        row=(row+['']*6)[:6]
        if not any(row):continue
        date=datetime.date.fromisoformat(str(row[0])[:10]).isoformat();service=str(row[1]).replace('부','部').strip()
        if service not in ('1部','2部'):raise ValueError(date+': 예배를 1부 또는 2부로 입력해주세요.')
        key=(date,service)
        if key in seen:raise ValueError(date+' '+service+' 담당이 중복됩니다.')
        seen.add(key)
        if any(len(str(v))>300 for v in row):raise ValueError('각 담당 칸은 300자 이내로 입력해주세요.')
        records.append(dict(zip(HEADERS,[date,service,*[str(v).strip() for v in row[2:]]])))
    if len(records)>1000:raise ValueError('대장은 1000행 이내로 입력해주세요.')
    PATH.parent.mkdir(parents=True,exist_ok=True);stage=PATH.with_suffix('.tmp');stage.write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8');stage.replace(PATH)
    return {'count':len(records),'message':str(len(records))+'개 예배 일정을 이 PC에 등록했습니다. 빈 담당 칸은 미정으로 처리합니다.'}
def proposal(document):
    records=json.loads(PATH.read_text(encoding='utf-8')) if PATH.exists() else []
    index={(r['날짜'],r['예배']):r for r in records};date=datetime.date.fromisoformat(document['serviceData']['date']);current=[index.get((date.isoformat(),s),{}) for s in ('1部','2部')]
    future=[]
    for delta in (7,14):
        day=date+datetime.timedelta(days=delta)
        for service in ('1部','2部'):
            r=index.get((day.isoformat(),service),{});future.append(dict(date=day.isoformat(),service=service,prayer=r.get('봉헌기도',''),interpreter=r.get('통역자',''),cleaning=r.get('청소','')))
    changes={};warnings=[];preachers=list(dict.fromkeys(r.get('설교자','') for r in current if r.get('설교자')))
    if len(preachers)==1 and all(r.get('설교자') for r in current):changes['preacher']='説教 '+preachers[0]
    elif len(preachers)>1:changes['preacher']='説教 '+', '.join(s+' '+r['설교자'] for s,r in zip(('1部','2部'),current))
    else:warnings.append('이번 주 설교 담당이 대장에 없습니다. 기존 표기를 유지합니다.')
    if all(r.get('통역자') for r in current):changes['interpreters']='(通訳 '+', '.join(s+' '+r['통역자'] for s,r in zip(('1部','2部'),current))+')'
    else:warnings.append('이번 주 통역 담당이 미정입니다. 기존 표기를 유지합니다.')
    complete=all(all(r[k] for k in ('prayer','interpreter','cleaning')) for r in future)
    if not complete:warnings.append('다음 두 주의 봉사표에 미정인 칸이 있어 표 전체 자동 적용은 보류합니다.')
    for i in (0,2):
        if future[i]['cleaning']!=future[i+1]['cleaning']:complete=False;warnings.append(future[i]['date']+': 청소는 두 예배에 공통으로 입력해주세요.')
    plan={'date':date.isoformat(),'changes':changes,'roster':future if complete else [],'warnings':warnings,'registered':len(records)}
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
                _replace(b,value[key],registry);page['blocks'].append(b)
    result['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in result['pages'] for b in p['blocks'] for a in b.get('fieldBindings',[])]
    validate_document(result);return result
