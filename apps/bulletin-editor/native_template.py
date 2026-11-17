"""Editable, measured 20261108 four-page seed. No legacy server dependency.

Public contract: native_template_document(), update_document(doc, flat_changes),
edit_block(doc, id, text), render_document(doc, mode), font_bytes(id),
background_bytes(zero_based_page). All coordinates are top-left PDF points.
Source faces are explicitly mapped to complete installed fonts at user request.
Registry entries disclose substitutions. PDF subsets are optional, not required.
The background contains graphics only: all PDF text operators are removed.
Font URLs and background URLs are endpoint suggestions for the hosting UI.
The host must use this renderer, not legacy server.text_layer.
"""
from __future__ import annotations

import copy
import base64
import datetime
import difflib
import hashlib
import io
import os
from pathlib import Path
import re
import struct
from functools import lru_cache

import pdfplumber
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parents[2]
W, H = 595, 842
CACHE = ROOT / 'output' / 'cache' / 'native-bulletin'
_FONT_BACKEND = None


class _EmbeddedAdapter:
    def __init__(self, module, extra_paths=()):
        self.module=module
        self.entries=module.discover_fonts(_source(),extra_paths)

    def resolve_font(self,name):
        entry=copy.deepcopy(self.entries.get(name))
        if not entry:return None
        rights=entry.get('fsType')
        rights=rights if isinstance(rights,list) else [rights]
        if any(r is not None and (r & (2|0x200) or (r&4 and not r&8)) for r in rights):
            entry.update(available=False,reason='Source fsType does not permit editable outline-font embedding.')
        return entry

    def font_bytes(self,entry):return self.module.font_bytes(entry['originalName'])
    def has_glyph(self,entry,char):return self.module.has_chars(entry['originalName'],char)
    def measure_text(self,entry,text,size):return self.module.measure(entry['originalName'],text,size)
    def draw_text(self,c,entry,text,x,y,size,color):
        # Reuse exact glyph outlines as local PDF forms within this layer.
        # This avoids recompiling the same CFF outline for every repeated letter.
        forms=getattr(c,'_native_glyph_forms',None)
        if forms is None:
            forms={};c._native_glyph_forms=forms
        key=(entry['originalName'],text,color)
        if key not in forms:
            name='native-glyph-'+str(len(forms));forms[key]=name
            c.beginForm(name);c.setFillColor(HexColor(color))
            self.module.draw(c,entry['originalName'],text,0,0,1)
            c.endForm()
        c.saveState();c.translate(x,y);c.scale(size,size);c.doForm(forms[key]);c.restoreState()


def enable_embedded_fonts(extra_paths=()):
    """Opt in to the sibling module after its dependencies are installed.

    Subset coverage stays strict; fsType evidence/unknowns remain in the registry.
    Returns the provider's discovery report; no full font is claimed/installed.
    """
    import embedded_fonts
    adapter=_EmbeddedAdapter(embedded_fonts,extra_paths)
    set_font_backend(adapter)
    return copy.deepcopy(adapter.entries)


def set_font_backend(backend):
    """Attach an embedded-font adapter; never changes approved files.

    Adapter methods: resolve_font(originalName)->dict|None (must explicitly set
    available=True), font_bytes(entry)->bytes, has_glyph(entry,unicode)->bool,
    measure_text(entry,text,size)->float, draw_text(canvas,entry,text,x,y,size,color).
    x/y for draw_text are PDF bottom-left coordinates. The returned entry may
    contain provider-specific IDs, permission evidence and partial glyph coverage.
    Host is responsible for calling this before loading a stored document.
    """
    global _FONT_BACKEND
    _FONT_BACKEND = backend
    _seed.cache_clear()


def enable_full_fonts():
    """Default weekly engine: complete installed faces, never PDF subsets."""
    set_font_backend(None)
    return native_template_document()['fonts']


def _map_runs(runs,registry):
    """Choose a complete installed face per character, shared by HTML/PDF."""
    result=[]
    def supports(entry,ch):
        return ch in '\n\r\t' or ord(ch) in _register(entry).face.charToGlyph
    for run in runs:
        source=registry[run['fontId']];segments=[];start=0
        for i,ch in enumerate(run['text']):
            chosen=source
            if not supports(chosen,ch):
                serif=bool(re.search('Batang|Myeongjo|Mincho|MinPro|Song|YuMin',run.get('sourceFont','')))
                choices=(['Batang','YuMincho-Regular','Meiryo','MalgunGothic'] if serif else
                         ['Meiryo','MalgunGothic','Batang','YuMincho-Regular'])
                for name in choices:
                    candidate=next((f for f in registry.values() if f.get('resolvedName')==name and supports(f,ch)),None)
                    if candidate:
                        chosen=candidate;break
            if not segments or segments[-1][2]!=chosen['id']:
                segments.append([i,i+1,chosen['id']])
            else:segments[-1][1]=i+1
        new=[]
        for a,b,font_id in segments:
            style={k:copy.deepcopy(v) for k,v in run.items() if k not in ('text','glyphs')}
            style.update(text=run['text'][a:b],fontId=font_id,glyphs=[])
            if font_id!=run['fontId']:style['mappedFromFontId']=run['fontId']
            new.append(style)
        cursor=0
        for glyph in run.get('glyphs',[]):
            offset=run['text'].find(glyph['text'],cursor)
            if offset<0:raise ValueError('Original glyph/text alignment is inconsistent.')
            cursor=offset+len(glyph['text'])
            index=next(i for i,(a,b,_) in enumerate(segments) if a<=offset<b)
            new[index]['glyphs'].append(copy.deepcopy(glyph))
        for item in new:
            glyphs=[g for g in item['glyphs'] if not g['text'].isspace() and g.get('w',0)>0]
            natural=sum(_width(registry[item['fontId']],g['text'],item['fontSize']) for g in glyphs)
            if natural:item['widthScale']=max(.65,min(1.25,sum(g['w'] for g in glyphs)/natural))
        result.extend(new)
    return result


def _provider(entry):
    if entry.get('backend')=='embedded':
        if _FONT_BACKEND is None:
            raise ValueError('Embedded font backend is not attached.')
        return _FONT_BACKEND
    return None


def _check_run(run, registry, positioned=False):
    entry=registry[run['fontId']];provider=_provider(entry)
    # Original PDF blank advances and inferred inter-word gaps are positioning,
    # not printable glyphs. Do not require a fictional space glyph for them.
    text=''.join(g['text'] for g in run['glyphs'] if not g['text'].isspace()) if positioned else run['text']
    if provider:
        missing=sorted({ch for ch in text if ch not in '\n\r\t' and not provider.has_glyph(entry,ch)})
    else:
        font=_register(entry)
        missing=sorted({ch for ch in text if ch not in '\n\r\t' and ord(ch) not in font.face.charToGlyph})
    if missing:
        raise ValueError(f"{entry['originalName']}: unsupported glyphs {''.join(missing)!r}")


