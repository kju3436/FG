"""Local week snapshots for the simple four-page church workflow."""
import copy,datetime,json,pathlib,re,threading,uuid
ROOT=pathlib.Path(__file__).resolve().parents[2]
STATE=ROOT/'output'/'주보_작업보관'
LOCK=threading.RLock()
_FULL_ENGINE_READY=False

def ensure_engine():
    global _FULL_ENGINE_READY
    import native_template
    with LOCK:
        if not _FULL_ENGINE_READY:
            native_template.enable_full_fonts()
            _FULL_ENGINE_READY=True

def validate(document):
    ensure_engine()
    from native_template import validate_document
    if not isinstance(document,dict) or document.get('engine')!='native-v2':raise ValueError('우리 교회 4페이지 주보 작업 파일을 선택해주세요.')
    validate_document(document)
    repair_layout(document)

def preview(document):
    """Use the renderer's glyph positions for exact browser/PDF line agreement."""
    from native_template import _layout,_friendly_label,_width
    repair_layout(document)
    registry={f['id']:f for f in document['fonts']}
    for page in document['pages']:
        for b in page['blocks']:
            b['label']=_friendly_label(b.get('binding',''),b.get('group',''),b.get('text',''))
            if not b.get('styleSeed') and b.get('runs'):
                b['styleSeed']=[{k:copy.deepcopy(v) for k,v in r.items() if k!='glyphs'} for r in b['runs']]
            if b.get('positioned') is False:
                b['previewGlyphs']=[{'text':ch,'x':x,'baseline':y,'w':_width(registry[run['fontId']],ch,size)*run.get('widthScale',1),'fontSize':size,'fontId':run['fontId'],'color':run['color']} for ch,x,y,size,run in _layout(b,registry)]
    return document

def repair_layout(document):
    """Upgrade old work without replacing its content with sample content."""
    if document.get('printLayoutVersion')==2:return document
    from native_template import native_template_document,_layout_spec,_map_runs,_canonical_fields,_set
    base=native_template_document();references={b['binding']:b for p in base['pages'] for b in p['blocks']}
    registry={f['id']:f for f in document['fonts']}
    for page in document['pages']:
        for b in page['blocks']:
            ref=references.get(b.get('binding'))
            b['runs']=_map_runs(b['runs'],registry)
            if ref:
                for run in b['runs']:
                    if not run.get('glyphs') and 'widthScale' not in run:
                        peers=[r for r in ref['runs'] if r['fontId']==run['fontId'] and r.get('widthScale')]
                        if peers:run['widthScale']=sum(r['widthScale']*len(r['text']) for r in peers)/sum(len(r['text']) for r in peers)
            if ref and not b.get('layoutSpec'):b['layoutSpec']=_layout_spec(ref)
            if ref and re.fullmatch(r'service\.\d+\.detailRaw',b.get('binding','')):
                lines=ref['text'].splitlines()
                if len(lines)>1 and not re.search('[가-힣]',lines[0]) and re.search('[가-힣]',b['text']) and '\n' not in b['text']:
                    from native_template import _replace
                    at=re.search('[가-힣]',b['text']).start()
                    _replace(b,b['text'][:at].rstrip()+'\n'+b['text'][at:],registry)
                    _set(document['serviceData'],b['binding'],b['text'])
    if document['serviceData'].get('communion',{}).get('enabled'):
        import communion
        communion.measured_page(document)
    _canonical_fields(document['serviceData'],document['pages'])
    document['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in document['pages'] for b in p['blocks'] for a in b.get('fieldBindings',[])]
    document['printLayoutVersion']=2
    return document

def _write(path,document):
    STATE.mkdir(parents=True,exist_ok=True)
    stage=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    stage.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8');stage.replace(path)

def initial():
    ensure_engine()
    from native_template import native_template_document
    current=STATE/'current.json'
    if current.exists():
        try:
            doc=json.loads(current.read_text(encoding='utf-8'));validate(doc);return preview(doc)
        except (ValueError,OSError):pass
    return preview(native_template_document())

def save_current(document):
    validate(document)
    document=copy.deepcopy(document);document['savedAt']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    with LOCK:_write(STATE/'current.json',document)
    return {'saved':True}

def archives():
    results=[]
    if STATE.exists():
        for file in STATE.glob('week-*.json'):
            try:
                d=json.loads(file.read_text(encoding='utf-8'));data=d.get('serviceData',{})
                results.append({'id':file.stem,'date':data.get('date',''),'issue':data.get('issue',''),'savedAt':d.get('archivedAt','')})
            except (ValueError,OSError):continue
    return sorted(results,key=lambda d:(d['date'],d['savedAt']),reverse=True)

def load_archive(identifier):
    if identifier=='baseline':
        from native_template import native_template_document
        return preview(native_template_document())
    if not re.fullmatch(r'week-[a-f0-9]{32}',identifier):raise ValueError('보관한 주보를 선택해주세요.')
    file=STATE/(identifier+'.json')
    if not file.exists():raise ValueError('해당 주보 파일을 찾지 못했습니다.')
    doc=json.loads(file.read_text(encoding='utf-8'));validate(doc);return preview(doc)

def archive(document):
    validate(document);doc=copy.deepcopy(document)
    date=doc.get('serviceData',{}).get('date','')
    try:datetime.date.fromisoformat(date)
    except (TypeError,ValueError):raise ValueError('보관할 주보의 예배 날짜를 확인해주세요.')
    doc['archivedAt']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    identifier='week-'+uuid.uuid4().hex
    with LOCK:_write(STATE/(identifier+'.json'),doc);_write(STATE/'current.json',doc)
    return {'id':identifier,'date':date,'saved':True}

def next_week(document):
    from native_template import update_document
    validate(document);data=document['serviceData']
    try:date=datetime.date.fromisoformat(data['date'])+datetime.timedelta(days=7);issue=int(data['issue'])+1
    except (ValueError,KeyError,TypeError):raise ValueError('기준 주보의 날짜와 호수를 확인해주세요.')
    result=update_document(document,{'date':date.isoformat(),'issue':str(issue)})
    result.setdefault('warnings',[]).append('지난 주의 말씀·소식·봉사 담당은 유지했습니다. 이번 주에 바뀐 부분만 확인해주세요.')
    return result
