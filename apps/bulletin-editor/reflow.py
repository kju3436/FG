"""Whole-page layout choices: AI selects validated, text-preserving candidates.

Existing bilingual text, colors, faces and vector-source band graphics remain
unchanged in content. Only a bounded uniform scale and band positions change.
The source document is never mutated or saved. All outputs require approval.
"""
import base64,copy,hashlib,io,math
from PIL import Image
import ai_client,additions,native_template as native

CUTS={0:[0,235,405,630,842],1:[0,45,140,396,567,806,842],2:[0,223,827,842],3:[0,34,232,288,344,450,584,650,827,842]}

def _bands(doc,page0):
    page=doc['pages'][page0]
    if page.get('layoutBands'):
        bands=copy.deepcopy(page['layoutBands'])
        covered=[bid for b in bands for bid in b['ids']]
        if sorted(covered)==sorted(b['id'] for b in page['blocks']):return bands
        raise ValueError('추가된 영역을 포함한 배치를 먼저 확인해주세요. 기존 배치와 영역 목록이 다릅니다.')
    cuts=[0]
    for proposed in CUTS[page0][1:-1]:
        candidates=sorted(range(max(int(cuts[-1])+1,int(proposed)-18),min(841,int(proposed)+18)+1),key=lambda y:abs(y-proposed))
        safe=next((y for y in candidates if not any(b['y']+.01<y<b['y']+b['h']-.01 for b in page['blocks'])),None)
        if safe is not None:cuts.append(safe)
    cuts.append(842);bands=[]
    for i,(top,bottom) in enumerate(zip(cuts,cuts[1:])):
        ids=[b['id'] for b in page['blocks'] if b['y']>=top-.01 and b['y']+b['h']<=bottom+.01]
        bands.append({'id':f'part-{i}','y':top,'h':bottom-top,'ids':ids})
    if sorted(bid for b in bands for bid in b['ids'])!=sorted(b['id'] for b in page['blocks']):
        raise ValueError('페이지의 영역 경계를 안전하게 나누지 못했습니다.')
    return bands

def _new_block(doc,text,page0):
    registry={f['id']:f for f in doc['fonts']};style=additions._template(doc,page0,registry)
    style={k:copy.deepcopy(v) for k,v in style.items() if k not in ('text','glyphs')};style.update(text=' ',glyphs=[])
    size=min(15,max(12,style['fontSize']));style['fontSize']=size
    index=len(doc['serviceData'].get('extras',[]));field=f'extras.{index}.text'
    bid='addition-'+hashlib.sha256((str(index)+text).encode()).hexdigest()[:16]
    b={'id':bid,'type':'text','addition':True,'binding':field,'fieldBindings':[{'field':field}],
       'group':'extras','label':'추가 안내','x':16,'y':0,'w':563,'h':842,'text':'',
       'runs':[style],'styleSeed':[copy.deepcopy(style)],'fontId':style['fontId'],'fontSize':size,
       'lineHeight':1.25,'positioned':False,'fontAvailable':True,'sameFontAvailable':False,
       'fit':{'preferredSize':size,'minimumSize':size,'wrap':True}}
    try:native._replace(b,text,registry)
    except ValueError as e:
        if '영역을 넘습니다' in str(e):raise ValueError('모든 내용을 유지하기에는 추가 문구가 너무 깁니다. 내용을 줄이거나 다른 면으로 나누어주세요.') from None
        raise
    glyphs=native._layout(b,registry);b['h']=math.ceil(max((y for _,_,y,_,_ in glyphs),default=size)+6)
    return b

def _min_scale(page):
    # Body floor is 12pt or 92% of a smaller original style; never tiny-fit a page.
    sizes=[r['fontSize'] for b in page['blocks'] for r in b['runs'] if r['text'].strip() and r['fontSize']>=12]
    return max([.82]+[min(12,s*.92)/s for s in sizes])