def _width(entry,text,size):
    provider=_provider(entry)
    if provider:return provider.measure_text(entry,text,size)
    font=_register(entry)
    return pdfmetrics.stringWidth(text,font.fontName,size)


def _draw(c,entry,text,x,y,size,color):
    provider=_provider(entry)
    if provider:
        provider.draw_text(c,entry,text,x,y,size,color)
    else:
        font=_register(entry);c.setFont(font.fontName,size);c.setFillColor(HexColor(color))
        if entry.get('syntheticBold'):
            c.saveState();c.setStrokeColor(HexColor(color));c.setLineWidth(size*.018)
            obj=c.beginText(x,y);obj.setTextRenderMode(2);obj.textOut(text);c.drawText(obj);c.restoreState()
        else:c.drawString(x,y,text)


def _source():
    found = list((ROOT / '자료' / '주보_기준원본').glob('20261108*.pdf'))
    if len(found) != 1:
        raise ValueError('Exactly one retained 20261108 PDF is required.')
    return found[0]


def _font_name(name):
    return re.sub(r'^[A-Z]{6}\+', '', name)


def _font_id(name):
    return 'native-' + hashlib.sha256(_font_name(name).encode()).hexdigest()[:16]


def _font_roots():
    """Explicit portable path bases, including optional Hancom shared folders."""
    roots = {'WindowsFont': Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
             'Project': ROOT}
    if os.environ.get('LOCALAPPDATA'):
        roots['UserFont'] = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Windows/Fonts'
    for key, env in [('ProgramFiles', 'PROGRAMFILES'), ('ProgramFilesX86', 'PROGRAMFILES(X86)')]:
        if os.environ.get(env):
            roots[key] = Path(os.environ[env])
    return roots


def _font_candidates():
    roots = _font_roots()
    candidates = set()
    for base in ('WindowsFont', 'UserFont'):
        folder = roots.get(base)
        if folder and folder.is_dir():
            candidates.update(p.resolve() for p in folder.iterdir()
                              if p.suffix.lower() in ('.ttf', '.ttc', '.otf'))
    for base in ('ProgramFiles', 'ProgramFilesX86', 'Project'):
        folder = roots.get(base)
        if not folder:
            continue
        for relative in (('fonts', '자료/fonts') if base == 'Project' else
                         ('Hnc', 'Hancom', 'Common Files/Hnc', 'Common Files/Hancom')):
            sub = folder / relative
            if sub.is_dir():
                candidates.update(p.resolve() for p in sub.rglob('*')
                                  if p.suffix.lower() in ('.ttf', '.ttc', '.otf'))
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        value = winreg.EnumValue(key, i)[1]
                        if isinstance(value, str):
                            p = Path(os.path.expandvars(value))
                            if not p.is_absolute():
                                p = roots['WindowsFont'] / p
                            if p.is_file():
                                candidates.add(p.resolve())
            except OSError:
                pass
    except ImportError:
        pass
    return sorted(candidates)


def _faces(path):
    """Read SFNT names, TTC face offsets and OS/2 fsType without fontTools."""
    raw = path.read_bytes()
    offsets = [0]
    if raw[:4] == b'ttcf':
        count = struct.unpack_from('>I', raw, 8)[0]
        offsets = list(struct.unpack_from('>' + 'I' * count, raw, 12))
    for face_index, offset in enumerate(offsets):
        try:
            count = struct.unpack_from('>H', raw, offset + 4)[0]
            tables = {}
            for i in range(count):
                tag, _, start, length = struct.unpack_from('>4sIII', raw, offset + 12 + i * 16)
                tables[tag] = (start, length)
            start, _ = tables[b'name']
            _, n, strings = struct.unpack_from('>HHH', raw, start)
            names = {}
            for i in range(n):
                platform, enc, lang, nid, length, off = struct.unpack_from('>HHHHHH', raw, start + 6 + 12 * i)
                data = raw[start + strings + off:start + strings + off + length]
                try:
                    value = data.decode('utf-16-be' if platform in (0, 3) else 'mac_roman')
                    names.setdefault(nid, set()).add(value)
                except UnicodeError:
                    pass
            fs_type = struct.unpack_from('>H', raw, tables[b'OS/2'][0] + 8)[0] if b'OS/2' in tables else None
            yield face_index, names, fs_type
        except (KeyError, struct.error):
            continue


def _location(path, index):
    for base, folder in _font_roots().items():
        try:
            return {'base': base, 'path': path.relative_to(folder.resolve()).as_posix(), 'faceIndex': index}
        except ValueError:
            pass
    return None


@lru_cache(maxsize=1)
def _installed_faces():
    result = []
    for path in _font_candidates():
        try:
            for index, names, fs_type in _faces(path):
                location = _location(path, index)
                if location:
                    result.append((names, fs_type, location))
        except OSError:
            pass
    return result


def _registry(names):
    result = []
    for original in sorted({_font_name(n) for n in names}):
        entry = {'id': _font_id(original), 'family': original, 'originalName': original,
                 'available': False, 'sameFontAvailable': False, 'location': None,
                 'reason': 'Exact source PostScript/full font name not installed; no alias applied.',
                 'url': '/api/weekly/font/' + _font_id(original)}
        for aliases, fs_type, location in _installed_faces():
            # Exact PS/full name only. Family-name similarity is not font identity.
            if original not in aliases.get(6, set()) | aliases.get(4, set()):
                continue
            entry.update(location=location, fsType=fs_type)
            if fs_type is None or fs_type & (2 | 0x200):
                entry['reason'] = 'Embedding permission unknown/restricted or bitmap-only (OS/2 fsType).'
            else:
                entry.update(available=True, sameFontAvailable=True, reason=None)
            break
        if not entry['available']:
            korean=bool(re.search('HCR|Haansoft|Nanum|Dotum|Batang',original))
            serif=bool(re.search('Batang|Myeongjo|Mincho|MinPro|Song|YuMin',original))
            bold=bool(re.search('Bold|ExtraBold|W6|Demibold',original))
            preferred=(['Batang'] if korean and serif else
                       ['MalgunGothicBold' if bold else 'MalgunGothic'] if korean else
                       ['YuMincho-Demibold' if bold else 'YuMincho-Regular','MS-Mincho'] if serif else
                       ['Meiryo-Bold' if bold else 'Meiryo'])
            for choice in preferred:
                match=next(((aliases,fs,loc) for aliases,fs,loc in _installed_faces()
                            if choice in aliases.get(6,set()) and fs is not None and not fs&(2|0x200)
                            and not(fs&4 and not fs&8)),None)
                if match:
                    aliases,fs,loc=match
                    entry.update(available=True,sameFontAvailable=False,location=loc,fsType=fs,
                                 family=choice,resolvedName=choice,backend='installed-full',subset=False,
                                 syntheticBold=bool(bold and choice=='Batang'),
                                 reason='User-authorized full installed font mapping; closest visual match, not original face.')
                    break
        if entry['available'] and entry.get('backend')!='installed-full':
            entry.update(resolvedName=original,backend='installed-full',subset=False)
        entry['substituted']=bool(entry.get('available') and not entry.get('sameFontAvailable'))
        if entry.get('location'):
            loc=entry['location']
            entry['renderFontId']='full-'+hashlib.sha256((loc['base']+'/'+loc['path']+':'+str(loc['faceIndex'])).encode()).hexdigest()[:16]
        if not entry['available'] and _FONT_BACKEND is not None:
            resolved=_FONT_BACKEND.resolve_font(original)
            if resolved and resolved.get('available') is True:
                entry.update(resolved)
                entry.update(id=_font_id(original),originalName=original,backend='embedded',sameFontAvailable=True)
            elif resolved:
                entry['embeddedReason']=resolved.get('reason')
        result.append(entry)
    return result


