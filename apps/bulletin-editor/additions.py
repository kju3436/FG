"""Offline native-text placement proposals. No AI, saves or existing-block edits.

propose(document, text, zero_based_page, zone='top') returns a reviewable copy.
Whole/half-width white gaps prefer the requested third, then nearby page thirds.
Source graphics are conservative obstacles: no overlay or existing-text shrink.
"""
from __future__ import annotations

import copy
import hashlib
import math
from functools import lru_cache

import pdfplumber
import native_template as native

MARGIN = 16.0
CLEARANCE = 5.0
ZONE_NAMES = {'top': '위쪽', 'middle': '가운데', 'bottom': '아래쪽'}


def _box(block):
    return (float(block['x']), float(block['y']),
            float(block['x'])+float(block['w']), float(block['y'])+float(block['h']))


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _white(color):
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return color >= .98
    if len(color) == 1:
        return color[0] >= .98
    if len(color) == 4:  # CMYK white
        return all(v <= .02 for v in color)
    return len(color) == 3 and all(v >= .98 for v in color)


@lru_cache(maxsize=16)
def _pdf_graphics(path, mtime, size, page0):
    # mtime/size are cache invalidation keys, never values written to source assets.
    result = []
    with pdfplumber.open(path) as pdf:
        if page0 >= len(pdf.pages):
            raise ValueError('배경 페이지를 확인할 수 없습니다.')
        page = pdf.pages[page0]
        for image in page.images:
            result.append((image['x0'], image['top'], image['x1'], image['bottom']))
        for rect in page.rects:
            x0, y0, x1, y1 = rect['x0'], rect['top'], rect['x1'], rect['bottom']
            if rect.get('fill') and not _white(rect.get('non_stroking_color')):
                result.append((x0, y0, x1, y1))
            elif rect.get('stroke'):
                result.extend(((x0, y0, x1, y0), (x0, y1, x1, y1),
                               (x0, y0, x0, y1), (x1, y0, x1, y1)))
        for item in page.lines + page.curves:
            if item.get('stroke') or (item.get('fill') and not _white(item.get('non_stroking_color'))):
                result.append((item['x0'], item['top'], item['x1'], item['bottom']))
    return tuple(result)


def _graphic_bounds(doc, page0):
    page = doc['pages'][page0]
    if page.get('layoutBands'):
        raise ValueError('전체 정돈을 적용한 페이지입니다. 전체 정돈 방식으로 추가해주세요.')
    result = []
    # Optional explicit reservations may supplement a measured PDF background.
    for rect in page.get('reservedAreas', []):
        result.append(_box(rect))
    source = doc.get('source', {}).get('path')
    if source:
        path = (native.ROOT / source).resolve()
        if not path.is_relative_to(native.ROOT.resolve()) or not path.is_file() or path.suffix.lower() != '.pdf':
            raise ValueError('원본 배경을 확인할 수 없어 안전한 위치를 제안할 수 없습니다.')
        stat = path.stat()
        result.extend(_pdf_graphics(str(path), stat.st_mtime_ns, stat.st_size, page0))
    elif page.get('backgroundUrl') or page.get('backgroundData'):
        raise ValueError('배경 그림의 위치를 확인할 수 없습니다. 빈 배경에서 다시 시도해주세요.')
    return result


def _occupied(doc, page0):
    rects = [_box(b) for b in doc['pages'][page0]['blocks']
             if not (b.get('addition') and not b.get('text','').strip())]
    rects.extend(_graphic_bounds(doc, page0))
    return [(x0-CLEARANCE, y0-CLEARANCE, x1+CLEARANCE, y1+CLEARANCE)
            for x0, y0, x1, y1 in rects]


