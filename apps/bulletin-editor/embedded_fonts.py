"""Exact retained-PDF subsets; no installed-font search or replacement.

Call discover_fonts(path) before the other functions. Registry values are JSON
metadata; fontFile is an opaque in-memory ID, not a persistent font installation.
Widths are font units. Only mapped, non-.notdef glyphs are exposed. fsType is
reported as source metadata, not a license determination. These are bounded PDF
subsets for the local editor, never reconstructed full font distributions.
"""
from __future__ import annotations

import hashlib
import io
import re
import threading
import sys
from pathlib import Path

_vendor=Path(__file__).resolve().parent/'vendor'
if _vendor.is_dir() and str(_vendor) not in sys.path:sys.path.insert(0,str(_vendor))

_faces = {}
_lock = threading.RLock()


class MissingGlyphError(ValueError):
    pass


def normalize_name(name):
    return re.sub(r'^[A-Z]{6}\+', '', str(name).lstrip('/'))


def _id(name):
    return 'native-' + hashlib.sha256(name.encode()).hexdigest()[:16]


def _unicode_map(font):
    # pypdf changed this private API; support both bundled generations.
    try:
        from pypdf._cmap import get_encoding
        encoding, mapping = get_encoding(font)
    except ImportError:
        from pypdf._cmap import _parse_encoding, _parse_to_unicode
        encoding = _parse_encoding(font)
        mapping = _parse_to_unicode(font)[0]
    result = {}
    for code, text in mapping.items():
        if isinstance(code, str) and len(text) == 1:
            result[int.from_bytes(code.encode('utf-16-be', 'surrogatepass'), 'big')] = text
    return encoding, result


def _pdf_widths(font):
    result = {}
    widths = font.get('/W', [])
    if hasattr(widths,'get_object'):widths=widths.get_object()
    i = 0
    while i < len(widths):
        first = int(widths[i]); value = widths[i + 1]; i += 2
        if hasattr(value,'get_object'):value=value.get_object()
        if isinstance(value, list):
            result.update((first + j, float(w)) for j, w in enumerate(value))
        else:
            last = int(value); width = float(widths[i]); i += 1
            result.update((j, width) for j in range(first, last + 1))
    if '/Widths' in font:
        result.update((int(font.get('/FirstChar', 0)) + j, float(w))
                      for j, w in enumerate(font['/Widths']))
    return result