def _resolve(entry):
    if not entry.get('available') or not entry.get('location'):
        raise ValueError('Original font unavailable: ' + entry['originalName'])
    loc = entry['location']; base = _font_roots().get(loc['base'])
    if base is None:
        raise ValueError('Font path base unavailable.')
    path = (base / loc['path']).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError('Invalid font path.')
    return path, loc.get('faceIndex', 0)


def _register(entry):
    path, index = _resolve(entry)
    name = entry.get('renderFontId',entry['id'])
    if name not in pdfmetrics.getRegisteredFontNames():
        try:
            pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=index))
        except Exception as exc:
            raise ValueError(f"Exact font cannot be rendered: {entry['originalName']}: {exc}") from exc
    return pdfmetrics.getFont(name)


def _color(value):
    if isinstance(value, (float, int)):
        value = [value] * 3
    if not value:
        value = [0, 0, 0]
    if len(value) != 3:
        return '#000000'
    return '#' + ''.join(f'{max(0,min(255,round(v*255))):02x}' for v in value)


def _clean(text):
    # Quartz encoded word-space NULs are spaces, not invented letters.
    return text.replace('\x00', ' ').replace('\U000f0035','↑').replace('\U000f0036','↓')


def _char_lines(chars):
    """Baseline clustering retains small-size annotations in their actual line."""
    lines = []
    for ch in sorted(chars, key=lambda c: (H - c['matrix'][5], c['x0'])):
        baseline = H - ch['matrix'][5]
        overlaps = bool(lines) and any(
            min(c['size'],ch['size']) < .8*max(c['size'],ch['size'])
            and abs(H-c['matrix'][5]-baseline)<.6*max(c['size'],ch['size'])
            and min(c['bottom'], ch['bottom']) - max(c['top'],ch['top'])
            > .4*min(c['height'],ch['height']) for c in lines[-1][1])
        if not lines or (abs(lines[-1][0] - baseline) > 6 and not overlaps):
            lines.append((baseline, []))
        lines[-1][1].append(ch)
    return [sorted(row, key=lambda c: c['x0']) for _, row in lines]


def _runs(chars):
    runs = []
    for row in _char_lines(chars):
        previous = None
        for ch in row:
            style = (_font_id(ch['fontname']), round(ch['size'], 4), _color(ch.get('non_stroking_color')))
            if not runs or runs[-1]['_key'] != style or previous is None:
                runs.append({'text': '', 'fontId': style[0], 'fontSize': style[1], 'color': style[2],
                             'sourceFont': _font_name(ch['fontname']), 'bold': bool(re.search('Bold|W6|ExtraBold', ch['fontname'])),
                             'italic': bool(re.search('Italic|Oblique', ch['fontname'])), 'glyphs': [], '_key': style})
            gap = ch['x0'] - previous['x1'] if previous else 0
            if previous and gap > max(.6, ch['size'] * .18):
                runs[-1]['text'] += ' '
            text = _clean(ch['text'])
            runs[-1]['text'] += text
            runs[-1]['glyphs'].append({'text': text, 'x': round(ch['x0'], 4), 'y': round(ch['top'], 4),
                                      'w': round(ch['width'], 4), 'baseline': round(H - ch['matrix'][5], 4),
                                      'sourceText': ch['text']})
            previous = ch
        if runs:
            runs[-1]['text'] += '\n'
    for run in runs:
        run.pop('_key')
    if runs:
        runs[-1]['text'] = runs[-1]['text'].rstrip('\n')
    return runs


def _friendly_label(field,group,text):
    fixed={'date':'예배 날짜','issue':'주보 호수','serviceTitleJa':'예배 제목','serviceTitleKo':'예배 제목',
           'preacher':'설교자','interpreters':'통역자'}
    if field in fixed:return fixed[field]
    parts=field.split('.')
    if parts[0]=='scripture':
        return ('사죄확인' if parts[1]=='absolution' else '설교본문')+' '+{'ja':'일본어 본문','ko':'한국어 본문','refJa':'일본어 구절','refKo':'한국어 구절'}.get(parts[-1],'말씀')
    if parts[0]=='service':
        return f'예배순서 {int(parts[1])+1} '+{'nameRaw':'순서명','detailRaw':'내용','leader':'담당자'}.get(parts[-1],'내용')
    if parts[0]=='notices':return '특별 안내' if parts[1]=='0' else '교회소식 '+parts[1]
    if parts[0]=='rosterRaw':return '예배봉사표 '+str(int(parts[1])+1)+'주차'
    if '時' in text and ('午前' in text or '午後' in text):return '예배 시간 안내'
    return {'cover':'표지 안내','service':'예배 안내','scripture':'말씀 안내','notices':'교회소식 안내'}.get(group,'주보 내용')


def _block(chars, ident, binding, group, box=None):
    runs = _runs(chars)
    x0 = min(c['x0'] for c in chars); y0 = min(c['top'] for c in chars)
    x1 = max(c['x1'] for c in chars); y1 = max(c['bottom'] for c in chars)
    if box:
        x0, y0, x1, y1 = box
    size = max(r['fontSize'] for r in runs)
    return {'id': ident, 'type': 'text', 'text': ''.join(r['text'] for r in runs), 'runs': runs,
            'x': round(x0, 4), 'y': round(y0, 4), 'w': round(x1-x0, 4), 'h': round(y1-y0, 4),
            'fontId': runs[0]['fontId'], 'fontSize': size, 'lineHeight': 1.2, 'align': 'left',
            'binding': binding, 'fieldBindings': [{'field': binding}], 'group': group,
            'label': _friendly_label(binding,group,''.join(r['text'] for r in runs)),
            'editable': True, 'padding': 0, 'positioned': True,
            'styleTransfer': 'character-index-clamped-to-last',
            'fit': {'preferredSize': size, 'minimumSize': min(size, 12), 'wrap': True},
            'sourceBounds': [x0, y0, x1, y1]}


def _in(ch, box):
    return box[0] <= (ch['x0']+ch['x1'])/2 < box[2] and box[1] <= (ch['top']+ch['bottom'])/2 < box[3]


