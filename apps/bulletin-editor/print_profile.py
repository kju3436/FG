"""Editable Nov 1 reference: measured cells, original outlines, full-face fallback.

This is a reusable layout, not a source-page screenshot. Every printed character
is a text run. Its approved coordinates survive unchanged text and round trips;
new text uses the same measured cell and the normal overflow validation.
"""
import base64,copy,datetime,hashlib,io,pathlib,re,threading
from functools import lru_cache
ROOT=pathlib.Path(__file__).resolve().parents[2]
PROFILE='communion-20261101-v3'
FONT_LOCK=threading.RLock()

def source():
    return next((ROOT/'자료'/'주보_기준원본').glob('20261101*.pdf'))

class OriginalOutlines:
    def __init__(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('_reference_original_fonts',pathlib.Path(__file__).with_name('embedded_fonts.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.module=module;self.entries=module.discover_fonts(source())
    def translate(self,entry,text):
        name=entry['originalName'];result=''
        for ch in text:
            if self.module.has_chars(name,ch):result+=ch
            elif ch in '↑↓' and self.module.has_chars(name,{'↑':'\U000f0035','↓':'\U000f0036'}[ch]):result+= {'↑':'\U000f0035','↓':'\U000f0036'}[ch]
            elif ch==' ' and self.module.has_chars(name,'\0'):result+='\0'
            else:result+=ch
        return result
    def has_glyph(self,entry,text):return self.module.has_chars(entry['originalName'],self.translate(entry,text))
    def font_bytes(self,entry):
        # Add Unicode aliases for the source's private-use up/down markers.
        from fontTools.ttLib import TTFont
        tt=TTFont(io.BytesIO(self.module.font_bytes(entry['originalName'])))
        for table in tt['cmap'].tables:
            if not table.isUnicode():continue
            for char,old in [('↑',0xf0035),('↓',0xf0036),(' ',0)]:
                if ord(char) not in table.cmap and old in table.cmap:table.cmap[ord(char)]=table.cmap[old]
        output=io.BytesIO();tt.save(output);return output.getvalue()
    def measure_text(self,entry,text,size):
        name=entry['originalName'];text=self.translate(entry,text)
        return sum(self.module.measure(name,ch,size) if self.module.has_chars(name,ch) else size*.25 if ch.isspace() else self.module.measure(name,ch,size) for ch in text)
    def draw_text(self,c,entry,text,x,y,size,color):
        from reportlab.lib.colors import HexColor
        forms=getattr(c,'_reference_forms',None)
        if forms is None:forms={};c._reference_forms=forms
        stroke=entry.get('sourceStrokeWidth',0) if entry.get('sourceRenderMode') in (1,2) else 0
        key=(entry['originalName'],text,color,stroke/size)
        if key not in forms:
            name='reference-glyph-'+str(len(forms));forms[key]=name;c.beginForm(name,-4,-4,4,4);c.setFillColor(HexColor(color))
            self.module.draw(c,entry['originalName'],self.translate(entry,text),0,0,1,stroke_width=stroke/size);c.endForm()
        c.saveState();c.translate(x,y);c.scale(size,size);c.doForm(forms[key]);c.restoreState()

_ORIGINAL_PROVIDER=None
def provider():
    global _ORIGINAL_PROVIDER
    with FONT_LOCK:
        if _ORIGINAL_PROVIDER is None:_ORIGINAL_PROVIDER=OriginalOutlines()
        return _ORIGINAL_PROVIDER

def font_id(name):
    import native_template as n
    return 'reference-'+n._font_id(name)

@lru_cache(maxsize=64)
def font_bytes(ident):
    p=provider();entry=next((v for k,v in p.entries.items() if font_id(k)==ident),None)
    if entry is None:raise ValueError('Unknown reference font.')
    return p.font_bytes(entry)

@lru_cache(maxsize=8192)
def glyph_path(name,text):
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    p=provider();face=p.module._face(name);tt,cmap,widths=face[:3];glyphset=tt.getGlyphSet();pen=SVGPathPen(glyphset);offset=0
    for ch in p.translate({'originalName':name},text):
        glyphset[cmap[ord(ch)]].draw(TransformPen(pen,(1,0,0,1,offset,0)));offset+=widths[ch]
    return dict(path=pen.getCommands(),unitsPerEm=tt['head'].unitsPerEm)

def fonts():
    result=[]
    for name,value in provider().entries.items():
        if not value.get('available'):continue
        rights=value.get('fsType');rights=rights if isinstance(rights,list) else [rights]
        if any(v is not None and (v&(2|0x200) or (v&4 and not v&8)) for v in rights):continue
        e=copy.deepcopy(value);e.update(id=font_id(name),backend='reference-subset',renderFontId=font_id(name),url='/api/weekly/font/'+font_id(name),resolvedName=name,substituted=False)
        result.append(e)
    return result

def make_block(chars,ident,binding,group,box=None):
    import native_template as n
    b=n._block(chars,ident,binding,group,box)
    for r in b['runs']:
        ident=font_id(r['sourceFont'])
        if any(e['id']==ident for e in fonts_cached()):r['fontId']=ident
    registry={e['id']:e for e in fonts_cached()}
    registry.update({e['id']:e for e in n._registry({c['fontname'] for c in chars})})
    b['runs']=n._map_runs(b['runs'],registry);b['layoutSpec']=n._layout_spec(b)
    b['fontId']=b['runs'][0]['fontId']
    b['referenceText']=b['text'];b['referenceRuns']=copy.deepcopy(b['runs']);b['referenceProfile']=PROFILE
    b['styleSeed']=[{k:copy.deepcopy(v) for k,v in r.items() if k!='glyphs'} for r in b['runs']]
    return b

@lru_cache(maxsize=1)
def fonts_cached():return fonts()

def regions():
    import native_template as n,communion
    rows=[r for r in n._regions() if r[0]==0]
    rows += [(1,'serviceTitleJa',(0,0,310,44),'service'),(1,'preacher',(310,0,595,25),'service'),(1,'interpreters',(310,25,595,44),'service')]
    for i,(top,bottom) in enumerate(communion.BOUNDS):
        for key,left,right in [('nameRaw',1.92,158.4),('detailRaw',158.4,455.76),('leader',455.76,593.4)]:rows.append((1,f'service.{i}.{key}',(left,top,right,bottom),'service'))
    for key,left,right in [('nameRaw',1.92,158.4),('detailRaw',158.4,455.76),('leader',455.76,593.4)]:rows.append((1,'communion.'+key,(left,521,right,565),'service'))
    for kind,top,bottom in [('absolution',31,214),('sermon',256,823)]:
        for lang,left,right in [('ja',1.32,300),('ko',300,594)]:rows.append((2,f'scripture.{kind}.{lang}',(left,top,right,bottom),'scripture'))
    for i,(top,bottom) in enumerate([(34,180),(180,236),(236,325),(325,458),(458,590),(590,640)]):rows.append((3,f'notices.{i}.raw',(1.32,top,594,bottom),'notices'))
    for i,(top,bottom) in enumerate([(715.44,769.32),(769.32,823.32)]):rows.append((3,f'rosterRaw.{i}',(41.52,top,594.12,bottom),'roster'))
    return rows

@lru_cache(maxsize=1)
def page_chars():
    import pdfplumber
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.pdfinterp import PDFResourceManager,PDFPageInterpreter
    from pdfminer.pdfpage import PDFPage
    from collections import defaultdict,deque
    class StyleCollector(PDFPageAggregator):
        def render_string(self,textstate,seq,ncs,graphicstate):
            self.mode=textstate.render
            return super().render_string(textstate,seq,ncs,graphicstate)
        def render_char(self,matrix,font,fontsize,scaling,rise,cid,ncs,graphicstate):
            result=super().render_char(matrix,font,fontsize,scaling,rise,cid,ncs,graphicstate)
            char=self.cur_item._objs[-1]
            import math
            scale=math.hypot(self.ctm[0],self.ctm[1])
            self.styles[(char.get_text(),round(matrix[4],5),round(matrix[5],5))].append(dict(sourceRenderMode=self.mode,sourceStrokeWidth=graphicstate.linewidth*scale))
            return result
    with pdfplumber.open(source()) as pdf:pages=[p.chars for p in pdf.pages]
    manager=PDFResourceManager();device=StyleCollector(manager);interpreter=PDFPageInterpreter(manager,device)
    with source().open('rb') as stream:
        for chars,page in zip(pages,PDFPage.get_pages(stream)):
            device.styles=defaultdict(deque);interpreter.process_page(page)
            for char in chars:
                key=(char['text'],round(char['matrix'][4],5),round(char['matrix'][5],5))
                if not device.styles[key]:raise ValueError('Reference text style alignment failed.')
                char.update(device.styles[key].popleft())
    device.close();return pages

@lru_cache(maxsize=1)
def backgrounds():
    import pypdfium2
    from pypdf import PdfReader,PdfWriter
    w=PdfWriter();w.append(PdfReader(source()));w.remove_text();buf=io.BytesIO();w.write(buf)
    pdf=pypdfium2.PdfDocument(buf.getvalue());images=[]
    for page in pdf:
        bitmap=page.render(scale=2);data=io.BytesIO();bitmap.to_pil().save(data,format='PNG');images.append('data:image/png;base64,'+base64.b64encode(data.getvalue()).decode());bitmap.close();page.close()
    pdf.close();return images

@lru_cache(maxsize=1)
def _template():
    import native_template as n
    base=n.native_template_document();doc=copy.deepcopy(base);data=doc['serviceData'];data.update(date='2026-11-01',issue='1934',serviceTitleKo='성찬예배')
    data['nativeText']={};data['scripture']['absolution'].update(refJa='コロサイ人への手紙 1:12~14',refKo='골로새서 1:12-14');data['scripture']['sermon'].update(refJa='マタイの福音書 17:1~8',refKo='마태복음 17:1~8')
    data['communion']={'enabled':True,'manual':True,'date':data['date'],'firstSunday':True}
    old_fixed=[b for p in base['pages'] for b in p['blocks'] if b['binding'].startswith('nativeText.')];used=set();pages=[]
    for pi,chars in enumerate(page_chars()):
        remaining=list(chars);blocks=[]
        for ri,(index,field,box,group) in enumerate(regions()):
            if index!=pi:continue
            chosen=[c for c in remaining if n._in(c,box)]
            if not chosen:continue
            chosen_ids={id(c) for c in chosen};remaining=[c for c in remaining if id(c) not in chosen_ids]
            old=next((b for b in base['pages'][pi]['blocks'] if b['binding']==field),None)
            b=make_block(chosen,old['id'] if old else 'reference-field-'+str(ri),field,group,box)
            if field in ('date','issue'):b.update(format='japanese-date' if field=='date' else 'issue',value=data[field])
            else:n._set(data,field,b['text'])
            blocks.append(b)
        for row in n._char_lines(remaining):
            parts=[[]]
            for ch in row:
                if parts[-1] and ch['x0']-parts[-1][-1]['x1']>18:parts.append([])
                parts[-1].append(ch)
            for part in parts:
                text=''.join(r['text'] for r in n._runs(part));norm=lambda s:re.sub(r'\s+','',s)
                matches=[b for b in old_fixed if b['id'] not in used and b in base['pages'][pi]['blocks'] and norm(b['text'])==norm(text)]
                if not matches and pi==2 and 'コロサイ人' in text:matches=[b for b in old_fixed if 'ヨハネ' in b['text']]
                old=min(matches,key=lambda b:abs(b['x']-part[0]['x0'])) if matches else None
                ident=old['id'] if old else f'reference-{pi}-text-{len(blocks)}';used.add(ident)
                # Reference headings get the whole measured title column, not a tight text bbox.
                box=(300,0,594,31) if pi==2 and 'コロサイ人' in text else (300,214,594,256) if pi==2 and 'マタイ' in text else None
                b=make_block(part,ident,'nativeText.'+ident,['cover','service','scripture','notices'][pi],box);data['nativeText'][ident]=b['text'];blocks.append(b)
        pages.append(dict(name=base['pages'][pi]['name'],blocks=blocks,backgroundData=backgrounds()[pi]))
    doc.update(pages=pages,fonts=base['fonts']+copy.deepcopy(fonts_cached()),layoutProfile=PROFILE,printLayoutVersion=3,communionLayoutVersion=3,title='2026-11-01 성찬식 주보 기준 배치',communionNormalPage=copy.deepcopy(base['pages'][1]))
    for f in doc['fonts']:
        if f.get('backend')=='reference-subset':
            fallback=n._registry([f['originalName']])[0]
            match=next((e for e in base['fonts'] if e.get('resolvedName')==fallback.get('resolvedName')),base['fonts'][0])
            f['previewFontId']=match['id']
    doc['source'].update(path=source().relative_to(ROOT).as_posix(),date=data['date'],issue=data['issue'],sha256=hashlib.sha256(source().read_bytes()).hexdigest())
    doc['warnings']=['11월 1일 기준 배치입니다. 새 본문·소식은 인쇄 전 미리보기를 확인해주세요.']
    n._canonical_fields(data,pages);replace_roster(doc,source_roster());refresh_bindings(doc);return doc

def refresh_bindings(doc):
    for kind in ('absolution','sermon'):
        for suffix in ('Ja','Ko'):
            field=f'scripture.{kind}.ref{suffix}';value=doc['serviceData']['scripture'][kind]['ref'+suffix]
            pattern=r'\s*'.join(re.escape(ch) for ch in re.sub(r'\s+','',value))
            for page in doc['pages']:
                for b in page['blocks']:
                    if b['binding'].startswith('scripture.'):continue
                    match=re.search(pattern,b['text'])
                    if match and not any(a['field']==field for a in b['fieldBindings']):b['fieldBindings'].append(dict(field=field,substring=match[0]))
    doc['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in doc['pages'] for b in p['blocks'] for a in b.get('fieldBindings',[])]

def document():return copy.deepcopy(_template())

def adopt(previous):
    """Repair a saved Nov 1 work file, carrying its own text into new cells."""
    import native_template as n
    doc=document();registry={f['id']:f for f in doc['fonts']}
    known={f['id'] for f in doc['fonts']}
    doc['fonts'].extend(copy.deepcopy(f) for f in previous['fonts'] if f['id'] not in known)
    old={b['binding']:b for p in previous['pages'] for b in p['blocks']}
    for page in doc['pages']:
        for b in page['blocks']:
            field=b['binding']
            if field.startswith(('roster.','rosterMonth','rosterRaw.')):continue
            if field in old:n._replace(b,old[field]['text'],registry)
            n._set(doc['serviceData'],field,b['text'])
    saved=previous['serviceData']
    for key in ('date','issue','versions','sourceProvider'):doc['serviceData'][key]=copy.deepcopy(saved[key])
    for kind in ('absolution','sermon'):
        for suffix in ('Ja','Ko'):doc['serviceData']['scripture'][kind]['ref'+suffix]=saved['scripture'][kind]['ref'+suffix]
    if saved.get('roster'):
        rows=[]
        for r in saved['roster']:
            row=copy.deepcopy(r)
            if re.fullmatch(r'\d{1,2}/\d{1,2}',row['date']):row['date']=f"{saved['date'][:4]}-{int(row['date'].split('/')[0]):02}-{int(row['date'].split('/')[1]):02}"
            if row.get('cleaningDate') and re.fullmatch(r'\d{1,2}/\d{1,2}',row['cleaningDate']):row['cleaningDate']=f"{saved['date'][:4]}-{int(row['cleaningDate'].split('/')[0]):02}-{int(row['cleaningDate'].split('/')[1]):02}"
            rows.append(row)
        replace_roster(doc,rows)
    for before,after in zip(previous['pages'],doc['pages']):
        known={b['id'] for b in after['blocks']}
        after['blocks'].extend(copy.deepcopy(b) for b in before['blocks'] if b['id'] not in known and (b.get('group')=='additions' or b['id'].startswith(('addition-','extra-'))))
    n._canonical_fields(doc['serviceData'],doc['pages']);refresh_bindings(doc)
    if previous.get('savedAt'):doc['savedAt']=previous['savedAt']
    return doc

def roster_cell(i,key):
    import native_template as n
    week=i//2;top=715.44+week*53.88;bottom=769.32+week*54
    rowtop=top+(27 if i%2 else 0);rowbottom=bottom if i%2 else top+27
    columns={'date':(41.52,81.24),'service':(81.24,135.12),'prayer':(135.12,277.2),'interpreter':(277.2,424.92),'cleaningDate':(424.92,485.28),'cleaning':(485.28,594.12)}
    left,right=columns[key];box=(left,top if key in ('date','cleaningDate','cleaning') else rowtop,right,bottom if key in ('date','cleaningDate','cleaning') else rowbottom)
    chars=[c for c in page_chars()[3] if n._in(c,box)]
    b=make_block(chars,f'annual-roster-{i}-{key}',f'roster.{i}.{key}','roster',box)
    b['layoutSpec'].update(align='center',indent=0);return b

def source_roster():
    rows=[]
    for i in range(4):
        values={key:roster_cell(i,key)['text'].strip() for key in ('service','prayer','interpreter','cleaning')}
        day=(8,15)[i//2];rows.append(dict(values,date=f'2026-11-{day:02}',cleaningDate=f'2026-11-{day-1:02}'))
    return rows

def replace_roster(doc,rows):
    import native_template as n
    page=doc['pages'][3];registry={f['id']:f for f in doc['fonts']}
    page['blocks']=[b for b in page['blocks'] if not b['binding'].startswith(('rosterRaw.','roster.','rosterMonth')) and not (b['x']<41.52 and 715.44<=b['y']<823.32)]
    canonical=[]
    for i,row in enumerate(rows):
        day=datetime.date.fromisoformat(row['date']);value=dict(row,date=f'{day.month}/{day.day}');canonical.append(value)
        keys=['service','prayer','interpreter']+(['date','cleaningDate','cleaning'] if i%2==0 else [])
        for key in keys:
            b=roster_cell(i,key);text=str(day.day) if key=='date' else row[key]
            if key=='cleaningDate':
                if not text:text=''
                else:
                    cd=datetime.date.fromisoformat(text);text=f'{cd.day}({"月火水木金土日"[cd.weekday()]})'
            n._replace(b,text,registry);page['blocks'].append(b)
    chars=[c for c in page_chars()[3] if n._in(c,(1.2,715.44,41.52,823.32))]
    month=make_block(chars,'annual-roster-month','rosterMonth','roster',(1.2,715.44,41.52,823.32))
    months=list(dict.fromkeys(datetime.date.fromisoformat(r['date']).month for r in rows))
    n._replace(month,'/'.join(map(str,months)),registry);page['blocks'].append(month)
    doc['serviceData'].update(roster=canonical,rosterMonth=month['text']);refresh_bindings(doc);return doc