def _gaps(doc, page0, zone, occupied):
    width, height = doc['pageWidth'], doc['pageHeight']
    index = list(ZONE_NAMES).index(zone)
    top = max(MARGIN, height*index/3)
    bottom = min(height-MARGIN, height*(index+1)/3)
    mid = width/2
    columns = [(MARGIN, width-MARGIN), (MARGIN, mid-CLEARANCE),
               (mid+CLEARANCE, width-MARGIN)]
    candidates = []
    for left, right in columns:
        spans = sorted((max(top, y0), min(bottom, y1)) for x0, y0, x1, y1 in occupied
                       if x0 < right and x1 > left and y0 < bottom and y1 > top)
        cursor = top
        for y0, y1 in spans + [(bottom, bottom)]:
            if y0-cursor >= 12:
                candidates.append((left, cursor, right, y0))
            cursor = max(cursor, y1)
    # Prefer broad gaps, then closeness to requested third's center. Stable ties.
    return sorted(set(candidates), key=lambda r: (-(r[2]-r[0]),
                  abs((r[1]+r[3])/2-(top+bottom)/2), r[1], r[0]))


def _template(doc, page0, registry):
    candidates = []
    for pi, page in enumerate(doc['pages']):
        for block in page['blocks']:
            for run in block.get('runs', []) or block.get('styleSeed', []):
                if run.get('fontId') not in registry or not registry[run['fontId']].get('available'):
                    continue
                size = float(run.get('fontSize', 15))
                if 12 <= size <= 18:
                    candidates.append(((pi != page0, abs(size-15)), run))
    if candidates:
        return copy.deepcopy(min(candidates, key=lambda item: item[0])[1])
    # A truly blank native page has no source style; use the document's complete
    # default face, never an invented/unregistered font or an external service.
    entries = sorted(registry.values(), key=lambda f: f.get('resolvedName') != 'MalgunGothic')
    entry = next((f for f in entries if f.get('available') and not f.get('subset')), None)
    if entry is None:
        raise ValueError('추가 문구에 사용할 기본 글꼴이 없습니다.')
    return {'text': ' ', 'fontId': entry['id'], 'fontSize': 15, 'color': '#000000',
            'sourceFont': entry['originalName'], 'bold': False, 'italic': False, 'glyphs': []}