def _regions():
    """Only measured fields; no schedule from compose.initial_data is reused."""
    regions = [
        (0, 'issue', (435, 28, 595, 51), 'date'),
        (0, 'date', (435, 71, 595, 94), 'date'),
        (1, 'serviceTitleJa', (0, 0, 310, 42), 'service'),
        (1, 'preacher', (310, 0, 595, 25), 'service'),
        (1, 'interpreters', (310, 25, 595, 44), 'service'),
        (2, 'scripture.absolution.ja', (1.32, 31.08, 325.92, 213.84), 'scripture'),
        (2, 'scripture.absolution.ko', (325.92, 31.08, 594, 213.84), 'scripture'),
        (2, 'scripture.sermon.ja', (1.32, 256.56, 320.28, 822.84), 'scripture'),
        (2, 'scripture.sermon.ko', (320.28, 256.56, 594, 822.84), 'scripture'),
    ]
    # Original rows, measured from actual text baselines. Three editable cells.
    bounds = [(70.56,101),(101,137),(167,203),(203,245),(245,280),(280,317),
              (317,359),(359,394),(422,462),(462,505),(505,565),(595,637),
              (637,679),(679,711),(711,739),(739,777),(777,803)]
    for i, (top, bottom) in enumerate(bounds):
        for key, left, right in [('nameRaw', 1.92, 158.4), ('detailRaw', 158.4, 455.76), ('leader', 455.76, 593.4)]:
            regions.append((1, f'service.{i}.{key}', (left, top, right, bottom), 'service'))
    # Notice boundaries from the actual baseline, including the bilingual mixed lines.
    for i, (top, bottom) in enumerate([(34,226),(232,288),(288,344),(344,450),(450,584),(584,640)]):
        regions.append((3, f'notices.{i}.raw', (1.32, top, 594, bottom), 'notices'))
    for i, (top, bottom) in enumerate([(718, 770), (770, 823)]):
        regions.append((3, f'rosterRaw.{i}', (1.32, top, 594, bottom), 'roster'))
    return regions


def _set(data, path, value):
    parts = path.split('.'); node = data
    for i, key in enumerate(parts[:-1]):
        following = parts[i+1]
        if isinstance(node, list):
            while len(node) <= int(key):
                node.append({})
            node = node[int(key)]
        else:
            node = node.setdefault(key, [] if following.isdigit() else {})
    if isinstance(node, list):
        while len(node)<=int(parts[-1]):
            node.append(None)
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value


def _get(data, path):
    for key in path.split('.'):
        data = data[int(key)] if isinstance(data, list) else data[key]
    return data


def _language_parts(text):
    """Actual mixed-line text, split at Hangul, keeping every printed segment."""
    ja, ko = [], []
    for line in text.splitlines(keepends=True):
        match = re.search('[가-힣]',line)
        if not match:
            if line.strip():
                ja.append(line)
        else:
            prefix = line[:match.start()]
            if re.search('[ぁ-ヿ一-鿿]',prefix):
                if prefix.strip():
                    ja.append(prefix)
                ko.append(line[match.start():])
            else:
                ko.append(line)
    return ja,ko


def _canonical_fields(data, pages):
    """Source-derived canonical PPT input; layout raw strings remain auxiliary."""
    def block(field):
        return next(b for p in pages for b in p['blocks'] if b['binding']==field)
    def alias(b,field,segments):
        b['fieldBindings']=[a for a in b['fieldBindings'] if a['field']!=field]
        b['fieldBindings'].append({'field':field,'segments':segments})
    for i,row in enumerate(data['service']):
        b=block(f'service.{i}.nameRaw')
        ja_parts,ko_parts=_language_parts(b['text'])
        raw_ja=''.join(ja_parts);raw_ko=''.join(ko_parts)
        row['ja']=re.sub('[\U000f0000-\U000ffffd]','',raw_ja).strip()
        # These are label normalization, not replacement source content.
        label=re.sub(r'\s+','',raw_ko)
        row['ko']={'봉헌을위한기도':'봉헌기도'}.get(label,label)
        alias(b,f'service.{i}.ja',[raw_ja] if raw_ja else [])
        alias(b,f'service.{i}.ko',[raw_ko] if raw_ko else [])
        detail=block(f'service.{i}.detailRaw')
        jp,kp=_language_parts(detail['text'])
        row['detailJa']=''.join(jp).strip('… \n')
        row['detailKo']=''.join(kp).strip('… \n')
        if i in (1,2) and not row['detailKo']:
            # Structured bilingual reference, not an invented printed paragraph.
            import scripture
            try:
                ref=scripture.parse_reference(row['detailJa']);index=scripture.IDS.index(ref['book'])
                suffix=re.search(r'\d.*$',row['detailJa'])[0]
                row['detailKo']=scripture.KO[index]+' '+suffix
                row['derivedFields']={'detailKo':{'from':f'service.{i}.detailJa',
                                               'transform':'canonical-book-name', 'printed':False}}
            except (ValueError,TypeError,IndexError):row.pop('derivedFields',None)
        alias(detail,f'service.{i}.detailJa',jp)
        alias(detail,f'service.{i}.detailKo',kp)
        # Distinct numbering systems must not be conflated by PPT hymn lookup.
        raw=detail['text'];number=re.search(r'韓日\s*(\d+)章',raw)
        row.pop('hymnReferences',None)
        if number:
            refs={'hanil':number[1]}
            for key,pattern in [('japanese',r'(?:教会[讃賛]美歌|[讃賛]美歌(?:第?(?:II|二)編)?|聖歌)\s*(\d+[ab]?)'),('newKorean',r'새\s*(\d+)')]:
                m=re.search(pattern,raw)
                if m:refs[key]=m[1]
            row['hymnReferences']=refs
            # Canonical existing PPT interface sees the hanil number only.
            title_ja=re.search('『([^』]+)』',raw)
            title_ko=re.search(r'\(([^()가-힣]*[가-힣][^()]*)\)',raw)
            row['detailJa']=(('『'+title_ja[1]+'』 ') if title_ja else '')+'韓日'+number[1]+'章'
            row['detailKo']=title_ko[1].strip() if title_ko else ''
        if i==3:
            row['detailJa']=data['scripture']['absolution']['refJa'];row['detailKo']=data['scripture']['absolution']['refKo']
        if i==8:
            row['detailJa']=data['scripture']['sermon']['refJa'];row['detailKo']=data['scripture']['sermon']['refKo']
    for i,notice in enumerate(data['notices']):
        b=block(f'notices.{i}.raw');jp,kp=_language_parts(b['text'])
        notice.update(ja=''.join(jp).strip(),ko=''.join(kp).strip())
        alias(b,f'notices.{i}.ja',jp);alias(b,f'notices.{i}.ko',kp)
    # Compact verse paragraphs for canonical PPT; preserve every source word,
    # punctuation and printed verse number. No absent verse 21 is invented.
    for kind in ('absolution','sermon'):
        for lang in ('ja','ko'):
            b=block(f'scripture.{kind}.{lang}')
            text=b['text'];lines=[];previous_space=False
            for line in text.splitlines():
                # Superscript 9 may be mid-line in the Korean source column.
                line=re.sub(r'(?<=[가-힣。요])\s+(9)(?=\s*만일)',r'\n\1 ',line)
                for part in line.splitlines():
                    m=re.match(r'^\s*(\d{1,3})[.．]?\s*(.*)$',part)
                    if m:
                        lines.append(m[1]+' '+m[2].strip())
                    elif lines:
                        lines[-1]+= (' ' if lang=='ko' and previous_space else '')+part.strip()
                    else:
                        lines.append(part.strip())
                    previous_space=part.endswith((' ','\t'))
            data['scripture'][kind][lang]='\n'.join(re.sub(' +',' ',line) for line in lines)
            # Canonical value and display may differ solely by source line wraps.
            b['canonicalText']=data['scripture'][kind][lang]


