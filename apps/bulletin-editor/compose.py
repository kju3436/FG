"""Provider-independent service data -> editable bulletin regions."""
import copy, datetime, math

SERVICE = [
 ('↑ 賛美','모임찬송','主をほめたたえよ。','주를 찬양하라','聖徒一同 / 성도 다같이'),
 ('↓ 礼拝への招き','예배로의 부르심','詩篇 124:8','시편 124:8','牧師 / 목사'),
 ('↑ 罪の公的な告白','죄의 공적 고백','詩篇 51:17','시편 51:17','聖徒一同 / 성도 다같이'),
 ('↓ 赦しの宣言','사죄확인','','','牧師 / 목사'),
 ('↑ 賛美','찬송','韓日 002','한일찬송가 002장','一同起立 / 다같이 일어서서'),
 ('↑ 信仰告白','신앙고백','使徒信条','사도신경','聖徒一同 / 성도 다같이'),
 ('↑ 感謝賛美','감사찬송','','','一同着席 / 다같이 앉아서'),
 ('↑ 牧会祈祷','목회기도','','','牧師 / 목사'),
 ('↓ 聖書奉読','성경봉독','','','牧師 / 목사'),
 ('↑ 讃揚','찬양','','','讃揚隊 / 찬양대'),
 ('↓ 言葉の宣布','말씀선포','','','牧師 / 목사'),
 ('↑ 奉献祈祷','봉헌기도','','',''),
 ('↑ 奉献','봉헌','','','聖徒一同 / 성도 다같이'),
 ('↑↓ 聖徒の交わり','성도의 교제','お知らせ','교회소식','聖徒一同 / 성도 다같이'),
 ('↓ 祝福の祈り','축복기도','','','牧師 / 목사'),
 ('↑ 主の祈り','주기도','主の祈り','주기도송','一同起立 / 다같이 일어서서'),
 ('↓ 祝祷','강복선언','','','牧師 / 목사')]

def initial_data():
    d={'schemaVersion':1,'date':'','issue':'','serviceTitleJa':'主日礼拝','serviceTitleKo':'주일예배',
      'preacher':'','interpreters':'','service':[dict(zip(['ja','ko','detailJa','detailKo','leader'],r)) for r in SERVICE],
      'scripture':{'absolution':{'refJa':'','refKo':'','ja':'','ko':''},'sermon':{'refJa':'','refKo':'','ja':'','ko':''}},
      'notices':[],'roster':[],'sourceProvider':'paste','versions':{'ko':'개역개정','ja':'新改訳2017'}}
    d['service'][3]['role']='absolution';d['service'][8]['role']='scripture'
    return d

def check_data(d):
    if not isinstance(d,dict):raise ValueError('입력 데이터가 올바르지 않습니다.')
    try:datetime.date.fromisoformat(d.get('date',''))
    except ValueError:raise ValueError('예배 날짜를 입력해주세요.')
    issue=str(d.get('issue','')).strip()
    if not issue.isdigit() or len(issue)>6 or int(issue)<1:raise ValueError('주보 호수는 1~6자리 양수로 입력해주세요.')
    for name,limit in [('service',32),('notices',30),('roster',12)]:
        if not isinstance(d.get(name),list) or len(d[name])>limit:raise ValueError(f'{name}: 입력 행 수를 확인해주세요.')
        if any(not isinstance(row,dict) for row in d[name]):raise ValueError(f'{name}: 행 형식이 올바르지 않습니다.')
    for k in ['absolution','sermon']:
        s=d.get('scripture',{}).get(k,{})
        if not all(isinstance(s.get(f),str) and s[f].strip() for f in ['refJa','refKo','ja','ko']):raise ValueError('사죄확인과 설교본문의 한일 구절 및 본문을 모두 입력해주세요.')
        if s.get('provenance'):
            from scripture import parse_reference
            try: ko_ref=parse_reference(s['refKo']);ja_ref=parse_reference(s['refJa'])
            except ValueError:raise ValueError('조회 후 한일 구절 표기를 책·장·절 형식으로 확인해주세요.')
            if ko_ref['book']!=ja_ref['book'] or ko_ref['chapter']!=ja_ref['chapter'] or (ko_ref['verses'] is not None and ja_ref['verses'] is not None and ko_ref['verses']!=ja_ref['verses']):
                raise ValueError('조회한 한국어와 일본어의 성경 범위가 다릅니다. 두 언어의 구절과 본문을 맞춰주세요.')
    for i,n in enumerate(d['notices']):
        if not str(n.get('ja','')).strip() and not str(n.get('ko','')).strip():raise ValueError(f'교회소식 {i+1}번의 내용을 한쪽 언어 이상 입력해주세요.')
    def walk(x):
        if isinstance(x,str) and len(x)>30000:raise ValueError('한 항목의 본문이 너무 깁니다.')
        if isinstance(x,dict):
            for v in x.values():walk(v)
        if isinstance(x,list):
            for v in x:walk(v)
    walk(d)