def _image(page,page0):
    if page.get('backgroundData'):
        return Image.open(io.BytesIO(base64.b64decode(page['backgroundData'].split(',',1)[1]))).convert('RGB')
    return Image.open(io.BytesIO(native.background_bytes(page0))).convert('RGB')

def _transform(block,scale,top,old_top):
    b=copy.deepcopy(block);dx=(595-595*scale)/2
    b['x']=dx+block['x']*scale;b['y']=top+(block['y']-old_top)*scale
    b['w']*=scale;b['h']*=scale;b['fontSize']*=scale
    for key in ('preferredSize','minimumSize'):
        if key in b.get('fit',{}):b['fit'][key]*=scale
    if 'fitSize' in b:b['fitSize']*=scale
    b.pop('previewGlyphs',None)
    for key in ('runs','styleSeed'):
        for run in b.get(key,[]):
            run['fontSize']*=scale
            for g in run.get('glyphs',[]):
                g['x']=dx+g['x']*scale;g['y']=top+(g['y']-old_top)*scale;g['w']*=scale
                g['baseline']=top+(g['baseline']-old_top)*scale
    return b

def _candidate(doc,text,page0,zone,bands,new,after,scale):
    page=doc['pages'][page0];height=sum(b['h'] for b in bands)*scale+new['h']+8
    if height>842+.001:return None
    top=(842-height)/2;positions=[];new_top=None
    for i,band in enumerate(bands):
        if i==after:new_top=top;top+=new['h']+8
        positions.append((band,top));top+=band['h']*scale
    if after==len(bands):new_top=top
    ident=f'layout-{after}-{round(scale*10000)}'
    return {'id':ident,'after':after,'scale':scale,'positions':positions,'newTop':new_top,
            'summary':f'기존 내용 {scale*100:.1f}% 크기 유지 · 새 안내 {new["fontSize"]:g}pt',
            'distance':abs(new_top/842-{'top':.18,'middle':.5,'bottom':.8}[zone])}

def choices(doc,text,page0,zone):
    native.validate_document(doc)
    if type(page0)is not int or not 0<=page0<4 or zone not in ('top','middle','bottom'):raise ValueError('페이지와 대략적인 위치를 선택해주세요.')
    if not isinstance(text,str) or not text.strip() or len(text)>30000:raise ValueError('추가할 내용을 30,000자 이내로 입력해주세요.')
    bands=_bands(doc,page0);new=_new_block(doc,text,page0);minimum=_min_scale(doc['pages'][page0])
    available=(842-new['h']-8)/sum(b['h'] for b in bands)
    if available<minimum-.0001:
        raise ValueError('모든 내용을 유지하고 읽기 쉬운 글자 크기로는 이 페이지에 들어가지 않습니다. 추가 문구를 줄이거나 다른 면을 선택해주세요.')
    scale=min(1,available);options=[]
    # Preserve headers and footer sequence; add only between semantic bands.
    for after in range(1,len(bands)):
        c=_candidate(doc,text,page0,zone,bands,new,after,scale)
        if c:options.append(c)
    if not options:raise ValueError('원본 영역을 나누지 않고 새 내용을 넣을 위치를 만들지 못했습니다.')
    options.sort(key=lambda c:c['distance'])
    return bands,new,options