def _roster_fields(page, data, pages):
    """Measured table cells; merged month/day and cleaning cells are shared."""
    data['roster']=[]
    def read(box):
        chars=[c for c in page.chars if _in(c,box)]
        return ''.join(r['text'] for r in _runs(chars)).strip() if chars else ''
    for week,(top,bottom,day,clean_day) in enumerate([(718,770,'15','14'),(770,823,'22','21')]):
        raw=next(b for b in pages[3]['blocks'] if b['binding']==f'rosterRaw.{week}')
        cleaning=read((424.8,top,594,bottom))
        for service,(yt,yb) in enumerate([(top,top+28),(top+28,bottom)]):
            i=len(data['roster'])
            row={'date':'11/'+day,'service':f'{service+1}部',
                 'prayer':read((135,yt,277.08,yb)), 'interpreter':read((277.08,yt,424.8,yb)),
                 'cleaning':cleaning,'cleaningDate':'11/'+clean_day}
            data['roster'].append(row)
            for key in ('service','prayer','interpreter'):
                value=row[key]
                if value and value in raw['text']:
                    raw['fieldBindings'].append({'field':f'roster.{i}.{key}','substring':value})
            raw['fieldBindings'].append({'field':f'roster.{i}.date','substring':day,'dateDayOnly':True})
            # Preserve original discontiguous cleaning lines as an explicit group.
            segments=[x for x in cleaning.splitlines(keepends=True) if x.strip()]
            raw['fieldBindings'].append({'field':f'roster.{i}.cleaning','segments':segments})


def _backgrounds():
    """Remove PDF text, render remaining vectors/images. Never crop source text."""
    import pypdfium2 as pdfium
    source = _source(); digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    paths = [CACHE / f'{digest}-graphics-{i}.png' for i in range(4)]
    if not all(p.is_file() for p in paths):
        CACHE.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter(); writer.append(str(source)); writer.remove_text()
        data = io.BytesIO(); writer.write(data)
        pdf = pdfium.PdfDocument(data.getvalue())
        try:
            for i, path in enumerate(paths):
                page = pdf[i]
                bitmap = page.render(scale=2)
                bitmap.to_pil().save(path)
                bitmap.close(); page.close()
        finally:
            pdf.close()
    return paths


def background_bytes(page0based):
    if type(page0based) is not int or not 0 <= page0based < 4:
        raise ValueError('Page index must be 0..3.')
    return _backgrounds()[page0based].read_bytes()