def compose(base,data,wrap,font_width):
    check_data(data);doc=copy.deepcopy(base);doc['serviceData']=copy.deepcopy(data);doc['warnings']=[]
    # Reclaim automatically borrowed appendix pages when the content becomes shorter.
    for index,saved in doc.get('extraPageSources',{}).items():doc['pages'][int(index)]=copy.deepcopy(saved)
    existing={b.get('binding'):b for p in base['pages'] for b in p['blocks'] if b.get('binding')}
    key=doc['template'];eight=len(doc['pages'])==8;managed={}
    def block(bind,label,text,x,y,w,h,size=15,font='ko',fill='#ffffff',align='left',pad=6,border=False):
        b={'id':'auto-'+bind,'binding':bind,'label':label,'type':'text','text':str(text),'x':x,'y':y,'w':w,'h':h,'fontSize':size,'lineHeight':1.4,'font':font,'fill':fill,'color':'#111111','padding':pad,'align':align,'border':border}
        # Keep user-adjusted geometry and typography when updating the same layout.
        styles=['x','y','w','h','fontSize','lineHeight','font','fill','color','padding','align','border']
        b['layoutBase']={k:b[k] for k in styles}
        if bind in existing:
            old=existing[bind]
            for k in styles:
                if k in old and old[k]!=old.get('layoutBase',{}).get(k,old[k]):b[k]=old[k]
        return b
    def lines(text,size,width,font):return wrap(text,'BulletinJA' if font=='ja' else 'BulletinKO',size,width)
    def height(text,size,width,font):return size+(len(lines(text,size,width,font))-1)*size*1.4+12
    def replace(page,blocks):
        # The first apply replaces sample regions. Subsequent applies retain manually added blocks.
        custom=[b for b in doc['pages'][page]['blocks'] if not b.get('binding') and b['type'] in ('text','image')]
        doc['pages'][page]['blocks']=blocks+custom;managed[page]=True
    def page_num(i):return block(f'page-{i}','쪽번호',str(i+1),270,825,55,16,10,align='center',pad=0)
    # Split only the date/issue rectangles out of the source header (no white overlays).
    cover=[]
    if 'cover-issue' in existing:
        cover=[b for b in doc['pages'][0]['blocks'] if b.get('binding') not in ['cover-issue','cover-date']]
    else:
        original=doc['pages'][0]['blocks'][0]
        for n,crop in enumerate([[0,0,435,240],[435,0,595,30],[435,49,595,72],[435,91,595,240]]):
            b=copy.deepcopy(original);x0,y0,x1,y1=crop;b.update(id=f'cover-fixed-{n}',crop=crop,x=x0,y=y0,w=x1-x0,h=y1-y0,label='교회 고정 정보');cover.append(b)
        cover+=doc['pages'][0]['blocks'][1:]
    date=datetime.date.fromisoformat(data['date'])
    if eight:
        cover=[b for b in cover if b.get('id')!='eight-0-1' and b.get('binding')!='cover-title']
        cover.append(block('cover-title','표지 예배명',data.get('serviceTitleJa','主日礼拝')+'\n'+data.get('serviceTitleKo','주일예배'),77,250,439,150,25,'ko',align='center',pad=15))
    cover += [block('cover-issue','주보 호수',f'週報 第{data["issue"]}号',435,30,160,19,12.5,'ja',pad=1),block('cover-date','예배 날짜',f'今 日: {date.year}年{date.month}月{date.day}日',435,72,160,19,11.5,'ja',pad=1)]
    for b in cover:
        if b['type']=='source':b['rasterize']=True
    doc['pages'][0]['blocks']=cover
    # Worship order: distinct bilingual cells, measured row heights.
    title=data.get('serviceTitleJa','主日礼拝')+' / '+data.get('serviceTitleKo','주일예배')
    title_size=min(24,300/max(font_width(title,'BulletinKO',1),1))
    rows=data['service'];service=[block('service-title','예배명',title,2,2,310,39,title_size,'ko',pad=1),block('service-staff','설교·통역',f'설교: {data.get("preacher","")}\n통역: {data.get("interpreters","")}',314,2,279,39,10,pad=2)]
    def service_blocks(size):
        out=[];y=45
        for i,row in enumerate(rows):
            row=dict(row)
            if row.get('role')=='absolution' or row.get('ko')=='사죄확인':row['detailJa']=data['scripture']['absolution']['refJa'];row['detailKo']=data['scripture']['absolution']['refKo']
            if row.get('role')=='scripture' or row.get('ko')=='성경봉독':row['detailJa']=data['scripture']['sermon']['refJa'];row['detailKo']=data['scripture']['sermon']['refKo']
            h=max(height(row.get('ja',''),size,148,'ja')+height(row.get('ko',''),size-1,148,'ko')-8,height(row.get('detailJa',''),size,289,'ja')+height(row.get('detailKo',''),size-1,289,'ko')-8,height(row.get('leader',''),size-1,130,'ko')-4)+2
            fill='#E5E5E5'
            for col,(x,w,ja,ko) in enumerate([(2,156,row.get('ja',''),row.get('ko','')),(158,297,row.get('detailJa',''),row.get('detailKo',''))]):
                hh=height(ja,size,w-8,'ja')-4;out.append(block(f'service-{i}-{col}-ja',f'순서 {i+1} 일본어',ja,x,y,w,hh,size,'ja',fill,pad=4));out.append(block(f'service-{i}-{col}-ko',f'순서 {i+1} 한국어',ko,x,y+hh,w,h-hh,size-1,'ko',fill,pad=4))
            out.append(block(f'service-{i}-leader',f'순서 {i+1} 담당',row.get('leader',''),455,y,138,h,size-1,'ko',fill,pad=4));y+=h+1
        return out,y
    for size in [12,11.5,11,10.5,10]:
        generated,end=service_blocks(size)
        if end<=811:break
    if end>811:raise ValueError('예배순서가 1페이지를 넘습니다. 순서의 긴 문구나 행 수를 조정해주세요.')
    replace(1,service+generated+[page_num(1)])
    # Scripture: original-text paragraphs remain independent for each language.
    def scripture_pair(s,prefix,heading,y,available):
        for size in [15,14.5,14,13.5,13,12.5,12]:
            hj=height(s['ja'],size,280,'ja');hk=height(s['ko'],size,280,'ko');h=max(hj,hk)
            if h+36<=available:break
        if h+36>available:return None
        title_size=min(21,575/max(font_width(heading,'BulletinKO',1),1))
        if title_size<9:raise ValueError('성경 구절 표기가 너무 깁니다. 책·장·절만 입력해주세요.')
        return [block(prefix+'-heading',heading,heading,2,y,591,32,title_size,'ko',pad=2),block(prefix+'-ja',heading+' 일본어',s['ja'],2,y+34,294,h,size,'ja','#E5E5E5'),block(prefix+'-ko',heading+' 한국어',s['ko'],300,y+34,293,h,size,'ko','#E5E5E5')],h+36
    a=data['scripture']['absolution'];s=data['scripture']['sermon']
    ah='赦しの御言葉 '+a['refJa']+' / '+a['refKo'];sh='今日の御言葉 '+s['refJa']+' / '+s['refKo']
    first=scripture_pair(a,'absolution',ah,2,340)
    if not first:raise ValueError('사죄확인 본문이 너무 깁니다. 구절 범위를 조정해주세요.')
    ab,h=first;second=scripture_pair(s,'sermon',sh,h+4,817-h)
    if second:replace(2,ab+second[0]+[page_num(2)])
    elif eight:
        full=scripture_pair(s,'sermon',sh,2,817)
        if not full:raise ValueError('설교본문이 확장형 말씀 한 면에도 들어가지 않습니다. 본문 범위를 줄이거나 영역을 수동 분할해주세요.')
        doc.setdefault('extraPageSources',{}).setdefault('3',copy.deepcopy(doc['pages'][3]))
        replace(2,ab+[page_num(2)]);replace(3,full[0]+[page_num(3)]);doc['pages'][3]['name']='설교본문';doc['warnings'].append('말씀 분량에 맞춰 4면을 설교본문으로 사용했습니다. 기존 기도문 면은 대체되었습니다.')
    else:raise ValueError('말씀 본문이 4페이지형의 말씀 면을 넘습니다. 8페이지 레이아웃을 선택해주세요. 입력한 본문은 유지됩니다.')
    # Last page: bilingual notices, followed by structured roster table.
    last=len(doc['pages'])-1;roster=data['roster'];roster_h=30+max(1,len(roster))*34;roster_top=820-roster_h
    if roster_top<450:raise ValueError('봉사표 행이 너무 많습니다. 최대 9행 이내로 정리해주세요.')
    nb=[block('notice-title','교회소식 제목','お知らせ / 교회소식',2,2,591,33,23,'ko',align='center',pad=2)];y=40
    entries=[]
    for i,n in enumerate(data['notices']):
        hj=height(n.get('ja',''),13.5,565,'ja');hk=height(n.get('ko',''),13.5,565,'ko')
        # Include the generated number when measuring the Japanese text.
        hj=height(f'{i+1}. '+n.get('ja',''),13.5,565,'ja');entries.append((i,n,hj,hk))
    if 40+sum(hj+hk+7 for _,_,hj,hk in entries)>roster_top-36 and eight:
        extra=[block('notice-extra-title','교회소식 제목','お知らせ / 교회소식',2,2,591,33,23,'ko',align='center',pad=2)];ey=40
        while entries and ey+entries[0][2]+entries[0][3]+7<=815:
            i,n,hj,hk=entries.pop(0)
            extra.extend([block(f'notice-{i}-ja',f'공지 {i+1} 일본어',f'{i+1}. '+n.get('ja',''),8,ey,579,hj,13.5,'ja'),block(f'notice-{i}-ko',f'공지 {i+1} 한국어',n.get('ko',''),8,ey+hj,579,hk,13.5,'ko')]);ey+=hj+hk+7
        doc.setdefault('extraPageSources',{}).setdefault('6',copy.deepcopy(doc['pages'][6]))
        replace(6,extra+[page_num(6)]);doc['pages'][6]['name']='교회소식';doc['warnings'].append('공지 분량에 맞춰 7면을 교회소식으로 사용했습니다. 기존 악보 면은 대체되었습니다.')
    for i,n,hj,hk in entries:
        nb.extend([block(f'notice-{i}-ja',f'공지 {i+1} 일본어',f'{i+1}. '+n.get('ja',''),8,y,579,hj,13.5,'ja'),block(f'notice-{i}-ko',f'공지 {i+1} 한국어',n.get('ko',''),8,y+hj,579,hk,13.5,'ko')]);y+=hj+hk+7
    if y>roster_top-36:raise ValueError('교회소식이 봉사표 영역을 넘습니다. 공지를 줄이거나 다른 면에 나눠주세요. 입력 내용은 보존됩니다.')
    nb.append(block('roster-title','봉사표 제목','来週の礼拝奉仕 / 다음 주 예배봉사',2,roster_top-34,591,32,19,'ko',align='center',pad=2))
    cols=[(2,90,'날짜'),(92,55,'예배'),(147,146,'봉헌기도'),(293,146,'통역'),(439,154,'청소')]
    keys=['date','service','prayer','interpreter','cleaning']
    for c,(x,w,title) in enumerate(cols):nb.append(block(f'roster-head-{c}','봉사표 머리행',title,x,roster_top,w,28,12,fill='#D8D8D8',align='center',pad=4,border=True))
    for r,row in enumerate(roster):
        for c,(x,w,_) in enumerate(cols):nb.append(block(f'roster-{r}-{c}',f'봉사표 {r+1}행',row.get(keys[c],''),x,roster_top+28+r*34,w,34,11,pad=4,border=True))
    replace(last,nb+[page_num(last)])
    doc['title']=f'{data["date"]} 주일주보 제{data["issue"]}호'
    return doc