def _extract(font):
    from fontTools.ttLib import TTFont
    from fontTools.cffLib import CFFFontSet
    from fontTools.fontBuilder import FontBuilder
    from fontTools.ttLib import newTable
    from pdfminer.cmapdb import CMapDB
    from fontTools.agl import toUnicode

    descendant = font.get('/DescendantFonts', [font])[0].get_object()
    descriptor = descendant['/FontDescriptor']
    encoding, unicode_by_code = _unicode_map(font)
    is_cid = '/DescendantFonts' in font
    raw_cff = False
    if '/FontFile2' in descriptor:
        tt = TTFont(io.BytesIO(descriptor['/FontFile2'].get_data()))
    elif '/FontFile3' in descriptor:
        stream = descriptor['/FontFile3']; data = stream.get_data()
        if stream.get('/Subtype') == '/OpenType':
            tt = TTFont(io.BytesIO(data))
        else:
            cff = CFFFontSet(); cff.decompile(io.BytesIO(data), None)
            top = cff.topDictIndex[0]
            order = list(top.charset)
            matrix = top.FontMatrix
            if matrix[1:4] != [0, 0, matrix[0]] or matrix[4:] != [0, 0]:
                raise ValueError('Unsupported nonuniform CFF FontMatrix')
            upem = round(1 / matrix[0])
            fb = FontBuilder(upem, isTTF=False)
            fb.setupGlyphOrder(order)
            table = newTable('CFF '); table.cff = cff; fb.font['CFF '] = table
            fb.font.sfntVersion = 'OTTO'
            # Keep the entire original CFF object, including CID FDSelect,
            # FDArray, local/global Subrs and original charstrings.
            metrics = {}
            from fontTools.pens.boundsPen import BoundsPen
            for glyph in order:
                cs = top.CharStrings[glyph]
                cs.draw(BoundsPen(None))
                metrics[glyph] = (round(cs.width), 0)
            fb.setupHorizontalMetrics(metrics)
            fb.setupHorizontalHeader(ascent=round(float(descriptor.get('/Ascent', 880))*upem/1000),
                                     descent=round(float(descriptor.get('/Descent', -120))*upem/1000))
            fb.setupCharacterMap({})
            name = normalize_name(font['/BaseFont'])
            fb.setupNameTable({'familyName': name, 'styleName': 'Regular',
                               'uniqueFontIdentifier': str(font['/BaseFont']),
                               'fullName': name, 'psName': name})
            fb.setupOS2(); fb.setupPost(); fb.setupMaxp()
            tt = fb.font; raw_cff = True
    else:
        raise ValueError('No supported embedded FontFile2/FontFile3')
    order = tt.getGlyphOrder(); original_cmap = tt.getBestCmap() or {}
    upem = tt['head'].unitsPerEm
    glyph_by_code = {}
    if is_cid:
        if 'CFF ' in tt:
            glyph_by_code = {int(g[3:]): g for g in order if g.startswith('cid') and g[3:].isdigit()}
            top = tt['CFF '].cff.topDictIndex[0]
            if not unicode_by_code and hasattr(top, 'ROS'):
                registry, ordering, _ = top.ROS
                unicode_by_code = CMapDB.get_unicode_map(registry + '-' + ordering).cid2unichr
        else:
            cidmap = descendant.get('/CIDToGIDMap', '/Identity')
            if hasattr(cidmap,'get_object'):cidmap=cidmap.get_object()
            if hasattr(cidmap, 'get_data'):
                data = cidmap.get_data()
                glyph_by_code = {cid: order[gid] for cid in range(len(data)//2)
                                 if (gid := int.from_bytes(data[2*cid:2*cid+2], 'big')) < len(order)}
            else:
                glyph_by_code = dict(enumerate(order))
    else:
        # PDF simple embedded subsets commonly have a MacRoman cmap whose
        # byte codes match ToUnicode; do not interpret these as Unicode.
        byte_cmap = next((t.cmap for t in tt['cmap'].tables if t.platformID == 1), {})
        for code in set(unicode_by_code) | set(byte_cmap):
            if code in byte_cmap:
                glyph_by_code[code] = byte_cmap[code]
        if isinstance(encoding, dict):
            for code, char in encoding.items():
                glyph = (byte_cmap.get(code) or original_cmap.get(ord(char))) if len(char) == 1 else None
                if glyph:
                    glyph_by_code.setdefault(code, glyph)
                    unicode_by_code.setdefault(code, char)
        if not unicode_by_code:
            unicode_by_code = {c: chr(c) for c in original_cmap}
            glyph_by_code.update(original_cmap)
        for code, glyph in byte_cmap.items():
            if code not in unicode_by_code:
                char = toUnicode(glyph)
                if len(char) == 1:
                    unicode_by_code[code] = char
    cmap = {}; widths = {}; pdf_widths = _pdf_widths(descendant)
    for code, char in unicode_by_code.items():
        glyph = glyph_by_code.get(code)
        if len(char) != 1 or not glyph or glyph == '.notdef':
            continue
        cp = ord(char)
        if cp in cmap and cmap[cp] != glyph:
            raise ValueError('Ambiguous Unicode glyph mapping')
        cmap[cp] = glyph
        widths[char] = pdf_widths.get(code, float(descendant.get('/DW', 1000)) if is_cid
                                     else tt['hmtx'][glyph][0] * 1000 / upem) * upem / 1000
    if not cmap:
        raise ValueError('Embedded subset has no reliable Unicode mapping')
    fs_type = None if raw_cff or 'OS/2' not in tt else int(tt['OS/2'].fsType)
    return tt, cmap, widths, fs_type


def discover_fonts(pdfPath, extra_paths=()):
    """Read this PDF only (at most four pages); publish atomically on success.

    Duplicate subsets of one PS face are merged without changing glyph shapes.
    Individual unsupported fonts remain explicit unavailable registry entries.
    """
    from pypdf import PdfReader
    from fontTools.merge import Merger
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
    paths=[Path(pdfPath)]+[Path(p) for p in extra_paths if Path(p)!=Path(pdfPath)]
    if len(paths)>7:raise ValueError('Only the seven retained bulletin sources may be combined.')
    readers=[PdfReader(p) for p in paths]
    if len(readers[0].pages)!=4 or sum(len(r.pages) for r in readers)>32:
        raise ValueError('Embedded font discovery is bounded to retained bulletin pages')
    groups = {}; seen = set()
    for source_index,reader in enumerate(readers):
      for page in reader.pages:
        for ref in page['/Resources'].get('/Font', {}).values():
            font = ref.get_object(); source_name = str(font['/BaseFont'])
            identity = (source_index,getattr(ref, 'idnum', None), source_name)
            if identity in seen: continue
            seen.add(identity)
            groups.setdefault(normalize_name(source_name), []).append(font)
    result = {}; pending = {}
    for name, fonts in groups.items():
        entry = dict(id=_id(name), family=name, originalName=name, available=False,
                     sameFontAvailable=False, subset=True, fontFile=_id(name),
                     location=None, url='/api/weekly/font/' + _id(name))
        result[name] = entry
        try:
            parts = []; all_widths = {}; rights = []; prior = {}
            for font in fonts:
                tt, cmap, widths, fs = _extract(font)
                # Merge only unique Unicode entries. Compare duplicate outlines
                # before dropping their redundant cmap entries, so a conflicting
                # subset never silently changes the face behind a character.
                from fontTools.pens.recordingPen import DecomposingRecordingPen
                glyphs = tt.getGlyphSet()
                for cp, glyph in list(cmap.items()):
                    pen = DecomposingRecordingPen(glyphs)
                    glyphs[glyph].draw(pen)
                    signature = (tt['head'].unitsPerEm, pen.value, widths[chr(cp)])
                    if cp in prior:
                        if signature != prior[cp]:
                            raise ValueError(f'Conflicting subset glyph U+{cp:04X}')
                        del cmap[cp]
                    else:
                        prior[cp] = signature
                tt['cmap'].tables = []
                for fmt, enc in [(4, 1), (12, 10)]:
                    table = CmapSubtable.newSubtable(fmt)
                    table.platformID = 3; table.platEncID = enc; table.language = 0
                    table.cmap = {cp: g for cp, g in cmap.items() if fmt == 12 or cp <= 0xffff}
                    tt['cmap'].tables.append(table)
                if not cmap:continue
                buf = io.BytesIO(); tt.save(buf); buf.seek(0); parts.append(buf)
                all_widths.update(widths); rights.append(fs)
            tt = Merger().merge(parts) if len(parts) > 1 else __import__('fontTools.ttLib',fromlist=['TTFont']).TTFont(parts[0])
            output = io.BytesIO(); tt.save(output)
            cmap = tt.getBestCmap()
            entry.update(available=True, sameFontAvailable=True, reason=None,
                         chars=''.join(sorted(all_widths)), unicodeChars=sorted(map(ord, all_widths)),
                         glyphWidths=all_widths, unitsPerEm=tt['head'].unitsPerEm,
                         fsType=rights[0] if len(set(rights)) == 1 else rights,
                         rightsNote='Source fsType only; not a license grant. Local retained-PDF subset use.',
                         format='otf' if 'CFF ' in tt else 'ttf')
            pending[name] = pending[entry['id']] = (tt, cmap, all_widths, output.getvalue(), entry)
        except Exception as exc:
            entry['reason'] = f'{type(exc).__name__}: {exc}'
    with _lock:
        _faces.clear(); _faces.update(pending)
    return result


def _face(name):
    with _lock:
        try: return _faces.get(name) or _faces[normalize_name(name)]
        except KeyError: raise ValueError('Embedded original font unavailable: ' + str(name)) from None


def font_bytes(name):
    """Return Unicode-reencoded SFNT bytes, preserving original glyph programs."""
    return _face(name)[3]


def has_chars(name, text):
    cmap = _face(name)[1]
    return all(ord(c) in cmap for c in text)


def _checked(name, text):
    face = _face(name)
    missing = sorted({ord(c) for c in text if ord(c) not in face[1]})
    if missing:
        raise MissingGlyphError(f'{name}: subset lacks ' + ', '.join(f'U+{cp:04X}' for cp in missing))
    return face


def measure(name, text, size):
    face = _checked(name, text)
    return sum(face[2][c] for c in text) * size / face[0]['head'].unitsPerEm


def draw(canvas, name, text, x, baseline, size):
    """Draw original outlines at a PDF bottom-left baseline, including CID CFF.

    Validate the whole string before touching the canvas. No silent .notdef and
    no ReportLab TTFont/CFF substitution. Quadratics become exact cubics via
    BasePen; CFF curves are retained. Returns advance in PDF points.
    """
    from fontTools.pens.basePen import BasePen
    face = _checked(name, text); tt, cmap, widths = face[:3]
    glyphs = tt.getGlyphSet()

    class CanvasPen(BasePen):
        def _moveTo(self, p): self.path.moveTo(*p)
        def _lineTo(self, p): self.path.lineTo(*p)
        def _curveToOne(self, a, b, c): self.path.curveTo(*a, *b, *c)
        def _closePath(self): self.path.close()
        def _endPath(self): pass

    scale = size / tt['head'].unitsPerEm
    canvas.saveState()
    try:
        canvas.translate(x, baseline); canvas.scale(scale, scale)
        for char in text:
            pen = CanvasPen(glyphs); pen.path = canvas.beginPath()
            glyphs[cmap[ord(char)]].draw(pen)
            canvas.drawPath(pen.path, stroke=0, fill=1)
            canvas.translate(widths[char], 0)
    finally:
        canvas.restoreState()
    return measure(name, text, size)