@lru_cache(maxsize=1)
def _seed():
    data = {'schemaVersion': 1, 'date': '2026-11-08', 'issue': '1935', 'serviceTitleKo': '주일예배',
            'service': [], 'scripture': {'absolution': {'refJa': 'ヨハネの手紙 第一 1:8,9', 'refKo': '요한일서 1:8,9'},
                                       'sermon': {'refJa': 'マタイの福音書 17:14~21', 'refKo': '마태복음 17:14~21'}},
            'notices': [], 'roster': [], 'nativeText': {}, 'sourceProvider': 'retained-pdf',
            'versions': {'ko': '개역개정', 'ja': '新改訳2017'}}
    pages = []; all_fonts = set(); bindings = []
    with pdfplumber.open(_source()) as pdf:
        if len(pdf.pages) != 4 or any((p.width, p.height) != (W, H) for p in pdf.pages):
            raise ValueError('Unexpected baseline dimensions/pages.')
        raw_pages = [p.extract_text(x_tolerance=2, y_tolerance=3) or '' for p in pdf.pages]
        for pi, page in enumerate(pdf.pages):
            all_fonts.update(c['fontname'] for c in page.chars)
            remaining = list(page.chars); blocks = []
            for ri, (pindex, field, box, group) in enumerate(_regions()):
                if pi != pindex:
                    continue
                selected = [c for c in remaining if _in(c, box)]
                if not selected:
                    continue
                chosen = {id(c) for c in selected}; remaining = [c for c in remaining if id(c) not in chosen]
                block = _block(selected, f'native-{pi}-field-{ri}', field, group, box)
                if field in ('date', 'issue'):
                    block['format'] = 'japanese-date' if field == 'date' else 'issue'
                    block['value'] = data[field]
                else:
                    _set(data, field, block['text'])
                blocks.append(block)
            # Every remaining character is native text, including fixed headings/times.
            # Segment large column gaps so independent cells remain independent.
            for row in _char_lines(remaining):
                segments = [[]]
                for ch in row:
                    if segments[-1] and ch['x0'] - segments[-1][-1]['x1'] > 18:
                        segments.append([])
                    segments[-1].append(ch)
                for segment in segments:
                    ident = f'native-{pi}-text-{len(blocks)}'
                    field = 'nativeText.' + ident
                    block = _block(segment, ident, field, ['cover', 'service', 'scripture', 'notices'][pi])
                    data['nativeText'][ident] = block['text']; blocks.append(block)
            for block in blocks:
                block['sourceText'] = block['text']
                bindings.append({'blockId': block['id'], 'field': block['binding'], 'group': block['group']})
            pages.append({'name': ['표지', '예배순서', '말씀', '교회소식'][pi],
                          'backgroundUrl': f'/api/weekly/background/{pi}', 'blocks': blocks})
    # Split only at actual Hangul/script boundaries. Raw source remains available.
    for i, row in enumerate(data['service']):
        raw = row.get('nameRaw', '')
        match = re.search('[가-힣]', raw)
        at = match.start() if match else len(raw)
        row['ja'] = raw[:at].strip(); row['ko'] = raw[at:].strip()
        detail = row.get('detailRaw', '')
        match = re.search('[가-힣]', detail)
        at = match.start() if match else len(detail)
        row['detailJa'] = detail[:at].strip(); row['detailKo'] = detail[at:].strip()
        for suffix, text in [('ja',row['ja']),('ko',row['ko']),('detailJa',row['detailJa']),('detailKo',row['detailKo'])]:
            source_field = f'service.{i}.' + ('nameRaw' if suffix in ('ja','ko') else 'detailRaw')
            block = next(b for b in pages[1]['blocks'] if b['binding'] == source_field)
            if text:
                block['fieldBindings'].append({'field':f'service.{i}.{suffix}', 'substring':text})
        if i == 3:
            row['role'] = 'absolution'
        if i == 8:
            row['role'] = 'scripture'
    # Bind semantic references to every visible occurrence (including service cells).
    for kind, ja_token, ko_token in [('absolution','ヨハネ','요한일서'),('sermon','マタイ','마태복음')]:
        for language, token in [('ja',ja_token),('ko',ko_token)]:
            field = f'scripture.{kind}.ref' + language.title()
            for page in pages:
                for b in page['blocks']:
                    if b['binding'].startswith('scripture.') or token not in b['text']:
                        continue
                    pattern = (r'ヨハネの手紙\s*第一\s*1:8,9' if token == 'ヨハネ' else
                               r'マタイの福音書\s*17:14~21' if token == 'マタイ' else
                               r'요한일서\s*1:8,9' if token == '요한일서' else r'마태복음\s*17:14~21')
                    match = re.search(pattern, b['text'])
                    if match:
                        b['fieldBindings'].append({'field':field, 'substring':match[0]})
    _canonical_fields(data,pages)
    with pdfplumber.open(_source()) as pdf:
        _roster_fields(pdf.pages[3],data,pages)
    bindings = [dict(item, blockId=b['id'], group=b['group']) for p in pages
                for b in p['blocks'] for item in b['fieldBindings']]
    fonts = _registry(all_fonts); registry = {f['id']: f for f in fonts}
    for p in pages:
        for b in p['blocks']:
            b['runs']=_map_runs(b['runs'],registry)
            b['styleSeed']=[{k:copy.deepcopy(v) for k,v in r.items() if k!='glyphs'} for r in b['runs']]
            b['sameFontAvailable'] = all(registry[r['fontId']]['sameFontAvailable'] for r in b['runs'])
            b['fontAvailable'] = all(registry[r['fontId']]['available'] for r in b['runs'])
            b['issues'] = []
            if '\ufffd' in b['text'] or any(0xF0000 <= ord(c) <= 0xFFFFD for c in b['text']):
                b['issues'].append('PDF contains unmapped/private-use text; human source confirmation required.')
    return {'schemaVersion': 1, 'engine': 'native-v2', 'template': 'four', 'pageWidth': W, 'pageHeight': H,
            'title': '2026-11-08 주일주보 원본 조판', 'pages': pages, 'serviceData': data,
            'fonts': fonts, 'fontRegistry': registry, 'fieldBindings': bindings,
            'source': {'path': _source().relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(_source().read_bytes()).hexdigest(),
                       'rawPages': raw_pages, 'date': '2026-11-08', 'issue': '1935'},
            'capabilities': {'nativeText': True, 'editableRuns': True, 'exactClone': False,
                             'fullInstalledFontMapping':True,'addNotice':False,
                             'background': 'text-removed-original-graphics',
                             'embeddedSubsetFonts': any(f.get('backend')=='embedded' for f in fonts),
                             'semanticServiceRows': 'source-derived standard fields plus measured raw cells',
                             'canonicalPPT': 'serviceData'},
            'warnings': ['원본과 비슷한 기본 글꼴을 사용합니다. 원본과 완전히 동일한 모양은 아닙니다.',
                         '보관된 2026년 11월 8일 주보로 시작합니다. 이번 주 말씀·소식·봉사 담당을 확인해주세요.']}


def native_template_document():
    """Return a fresh JSON-serializable seed. Original assets are read-only."""
    return copy.deepcopy(_seed())