def _materialize(doc,text,page0,zone,new,option):
    result=copy.deepcopy(doc);source=doc['pages'][page0];target=result['pages'][page0]
    image=_image(source,page0);sx=image.width/595;sy=image.height/842
    bg=Image.new('RGB',(1190,1684),'white');blocks={b['id']:b for b in source['blocks']};output=[];layout=[]
    scale=option['scale']
    for band,top in option['positions']:
        crop=image.crop((0,round(band['y']*sy),image.width,round((band['y']+band['h'])*sy)))
        crop=crop.resize((max(1,round(1190*scale)),max(1,round(band['h']*scale*2))),Image.Resampling.LANCZOS)
        bg.paste(crop,(round((1190-crop.width)/2),round(top*2)))
        for bid in band['ids']:output.append(_transform(blocks[bid],scale,top,band['y']))
        layout.append(dict(id=band['id'],y=top,h=band['h']*scale,ids=band['ids']))
    new=copy.deepcopy(new);new['y']=option['newTop'];output.append(new)
    layout.insert(option['after'],{'id':new['id'],'y':new['y'],'h':new['h']+8,'ids':[new['id']]})
    mem=io.BytesIO();bg.save(mem,format='PNG')
    target.update(blocks=output,backgroundData='data:image/png;base64,'+base64.b64encode(mem.getvalue()).decode(),layoutBands=layout)
    target.pop('backgroundUrl',None)
    result['serviceData'].setdefault('extras',[]).append({'id':new['id'],'text':text,'page':page0,'zone':zone})
    result['fieldBindings'].append({'blockId':new['id'],'field':new['binding'],'group':'extras'})
    original={b['id']:b['text'] for b in source['blocks']};actual={b['id']:b['text'] for b in output if b['id'] in original}
    if actual!=original:raise ValueError('기존 내용 보존 검사에 실패해 배치안을 적용하지 않았습니다.')
    # Verify each final box and actual wrapping, including previously edited text.
    registry={f['id']:f for f in result['fonts']}
    for b in output:
        if b['x']<-.01 or b['y']<-.01 or b['x']+b['w']>595+.01 or b['y']+b['h']>842+.01:raise ValueError('페이지 밖으로 나가는 배치안입니다.')
        if b.get('positioned')is False:native._layout(b,registry)
    native.validate_document(result)
    return result

def prompt(doc,text,page0,zone='middle'):
    bands,new,options=choices(doc,text,page0,zone)
    data={'page':page0+1,'requestedZone':zone,'existingSections':[{'id':b['id'],'height':round(b['h'],1)} for b in bands],
          'options':[{'optionId':c['id'],'summary':c['summary'],'location':round(c['newTop']/842,2)} for c in options]}
    instruction='You select a whole-page church bulletin layout. Existing text, fonts, colors, order and graphics are immutable. Select exactly one supplied optionId balancing requested location and readability. Never invent a layout or rewrite content. Return only JSON {"optionId":string,"reason":brief Korean string}.'
    return {'instruction':instruction,'data':data}

def propose(doc,text,page0,zone='middle',use_ai=True,selection=None):
    bands,new,options=choices(doc,text,page0,zone)
    if use_ai or selection is not None:
        if selection is None:
            request=prompt(doc,text,page0,zone)
            selected=ai_client.complete_json(request['instruction'],request['data'],1200)
        else:selected=ai_client._parse(selection)
        if not isinstance(selected,dict) or set(selected)!={'optionId','reason'} or not isinstance(selected['reason'],str) or len(selected['reason'])>500:
            raise ValueError('AI 배치 응답 형식이 올바르지 않습니다. 다시 시도해주세요.')
        option=next((c for c in options if c['id']==selected['optionId']),None)
        if option is None:raise ValueError('AI가 검증되지 않은 배치안을 선택했습니다. 원래 주보는 유지했습니다.')
        reason=selected['reason'];provider=ai_client.status()['provider'] if selection is None else 'external'
    else:option=options[0];reason='요청한 위치와 가장 가까운 검증된 전체 배치안입니다.';provider='local'
    result=_materialize(doc,text,page0,zone,new,option)
    result['layoutProposal']={'kind':'whole-page-reflow','requiresApproval':True,'usesAI':use_ai or selection is not None,'provider':provider,'page':page0,'optionId':option['id']}
    return {'document':result,'summary':('외부 AI에서 가져온 배치안: ' if selection is not None else 'AI 전체 정돈: ' if use_ai else '자동 전체 정돈: ')+reason,
            'warnings':[option['summary'],'기존 글·그림·표·순서를 보존했습니다. 변경 전·후를 확인한 뒤 적용해주세요.'],
            'changes':{'originalBlocks':len(doc['pages'][page0]['blocks']),'preservedBlocks':len(doc['pages'][page0]['blocks']),'addedBlocks':1,'scale':option['scale'],'provider':provider}}