def propose(doc, text, page0, zone='top'):
    """Return {document, summary, warnings}; original doc is never mutated.

    This is a measured layout suggestion, not AI review or approval. Unknown
    backgrounds are rejected. Dense pages are deliberately allowed to fail.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('추가할 내용을 입력해주세요.')
    if len(text) > 30000 or any(ord(ch) < 32 and ch not in '\n\r\t' for ch in text):
        raise ValueError('추가 문구가 너무 길거나 표시할 수 없는 문자가 있습니다.')
    if not isinstance(doc, dict) or doc.get('engine') != 'native-v2' or len(doc.get('pages', [])) != 4:
        raise ValueError('편집 중인 4페이지 주보를 선택해주세요.')
    if type(page0) is not int or not 0 <= page0 < 4:
        raise ValueError('내용을 넣을 페이지를 1~4면 중에서 선택해주세요.')
    if zone not in ZONE_NAMES:
        raise ValueError('위쪽·가운데·아래쪽 중에서 위치를 선택해주세요.')
    width, height = doc.get('pageWidth'), doc.get('pageHeight')
    if not isinstance(width, (int,float)) or not isinstance(height, (int,float)) or not (80 < width < 3000 and 80 < height < 3000):
        raise ValueError('주보의 페이지 크기를 확인해주세요.')
    extras = doc.get('serviceData', {}).get('extras', [])
    if not isinstance(extras, list):
        raise ValueError('추가 문구 목록을 확인해주세요.')
    registry = {entry['id']: entry for entry in doc['fonts']}
    occupied = _occupied(doc, page0)
    template = _template(doc, page0, registry)
    style = {k: copy.deepcopy(v) for k, v in template.items() if k not in ('text', 'glyphs')}
    style.update(text=' ', glyphs=[])
    preferred = float(style['fontSize'])
    field = f'extras.{len(extras)}.text'
    digest = hashlib.sha256(f'{page0}|{zone}|{len(extras)}|{text}'.encode()).hexdigest()[:16]
    ident = 'addition-' + digest
    existing_ids = {b['id'] for p in doc['pages'] for b in p['blocks']}
    while ident in existing_ids:
        ident += '-next'
    zones=list(ZONE_NAMES)
    ordered_zones=sorted(zones,key=lambda z:(abs(zones.index(z)-zones.index(zone)),zones.index(z)))
    candidates=[(actual_zone,rect) for actual_zone in ordered_zones
                for rect in _gaps(doc,page0,actual_zone,occupied)]
    for actual_zone,(x0, y0, x1, y1) in candidates:
        reserve = max(4, preferred*.35)
        if y1-y0 < preferred+reserve:
            continue
        block = {'id': ident, 'type': 'text', 'addition': True, 'text': '', 'runs': [copy.deepcopy(style)],
                 'styleSeed': [copy.deepcopy(style)], 'positioned': False, 'x': x0, 'y': y0,
                 'w': x1-x0, 'h': y1-y0-reserve, 'fontId': style['fontId'], 'fontSize': preferred,
                 'lineHeight': 1.2, 'align': 'left', 'padding': 0, 'color': style.get('color','#000000'),
                 'binding': field, 'fieldBindings': [{'field': field}], 'group': 'extras',
                 'label': '추가 안내', 'editable': True, 'fontAvailable': True,
                 'sameFontAvailable': False, 'sourceText': '',
                 'fit': {'preferredSize': preferred, 'minimumSize': 12, 'wrap': True}}
        try:
            native._replace(block, text, registry)
        except ValueError as exc:
            if '영역을 넘습니다' in str(exc):
                continue
            raise ValueError('입력한 문자 일부를 기본 글꼴로 표시할 수 없습니다. 내용을 확인해주세요.') from exc
        # Freeze the proposal's selected size, then reclaim unused gap height.
        selected = block.get('fitSize', preferred)
        for run in block['runs']:
            run['fontSize'] *= selected/preferred
        block['fontSize'] = selected
        block['fit'] = {'preferredSize': selected, 'minimumSize': min(12,selected), 'wrap': True}
        glyphs = native._layout(block, registry)
        used = max((y-y0 for _,_,y,_,_ in glyphs), default=selected)
        block['h'] = min(y1-y0, math.ceil(used+reserve))
        if any(_intersects(_box(block), obstacle) for obstacle in occupied):
            continue
        block['styleSeed'] = [{k:copy.deepcopy(v) for k,v in r.items() if k!='glyphs'} for r in block['runs']]
        block['previewGlyphs'] = [{'text':ch,'x':x,'baseline':y,'fontSize':size,'fontId':run['fontId'],
                                  'color':run['color']} for ch,x,y,size,run in native._layout(block,registry)]
        proposal = copy.deepcopy(doc)
        proposal['pages'][page0]['blocks'].append(block)
        proposal.setdefault('serviceData', {}).setdefault('extras', []).append(
            {'id':ident,'text':text,'page':page0,'zone':actual_zone,'requestedZone':zone})
        proposal.setdefault('fieldBindings', []).append({'blockId':ident,'field':field,'group':'extras'})
        proposal['layoutProposal'] = {'kind':'measured-native-gap','requiresApproval':True,
                                     'blockId':ident,'page':page0,'zone':actual_zone,'requestedZone':zone,'usesAI':False}
        warnings=['글꼴과 빈 공간을 측정한 배치 제안입니다. 적용 전에 미리보기를 확인해주세요.']
        if actual_zone!=zone:
            warnings.append(f'요청하신 {ZONE_NAMES[zone]}에는 공간이 없어 같은 페이지의 {ZONE_NAMES[actual_zone]}에 배치했습니다.')
        return {'document': proposal,
                'summary': f'{page0+1}면 {ZONE_NAMES[actual_zone]}의 빈 공간에 추가 안내를 배치했습니다. 확인 후 적용해주세요.',
                'warnings': warnings}
    raise ValueError('선택한 위치에 내용을 안전하게 넣을 빈 공간이 없습니다. 문구를 줄이거나 다른 위치·페이지를 선택해주세요.')