def font_bytes(fontId):
    entries = _seed()['fontRegistry']
    if fontId not in entries:
        raise ValueError('Unknown native font ID.')
    entry = entries[fontId]
    provider=_provider(entry)
    if provider:return provider.font_bytes(entry)
    path, index = _resolve(entry)
    # Preview/print-only embedding does not authorize editable browser embedding.
    if entry.get('fsType', 0) & 4 and not entry.get('fsType', 0) & 8:
        raise ValueError('Font permits preview/print embedding only, not editable web embedding.')
    raw = path.read_bytes()
    if raw[:4] != b'ttcf':
        return raw
    # A browser font resource must select the same TTC face as ReportLab.
    # Repackage the complete face, without glyph subsetting or cmap modification.
    offset = struct.unpack_from('>I', raw, 12 + 4 * index)[0]
    count = struct.unpack_from('>H', raw, offset+4)[0]
    header = raw[offset:offset+12]
    directory = bytearray(); body = bytearray(); position = 12 + count*16
    head_offset = None
    for i in range(count):
        tag, checksum, start, length = struct.unpack_from('>4sIII', raw, offset+12+i*16)
        table = bytearray(raw[start:start+length])
        if tag == b'head':
            table[8:12] = b'\0'*4
            head_offset = position
        directory.extend(struct.pack('>4sIII',tag,checksum,position,length))
        body.extend(table); body.extend(b'\0' * (-length % 4)); position += (length+3)//4*4
    result = bytearray(header) + directory + body
    if head_offset is not None:
        checksum = sum(struct.unpack('>'+'I'*(len(result)//4),result)) & 0xffffffff
        struct.pack_into('>I',result,head_offset+8,(0xB1B0AFBA-checksum)&0xffffffff)
    return bytes(result)


def _style_chars(block):
    return [(ch, {k: copy.deepcopy(v) for k, v in run.items() if k not in ('text', 'glyphs')})
            for run in (block['runs'] or block.get('styleSeed',[])) for ch in run['text']]


def _replace(block, text, registry):
    if not isinstance(text, str) or len(text) > 30000:
        raise ValueError('Text must be a string of at most 30000 characters.')
    if not block.get('layoutSpec'):block['layoutSpec']=_layout_spec(block)
    if text == block['text']:
        return
    if not block.get('styleSeed'):
        block['styleSeed']=[{k:copy.deepcopy(v) for k,v in r.items() if k!='glyphs'} for r in block['runs']]
    styles = _style_chars(block)
    if not styles:
        raise ValueError('Block has no source style.')
    runs = []
    source_text = ''.join(ch for ch, _ in styles)
    assigned = []
    for op, a, b, c, d in difflib.SequenceMatcher(None, source_text, text, autojunk=False).get_opcodes():
        if op == 'equal':
            assigned.extend(styles[i][1] for i in range(a,b))
        elif op != 'delete':
            assigned.extend(styles[min(a+i, max(a,b-1),len(styles)-1)][1] for i in range(d-c))
    for i, ch in enumerate(text):
        style = assigned[i]
        if not runs or any(runs[-1].get(k) != v for k, v in style.items()):
            runs.append(dict(style, text='', glyphs=[]))
        runs[-1]['text'] += ch
    runs=_map_runs(runs,registry)
    # Validate complete mapped-face coverage before committing, including edits.
    for run in runs:
        _check_run(run,registry)
    block['text'] = text; block['runs'] = runs; block['positioned'] = False
    _layout(block, registry)  # Throws on overflow; caller mutates only a copy.


def _layout_spec(block):
    """Measured line origins and alignment; no generic left alignment."""
    glyphs=[g for r in block.get('runs',[]) for g in r.get('glyphs',[]) if not g['text'].isspace()]
    lines=[]
    for g in sorted(glyphs,key=lambda g:(g['baseline'],g['x'])):
        line=next((l for l in lines if abs(l['baseline']-g['baseline'])<2),None)
        if line is None:line={'baseline':g['baseline'],'glyphs':[]};lines.append(line)
        line['glyphs'].append(g)
    if not lines:return {'align':block.get('align','left'),'indent':0,'baselines':[]}
    lefts=[min(g['x'] for g in l['glyphs']) for l in lines];rights=[max(g['x']+g.get('w',0) for g in l['glyphs']) for l in lines]
    lg=min(lefts)-block['x'];rg=block['x']+block['w']-max(rights)
    if len(lines)>1 and max(lefts)-min(lefts)<2:align='left';indent=max(0,min(lefts)-block['x'])
    elif abs(lg-rg)<min(15,block['w']*.08):align='center';indent=0
    elif rg<min(25,lg*.35):align='right';indent=max(0,rg)
    else:align='left';indent=max(0,lg)
    return {'align':align,'indent':indent,'baselines':[l['baseline']-block['y'] for l in lines]}

def _layout(block, registry):
    """Source alignment, calibrated advances and paragraph-aware line breaks."""
    preferred = block['fit']['preferredSize']; minimum = block['fit']['minimumSize']
    spec=block.get('layoutSpec') or _layout_spec(block);block['layoutSpec']=spec
    # Large silent font reductions are not an acceptable overflow solution.
    minimum=max(minimum,preferred*.95)
    chars = [(ch, run) for run in block['runs'] for ch in run['text']]
    if block.get('group')=='service':
        # Keep the sample's explicit two-line cells; modest tracking adjustment
        # precedes adding a third line or reducing the font height.
        adjusted=[];paragraph=[]
        def fit_paragraph(items):
            extent=sum(_width(registry[r['fontId']],c,r['fontSize'])*r.get('widthScale',1) for c,r in items)
            ratio=(block['w']-spec.get('indent',0))/extent if extent else 1
            return [(c,dict(r,widthScale=r.get('widthScale',1)*ratio)) for c,r in items] if .8<=ratio<1 else items
        for ch,run in chars:
            if ch=='\n':adjusted.extend(fit_paragraph(paragraph));adjusted.append((ch,run));paragraph=[]
            else:paragraph.append((ch,run))
        adjusted.extend(fit_paragraph(paragraph));chars=adjusted
    sizes = [preferred - .5*i for i in range(int((preferred-minimum)*2)+1)]
    if not sizes or sizes[-1] > minimum:
        sizes.append(minimum)
    for target in sizes:
        output=[];lines=[];line=[];bad=False;available=block['w']-spec.get('indent',0)
        def width(items):return sum(_width(registry[r['fontId']],c,r['fontSize']*target/preferred)*r.get('widthScale',1) for c,r in items)
        for ch, run in chars:
            if ch == '\n':
                lines.append(line);line=[];continue
            if width([(ch,run)])>available+.001:bad=True;break
            if line and width(line+[(ch,run)])>available+.001:
                spaces=[i for i,(c,_) in enumerate(line) if c.isspace()]
                if spaces and re.search('[가-힣A-Za-z]',''.join(c for c,_ in line)):
                    at=spaces[-1];lines.append(line[:at]);line=line[at+1:]
                elif ch in '、。，．！？）」』】)]' and len(line)>1:
                    last=line.pop();lines.append(line);line=[last]
                else:lines.append(line);line=[]
                if ch.isspace() and not line:continue
            line.append((ch,run))
        if bad:continue
        lines.append(line);baselines=spec.get('baselines',[]);leading=(sum(b-a for a,b in zip(baselines,baselines[1:]))/(len(baselines)-1) if len(baselines)>1 else preferred*block.get('lineHeight',1.2))*target/preferred
        offsets=[v*target/preferred for v in baselines] if len(lines)==len(baselines) else []
        if not offsets:
            total=target+(len(lines)-1)*leading;first=max(target*.85,(block['h']-total)/2+target*.85);offsets=[first+i*leading for i in range(len(lines))]
        if not offsets or offsets[-1]>block['h']+.001:continue
        for items,offset in zip(lines,offsets):
            extent=width(items);align=spec.get('align','left');indent=spec.get('indent',0)
            x=block['x']+(block['w']-extent)/2 if align=='center' else block['x']+block['w']-indent-extent if align=='right' else block['x']+indent
            if extent>available+.001:bad=True;break
            for ch,run in items:
                size=run['fontSize']*target/preferred;output.append((ch,x,block['y']+offset,size,run));x+=_width(registry[run['fontId']],ch,size)*run.get('widthScale',1)
        if not bad:block['fitSize']=target;return output
    raise ValueError(f"글이 영역을 넘습니다. 내용을 줄이거나 영역을 넓혀주세요. (최소 {minimum:g}pt)")


def edit_block(doc, id, text):
    result = copy.deepcopy(doc)
    found = [b for p in result['pages'] for b in p['blocks'] if b['id'] == id]
    if len(found) != 1:
        raise ValueError('Unknown or duplicate block ID.')
    b = found[0]
    if b.get('format'):
        if b['format']=='japanese-date':
            match=re.search(r'(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?',text)
            if not match:raise ValueError('날짜는 2026-11-15 또는 2026年11月15日 형식으로 입력해주세요.')
            try:value=datetime.date(*map(int,match.groups())).isoformat()
            except ValueError as exc:raise ValueError('달력에 있는 올바른 날짜를 입력해주세요.') from exc
        else:
            match=re.fullmatch(r'\s*(?:週報\s*第\s*)?(\d{1,6})\s*号?\s*',text)
            if not match:raise ValueError('주보 호수는 1936 또는 週報 第1936号 형식으로 입력해주세요.')
            value=match[1]
        return update_document(doc,{b['binding']:value})
    _replace(b, text, {f['id']: f for f in result['fonts']})
    _set(result['serviceData'], b['binding'], text)
    _canonical_fields(result['serviceData'],result['pages'])
    result['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in result['pages']
                             for b in p['blocks'] for a in b['fieldBindings']]
    return result


def update_document(doc, changes):
    """Atomic flat-dotpath -> string changes. Unbound fields are rejected."""
    if not isinstance(changes, dict) or not all(isinstance(v, str) for v in changes.values()):
        raise ValueError('Changes must be flat dotpath -> string.')
    result = copy.deepcopy(doc); registry = {f['id']: f for f in result['fonts']}
    for field, value in changes.items():
        try:
            if _get(result['serviceData'], field) == value:
                continue
        except (KeyError, IndexError, TypeError):
            pass
        found = [b for p in result['pages'] for b in p['blocks']
                 if any(x['field'] == field for x in b.get('fieldBindings',[]))]
        if not found:
            raise ValueError('No visible native field binding: ' + field)
        display = value
        for b in found:
            descriptor = next(x for x in b['fieldBindings'] if x['field'] == field)
            if 'segments' in descriptor:
                segments=descriptor['segments'];positions=[];cursor=0
                for segment in segments:
                    at=b['text'].find(segment,cursor)
                    if at<0:raise ValueError('Stale segmented binding: '+field)
                    positions.append((at,at+len(segment)));cursor=at+len(segment)
                display=b['text']
                inserted=value
                if value and segments and segments[0].endswith('\n') and not value.endswith('\n'):
                    inserted+='\n'
                if positions:
                    for i,(start,end) in reversed(list(enumerate(positions))):
                        display=display[:start]+(inserted if i==0 else '')+display[end:]
                elif value:
                    display=display+('\n' if display and not display.endswith('\n') else '')+value
                _replace(b,display,registry);descriptor['segments']=[inserted] if value else []
                _set(result['serviceData'],b['binding'],display)
                continue
            if 'substring' in descriptor:
                old = descriptor['substring']
                if b['text'].count(old) != 1:
                    raise ValueError('Stale/ambiguous substring binding: ' + field)
                replacement=value
                if descriptor.get('dateDayOnly'):
                    match=re.fullmatch(r'(\d{1,2})/(\d{1,2})',value)
                    if not match:raise ValueError('Roster date must be M/D.')
                    if match[1]!='11':raise ValueError('Merged month changes need an explicit month-cell edit.')
                    replacement=str(int(match[2]))
                display = b['text'].replace(old, replacement, 1)
                _replace(b, display, registry)
                descriptor['substring'] = replacement
                _set(result['serviceData'], b['binding'], display)
                continue
            if b.get('format') == 'japanese-date':
                date = datetime.date.fromisoformat(value)
                display = f'今 日:{date.year}年{date.month}月{date.day}日'
            elif b.get('format') == 'issue':
                if not value.isdigit() or not 1 <= int(value) <= 999999:
                    raise ValueError('Issue must be a positive 1..6 digit integer.')
                display = '週報 第' + value + '号'
            _replace(b, display, registry)
            if b.get('format'):
                b['value'] = value
        _set(result['serviceData'], field, value)
    # Re-derive compound service/notice/scripture semantics from the edited source.
    # Tiny synthetic documents are useful for testing the font hook separately.
    if result.get('engine')=='native-v2':
        _canonical_fields(result['serviceData'],result['pages'])
        result['fieldBindings']=[dict(a,blockId=b['id'],group=b['group']) for p in result['pages']
                                 for b in p['blocks'] for a in b['fieldBindings']]
    return result


def native_text_layer(block, registry):
    """ReportLab hook for mapped full faces, runs and positioned glyphs."""
    if isinstance(registry, list):
        registry = {f['id']: f for f in registry}
    mem = io.BytesIO(); c = canvas.Canvas(mem, pagesize=(W, H))
    def draw(text,x,y,size,run,advance):
        font=registry[run['fontId']];natural=_width(font,text,size)
        c.saveState();c.translate(x,H-y)
        if natural and advance:c.scale(advance/natural,1)
        _draw(c,font,text,0,0,size,run['color']);c.restoreState()
    for run in block['runs']:
        _check_run(run,registry,block.get('positioned',False))
    if block.get('positioned'):
        for run in block['runs']:
            for glyph in run['glyphs']:
                if glyph['text'].isspace():continue
                draw(glyph['text'],glyph['x'],glyph['baseline'],run['fontSize'],run,glyph.get('w'))
    else:
        for ch, x, y, size, run in _layout(block, registry):
            draw(ch,x,y,size,run,_width(registry[run['fontId']],ch,size)*run.get('widthScale',1))
    c.save()
    return PdfReader(io.BytesIO(mem.getvalue())).pages[0]


def validate_document(doc):
    if doc.get('engine') != 'native-v2' or doc.get('schemaVersion') != 1 or len(doc.get('pages', [])) != 4:
        raise ValueError('Expected native-v2 four-page document.')
    ids = set(); registry = {f['id']: f for f in doc['fonts']}
    for p in doc['pages']:
        for b in p['blocks']:
            if b['id'] in ids or b['type'] != 'text':
                raise ValueError('Invalid/duplicate native text block.')
            ids.add(b['id'])
            if ''.join(r['text'] for r in b['runs']) != b['text']:
                raise ValueError('Text/runs disagree: ' + b['id'])
            if any(r['fontId'] not in registry for r in b['runs']):
                raise ValueError('Unknown font.')
            if not all(isinstance(b[k], (int, float)) for k in ('x', 'y', 'w', 'h')) or b['w'] <= 0 or b['h'] <= 0:
                raise ValueError('Invalid native geometry.')
    return True


def render_document(doc, mode='booklet'):
    validate_document(doc)
    registry = {f['id']: f for f in doc['fonts']}
    missing = sorted({registry[r['fontId']]['originalName'] for p in doc['pages']
                      for b in p['blocks'] for r in b['runs'] if not registry[r['fontId']]['available']})
    if missing:
        raise ValueError('Original fonts unavailable (no fallback): ' + ', '.join(missing))
    writer = PdfWriter()
    for i, page in enumerate(doc['pages']):
        mem = io.BytesIO(); c = canvas.Canvas(mem, pagesize=(W, H))
        background=base64.b64decode(page['backgroundData'].split(',',1)[1]) if page.get('backgroundData') else background_bytes(i)
        c.drawImage(ImageReader(io.BytesIO(background)), 0, 0, W, H); c.save()
        out = writer.add_page(PdfReader(io.BytesIO(mem.getvalue())).pages[0])
        for block in page['blocks']:
            out.merge_page(native_text_layer(block, registry))
    if mode in ('logical', 'original'):
        result = writer
    elif mode == 'booklet':
        result = PdfWriter(); aw, ah = 841.8897637795277, 595.2755905511812
        scale = min(aw / 2 / W, ah / H)
        for pair in ((3, 0), (1, 2)):
            out = result.add_blank_page(width=aw, height=ah)
            for col, index in enumerate(pair):
                out.merge_transformed_page(writer.pages[index], Transformation().scale(scale).translate(
                    col*aw/2+(aw/2-W*scale)/2, (ah-H*scale)/2))
    else:
        raise ValueError('Mode must be logical or booklet.')
    mem = io.BytesIO(); result.write(mem)
    return mem.getvalue()
