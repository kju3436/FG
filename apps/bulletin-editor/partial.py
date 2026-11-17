"""Apply sparse content updates while preserving untouched PDF regions and edits."""
import copy, datetime

def merge_values(old, patch):
    result=copy.deepcopy(old)
    for key,value in patch.items():
        if isinstance(value,dict):result[key]=merge_values(result.get(key,{}) if isinstance(result.get(key),dict) else {},value)
        elif isinstance(value,list):
            previous=result.get(key,[])
            def previous_row(row,i):
                if isinstance(row,dict) and row.get('_rowKey'):
                    match=next((r for r in previous if r.get('_rowKey')==row['_rowKey']),None)
                    if match is not None:return match
                    if row['_rowKey'].startswith('existing-'):
                        n=int(row['_rowKey'].split('-')[-1])
                        if n<len(previous) and not previous[n].get('_rowKey'):return previous[n]
                    return {}
                return previous[i] if i<len(previous) and isinstance(previous[i],dict) else {}
            result[key]=[merge_values(previous_row(row,i),row) if isinstance(row,dict) else row for i,row in enumerate(value)]
        elif value is None:result[key]=''
        elif isinstance(value,str) and not value.strip():continue
        else:result[key]=value
    return result

def apply_partial(base,patch,wrap,width):
    if not isinstance(patch,dict):raise ValueError('부분 입력 형식을 확인해주세요.')
    def check(x):
        if isinstance(x,str) and len(x)>30000:raise ValueError('한 항목의 본문이 너무 깁니다.')
        if isinstance(x,dict):
            for v in x.values():check(v)
        if isinstance(x,list):
            if len(x)>32:raise ValueError('입력 행 수를 확인해주세요.')
            for v in x:check(v)
    check(patch)
    doc=copy.deepcopy(base);old=base.get('serviceData',{});data=merge_values(old,patch)
    doc['serviceData']=data;doc['warnings']=[];eight=len(doc['pages'])==8
    def text(bind,label,value,x,y,w,h,size=15,font='ko',pad=6,fill='#ffffff'):
        # Retain intentional styling/geometry edits on existing bound fields.
        b=dict(id='partial-'+bind,binding=bind,label=label,type='text',text=str(value),x=x,y=y,w=w,h=h,fontSize=size,lineHeight=1.4,font=font,fill=fill,color='#111111',padding=pad,align='left')
        styles=['x','y','w','h','fontSize','lineHeight','font','fill','color','padding','align']
        b['layoutBase']={k:b[k] for k in styles}
        previous=next((b for p in base['pages'] for b in p['blocks'] if b.get('binding')==bind),None)
        if previous:
            for k in styles:
                if previous.get(k)!=previous.get('layoutBase',{}).get(k,previous.get(k)):b[k]=previous[k]
        return b
    def measured(value,font,size,w):return size+(len(wrap(value,'BulletinJA' if font=='ja' else 'BulletinKO',size,w))-1)*size*1.4+12
    def fit(bind,label,value,rect,font='ko',preferred=15):
        x,y,w,h=rect
        for size in [preferred-i*.5 for i in range(int((preferred-12)*2)+1)]:
            if measured(value,font,size,w-12)<=h:return text(bind,label,value,x,y,w,h,size,font)
        raise ValueError(label+'이 현재 영역에 들어가지 않습니다. 8페이지 양식 또는 더 넓은 영역을 선택해주세요. 기존 지면은 유지됩니다.')
    def intersect(a,b):return a[0]<b[2]-.01 and a[2]>b[0]+.01 and a[1]<b[3]-.01 and a[3]>b[1]+.01
    def replace_rect(page,rect,blocks,bindings=()):
        x,y,w,h=rect;cut=(x,y,x+w,y+h);out=[]
        for b in doc['pages'][page]['blocks']:
            box=(b['x'],b['y'],b['x']+b['w'],b['y']+b['h'])
            if b.get('binding') in bindings:continue
            if not intersect(box,cut):out.append(b);continue
            if b['type']!='source':
                if not b.get('binding'):out.append(b) # Keep manually added text.
                continue
            l,t,r,bt=box;cl,ct,cr,cb=max(l,cut[0]),max(t,cut[1]),min(r,cut[2]),min(bt,cut[3])
            pieces=[(l,t,r,ct),(l,cb,r,bt),(l,ct,cl,cb),(cr,ct,r,cb)]
            for n,(a,c,d,e) in enumerate(pieces):
                if d-a<.1 or e-c<.1:continue
                nb=copy.deepcopy(b);sx,sy,ex,ey=b['crop']
                nb.update(id=b['id']+'-cut-'+str(n),x=a,y=c,w=d-a,h=e-c,crop=[sx+(a-l)*(ex-sx)/b['w'],sy+(c-t)*(ey-sy)/b['h'],sx+(d-l)*(ex-sx)/b['w'],sy+(e-t)*(ey-sy)/b['h']],rasterize=True)
                out.append(nb)
        doc['pages'][page]['blocks']=out+blocks
    def meaningful(key):return key in patch and (patch[key] is None or not isinstance(patch[key],str) or bool(patch[key].strip()))
    if meaningful('date'):
        try:d=datetime.date.fromisoformat(data['date'])
        except (ValueError,TypeError):raise ValueError('예배 날짜 형식을 확인해주세요.')
        replace_rect(0,(435,72,160,19),[text('cover-date','예배 날짜',f'今 日: {d.year}年{d.month}月{d.day}日',435,72,160,19,11.5,'ja',1)],['cover-date'])
    if meaningful('issue'):
        issue=str(data['issue'])
        if not issue.isdigit() or not 1<=int(issue)<=999999:raise ValueError('주보 호수는 1~6자리 양수로 입력해주세요.')
        replace_rect(0,(435,30,160,19),[text('cover-issue','주보 호수',f'週報 第{issue}号',435,30,160,19,12.5,'ja',1)],['cover-issue'])
    if any(meaningful(k) for k in ['serviceTitleJa','serviceTitleKo']):
        title=' / '.join(str(data.get(k,'')) for k in ['serviceTitleJa','serviceTitleKo'] if data.get(k))
        size=min(24,300/max(width(title,'BulletinKO',1),1))
        replace_rect(1,(2,2,310,39),[text('service-title','예배명',title,2,2,310,39,size,'ko',1)])
        if eight:
            replace_rect(0,(77,250,439,150),[text('cover-title','표지 예배명',title.replace(' / ','\n'),77,250,439,150,25,'ko',15)])
    if any(meaningful(k) for k in ['preacher','interpreters']):
        # Separate each staff line so an unknown counterpart is not erased.
        combined=next((b for b in doc['pages'][1]['blocks'] if b.get('binding')=='service-staff'),None)
        if combined:
            all_staff=[text('staff-'+k,label,label+': '+str(data.get(k,'')),314,y,279,19.5,10,'ko',2) for k,label,y in [('preacher','설교',2),('interpreters','통역',21.5)]]
            replace_rect(1,(314,2,279,39),all_staff,['service-staff'])
        for k,label,y in [('preacher','설교',2),('interpreters','통역',21.5)]:
            if meaningful(k):replace_rect(1,(314,y,279,19.5),[text('staff-'+k,label,label+': '+str(data.get(k,'')),314,y,279,19.5,10,'ko',2)])
    # Scripture updates touch only the selected language and section.
    for kind,part in patch.get('scripture',{}).items():
        if kind not in ('absolution','sermon') or not isinstance(part,dict):continue
        s=data.get('scripture',{}).get(kind,{})
        existing={b.get('binding'):b for p in doc['pages'] for b in p['blocks'] if b.get('binding')}
        body_top,body_bottom=((31,116) if kind=='absolution' else (144,824)) if eight else ((31,214) if kind=='absolution' else (257,823))
        title_top,title_bottom=((0,31) if kind=='absolution' else (116,144)) if eight else ((0,31) if kind=='absolution' else (214,257))
        for lang,refkey,x,w in [('ja','refJa',2,318),('ko','refKo',320,273)]:
            if refkey in part and lang not in part and not existing.get(kind+'-heading'):
                heading=('赦しの御言葉 ' if kind=='absolution' else '今日の御言葉 ') if lang=='ja' else ('사죄확인 ' if kind=='absolution' else '설교본문 ')
                label=heading+(s.get(refkey) or '')
                size=min(17,(w-8)/max(width(label,'BulletinJA' if lang=='ja' else 'BulletinKO',1),1))
                replace_rect(2,(x,title_top,w,title_bottom-title_top),[text(kind+'-ref-'+lang,heading,label,x,title_top,w,title_bottom-title_top,size,lang,2)])
            if part.get(lang) is not None and not str(part.get(lang,'')).strip():continue
            if lang not in part:continue
            bind=kind+'-'+lang;previous=existing.get(bind);page=2
            if previous:
                page=next(i for i,p in enumerate(doc['pages']) if previous in p['blocks'])
                rect=(previous['x'],previous['y'],previous['w'],previous['h'])
            else:rect=(x,body_top,w,body_bottom-body_top)
            b=fit(bind,('사죄확인' if kind=='absolution' else '설교본문')+' '+lang,s.get(lang,'') or '',rect,lang)
            replace_rect(page,rect,[b],[bind])
            ref=s.get(refkey,'')
            if ref and not existing.get(kind+'-heading'):
                heading=('赦しの御言葉 ' if kind=='absolution' else '今日の御言葉 ') if lang=='ja' else ('사죄확인 ' if kind=='absolution' else '설교본문 ')
                label=heading+ref;size=min(17,(w-8)/max(width(label,'BulletinJA' if lang=='ja' else 'BulletinKO',1),1))
                replace_rect(2,(x,title_top,w,title_bottom-title_top),[text(kind+'-ref-'+lang,heading,label,x,title_top,w,title_bottom-title_top,size,lang,2)])
        if existing.get(kind+'-heading') and any(k in part for k in ['refJa','refKo']):
            previous=existing[kind+'-heading'];page=next(i for i,p in enumerate(doc['pages']) if previous in p['blocks'])
            value=('赦しの御言葉 ' if kind=='absolution' else '今日の御言葉 ')+(s.get('refJa') or '')+' / '+(s.get('refKo') or '')
            b=copy.deepcopy(previous);b['text']=value
            replace_rect(page,(b['x'],b['y'],b['w'],b['h']),[b],[kind+'-heading'])
        # If both refs exist, flag mismatches without preventing partial drafting.
        if s.get('refJa') and s.get('refKo'):
            from scripture import parse_reference
            try:
                if parse_reference(s['refJa'])!=parse_reference(s['refKo']):doc['warnings'].append('한일 성경 범위가 다릅니다. 최종 출력 전에 확인해주세요.')
            except ValueError:doc['warnings'].append('성경 구절 표기를 확인해주세요.')
        if ('ja' in part)!=('ko' in part) or not s.get('ja') or not s.get('ko'):doc['warnings'].append('말씀의 입력하지 않은 언어는 기존 지면을 유지했습니다. 이번 주 본문과 맞는지 확인해주세요.')
    # Reuse established list typesetting while preserving the other sections.
    for group in ['service','notices','roster']:
        if group not in patch:continue
        from compose import compose, initial_data
        full=initial_data();full.update(date='2000-01-01',issue='1')
        full=merge_values(full,data)
        for kind in ['absolution','sermon']:
            full['scripture'][kind]={k:(full['scripture'][kind].get(k) or '—') if k.startswith('ref') else '—' for k in ['refJa','refKo','ja','ko']}
        # Other sections must not prevent this list update.
        for k in ['service','notices','roster']:
            if k!=group:full[k]=initial_data()[k]
        # Full compositor now accepts one-sided notices for partial reuse.
        candidate=compose({**base,'serviceData':{}},full,wrap,width)
        page=1 if group=='service' else len(doc['pages'])-1
        prefix={'service':'service-','notices':'notice-','roster':'roster-'}[group]
        generated=[b for b in candidate['pages'][page]['blocks'] if b.get('binding','').startswith(prefix)]
        if group=='notices' and sum(b.get('binding','').endswith('-ko') for b in generated)!=len(full['notices']):raise ValueError('교회소식이 현재 영역을 넘습니다. 기존 지면은 유지됩니다.')
        if group=='service':
            generated=[b for b in generated if b.get('binding') not in ('service-title','service-staff')]
            rect=(0,42,595,782)
        elif group=='notices':rect=(0,0,595,665)
        else:rect=(0,665,595,159)
        if any(b['y']<rect[1] or b['y']+b['h']>rect[1]+rect[3] for b in generated):
            raise ValueError('입력한 내용이 '+group+' 영역을 넘습니다. 기존 내용은 유지됩니다.')
        replace_rect(page,rect,generated)
    if data.get('date') and data.get('issue'):doc['title']=f'{data["date"]} 주일주보 제{data["issue"]}호'
    doc['partialInput']=True
    return doc
