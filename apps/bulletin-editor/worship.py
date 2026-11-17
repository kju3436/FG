"""Local worship PPTX backend; Python standard library, no external uploads.

plan_worship(document: dict) -> dict returns ready, Korean errors/warnings,
summary, categorized issues, library metadata, hymn candidates and section plan.
build_worship(document: dict) -> bytes raises WorshipError on unsafe/incomplete
input or failed desktop PowerPoint checks. Ordinary callers supply serviceData;
section ranges and source hashes are discovered internally from the actual latest
approved deck and approved template shape names. No manual manifest is required.

An optional advanced document['worship'] manifest supports explicit keep/reuse/
template/edit/omit ranges, bindings and baseline/serviceData hashes. It is not
required by the editor. Automatic mode rejects unknown layouts and unsupported
special orders rather than keeping last week's content. Long bilingual scripture
requires matching numbered verse lines; oversized individual verses require
human semantic splitting. No text is translated, invented or silently truncated.

Hymn slide XML and dependencies are copied from index-selected approved assets.
Template text nodes alone are patched; differing template page sizes are inserted
using PowerPoint InsertFromFile, as in retained .build assembly scripts. Source
libraries are never saved. PowerPoint checks changed text, bounds and Korean
scripture minimum size, then renders <=12 samples to .build/worship-qa. Rendering
is not human visual approval. Output is a working deck, not an approved library
entry. --self-test performs package checks; --fixture PATH writes validation-only
JSON; --build-fixture PATH builds a validation-only deck using desktop PowerPoint.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tempfile
import unicodedata
import zipfile
from io import BytesIO
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'


class WorshipError(ValueError):
    """Missing, stale, unsupported or unsafe production input."""


def _require(ok, message):
    if not ok:
        raise WorshipError(message)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _song_title(value):
    """Normalize typography only; never infer translations or similar songs."""
    text = unicodedata.normalize('NFKC', str(value or '')).strip()
    wrappers = {'「': '」', '『': '』', '“': '”', '"': '"', "'": "'"}
    while len(text) >= 2 and wrappers.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return re.sub(r'\s+', '', text).rstrip('。.').casefold()


def _gospel_candidates(index, *titles):
    """Use only indexed titles/first lines or explicitly curated title aliases.

    Bilingual inputs must agree when both identify indexed assets. Unindexed
    Japanese text cannot itself prove an equivalence to a Korean song title.
    """
    matches = []
    for title in titles:
        key = _song_title(title)
        if not key:
            continue
        found = []
        for entry in index.get('gospelSongs', []):
            aliases = entry.get('aliases', [])
            if not isinstance(aliases, list):
                aliases = []
            names = [entry.get('title'), entry.get('firstLine'),
                     *(x for x in aliases if isinstance(x, str))]
            if any(key == _song_title(n) for n in names if n):
                found.append(entry)
        if found:
            matches.append(found)
    if not matches:
        return []
    files = set.intersection(*({e['file'] for e in group} for group in matches))
    _require(files, '한일 곡명이 서로 다른 승인 곡을 가리킵니다. 같은 곡을 선택해주세요.')
    candidates = [entry for entry in matches[0] if entry['file'] in files]
    identities = {_song_title(e.get('title') or e.get('firstLine')) for e in candidates}
    _require(len(identities) == 1, '곡명 별칭이 여러 승인 곡과 겹칩니다. 정확한 곡명을 선택해주세요.')
    return candidates


def _path(relative):
    _require(isinstance(relative, str) and relative and ':' not in relative,
             'Expected a project-relative asset path.')
    path = (ROOT / relative).resolve()
    _require(path.is_relative_to(ROOT.resolve()) and path.is_file(),
             f'Missing or unsafe project asset: {relative}')
    return path


def _json(relative):
    return json.loads(_path(relative).read_text(encoding='utf-8-sig'))


def _xml(data):
    _require(b'<!DOCTYPE' not in data and b'<!ENTITY' not in data,
             'DTD/entity declarations are not supported.')
    return ET.fromstring(data)


def _serialize(node):
    return ET.tostring(node, encoding='utf-8', xml_declaration=True)


def _rels(part):
    p = PurePosixPath(part)
    return str(p.parent / '_rels' / (p.name + '.rels'))


def _target(part, target):
    result = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
    if target.startswith('/'):
        result = target.lstrip('/')
    _require(not result.startswith('../') and ':' not in result,
             'Unsafe package relationship.')
    return result


class _Deck:
    def __init__(self, path):
        self.path = path
        self.raw = path.read_bytes()
        with zipfile.ZipFile(BytesIO(self.raw)) as z:
            _require(z.testzip() is None, f'Corrupt PPTX: {path.name}')
            _require(len(z.namelist()) == len(set(z.namelist())), 'Duplicate ZIP parts.')
            self.parts = {n: z.read(n) for n in z.namelist()}
        self.presentation = _xml(self.parts['ppt/presentation.xml'])
        rels = _xml(self.parts['ppt/_rels/presentation.xml.rels'])
        mapping = {e.get('Id'): _target('ppt/presentation.xml', e.get('Target'))
                   for e in rels if e.get('TargetMode') != 'External'}
        self.slides = [mapping[s.get(f'{{{R}}}id')]
                       for s in self.presentation.findall(f'{{{P}}}sldIdLst/{{{P}}}sldId')]
        size = self.presentation.find(f'{{{P}}}sldSz')
        self.size = (size.get('cx'), size.get('cy'))


def _field(data, name):
    _require(isinstance(name, str), 'Binding field must be a dotted serviceData path.')
    value = data
    try:
        for key in name.split('.'):
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WorshipError(f'Missing serviceData.{name}') from exc
    _require(isinstance(value, str) and value.strip() and len(value) <= 30000,
             f'Expected nonempty text at serviceData.{name}')
    return value


def _bound_slide(deck, number, bindings, data, coverage):
    _require(type(number) is int and 1 <= number <= len(deck.slides), 'Invalid source slide.')
    node = _xml(deck.parts[deck.slides[number - 1]])
    seen = set()
    checks = []
    for binding in bindings:
        sid = binding['shapeId']
        _require(type(sid) is int and sid not in seen, 'Duplicate/invalid shapeId.')
        seen.add(sid)
        shapes = [s for s in node.iter(f'{{{P}}}sp')
                  if s.find(f'{{{P}}}nvSpPr/{{{P}}}cNvPr') is not None
                  and s.find(f'{{{P}}}nvSpPr/{{{P}}}cNvPr').get('id') == str(sid)]
        _require(len(shapes) == 1, f'Shape {sid} not found on slide {number}.')
        field = binding['field']
        original = _field(data, field)
        start, end = binding.get('start', 0), binding.get('end', len(original))
        _require(type(start) is int and type(end) is int and 0 <= start < end <= len(original),
                 f'Invalid text slice for {field}.')
        value = original[start:end]
        _require(not any(ord(c) < 32 and c not in '\n\r\t' for c in value), 'Invalid XML text.')
        runs = list(shapes[0].iter(f'{{{A}}}r'))
        texts = list(shapes[0].iter(f'{{{A}}}t'))
        _require(texts and len(runs) == len(texts), 'Unsupported field/paragraph structure.')
        formats = {ET.tostring(r.find(f'{{{A}}}rPr')) if r.find(f'{{{A}}}rPr') is not None else b'' for r in runs}
        text_index = binding.get('textIndex')
        _require(text_index is not None or len(formats) == 1 or binding.get('preserveFirstRun') is True,
                 f'Mixed run formatting in shape {sid}; choose a uniform template box.')
        # Preserve all non-text XML, including paragraph/run properties.
        if text_index is not None:
            _require(type(text_index) is int and 0 <= text_index < len(texts), 'Invalid textIndex.')
            texts[text_index].text = value
        else:
            texts[0].text = value
            for text in texts[1:]:
                text.text = ''
        coverage.setdefault(field, []).append((start, end))
        checks.append({'shapeId': sid, 'text': ''.join(t.text or '' for t in texts),
                       'korean': field.endswith('.ko') and field.startswith(('scripture.', '_worship_absolution_', '_worship_sermon_'))})
    # Replace only text node contents in the original XML. This also preserves
    # namespace declarations used in mc:Ignorable and all paragraph formatting.
    from xml.sax.saxutils import escape
    values = iter(t.text or '' for t in node.iter(f'{{{A}}}t'))
    raw = deck.parts[deck.slides[number - 1]].decode('utf-8')
    pattern = r'(<(?:[\w]+:)?t(?:\s[^>]*)?>)(.*?)(</(?:[\w]+:)?t>)'
    raw = re.sub(pattern, lambda m: m[1] + escape(next(values)) + m[3], raw, flags=re.S)
    return raw.encode('utf-8'), checks


def _shapes(deck, number):
    result = {}
    for shape in _xml(deck.parts[deck.slides[number - 1]]).iter(f'{{{P}}}sp'):
        meta = shape.find(f'{{{P}}}nvSpPr/{{{P}}}cNvPr')
        if meta is not None:
            result[meta.get('name')] = (int(meta.get('id')), [t.text or '' for t in shape.iter(f'{{{A}}}t')])
    return result


def _auto_prepare(document):
    """Discover standard boundaries in the actual latest deck, never stale positions."""
    data = copy.deepcopy(document.get('serviceData'))
    _require(isinstance(data, dict), '주보의 예배 정보를 먼저 입력해주세요.')
    try:
        datetime.date.fromisoformat(data.get('date', ''))
    except (ValueError, TypeError) as exc:
        raise WorshipError('예배 날짜를 입력해주세요. 예: 2026-11-22') from exc
    for kind in ('absolution', 'sermon'):
        for field in ('ja', 'ko', 'refJa', 'refKo'):
            _field(data, f'scripture.{kind}.{field}')
    for field in ('serviceTitleJa', 'serviceTitleKo'):
        _field(data, field)
    config = _json('system/system-config.json')
    index = _json(config['paths']['libraryIndex'])
    # Read retained reference metadata for diagnostics; actual XML wins if stale.
    reference = _json(config['paths']['referenceDeckIndex'])
    baseline = _Deck(_path(index['latestFinalDeck']))
    cache = {str(baseline.path): baseline}
    rows = data.get('service')
    _require(isinstance(rows, list) and all(isinstance(r, dict) for r in rows), '예배 순서를 입력해주세요.')
    norm = lambda s: re.sub(r'\s+', '', s)
    known = {'모임찬송', '예배로의부르심', '죄의공적고백', '죄의공적인고백', '사죄확인', '찬송',
             '신앙고백', '감사찬송', '목회기도', '성경봉독', '찬양', '말씀선포', '봉헌기도',
             '봉헌', '성도의교제', '축복기도', '주기도', '강복선언'}
    _require(all(norm(str(r.get('ko', ''))) in known for r in rows),
             '표준 이외의 특별순서가 있습니다. 제작 담당자가 해당 순서를 확인해야 합니다.')
    from scripture import parse_reference
    for kind in ('absolution', 'sermon'):
        refs = [parse_reference(data['scripture'][kind][f]) for f in ('refJa', 'refKo')]
        _require(all(refs[0].get(k) == refs[1].get(k) for k in ('book', 'chapter', 'verses')),
                 '말씀의 한일 성경 범위가 다릅니다. 같은 책·장·절을 입력해주세요.')
    text = [''.join(t.text or '' for t in _xml(baseline.parts[n]).iter(f'{{{A}}}t')) for n in baseline.slides]

    def locate(label):
        matches = [i + 1 for i, s in enumerate(text) if norm(label) in norm(s)]
        _require(matches, f'최신 기준 PPT에서 {label} 구간을 찾지 못했습니다. 제작 담당자 확인이 필요합니다.')
        return matches[0]

    boundaries = {label: locate(label) for label in
                  ('예배로의 부르심', '공적인고백', '사죄확인', '찬송', '사도신경',
                   '감사찬송', '목회기도', '성경봉독', '봉헌을위한기도', '봉헌',
                   '교회소식', '축복기도', '주기도', '강복선언')}
    # '찬송' and '봉헌' substrings occur elsewhere; demand standalone labels.
    for label in ('찬송', '봉헌'):
        matches = [i + 1 for i, n in enumerate(baseline.slides)
                   if any(norm(t.text or '') == label for t in _xml(baseline.parts[n]).iter(f'{{{A}}}t'))]
        _require(len(matches) == 1, f'{label} 표지 구간이 불명확합니다. 제작 담당자가 기준 PPT를 확인해주세요.')
        boundaries[label] = matches[0]
    _require(list(boundaries.values()) == sorted(boundaries.values()), '기준 PPT 예배 순서가 표준과 다릅니다. 제작 담당자 확인이 필요합니다.')
    sequence, sections, warnings = [], [], []
    if reference.get('slideCount') != len(baseline.slides):
        warnings.append('기준 색인의 장수와 실제 PPT가 달라 실제 파일에서 예배 구간을 확인했습니다.')

    def emit(source, number, bindings=None):
        patch, checks = _bound_slide(source, number, bindings or [], data, {})
        # PowerPoint InsertFromFile scales a template to the target page size.
        # Retain source XML here; build_worship delegates differing-size template
        # insertion to that proven COM technique instead of altering geometry.
        _require(source.size == baseline.size or source.path.parent ==
                 (ROOT / config['paths']['templates']).resolve(),
                 '찬송가 원본의 슬라이드 크기가 기준 PPT와 다릅니다. 원본을 바꾸지 않고 합칠 수 없습니다.')
        sequence.append((source, number, patch if bindings else None, checks))

    def keep(first, last):
        for n in range(first, last + 1):
            emit(baseline, n)

    def row(label, required=True):
        found = [r for r in rows if norm(str(r.get('ko', ''))) == norm(label)]
        _require(len(found) <= 1 and (found or not required), f'예배 순서에서 {label} 항목을 확인해주세요.')
        return found[0] if found else {}

    def asset(relative):
        path = _path(relative)
        if str(path) not in cache:
            cache[str(path)] = _Deck(path)
        return cache[str(path)]

    def song(label, optional=False):
        r = row(label, not optional)
        detail = str(r.get('detailKo') or '').strip()
        ja = str(r.get('detailJa') or '').strip()
        if not detail and not ja and optional:
            warnings.append(f'{label}: 이번 주 곡이 없어 지난주 자막을 제외했습니다.')
            return
        _require(detail or ja, f'{label} 곡명 또는 찬송가 번호를 입력해주세요.')
        numbers = re.findall(r'(?:찬송가|韓日|讃美歌)\s*(\d{1,3})', detail + ' ' + ja)
        if numbers:
            _require(len(set(int(n) for n in numbers)) == 1, f'{label}의 한일 찬송가 번호가 다릅니다.')
            candidates = [x for x in index.get('hymns', []) if int(x['number']) == int(numbers[0])]
        else:
            candidates = _gospel_candidates(index, detail, ja)
        _require(candidates, f'{label}: 승인 라이브러리에 「{detail or ja}」가 없습니다. 승인된 곡을 선택해주세요.')
        candidates.sort(key=lambda x: (x.get('preferred') is True, '(수정)' in x['file'], x.get('modifiedUtc', ''), x['file']), reverse=True)
        source = asset(candidates[0]['file'])
        for n in range(1, len(source.slides) + 1):
            emit(source, n)
        sections.append({'name': label, 'action': 'reuse', 'source': candidates[0]['file'], 'slideCount': len(source.slides)})

    def template(kind):
        filename = '사죄확인_템플릿.pptx' if kind == 'absolution' else '설교본문_템플릿.pptx'
        source = asset(config['paths']['templates'] + '/' + filename)
        s = data['scripture'][kind]
        cover = _shapes(source, 1)
        emit(source, 1, [{'shapeId': cover['cover-japanese'][0], 'field': f'scripture.{kind}.refJa'},
                         {'shapeId': cover['cover-korean'][0], 'field': f'scripture.{kind}.refKo'}])
        def verses(value):
            lines = value.strip().splitlines()
            parsed = [re.match(r'^\s*(\d+)\s*[.．:：]?\s+(.+)$', line) for line in lines if line.strip()]
            if len(parsed) > 1 and all(parsed):
                return [(m[1], m[2]) for m in parsed]
            return [('', value)]
        jp, ko = verses(s['ja']), verses(s['ko'])
        # Reuse already approved bilingual semantic splits when the supplied
        # verse exactly matches a retained template verse. Never invent a split.
        sample_parts = []
        reuse_sample_layouts = False
        for n in range(2, len(source.slides) + 1):
            sample = _shapes(source, n)
            reference_text = ''.join(sample['reference'][1])
            if not sample_parts:
                sample_ref = re.sub(r'[abc]$', '', reference_text)
            if re.sub(r'[abc]$', '', reference_text) != sample_ref:
                break
            sample_parts.append((''.join(sample['japanese-text'][1]), ''.join(sample['korean-text'][1])))
        if (len(jp) == 1 and len(sample_parts) > 1 and
                ''.join(x[0] for x in sample_parts) == s['ja'] and
                ''.join(x[1] for x in sample_parts) == s['ko']):
            jp = [('', x[0]) for x in sample_parts]
            ko = [('', x[1]) for x in sample_parts]
            reuse_sample_layouts = True
        _require([n for n, _ in jp] == [n for n, _ in ko], '말씀의 한일 절 번호가 서로 다릅니다. 같은 절끼리 입력해주세요.')
        _require(len(jp) > 1 or (len(s['ja']) <= 150 and len(s['ko']) <= 110),
                 '긴 말씀은 각 절을 줄바꿈하고 줄 앞에 절 번호를 붙여주세요. 한 절이 길면 담당자의 의미 단위 분할이 필요합니다.')
        # Select an already approved compact body layout instead of changing
        # fonts or geometry. PowerPoint scales imported templates to the
        # baseline canvas, so evaluate the 40pt Korean floor after that scaling.
        compact_candidates=[]
        scale=int(baseline.size[0])/int(source.size[0])
        for number in range(2,len(source.slides)+1):
            sizes={}
            for shape in _xml(source.parts[source.slides[number-1]]).iter(f'{{{P}}}sp'):
                meta=shape.find(f'{{{P}}}nvSpPr/{{{P}}}cNvPr')
                if meta is not None and meta.get('name') in ('japanese-text','korean-text'):
                    vals=[int(r.get('sz'))/100 for r in shape.iter(f'{{{A}}}rPr') if r.get('sz')]
                    if vals:sizes[meta.get('name')]=max(vals)
            if len(sizes)==2 and sizes['korean-text']*scale>=39.999:
                compact_candidates.append((max(sizes.values()),number))
        compact_number=min(compact_candidates)[1] if compact_candidates else 2
        for i, ((verse, japanese), (_, korean)) in enumerate(zip(jp, ko)):
            template_number = i + 2 if reuse_sample_layouts else 2
            if len(re.sub(r'\s','',korean))>18 or len(japanese)>30:template_number=compact_number
            shapes = _shapes(source, template_number)
            _require(len(japanese) <= 150 and len(korean) <= 110, f'{verse}절이 한 화면에 길어 의미 단위 분할이 필요합니다.')
            key = f'_worship_{kind}_{i}'
            refja, refko = s['refJa'], s['refKo']
            if verse:
                refja = re.sub(r'(:|：)\s*\d+.*$', r'\g<1>' + verse, refja)
                refko = re.sub(r'(:|：)\s*\d+.*$', r'\g<1>' + verse, refko)
            elif len(jp) > 1:
                refja += chr(ord('a') + i)
                refko += chr(ord('a') + i)
            # Compact only reference whitespace, retaining both full book names,
            # approved font size and the existing header box geometry.
            data[key] = {'ref': norm(refja) + '/' + norm(refko), 'ja': japanese, 'ko': korean}
            emit(source, template_number, [{'shapeId': shapes[name][0], 'field': key + '.' + field}
                             for name, field in [('reference', 'ref'), ('japanese-text', 'ja'), ('korean-text', 'ko')]])
        sections.append({'name': '사죄확인' if kind == 'absolution' else '설교본문', 'action': 'template', 'slideCount': len(jp) + 1})

    b = boundaries
    call = row('예배로의 부르심')
    for language, expected_ref in [('Ko', {'시편124:8', '시124:8'}), ('Ja', {'詩篇124:8'})]:
        _require(norm(str(call.get('detail' + language) or '')) in expected_ref,
                 '예배로의 부르심 구절이 기준 시편 124:8과 다릅니다. 이 구간은 제작 담당자 확인이 필요합니다.')
    cover = _shapes(baseline, 1)
    dated = [(sid, ts) for sid, ts in cover.values() if any(re.search(r'\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}', t) for t in ts)]
    _require(len(dated) == 1 and len(dated[0][1]) == 2, '날짜·예배 제목 표지 구조를 확인할 수 없습니다. 제작 담당자 확인이 필요합니다.')
    # The approved cover contains title and date in distinct formatted runs.
    sid, ts = dated[0]
    date_i = next(i for i, t in enumerate(ts) if re.search(r'\d{4}[.\-/]', t))
    title_i = 1 - date_i
    data['_worshipCover'] = data['serviceTitleJa'] + ' / ' + data['serviceTitleKo']
    data['_worshipDate'] = data['date'].replace('-', '.')
    # Edit each text node separately; retain the two original run formats.
    patch, _ = _bound_slide(baseline, 1, [{'shapeId': sid, 'textIndex': title_i, 'field': '_worshipCover'}], data, {})
    temporary = copy.copy(baseline)
    temporary.parts = dict(baseline.parts)
    temporary.parts[baseline.slides[0]] = patch
    patch, checks = _bound_slide(temporary, 1, [{'shapeId': sid, 'textIndex': date_i, 'field': '_worshipDate'}], data, {})
    sequence.append((baseline, 1, patch, checks))
    song('모임찬송')
    keep(b['예배로의 부르심'], b['사죄확인'] - 1)
    template('absolution')
    keep(b['찬송'], b['찬송'])
    song('찬송')
    keep(b['사도신경'], b['감사찬송'])
    song('감사찬송')
    keep(b['목회기도'], b['성경봉독'] - 1)
    template('sermon')
    warnings.append('말씀 상단의 한일 성경 표기는 공백을 줄여 표시합니다. 원래 글꼴·상자에서 넘치면 제작을 중단합니다.')
    song('찬양', optional=True)
    warnings.append('지난주 찬양대 자막·제직임명 등 특별순서는 자동으로 재사용하지 않았습니다.')
    keep(b['봉헌을위한기도'], b['봉헌'])
    song('봉헌')
    keep(b['교회소식'], b['교회소식'])
    notices = data.get('notices', [])
    _require(isinstance(notices, list) and len(notices) <= 30, '교회소식은 최대 30개까지 입력해주세요.')
    if notices:
        source = asset(config['paths']['templates'] + '/お知らせ_교회소식_템플릿.pptx')
        shapes = _shapes(source, 2)
        for i, notice in enumerate(notices):
            _require(isinstance(notice, dict) and isinstance(notice.get('ja'), str) and
                     isinstance(notice.get('ko'), str) and notice['ja'].strip() and notice['ko'].strip(),
                     f'교회소식 {i+1}의 한국어와 일본어를 모두 입력해주세요.')
            key = f'_worship_notice_{i}'
            data[key] = {'titleJa': notice.get('titleJa') or f'お知らせ {i+1}',
                         'titleKo': notice.get('titleKo') or f'교회소식 {i+1}',
                         'ja': notice['ja'], 'ko': notice['ko']}
            emit(source, 2, [{'shapeId': shapes[name][0], 'field': key + '.' + field, 'preserveFirstRun': True}
                             for name, field in [('TextBox 7', 'titleJa'), ('TextBox 8', 'titleKo'),
                                                  ('TextBox 10', 'ja'), ('TextBox 11', 'ko')]])
    keep(b['축복기도'], len(baseline.slides))
    plan = {'baseline': index['latestFinalDeck'], 'serviceDate': data['date'], 'slideCount': len(sequence),
            'sections': sections, 'warnings': warnings, 'automatic': True,
            'sources': {d.path.relative_to(ROOT).as_posix(): _hash(d.raw) for d in cache.values()}}
    return plan, baseline, sequence


def _prepare(document):
    _require(isinstance(document, dict), 'document must be an object.')
    if not document.get('worship'):
        return _auto_prepare(document)
    data, spec = document.get('serviceData'), document.get('worship')
    _require(isinstance(data, dict) and isinstance(spec, dict),
             'serviceData and explicit worship range/binding manifest are required; no inferred slide ranges.')
    try:
        datetime.date.fromisoformat(data.get('date', ''))
    except (ValueError, TypeError) as exc:
        raise WorshipError('serviceData.date must be YYYY-MM-DD.') from exc
    _require(isinstance(data.get('service'), list) and data['service'], 'Complete service order is required.')
    _require(spec.get('serviceDataSha256') == _hash(_canonical(data)),
             'serviceData changed or is not confirmed: refresh serviceDataSha256 after reviewing the manifest.')
    config = _json('system/system-config.json')
    index = _json(config['paths']['libraryIndex'])
    baseline_path = _path(index['latestFinalDeck'])
    _require(baseline_path.parent == _path(config['paths']['finalLibrary'] + '/' + baseline_path.name).parent,
             'Latest baseline must be in the approved integrated library.')
    baseline = _Deck(baseline_path)
    _require(spec.get('baselineSha256') == _hash(baseline.raw), 'Baseline changed: review ranges against latestFinalDeck.')
    cache = {str(baseline_path): baseline}
    sequence, sections, coverage = [], [], {}
    expected = 1
    for section in spec.get('sections', []):
        bounds = section.get('range', [])
        _require(isinstance(bounds, list) and len(bounds) == 2 and
                 all(type(n) is int for n in bounds), 'Section range must be [first, last].')
        first, last = bounds
        _require(first == expected and first <= last <= len(baseline.slides),
                 'Ranges must cover every baseline slide exactly once, in order.')
        expected = last + 1
        action = section.get('action')
        source = baseline
        if action == 'reuse':
            hymn, gospel = section.get('hymn'), section.get('gospel')
            _require((hymn is not None) != (gospel is not None), 'reuse requires hymn OR gospel.')
            if hymn is not None:
                _require(str(hymn).isdigit(), 'Hymn must be a number.')
                candidates = [x for x in index.get('hymns', []) if str(x['number']).isdigit() and int(x['number']) == int(hymn)]
            else:
                candidates = _gospel_candidates(index, gospel)
            _require(candidates, f'No approved library match: {hymn or gospel}')
            candidates.sort(key=lambda x: (x.get('preferred') is True, '(수정)' in x['file'], x.get('modifiedUtc', ''), x['file']), reverse=True)
            path = _path(candidates[0]['file'])
            library = (ROOT / config['paths']['hymnLibrary' if hymn is not None else 'gospelLibrary']).resolve()
            _require(path.parent == library, 'Indexed song is outside its approved library.')
            source = cache.setdefault(str(path), _Deck(path))
            items = [{'slide': n} for n in range(1, len(source.slides) + 1)]
        elif action == 'template':
            kind = section.get('kind')
            _require(kind in ('absolution', 'sermon'), 'Unknown scripture template kind.')
            filename = '사죄확인_템플릿.pptx' if kind == 'absolution' else '설교본문_템플릿.pptx'
            path = _path(config['paths']['templates'] + '/' + filename)
            source = cache.setdefault(str(path), _Deck(path))
            items = section.get('slides', [])
            _require(items, 'Template requires explicit source slides and text bindings.')
        elif action == 'edit':
            items = section.get('slides', [])
            _require(items and [x['slide'] for x in items] == list(range(first, last + 1)),
                     'edit must preserve its baseline slide range.')
        elif action == 'keep':
            _require(section.get('unchangedConfirmed') is True,
                     'keep requires unchangedConfirmed=true after comparing this service with the baseline.')
            items = [{'slide': n} for n in range(first, last + 1)]
        elif action == 'omit':
            _require(section.get('reason'), 'omit requires an explicit reason.')
            items = []
        else:
            raise WorshipError(f'Unsupported action: {action}')
        _require(source.size == baseline.size, 'Source slide size differs from approved baseline.')
        for item in items:
            n = item['slide']
            bindings = item.get('bindings', [])
            _require(action in ('edit', 'template') or not bindings, 'Library and keep slides are immutable.')
            if action == 'template':
                _require(bindings and all(b['field'].startswith('scripture.' + kind + '.') for b in bindings),
                         'Every template slide must bind its own scripture text.')
            else:
                _require(not any(b['field'].startswith('scripture.') for b in bindings),
                         'Scripture text can only be bound in its approved template.')
            patched, checks = _bound_slide(source, n, bindings, data, coverage)
            sequence.append((source, n, patched if bindings else None, checks))
        sections.append({'action': action, 'range': bounds, 'source': source.path.relative_to(ROOT).as_posix(), 'slideCount': len(items)})
    _require(expected == len(baseline.slides) + 1 and sequence, 'Manifest does not cover the complete baseline.')
    for field in ['date'] + [f'scripture.{k}.{f}' for k in ('absolution', 'sermon') for f in ('ja', 'ko', 'refJa', 'refKo')]:
        text = _field(data, field)
        spans = sorted(coverage.get(field, []))
        cursor = 0
        for start, end in spans:
            _require(start == cursor, f'Overlapping or missing text in {field}.')
            cursor = end
        _require(cursor == len(text), f'Complete explicit template bindings required for {field}.')
    plan = {'baseline': index['latestFinalDeck'], 'serviceDate': data['date'],
            'slideCount': len(sequence), 'sections': sections,
            'sources': {d.path.relative_to(ROOT).as_posix(): _hash(d.raw) for d in cache.values()},
            'verification': 'PowerPoint read-only text/overflow checks and <=12 representative renders; human visual review still required'}
    return plan, baseline, sequence


def plan_worship(document: dict) -> dict:
    """Return ready/errors/warnings/summary and local index candidates, even for sparse input.

    ready means generation prerequisites pass, not that visual review has occurred.
    Planning never launches PowerPoint, alters files or contacts external services.
    """
    result = {'ready': False, 'errors': [], 'warnings': [
        'Working deck only: representative PowerPoint renders still need human visual review.',
        'Explicit range map and unchanged confirmations are required; last-week content is never inferred.'],
        'summary': '', 'library': {}, 'hymnCandidates': []}
    errors = result['errors']
    try:
        config = _json('system/system-config.json')
        index = _json(config['paths']['libraryIndex'])
        latest = index.get('latestFinalDeck')
        result['library'] = {'hymnFileCount': len(index.get('hymns', [])),
                             'uniqueHymnCount': len({str(x['number']) for x in index.get('hymns', [])}),
                             'gospelFileCount': len(index.get('gospelSongs', [])),
                             'latestFinalDeck': latest,
                             'latestSha256': _hash(_path(latest).read_bytes())}
        result['latestFinalDeck'] = latest
        result['gospelCandidates'] = index.get('gospelSongs', [])
        data = document.get('serviceData', {}) if isinstance(document, dict) else {}
        _require(isinstance(data, dict), 'serviceData must be an object.')
        rows = data.get('service', [])
        import re
        if isinstance(rows, list):
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    errors.append(f'serviceData.service.{i}: expected object.')
                    continue
                detail = str(row.get('detailKo') or '') + ' ' + str(row.get('detailJa') or '')
                numbers = re.findall(r'(?:찬송가|韓日|讃美歌)\s*(\d{1,3})', detail)
                for number in sorted(set(numbers)):
                    candidates = [x for x in index.get('hymns', []) if int(x['number']) == int(number)]
                    candidates.sort(key=lambda x: (x.get('preferred') is True, '(수정)' in x['file'], x.get('modifiedUtc', ''), x['file']), reverse=True)
                    result['hymnCandidates'].append({'serviceIndex': i, 'number': number.zfill(3),
                                                       'selected': candidates[0] if candidates else None,
                                                       'candidates': candidates})
                    if not candidates:
                        errors.append(f'No approved library hymn: {number}')
        for kind in ('absolution', 'sermon'):
            for field in ('refJa', 'refKo', 'ja', 'ko'):
                try:
                    _field(data, f'scripture.{kind}.{field}')
                except WorshipError as exc:
                    errors.append(str(exc))
        result['serviceDataSha256'] = _hash(_canonical(data))
        if os.name != 'nt':
            errors.append('Generation requires Windows desktop PowerPoint.')
        else:
            # Registry lookup only; planning must not create or attach a COM app.
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'PowerPoint.Application\CLSID'):
                    pass
            except OSError:
                errors.append('Desktop PowerPoint COM registration is missing.')
        prepared = _prepare(document)[0]
        result['warnings'].extend(prepared.pop('warnings', []))
        result.update(prepared)
    except (WorshipError, KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        if str(exc) not in errors:
            errors.append(str(exc))
    translated = []
    issues = []
    for error in errors:
        category = 'generation'
        if 'scripture.' in error:
            category = 'scripture'
            kind = '사죄확인' if 'absolution' in error else '설교본문'
            lang = '일본어' if error.endswith(('.ja', '.refJa')) else '한국어'
            error = f'{kind}의 {lang} 구절과 본문을 모두 입력해주세요.'
        elif 'serviceTitle' in error:
            category, error = 'title', '예배 제목을 한국어와 일본어로 입력해주세요.'
        elif 'hymn' in error.lower() or '찬송' in error or '라이브러리' in error:
            category = 'hymns'
        elif not any('\uac00' <= c <= '\ud7a3' for c in error):
            error = 'PPT 제작 정보를 확인해주세요. 담당자 확인 내용: ' + error
        translated.append(error)
        issues.append({'category': category, 'message': error})
    result['errors'] = list(dict.fromkeys(translated))
    result['issues'] = issues
    result['warnings'] = list(dict.fromkeys(w if any('\uac00' <= c <= '\ud7a3' for c in w)
                          else '완성 후 대표 화면을 확인해주세요. 승인 원본은 변경하지 않습니다.'
                          for w in result['warnings']))
    result['ready'] = not result['errors']
    result['summary'] = (f"PPT 제작 준비가 되었습니다. 총 {result.get('slideCount', 0)}장입니다." if result['ready']
                         else f"입력이 필요한 항목이 {len(result['errors'])}개 있습니다. 아래 안내를 확인해주세요.")
    return result


def _slide_copy(source, number, patch):
    """Patch only selected slide text in a disposable full source package."""
    result = BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, raw in source.parts.items():
            z.writestr(name, patch if name == source.slides[number - 1] else raw)
    return result.getvalue()


_VERIFY = r'''
param([string]$Deck,[string]$Checks,[int]$Count,[string]$ReviewDir)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
$app=$null; $p=$null
$oldSecurity=$null
function Stage([string]$text){
  Add-Content -LiteralPath (Join-Path $ReviewDir 'stage.log') -Value ((Get-Date -Format o)+' '+$text) -Encoding UTF8
}
function Word-Lines($shape,[string]$original){
  # Measure with PowerPoint's actual first visible line. Never break a word,
  # change font size or resize the approved text box.
  $words=@([regex]::Matches($original,'\S+') | ForEach-Object {$_.Value})
  $lines=[Collections.Generic.List[string]]::new()
  $line=''
  foreach($word in $words){
    $trial=if($line){$line+' '+$word}else{$word}
    $shape.TextFrame.TextRange.Text=$trial
    $first=$shape.TextFrame.TextRange.Lines(1,1).Text.TrimEnd([char]13,[char]10,[char]11)
    $available=$shape.Width-$shape.TextFrame2.MarginLeft-$shape.TextFrame2.MarginRight-1
    if($first -ceq $trial -and $shape.TextFrame2.TextRange.BoundWidth -le $available){$line=$trial;continue}
    if(!$line){throw '한국어 단어 하나가 승인된 본문 상자 폭을 넘습니다. 담당자 분할이 필요합니다.'}
    $lines.Add($line);$line=$word
    $shape.TextFrame.TextRange.Text=$word
    $first=$shape.TextFrame.TextRange.Lines(1,1).Text.TrimEnd([char]13,[char]10,[char]11)
    if($first -cne $word -or $shape.TextFrame2.TextRange.BoundWidth -gt $available){throw '한국어 단어 하나가 승인된 본문 상자 폭을 넘습니다.'}
  }
  if($line){$lines.Add($line)}
  $shape.TextFrame.TextRange.Text=($lines -join [char]11)
  $shape.TextFrame.TextRange.ParagraphFormat.SpaceWithin=1.2
}
try {
  Stage 'COM create'
  $app=New-Object -ComObject PowerPoint.Application
  $oldSecurity=$app.AutomationSecurity
  $app.AutomationSecurity=3
  $job=Get-Content -LiteralPath $Checks -Raw -Encoding UTF8 | ConvertFrom-Json
  if(@($job.insertions).Count -gt 0){
    Stage 'Open working baseline'
    $p=$app.Presentations.Open($Deck,0,0,0)
    Stage 'Delete working-copy slides'
    for($i=$p.Slides.Count;$i -ge 1;$i--){$p.Slides.Item($i).Delete()}
    foreach($entry in @($job.insertions)){
      Stage ('Insert '+$entry.path+' source slides '+$entry.first+'..'+$entry.last+' after '+$p.Slides.Count)
      $added=$p.Slides.InsertFromFile([string]$entry.path,$p.Slides.Count,[int]$entry.first,[int]$entry.last)
      if($added -ne ([int]$entry.last-[int]$entry.first+1)){throw 'Slide insertion failed'}
    }
    foreach($c in @($job.rows)){
      if($c.korean){
        Stage ('Word-aware Korean line breaks slide '+$c.slide)
        $slide=$p.Slides.Item([int]$c.slide)
        $shape=@($slide.Shapes | Where-Object {$_.Id -eq [int]$c.shapeId})
        if($shape.Count -ne 1){throw '본문 도형을 찾지 못했습니다.'}
        Word-Lines $shape[0] ([string]$c.text)
      }
    }
    Stage 'Save assembled deck'
    $p.Save();$p.Close();[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($p);$p=$null
  }
  Stage 'Reopen read-only for verification'
  $p=$app.Presentations.Open($Deck,-1,0,0)
  if($p.Slides.Count -ne $Count){throw 'Slide count mismatch'}
  $rows=@($job.rows)
  @($rows | Select-Object -ExpandProperty slide -Unique | Select-Object -First 12) | ForEach-Object {
    Stage ('Export representative slide '+$_)
    $p.Slides.Item([int]$_).Export((Join-Path $ReviewDir ('sample-'+$_+'.png')),'PNG',1280,720)
  }
  foreach($c in $rows){
    Stage ('Check slide '+$c.slide+' shape '+$c.shapeId)
    $s=$p.Slides.Item([int]$c.slide)
    $sh=@($s.Shapes | Where-Object {$_.Id -eq [int]$c.shapeId})
    if($sh.Count -ne 1){throw 'Changed shape missing'}
    $t=$sh[0].TextFrame2
    $actual=($t.TextRange.Text -replace '\s','')
    $want=($c.text -replace '\s','')
    if($actual -cne $want){throw 'Saved text mismatch'}
    if($c.korean -and $t.TextRange.Font.Size -lt 40){throw 'Korean scripture below 40pt'}
    $overflow=($t.TextRange.BoundWidth -gt ($sh[0].Width-$t.MarginLeft-$t.MarginRight+1) -or
               $t.TextRange.BoundHeight -gt ($sh[0].Height-$t.MarginTop-$t.MarginBottom+1))
    if($overflow){
       throw ('Text overflow slide '+$c.slide+' shape '+$c.shapeId+
              ' bounds '+$t.TextRange.BoundWidth+'x'+$t.TextRange.BoundHeight+
              ' box '+$sh[0].Width+'x'+$sh[0].Height)
    }
  }
  Stage 'PASS'
} finally {
  if($null -ne $p){$p.Close();[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($p)}
  # PowerPoint may be shared with the user's desktop: never Quit or kill it.
  if($null -ne $app){
    if($null -ne $oldSecurity){$app.AutomationSecurity=$oldSecurity}
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app)
  }
}
'''


def build_worship(document: dict) -> bytes:
    """Build a working editable PPTX; require successful desktop PowerPoint checks."""
    _require(os.name == 'nt', 'build_worship requires Windows desktop PowerPoint.')
    document = copy.deepcopy(document)
    assessment = plan_worship(document)
    _require(assessment['ready'], '\n'.join(assessment['errors']))
    plan, baseline, sequence = _prepare(document)
    result = baseline.raw
    checks = [dict(c, slide=i) for i, (_, _, _, bindings) in enumerate(sequence, 1) for c in bindings]
    qa_root = ROOT / '.build' / 'worship-qa'
    qa_root.mkdir(parents=True, exist_ok=True)
    review_dir = Path(tempfile.mkdtemp(prefix='review-', dir=qa_root))
    with tempfile.TemporaryDirectory(prefix='worship-') as folder:
        work = Path(folder)
        (work / 'deck.pptx').write_bytes(result)
        insertions = []
        for i, (source, n, patch, bindings) in enumerate(sequence, 1):
            if patch is not None:
                path = work / f'patched-{i}.pptx'
                path.write_bytes(_slide_copy(source, n, patch))
            else:
                path = source.path
            if (insertions and insertions[-1]['path'] == str(path)
                    and insertions[-1]['last'] == n - 1):
                insertions[-1]['last'] = n
            else:
                insertions.append({'path': str(path), 'first': n, 'last': n})
        (work / 'checks.json').write_text(json.dumps({'rows': checks, 'insertions': insertions}, ensure_ascii=False), encoding='utf-8-sig')
        (work / 'verify.ps1').write_text(_VERIFY, encoding='utf-8-sig')
        try:
            run = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                                  str(work / 'verify.ps1'), '-Deck', str(work / 'deck.pptx'),
                                  '-Checks', str(work / 'checks.json'), '-Count', str(plan['slideCount']),
                                  '-ReviewDir', str(review_dir)],
                                 capture_output=True, timeout=180, creationflags=0x08000000)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorshipError(f'PowerPoint verification unavailable or timed out: {exc}') from exc
        if run.returncode != 0:
            (review_dir / '검증실패_작업본.pptx').write_bytes((work / 'deck.pptx').read_bytes())
            raise WorshipError('PowerPoint 검증에 실패했습니다. 확인 자료: ' + str(review_dir) + '\n' +
                               run.stderr.decode('utf-8', errors='replace')[-3000:])
        result = (work / 'deck.pptx').read_bytes()
    # Fail if approved assets changed during production.
    for relative, digest in plan['sources'].items():
        _require(_hash(_path(relative).read_bytes()) == digest, f'Source changed during build: {relative}')
    return result


def validation_document() -> dict:
    """Return a NON-SERVICE smoke-test fixture using only approved local sample text.

    This is explicitly a validation deck, not a bulletin for an actual service.
    Bible excerpts come from the first approved template verse, without lookup.
    """
    from compose import initial_data
    data = initial_data()
    data.update(date=datetime.date.today().isoformat(), serviceTitleJa='動作確認', serviceTitleKo='검증용')
    index = _json('system/library-index.json')
    for row in data['service']:
        if row['ko'] == '모임찬송':
            row['detailKo'] = index['gospelSongs'][0]['firstLine']
            row['detailJa'] = ''
        if row['ko'] in ('감사찬송', '봉헌'):
            n = '306' if row['ko'] == '감사찬송' else '278'
            row.update(detailKo=f'한일찬송가 {n}장', detailJa=f'韓日 {n}')
    for kind, filename in [('absolution', '사죄확인_템플릿.pptx'), ('sermon', '설교본문_템플릿.pptx')]:
        deck = _Deck(_path('templates/채택된_템플릿/' + filename))
        first = _shapes(deck, 2)
        reference = ''.join(first['reference'][1])
        verse_ref = re.sub(r'[abc]$', '', reference)
        # Collect only consecutive sample parts of the same first verse.
        ja, ko = [], []
        for n in range(2, len(deck.slides) + 1):
            shapes = _shapes(deck, n)
            if re.sub(r'[abc]$', '', ''.join(shapes['reference'][1])) != verse_ref:
                break
            ja.append(''.join(shapes['japanese-text'][1]))
            ko.append(''.join(shapes['korean-text'][1]))
        # The approved cover supplies bilingual book names. Use its book prefix
        # with the first body chapter:verse, rather than the cover's full range.
        cover = _shapes(deck, 1)
        cv = re.search(r'\d+:\d+', verse_ref).group()
        data['scripture'][kind] = {
            'refJa': re.sub(r'\d+:.*$', cv, ''.join(cover['cover-japanese'][1])),
            'refKo': re.sub(r'\d+:.*$', cv, ''.join(cover['cover-korean'][1])),
            'ja': ''.join(ja), 'ko': ''.join(ko)}
    return {'serviceData': data, 'validationOnly': True}


def _self_test():
    """No COM or source writes: real-asset planning plus package integrity tests."""
    sparse = plan_worship({})
    assert not sparse['ready'] and sparse['errors'] and sparse['library']['hymnFileCount'] > 0
    fixture = validation_document()
    plan = plan_worship(fixture)
    assert plan['ready'], plan['errors']
    prepared, baseline, sequence = _prepare(fixture)
    for source, number, patch, checks in sequence:
        if patch is not None:
            candidate = _slide_copy(source, number, patch)
            with zipfile.ZipFile(BytesIO(candidate)) as z:
                assert z.testzip() is None
                assert z.read(source.slides[number-1]) == patch
                for name, original in source.parts.items():
                    if name != source.slides[number-1]:
                        assert z.read(name) == original
    for field in ('ja', 'ko', 'refJa', 'refKo'):
        bad = copy.deepcopy(fixture)
        bad['serviceData']['scripture']['sermon'][field] = ''
        assert not plan_worship(bad)['ready']
    bad = copy.deepcopy(fixture)
    bad['serviceData']['date'] = ''
    assert not plan_worship(bad)['ready']
    bad = copy.deepcopy(fixture)
    bad['serviceData']['service'][4]['detailKo'] = '한일찬송가 999장'
    bad['serviceData']['service'][4]['detailJa'] = '韓日 999'
    assert not plan_worship(bad)['ready']
    assert all(_hash(_path(p).read_bytes()) == digest for p, digest in prepared['sources'].items())
    print(f'PASS: sparse input, missing scripture/date/hymn, automatic {len(sequence)}-slide plan, text-only working copies, unchanged sources')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Local worship PPTX backend checks')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--fixture', type=Path, help='Write a clearly marked validation-only JSON document')
    parser.add_argument('--build-fixture', type=Path, help='Build validation-only PPTX through desktop PowerPoint checks')
    args = parser.parse_args()
    if args.self_test:
        _self_test()
    if args.fixture:
        args.fixture.write_text(json.dumps(validation_document(), ensure_ascii=False, indent=2), encoding='utf-8')
    if args.build_fixture:
        args.build_fixture.write_bytes(build_worship(validation_document()))
        print('QA PNG:', max((ROOT / '.build' / 'worship-qa').iterdir(), key=lambda p: p.stat().st_mtime))
