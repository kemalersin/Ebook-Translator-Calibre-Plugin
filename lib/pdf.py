"""Translate PDF files in place, so that the result keeps the original layout.

The text of every paragraph is erased from its page and the translation is
written into the same area; images, drawings, page size and page count stay
as they are. PyMuPDF does the work. It is too large to bundle with the plugin,
so it is installed into the plugin's config directory on demand.
"""
import io
import os
import re
import sys
import json
import html
import shutil
import hashlib
import zipfile
import platform
from collections import Counter
from statistics import median

from calibre.utils.config_base import plugin_dir  # type: ignore
from calibre.utils.localization import _  # type: ignore

from .. import EbookTranslator
from .utils import log, request, socks_proxy
from .config import get_config
from .element import Element, Extraction


load_translations()  # type: ignore


PDF_CACHE_VERSION = 'pdf-layout-v2'
PYMUPDF_VERSION = '2.1'
PYMUPDF_RELEASE = 'https://pypi.org/pypi/PyMuPDF/%s/json' % PYMUPDF_VERSION

# The platform part of the wheel names that PyMuPDF publishes.
WHEEL_PLATFORMS = {
    ('windows', 'amd64'): ('win', 'amd64'),
    ('darwin', 'arm64'): ('macosx', 'arm64'),
    ('darwin', 'x86_64'): ('macosx', 'x86_64'),
    ('linux', 'x86_64'): ('manylinux', 'x86_64'),
    ('linux', 'aarch64'): ('manylinux', 'aarch64'),
}

SENTENCE_END = re.compile(r'[.!?…:;。！？：；)\]"”’»]$')
# Dashes are left out: a line may start with a dash in the middle of text.
LIST_ITEM = re.compile(r'([•◦▪●○■□‣⁃*-]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s')
CJK = re.compile(r'[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]')
MONO_FONTS = re.compile(r'mono|courier|consol|menlo|code', re.I)
SANS_FONTS = re.compile(
    r'sans|arial|helvetica|calibri|verdana|tahoma|segoe|gothic|futura|'
    r'myriad|roboto|lato|inter', re.I)
SERIF_FONTS = re.compile(
    r'serif|times|roman|georgia|garamond|minion|palatino|cambria|'
    r'baskerville|caslon|book|cmr|lmroman|nimbusrom', re.I)
BOLD_FONTS = re.compile(r'bold|black|heavy|semibold|demi', re.I)
ITALIC_FONTS = re.compile(r'italic|oblique', re.I)

# MuPDF's own fonts for the generic families. Its default serif font is
# wider than Times, which most PDFs use.
BUILTIN_FONTS = {
    'serif': 'Times',
    'sans-serif': 'Helvetica',
    'monospace': 'Courier',
}

FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16

# Smaller fonts are tried in steps so that neighbouring paragraphs end up
# with the same size instead of slightly different ones. The fallback steps
# are used only when the preferred ones do not fit.
FONT_SCALES = (1.0, 0.9, 0.8, 0.7)
FALLBACK_SCALES = (0.6, 0.5, 0.4, 0.3)


def get_library_dir():
    return os.path.join(plugin_dir, EbookTranslator.identifier, 'pymupdf')


def load_pymupdf():
    """Return the PyMuPDF module, or None when it is not installed."""
    library_dir = get_library_dir()
    if not os.path.isdir(library_dir):
        return None
    if library_dir not in sys.path:
        sys.path.insert(0, library_dir)
    try:
        import pymupdf  # type: ignore
    except Exception as e:  # ImportError, or OSError from a broken binary.
        log.debug('PyMuPDF is unavailable: %s' % e)
        return None
    # A folder named pymupdf elsewhere in the path imports as an empty
    # namespace package.
    return pymupdf if hasattr(pymupdf, 'Document') else None


def get_installed_version():
    """Return the version of the installed PyMuPDF without loading it."""
    library_dir = get_library_dir()
    if os.path.isdir(library_dir):
        for name in os.listdir(library_dir):
            match = re.match(r'pymupdf-(.+)\.dist-info$', name, re.I)
            if match:
                return match.group(1)
    return None


def require_pymupdf():
    pymupdf = load_pymupdf()
    if pymupdf is None:
        raise Exception(_(
            'PyMuPDF is required to keep the layout of PDF files. Please '
            'install it in the General tab of the plugin settings.'))
    return pymupdf


def is_pdf_layout(input_format, output_format):
    """Whether a PDF is translated in place instead of being converted."""
    return input_format == 'pdf' and output_format == 'pdf' \
        and bool(get_config().get('pdf_layout.enabled'))


# uname and platform.machine() do not use one name per architecture.
MACHINE_ALIASES = {
    ('linux', 'amd64'): 'x86_64',
    ('linux', 'x64'): 'x86_64',
    ('linux', 'arm64'): 'aarch64',
    ('windows', 'x86_64'): 'amd64',
    ('darwin', 'aarch64'): 'arm64',
}


def find_wheel(files, system=None, machine=None, version=None):
    """Return the wheel of a PyPI release that this calibre can load."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = MACHINE_ALIASES.get((system, machine), machine)
    version = tuple(version or sys.version_info[:2])
    target = WHEEL_PLATFORMS.get((system, machine))
    if target is None:
        return None
    prefix, arch = target
    for file in files:
        name = file['filename']
        if not name.endswith('.whl'):
            continue
        python_tag, abi_tag, platform_tag = name[:-4].split('-')[-3:]
        if not (platform_tag.startswith(prefix)
                and platform_tag.endswith(arch)):
            continue
        required = (int(python_tag[2]), int(python_tag[3:]))
        if version == required or (abi_tag == 'abi3' and version > required):
            return file
    return None


def get_proxy():
    """Return the type, host and port of the plugin's proxy, if enabled."""
    config = get_config()
    if not config.get('proxy_enabled'):
        return None
    proxy_type = config.get('proxy_type') or 'http'
    setting = config.get('proxy_setting') or {}
    # Compatible with old proxy settings stored as a list.
    if isinstance(setting, list):
        setting = {'http': setting}
    host, port = setting.get(proxy_type) or ['', '']
    if not host or not port:
        return None
    return proxy_type, host, port


def fetch(url, progress=None):
    """Download a URL through the plugin's network proxy, if any."""
    proxy = get_proxy()
    if proxy is not None and proxy[0] == 'socks5':
        with socks_proxy(proxy[1], int(proxy[2])):
            return _fetch(url, None, progress)
    proxy_uri = None
    if proxy is not None:
        proxy_uri = '%s:%s' % proxy[1:]
        if not proxy_uri.startswith('http'):
            proxy_uri = 'http://' + proxy_uri
    return _fetch(url, proxy_uri, progress)


def _fetch(url, proxy_uri, progress):
    response = request(url, timeout=60, proxy_uri=proxy_uri, raw_object=True)
    total = int(response.info().get('Content-Length') or 0)
    data = bytearray()
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if progress is not None:
            progress(len(data), total)
    return bytes(data)


def install_pymupdf(progress=None, wheel_path=None):
    """Install PyMuPDF from PyPI, or from a given wheel file. A wheel is a
    zip archive, so unpacking it is all that pip would do here.
    """
    if 'pymupdf' in sys.modules:
        raise Exception(_(
            'PyMuPDF is in use. Please restart calibre and try again.'))
    if wheel_path is None:
        release = json.loads(fetch(PYMUPDF_RELEASE))
        wheel = find_wheel(release['urls'])
        if wheel is None:
            raise Exception(_(
                'PyMuPDF {} is not available for this system ({} {}, '
                'Python {}).').format(
                    PYMUPDF_VERSION, platform.system(), platform.machine(),
                    platform.python_version()))
        data = fetch(wheel['url'], progress)
        if hashlib.sha256(data).hexdigest() != wheel['digests']['sha256']:
            raise Exception(_(
                'The downloaded file is damaged. Please try again.'))
        archive = zipfile.ZipFile(io.BytesIO(data))
    else:
        archive = zipfile.ZipFile(wheel_path)
    library_dir = get_library_dir()
    partial_dir = library_dir + '.partial'
    shutil.rmtree(partial_dir, ignore_errors=True)
    with archive:
        archive.extractall(partial_dir)
    shutil.rmtree(library_dir, ignore_errors=True)
    os.rename(partial_dir, library_dir)
    return library_dir


# Inline formatting travels through the translation inside these markers.
# They contain no words, so an engine has nothing to translate, and spaces
# around them keep the words themselves out of the marker.
MARKER = re.compile(r'⟦\s*(/)?\s*(\d+)\s*⟧')


def plain_text(text):
    """The text a reader sees: the same string without inline markers."""
    return MARKER.sub('', text)


def _drop_last_visible(text):
    """Drop the last character that is not part of a trailing marker."""
    match = re.search(r'(?:⟦\s*/?\s*\d+\s*⟧\s*)*$', text)
    suffix = match.group(0)
    body = text[:-len(suffix)] if suffix else text
    return body[:-1] + suffix


def join_text(head, tail):
    """Join two lines of a paragraph, undoing hyphenation at the line end."""
    if head == '' or tail == '':
        return head or tail
    plain_head, plain_tail = plain_text(head), plain_text(tail)
    if plain_head == '' or plain_tail == '':
        return head + tail
    if plain_head.endswith('\xad'):
        return _drop_last_visible(head) + tail
    if plain_head.endswith('-') and len(plain_head) > 1 \
            and plain_head[-2].isalpha():
        if plain_tail[0].islower():
            return _drop_last_visible(head) + tail
        return head + tail
    if CJK.match(plain_head[-1]) and CJK.match(plain_tail[0]):
        return head + tail
    return head + ' ' + tail


def get_rules(drawings):
    """Return the boxes of the thin lines among the drawings of a page, such
    as the borders of table cells, and of the small shapes, such as bullets.
    """
    rules, marks = [], []
    # One drawing may hold all the lines of a table, so look at its items.
    for drawing in drawings:
        x0, y0, x1, y1 = drawing['rect']
        if max(x1 - x0, y1 - y0) <= 8 and min(x1 - x0, y1 - y0) >= 1:
            marks.append([x0, y0, x1, y1])
            continue
        for item in drawing['items']:
            if item[0] == 'l':
                x0, x1 = sorted((item[1][0], item[2][0]))
                y0, y1 = sorted((item[1][1], item[2][1]))
            elif item[0] == 're':
                x0, y0, x1, y1 = item[1]
            else:
                continue
            if min(x1 - x0, y1 - y0) <= 2 and max(x1 - x0, y1 - y0) >= 8:
                rules.append([x0, y0, x1, y1])
    return rules, marks


def _same_row(a, b):
    """Whether two boxes sit on one row, rather than one below the other."""
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    smaller = min(a[3] - a[1], b[3] - b[1])
    taller = max(a[3] - a[1], b[3] - b[1])
    return smaller > 0 and overlap > 0.5 * smaller and taller < 2.5 * smaller


def _gap_has_separator(a, b, separators):
    """Whether a column boundary falls in the space between two boxes."""
    left, right = (a, b) if a[0] <= b[0] else (b, a)
    gap0, gap1 = left[2], right[0]
    if gap1 - gap0 < 4:
        return False
    middle = (max(left[1], right[1]) + min(left[3], right[3])) / 2
    if min(left[3], right[3]) <= max(left[1], right[1]):
        middle = (left[1] + left[3] + right[1] + right[3]) / 4
    for x0, x1, y0, y1 in separators:
        if y0 <= middle <= y1 and x1 > gap0 + 0.5 and x0 < gap1 - 0.5:
            return True
    return False


def _fill_separators(drawings, width, height):
    """Column edges of shaded cells that tile a row.

    A borderless table is often a row of filled rectangles with no stroked
    line between them. The shared edge is the column boundary, and it also
    applies to the unshaded rows of the same grid.
    """
    fills = []
    for drawing in drawings:
        if not drawing.get('fill') or drawing.get('fill_opacity') == 0:
            continue
        rect = drawing.get('rect')
        if not rect:
            continue
        x0, y0, x1, y1 = (float(value) for value in rect)
        if x1 - x0 < 12 or y1 - y0 < 8 or y1 - y0 > 0.45 * height \
                or x1 - x0 > 0.95 * width:
            continue
        fills.append((x0, y0, x1, y1))
    fills.sort(key=lambda box: (box[1], box[0]))
    rows = []
    for box in fills:
        if rows and abs(box[1] - rows[-1][0][1]) <= 2 \
                and abs(box[3] - rows[-1][0][3]) <= 2:
            rows[-1].append(box)
        else:
            rows.append([box])
    edges = []
    for row in rows:
        row.sort(key=lambda box: box[0])
        run = [row[0]]
        runs = []
        for box in row[1:]:
            if abs(box[0] - run[-1][2]) <= 3:
                run.append(box)
            else:
                runs.append(run)
                run = [box]
        runs.append(run)
        for run in runs:
            if len(run) < 2:
                continue
            top = min(box[1] for box in run)
            bottom = max(box[3] for box in run)
            for left, right in zip(run, run[1:]):
                edges.append(((left[2] + right[0]) / 2, top, bottom))
    if not edges:
        return []
    edges.sort(key=lambda item: (item[0], item[1]))
    groups = [[edges[0][0], [edges[0][1:]]]]
    for edge, top, bottom in edges[1:]:
        if abs(edge - groups[-1][0]) <= 2:
            groups[-1][1].append((top, bottom))
        else:
            groups.append([edge, [(top, bottom)]])
    separators = []
    for edge, bands in groups:
        bands.sort()
        clusters, cluster = [], [bands[0]]
        for band in bands[1:]:
            span = max(36, cluster[-1][1] - cluster[-1][0])
            if band[0] - cluster[-1][1] <= span:
                cluster.append(band)
            else:
                clusters.append(cluster)
                cluster = [band]
        clusters.append(cluster)
        for cluster in clusters:
            pad = max(band[1] - band[0] for band in cluster)
            separators.append((
                edge, edge,
                min(band[0] for band in cluster) - pad,
                max(band[1] for band in cluster) + pad))
    return separators


def _line_fragments(line):
    """Boxes of one line, split where a wide gap may be a column."""
    spans = [
        span for span in line.get('spans', []) if span.get('text', '').strip()]
    if not spans or any('bbox' not in span for span in spans):
        return [list(line['bbox'])]
    spans = sorted(spans, key=lambda span: span['bbox'][0])
    groups = [[spans[0]]]
    for span in spans[1:]:
        previous = groups[-1][-1]
        size = max(previous.get('size', 10), span.get('size', 10))
        if span['bbox'][0] - previous['bbox'][2] >= max(12, 2 * size):
            groups.append([span])
        else:
            groups[-1].append(span)
    boxes = []
    for group in groups:
        boxes.append([
            min(span['bbox'][0] for span in group),
            min(span['bbox'][1] for span in group),
            max(span['bbox'][2] for span in group),
            max(span['bbox'][3] for span in group)])
    return boxes


def _covered_gaps(gaps):
    """Pieces of the x axis covered by gaps from two or more rows."""
    events = []
    for x0, x1, row in gaps:
        events.append((x0, 1, row))
        events.append((x1, -1, row))
    events.sort(key=lambda item: (item[0], -item[1]))
    counts, segments, start, depth = {}, [], None, 0
    for x, delta, row in events:
        if delta > 0:
            counts[row] = counts.get(row, 0) + 1
        else:
            counts[row] -= 1
            if counts[row] <= 0:
                del counts[row]
        count = len(counts)
        if start is not None and count != depth:
            if x > start:
                segments.append((start, x, depth))
            start = x if count else None
        elif start is None and count:
            start = x
        depth = count
    return segments


def _gap_separators(data):
    """Empty vertical channels where several rows leave the same gap.

    Justified text also leaves wide word gaps, but those gaps do not line
    up down the page. A channel that does is the space between columns.
    """
    fragments = []
    for block in data.get('blocks', []):
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            if tuple(line.get('dir', (1, 0))) != (1, 0):
                continue
            size = max(
                (span.get('size', 10) for span in line.get('spans', [])),
                default=10)
            for box in _line_fragments(line):
                fragments.append((box, size))
    fragments.sort(key=lambda item: ((item[0][1] + item[0][3]) / 2, item[0][0]))
    rows = []
    for box, size in fragments:
        if rows and _same_row(rows[-1]['boxes'][0], box):
            row = rows[-1]
            row['boxes'].append(box)
            row['size'] = min(row['size'], size)
            row['y0'] = min(row['y0'], box[1])
            row['y1'] = max(row['y1'], box[3])
        else:
            rows.append({
                'boxes': [box], 'size': size, 'y0': box[1], 'y1': box[3]})
    # Eight ems is past anything justification does to a word space. A
    # running head uses a hole of that size to park the page label, and the
    # label has no second row to confirm it.
    separators = []
    for row in rows:
        boxes = sorted(row['boxes'], key=lambda box: box[0])
        limit = max(64, 8 * row['size'])
        for left, right in zip(boxes, boxes[1:]):
            if right[0] - left[2] >= limit:
                separators.append((
                    left[2], right[0], row['y0'] - 1, row['y1'] + 1))
    if len(rows) < 2:
        return separators
    clusters, cluster = [], [rows[0]]
    for row in rows[1:]:
        previous = cluster[-1]
        if row['y0'] - previous['y1'] <= max(40, 3 * previous['size']):
            cluster.append(row)
        else:
            clusters.append(cluster)
            cluster = [row]
    clusters.append(cluster)
    for cluster in clusters:
        gaps = []
        for index, row in enumerate(cluster):
            boxes = sorted(row['boxes'], key=lambda box: box[0])
            for left, right in zip(boxes, boxes[1:]):
                if right[0] - left[2] >= max(12, 2 * row['size']):
                    gaps.append((left[2], right[0], index))
        bands = []
        for x0, x1, count in _covered_gaps(gaps):
            width = x1 - x0
            if count >= 2 and width >= 24 or count >= 3 and width >= 16:
                bands.append((x0, x1))
        for x0, x1 in _merge_bands(bands):
            used = [
                row for row in cluster
                if any(min(x1, right[0]) - max(x0, left[2]) >= 8
                       for left, right in zip(
                           sorted(row['boxes'], key=lambda box: box[0]),
                           sorted(row['boxes'], key=lambda box: box[0])[1:])
                       if right[0] - left[2] >= max(12, 2 * row['size']))]
            if len(used) < 2:
                continue
            separators.append((
                x0, x1,
                min(row['y0'] for row in used) - 1,
                max(row['y1'] for row in used) + 1))
    return separators


def _merge_bands(bands):
    if not bands:
        return []
    bands = sorted(bands)
    merged = [list(bands[0])]
    for x0, x1 in bands[1:]:
        if x0 <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    return [tuple(band) for band in merged]


def column_separators(drawings, data, width, height):
    """Vertical boundaries that table columns share on this page."""
    return _fill_separators(drawings, width, height) + _gap_separators(data)


def _split_on_separators(line, separators):
    """Break one line into a line per cell when a column crosses it."""
    spans = list(line.get('spans') or [])
    if len(spans) < 2 or any('bbox' not in span for span in spans):
        return [line]
    spans = sorted(spans, key=lambda span: span['bbox'][0])
    groups = [[]]
    for span in spans:
        box = span['bbox']
        blank = not span.get('text', '').strip()
        if blank and _box_crosses(box, separators):
            if groups[-1]:
                groups.append([])
            continue
        if groups[-1] and _gap_has_separator(
                groups[-1][-1]['bbox'], box, separators):
            groups.append([span])
        else:
            groups[-1].append(span)
    groups = [
        group for group in groups
        if any(span.get('text', '').strip() for span in group)]
    if len(groups) <= 1:
        return [line]
    pieces = []
    for group in groups:
        piece = dict(line)
        piece['spans'] = group
        piece['bbox'] = [
            min(span['bbox'][0] for span in group),
            min(span['bbox'][1] for span in group),
            max(span['bbox'][2] for span in group),
            max(span['bbox'][3] for span in group)]
        piece['cell'] = True
        pieces.append(piece)
    return pieces


def _box_crosses(box, separators):
    middle = (box[1] + box[3]) / 2
    for x0, x1, y0, y1 in separators:
        if y0 <= middle <= y1 and x0 < box[2] - 0.5 and x1 > box[0] + 0.5:
            return True
    return False


def _mark_columns(data, separators):
    if not separators:
        return
    for block in data['blocks']:
        if block.get('type') != 0:
            continue
        lines = []
        for line in block['lines']:
            lines.extend(_split_on_separators(line, separators))
        ordered = sorted(
            range(len(lines)),
            key=lambda index: (lines[index]['bbox'][1], lines[index]['bbox'][0]))
        for previous, index in zip(ordered, ordered[1:]):
            left, right = lines[previous], lines[index]
            if _same_row(left['bbox'], right['bbox']) and _gap_has_separator(
                    left['bbox'], right['bbox'], separators):
                left['cell'] = True
                right['cell'] = True
        block['lines'] = lines


def mark_lines(page, data, rules, marks, flags, separators=()):
    """Prepare the lines of a page with what only the drawings tell: a line
    that crosses the border of table cells is cut, a line that is a table
    cell is marked as such, and so is a line after a drawn list bullet.

    ``separators`` are the column boundaries of a borderless table. Text on
    either side of one stays a cell of its own.
    """
    verticals = [
        ((x0 + x1) / 2, y0, y1) for x0, y0, x1, y1 in rules if x1 - x0 <= 2]

    def has_rule(left, right, middle):
        return any(left < x < right and top < middle < bottom
                   for x, top, bottom in verticals)

    for block in data['blocks']:
        if block.get('type') != 0:
            continue
        lines = []
        for line in block['lines']:
            x0, y0, x1, y1 = line['bbox']
            middle = (y0 + y1) / 2
            cuts = sorted(
                x for x, top, bottom in verticals
                if x0 + 1 < x < x1 - 1 and top < middle < bottom)
            if len(cuts) == 0:
                lines.append(line)
                continue
            # A thin band keeps the characters of other lines out.
            edges = [x0 - 1] + cuts + [x1 + 1]
            for left, right in zip(edges, edges[1:]):
                clipped = page.get_text(
                    'dict', clip=(left, middle - 1, right, middle + 1),
                    flags=flags)
                for clipped_block in clipped['blocks']:
                    for item in clipped_block.get('lines', []):
                        item['cell'] = True
                        lines.append(item)
        for previous, line in zip([None] + lines, lines):
            x0, y0, x1, y1 = line['bbox']
            middle = (y0 + y1) / 2
            if previous is not None and has_rule(
                    previous['bbox'][2] - 1, x0 + 1, middle):
                line['cell'] = True
            size = line['spans'][0]['size'] if line['spans'] else 0
            if any(mark[2] <= x0 + 1 and x0 - mark[2] < 2 * size
                   and mark[1] < middle < mark[3] for mark in marks):
                line['item'] = True
        block['lines'] = lines
    _mark_columns(data, separators)


def _glyph_covers(a, b):
    """Whether two glyph boxes are the same ink, not two letters in a word."""
    aw, bw = a[2] - a[0], b[2] - b[0]
    ah, bh = a[3] - a[1], b[3] - b[1]
    if min(aw, bw, ah, bh) <= 0:
        return False
    overlap_x = min(a[2], b[2]) - max(a[0], b[0])
    overlap_y = min(a[3], b[3]) - max(a[1], b[1])
    return overlap_x >= 0.5 * min(aw, bw) and overlap_y >= 0.5 * min(ah, bh)


def _dedupe_span_chars(spans):
    """Remove a second copy of a letter printed on top of the first.

    Some files fake bold by drawing the word twice, a fraction apart. Both
    copies are real text, so the word arrives doubled (``InInvoic oice``).
    """
    owned = []
    for span in spans:
        for char in span.get('chars') or []:
            if char.get('bbox'):
                owned.append((span, char))
    if not owned:
        return False
    owned.sort(key=lambda item: (item[1]['bbox'][0], item[1]['bbox'][1]))
    kept, dropped = [], False
    for span, char in owned:
        letter, box = char.get('c', ''), char['bbox']
        if letter.strip() and any(
                previous == letter and _glyph_covers(previous_box, box)
                for previous, previous_box, _span, _char in kept):
            dropped = True
            continue
        kept.append((letter, box, span, char))
    if not dropped:
        return False
    by_span = {}
    for _letter, _box, span, char in kept:
        by_span.setdefault(id(span), []).append(char)
    for span in spans:
        if not span.get('chars'):
            continue
        chars = by_span.get(id(span), [])
        chars.sort(key=lambda char: char['bbox'][0])
        span['chars'] = chars
        span['text'] = ''.join(char.get('c', '') for char in chars)
        if chars:
            span['bbox'] = [
                min(char['bbox'][0] for char in chars),
                min(char['bbox'][1] for char in chars),
                max(char['bbox'][2] for char in chars),
                max(char['bbox'][3] for char in chars)]
    return True


def _overlaps_x(a, b):
    return min(a[2], b[2]) - max(a[0], b[0]) > 1


def _collapse_overprint(block):
    """Join the passes of a double-printed line into the letters once."""
    rows = []
    for line in block.get('lines') or []:
        if rows and _same_row(rows[-1][0]['bbox'], line['bbox']):
            rows[-1].append(line)
        else:
            rows.append([line])
    collapsed = []
    for row in rows:
        spans = [span for line in row for span in line.get('spans', [])]
        if not _dedupe_span_chars(spans):
            collapsed.extend(row)
            continue
        pending = sorted(row, key=lambda line: line['bbox'][0])
        clusters = []
        for line in pending:
            if clusters and _overlaps_x(clusters[-1]['bbox'], line['bbox']):
                cluster = clusters[-1]
                cluster['lines'].append(line)
                box, other = cluster['bbox'], line['bbox']
                cluster['bbox'] = [
                    min(box[0], other[0]), min(box[1], other[1]),
                    max(box[2], other[2]), max(box[3], other[3])]
            else:
                clusters.append({'bbox': list(line['bbox']), 'lines': [line]})
        for cluster in clusters:
            lines = cluster['lines']
            kept = [
                span for line in lines for span in line.get('spans', [])
                if span.get('text', '').strip()]
            kept.sort(key=lambda span: span.get('bbox', (0,))[0])
            if len(lines) == 1:
                lines[0]['spans'] = kept
                collapsed.append(lines[0])
                continue
            joined = dict(lines[0])
            joined['spans'] = kept
            joined['bbox'] = cluster['bbox']
            joined['cell'] = any(line.get('cell') for line in lines)
            joined['item'] = any(line.get('item') for line in lines)
            collapsed.append(joined)
    block['lines'] = collapsed


def _ensure_span_text(block):
    """rawdict stores the letters on each glyph and has no span text."""
    for line in block.get('lines') or []:
        for span in line.get('spans') or []:
            if 'text' not in span:
                span['text'] = ''.join(
                    char.get('c', '') for char in span.get('chars') or [])


def get_lines(block):
    """Return the visual lines of a text block, or None if it has rotated
    text. PyMuPDF may split a visual line where words are far apart.
    """
    _ensure_span_text(block)
    _collapse_overprint(block)
    lines = []
    for line in block['lines']:
        if tuple(line['dir']) != (1, 0):
            return None
        text = ''.join(span['text'] for span in line['spans']).strip()
        spans = [span for span in line['spans'] if span['text'].strip()]
        if text == '':
            continue
        item = {
            'bbox': list(line['bbox']), 'text': text, 'spans': spans,
            # Superscripts sit higher than the rest of the line.
            'baseline': max(spans, key=lambda span: span['size'])[
                'origin'][1],
            'item': bool(line.get('item')) or bool(LIST_ITEM.match(text)),
            'cell': bool(line.get('cell'))}
        previous = lines[-1] if len(lines) > 0 else None
        if previous is not None and not line.get('cell') \
                and abs(item['baseline'] - previous['baseline']) < 1 \
                and item['bbox'][0] >= previous['bbox'][2] - 1:
            previous['text'] += ' ' + text
            previous['spans'] += spans
            previous['bbox'][2] = max(previous['bbox'][2], item['bbox'][2])
            previous['bbox'][1] = min(previous['bbox'][1], item['bbox'][1])
            previous['bbox'][3] = max(previous['bbox'][3], item['bbox'][3])
            continue
        lines.append(item)
    return lines


def dominant(spans, key):
    """Return the value of a span property that most characters have."""
    counter = Counter()
    for span in spans:
        counter[key(span)] += len(span['text'].strip())
    return counter.most_common(1)[0][0]


def share(spans, test):
    total = sum(len(span['text'].strip()) for span in spans) or 1
    return sum(
        len(span['text'].strip()) for span in spans if test(span)) / total


def is_bold(spans):
    return share(spans, lambda span: bool(span['flags'] & FLAG_BOLD)
                 or bool(BOLD_FONTS.search(span['font']))) > 0.5


def get_family(font, flags):
    if MONO_FONTS.search(font) or flags & FLAG_MONO:
        return 'monospace'
    if SANS_FONTS.search(font):
        return 'sans-serif'
    if SERIF_FONTS.search(font) or flags & FLAG_SERIF:
        return 'serif'
    return 'sans-serif'


def get_style(spans):
    """Pick the style that most of the characters of a paragraph have."""
    font = dominant(spans, lambda span: span['font'])
    flags = dominant(spans, lambda span: span['flags'])
    return {
        'size': round(dominant(spans, lambda span: span['size']), 1),
        'family': get_family(font, flags),
        'bold': is_bold(spans),
        'italic': share(spans, lambda span: bool(span['flags'] & FLAG_ITALIC)
                        or bool(ITALIC_FONTS.search(span['font']))) > 0.5,
        'color': '#%06x' % dominant(spans, lambda span: span['color']),
    }


def line_size(line):
    return dominant(line['spans'], lambda span: span['size'])


def _beside(a, b):
    """Whether two lines sit on one row with a gap between them."""
    if not _same_row(a['bbox'], b['bbox']):
        return False
    left, right = (a, b) if a['bbox'][0] <= b['bbox'][0] else (b, a)
    return right['bbox'][0] - left['bbox'][2] >= 4


def _is_cell_line(line, lines):
    """A column cell, not a paragraph that merely shares the row with one.

    The footnote under an invoice sits beside the totals. It is the wide
    line; splitting it off translates each of its lines alone and the new
    glyphs overlap.
    """
    if line.get('cell'):
        return True
    neighbors = [
        other for other in lines if other is not line and _beside(line, other)]
    if not neighbors:
        return False
    width = line['bbox'][2] - line['bbox'][0]
    return not any(
        width > max(80, (other['bbox'][2] - other['bbox'][0]) * 3)
        for other in neighbors)


def leader_split(text):
    """A contents row: the words, then dots or a wide gap, then a page number.

    Justified prose can have a wide gap too. A row counts only when that gap
    is the leader in front of a short page number, or when the leader is dots.
    """
    match = _LEADER_ROW.match((text or '').strip())
    if not match or not re.search(r'[^\W\d_]', match.group('label'), re.U):
        return None
    return match.group('label').strip(), match.group('trailer')


def _is_leader_line(line):
    return leader_split(line.get('text', '')) is not None


def split_block(lines):
    """Split the lines of a block into paragraphs. PyMuPDF puts paragraphs
    that are not separated by space, as in most novels, into one block.
    """
    if len(lines) < 2:
        return [lines]
    cells = [_is_cell_line(line, lines) for line in lines]
    leaders = [_is_leader_line(line) for line in lines]
    if not any(cells) and not any(leaders):
        return _split_prose(lines)
    paragraphs, prose = [], []

    def flush():
        if prose:
            paragraphs.extend(_split_prose(prose))
            del prose[:]

    for is_cell, is_leader, line in zip(cells, leaders, lines):
        # A contents row is one entry. Joining it with the next row turns
        # the page numbers and the dots into a paragraph.
        if is_leader or is_cell:
            flush()
            paragraphs.append([line])
        else:
            prose.append(line)
    flush()
    return paragraphs


def _split_prose(lines):
    """Group stacked lines into paragraphs. Every line here is below the last."""
    if len(lines) < 2:
        return [list(lines)]
    sizes = [line_size(line) for line in lines]
    size = median(sizes)
    left = min(line['bbox'][0] for line in lines)
    right = max(line['bbox'][2] for line in lines)
    steps = [b['baseline'] - a['baseline'] for a, b in zip(lines, lines[1:])]
    step = median(steps)
    indented = [line['bbox'][0] - left > 0.8 * size for line in lines]
    # First-line indent: only a few lines start further right. Hanging
    # indent, as in lists: only a few lines start at the left edge.
    # Centered lines start anywhere.
    aligned = len(lines) > 2 and get_alignment(lines, size) != 'center'
    first_line_indent = aligned and sum(indented) * 2 < len(lines) \
        and not any(line['item'] for line in lines)
    hanging_indent = aligned and sum(indented) * 2 > len(lines) \
        and not indented[0]
    paragraphs = [[lines[0]]]
    for i in range(1, len(lines)):
        previous, line = lines[i - 1], lines[i]
        short = previous['bbox'][2] - left < 0.75 * (right - left)
        if line['item'] or abs(sizes[i] - sizes[i - 1]) > 0.5 \
                or is_bold(previous['spans']) != is_bold(line['spans']) \
                or steps[i - 1] > 1.25 * step \
                or (first_line_indent and indented[i]) \
                or (hanging_indent and not indented[i]) \
                or (short and SENTENCE_END.search(previous['text'])):
            paragraphs.append([line])
        else:
            paragraphs[-1].append(line)
    return paragraphs


def get_alignment(lines, size):
    left = min(line['bbox'][0] for line in lines)
    right = max(line['bbox'][2] for line in lines)
    if len(lines) < 2:
        return 'left'
    centers = [(line['bbox'][0] + line['bbox'][2]) / 2 for line in lines]
    widths = [line['bbox'][2] - line['bbox'][0] for line in lines]
    if max(centers) - min(centers) < 0.25 * size \
            and max(widths) - min(widths) > 2:
        return 'center'
    # One long line followed by a short one only proves that the first line
    # is the longer of the two. Its right edge is then this block's own
    # maximum, so a wrapped sentence looks justified and the translation is
    # stretched until the gaps swallow the line. A real column has at least
    # two lines that share the right margin.
    if len(lines) > 2 and right - left >= 16 * size \
            and all(abs(line['bbox'][2] - right) < 1 for line in lines[:-1]):
        return 'justify'
    return 'left'


def snapshot_links(page):
    """Copy the links that can be attached to text, as plain data."""
    links = []
    for link in page.get_links():
        item = {'from': [float(value) for value in link['from']]}
        if link.get('uri'):
            item['uri'] = link['uri']
        elif link.get('page') is not None:
            item['page'] = link['page']
            if link.get('to') is not None:
                item['to'] = [float(value) for value in link['to']]
        else:
            continue
        links.append(item)
    return links


def _span_bold(span):
    return bool(span['flags'] & FLAG_BOLD) \
        or bool(BOLD_FONTS.search(span['font']))


def _span_italic(span):
    return bool(span['flags'] & FLAG_ITALIC) \
        or bool(ITALIC_FONTS.search(span['font']))


def _covering_link(span, links):
    """The link that covers this span, when the span is the linked text.

    A link rectangle drawn over a long line covers only part of it. That
    line stays plain text and the link is put back where it was.
    """
    box = span.get('bbox')
    if not box or not links:
        return None
    width = box[2] - box[0]
    if width <= 0:
        return None
    best, best_width = None, 0
    for link in links:
        rect = link['from']
        overlap_x = min(box[2], rect[2]) - max(box[0], rect[0])
        overlap_y = min(box[3], rect[3]) - max(box[1], rect[1])
        if overlap_x > best_width and overlap_y > 0:
            best, best_width = link, overlap_x
    if best is not None and best_width >= 0.6 * width:
        return best
    return None


def _span_key(span, links):
    link = _covering_link(span, links)
    link_id = None if link is None else links.index(link)
    return ('#%06x' % span['color'], _span_bold(span), _span_italic(span),
            link_id)


def _run_record(key, links):
    color, bold, italic, link_id = key
    record = {'color': color, 'bold': bold, 'italic': italic}
    if link_id is None:
        return record
    link = links[link_id]
    record['from'] = link['from']
    if link.get('uri'):
        record['uri'] = link['uri']
    else:
        record['page'] = link.get('page')
        if link.get('to'):
            record['to'] = link['to']
    return record


# Spaces and dot leaders that only stretch a contents entry to the page number.
_LEADER = re.compile(r'(?:\s*[.·∙•⋅]\s*){3,}|[ \t]{2,}')
# A whole line that is only a label, a leader, and a page number.
_LEADER_ROW = re.compile(
    r'^(?P<label>.+?[^\s.·∙•⋅])'
    r'(?:(?:\s*[.·∙•⋅]\s*){3,}|[ \t]{3,})'
    r'(?P<trailer>\d{1,4})\s*$')
_NOTE = re.compile(r'⟦\^(\d{1,3})⟧')


def _link_text(text):
    """The words of a link, without the padding after them."""
    return _LEADER.sub(' ', text).strip()


def _pieces_of_line(line, links):
    pieces = []
    for span in line['spans']:
        if span['text'] == '':
            continue
        key = _span_key(span, links)
        if pieces and pieces[-1]['key'] == key:
            pieces[-1]['text'] += span['text']
        else:
            pieces.append({'key': key, 'text': span['text']})
    if not pieces:
        return []
    pieces[0]['text'] = pieces[0]['text'].lstrip()
    pieces[-1]['text'] = pieces[-1]['text'].rstrip()
    return [piece for piece in pieces if piece['text']]


def _extend_pieces(pieces, more):
    """Append the next line, joining a hyphenated word across the boundary."""
    if not pieces:
        return more
    if not more:
        return pieces
    head, tail = pieces[-1], more[0]
    if head['text'].endswith('\xad'):
        head['text'] = head['text'][:-1]
        separator = ''
    elif head['text'].endswith('-') and len(head['text']) > 1 \
            and head['text'][-2].isalpha():
        if tail['text'][:1].islower():
            head['text'] = head['text'][:-1]
        separator = ''
    elif CJK.match(head['text'][-1]) and CJK.match(tail['text'][0]):
        separator = ''
    else:
        separator = ' '
    if separator == '' and head['key'] == tail['key']:
        head['text'] += tail['text']
        return pieces + more[1:]
    if separator == ' ' and head['key'] == tail['key']:
        head['text'] += ' ' + tail['text']
        return pieces + more[1:]
    if separator == ' ':
        pieces = pieces + [{'key': None, 'text': ' '}]
    return pieces + more


def mark_paragraph(lines, style, links, ids):
    """Wrap the runs that differ from the paragraph's own style.

    The markers are ``⟦1⟧ words ⟦/1⟧``. The spaces inside the markers keep
    the words detachable; they are removed again when the translation is
    written. ``ids`` is a one-item list used as a counter, so a marker means
    the same run everywhere in the file.
    """
    pieces = []
    for line in lines:
        pieces = _extend_pieces(pieces, _pieces_of_line(line, links))
    base = (style['color'], style['bold'], style['italic'], None)
    runs, parts = {}, []
    for piece in pieces:
        text, key = piece['text'], piece['key']
        if key not in (None, base) and key[3] is not None:
            text = _link_text(text)
        if key in (None, base) or text.strip() == '':
            parts.append(text)
            continue
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        number = ids[0]
        ids[0] += 1
        runs[number] = _run_record(key, links)
        parts.append('%s⟦%d⟧ %s ⟦/%d⟧%s' % (
            lead, number, text.strip(), number, trail))
    return ''.join(parts), runs


def _baseline(span):
    if span.get('origin'):
        return span['origin'][1]
    box = span.get('bbox')
    return box[3] if box else None


def _detach_notes(spans):
    """Pull a raised page-note digit off the words it sits on.

    The digit is smaller than the line and stands above its baseline.
    A number that is part of the sentence, a heading or a formula stays.
    """
    sized = [span for span in spans if span.get('size')]
    if not sized:
        return spans, []
    # The size most of the letters use. One large symbol on the line, such as
    # a formula arrow, must not make every smaller digit look like a note.
    counter = Counter()
    for span in sized:
        counter[span['size']] += max(len(span.get('text', '').strip()), 1)
    body = counter.most_common(1)[0][0]
    bases = [
        _baseline(span) for span in sized
        if span['size'] >= body * 0.92 and _baseline(span) is not None]
    if not bases:
        return spans, []
    baseline = max(bases)
    kept, notes, words, gap = [], [], 0, False
    for span in spans:
        text = span.get('text', '').strip()
        box = span.get('bbox')
        raised = (
            re.fullmatch(r'\d{1,3}', text) is not None
            and span.get('size', body) <= body * 0.92
            and box is not None
            and box[3] < baseline - 0.12 * body)
        if raised:
            notes.append((max(words - 1, 0), text))
            gap = True
            continue
        # The digit sat between two words. The space that followed it is its
        # own span and has already been dropped, so the next word needs one.
        raw = span.get('text', '')
        if (gap and kept and raw and not raw[0].isspace()
                and raw[0].isalnum()):
            span = dict(span)
            span['text'] = ' ' + raw
        gap = False
        kept.append(span)
        words += len([word for word in text.split() if word])
    return (kept or spans), notes


def _trailer_box(spans, trailer):
    """The box of the page number at the end of a contents row."""
    chars = [
        char for span in spans for char in span.get('chars') or []
        if char.get('c') and char.get('bbox') and not char['c'].isspace()]
    if len(chars) < len(trailer):
        return None
    tail = chars[-len(trailer):]
    if ''.join(char['c'] for char in tail) != trailer:
        return None
    boxes = [char['bbox'] for char in tail]
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes)]


def _spans_for_label(spans, label):
    """The spans of a contents row, without the leader or the page number."""
    kept, used = [], 0
    for span in spans:
        if used >= len(label):
            break
        text = span.get('text', '')
        take = 0
        while (take < len(text) and used + take < len(label)
               and text[take] == label[used + take]):
            take += 1
        if take == 0:
            break
        if take == len(text):
            kept.append(dict(span))
        else:
            copied = dict(span)
            copied['text'] = text[:take]
            kept.append(copied)
        used += take
    if kept and used == len(label):
        return kept
    copied = dict(spans[0])
    copied['text'] = label
    return [copied]


def _prepare_line(line):
    """Copy a line, detach note markers, and reduce a contents row to its label."""
    spans = [dict(span) for span in line.get('spans') or []]
    spans, notes = _detach_notes(spans)
    prepared = dict(line)
    prepared['spans'] = spans
    prepared['text'] = ''.join(span.get('text', '') for span in spans).strip()
    found = leader_split(prepared['text'])
    trailer = None
    if found and spans:
        label, number = found
        trailer = {
            'text': number,
            'box': _trailer_box(line.get('spans') or [], number)}
        prepared['spans'] = _spans_for_label(spans, label)
        prepared['text'] = label
    return prepared, notes, trailer


def _ink_box(spans, fallback):
    boxes = []
    for span in spans:
        chars = [
            char['bbox'] for char in span.get('chars') or []
            if char.get('bbox') and str(char.get('c', '')).strip()]
        if chars:
            boxes.extend(chars)
        elif span.get('bbox'):
            boxes.append(span['bbox'])
    if not boxes:
        return list(fallback)
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes)]


def make_paragraph(page, lines, links=(), ids=None):
    prepared, notes, trailer = [], [], None
    for line in lines:
        line, line_notes, line_trailer = _prepare_line(line)
        prepared.append(line)
        notes.extend(line_notes)
        if line_trailer:
            trailer = line_trailer
    lines = prepared
    spans = [span for line in lines for span in line['spans']]
    style = get_style(spans)
    size = style['size']
    left = min(line['bbox'][0] for line in lines)
    if ids is None:
        ids = [1]
    text, runs = mark_paragraph(lines, style, links, ids)
    line_height = 1.2
    if len(lines) > 1:
        steps = [
            b['baseline'] - a['baseline'] for a, b in zip(lines, lines[1:])]
        line_height = min(max(median(steps) / size, 1.0), 2.0)
    indent = lines[0]['bbox'][0] - left
    style.update(
        line_height=round(line_height, 2),
        align='left' if trailer else get_alignment(lines, size),
        indent=round(indent, 1) if len(lines) > 1 and indent > 0.8 * size
        else 0)
    rect = [
        left, min(line['bbox'][1] for line in lines),
        max(line['bbox'][2] for line in lines),
        max(line['bbox'][3] for line in lines)]
    made = {
        'page': page, 'rect': rect, 'frame': list(rect),
        'lines': [list(line['bbox']) for line in lines], 'text': text,
        'style': style, 'runs': runs, 'item': lines[0]['item'],
        'ignored': False,
        'ink': [_ink_box(line['spans'], line['bbox']) for line in lines]}
    if notes:
        made['notes'] = notes
    if trailer:
        # The page number stays put. The label is the only part translated,
        # and the dots are drawn again in whatever gap is left.
        made['leader'] = True
        made['trailer'] = trailer
    return made


def get_page_paragraphs(page, data, rules=(), links=(), ids=None):
    groups, paragraphs = [], []
    if ids is None:
        ids = [1]
    for block in data['blocks']:
        if block.get('type') != 0:
            continue
        lines = get_lines(block)
        if not lines:
            continue
        for index, group in enumerate(split_block(lines)):
            paragraph = make_paragraph(page, group, links, ids)
            if index == 0 and paragraphs \
                    and is_broken_off(paragraphs[-1], paragraph, rules):
                paragraphs.pop()
                group = groups.pop() + group
                paragraph = make_paragraph(page, group, links, ids)
            groups.append(group)
            paragraphs.append(paragraph)
    return paragraphs


def overlaps(a, b, axis):
    """Whether two boxes overlap horizontally (axis 0) or vertically (1)."""
    return a[axis] < b[axis + 2] and a[axis + 2] > b[axis]


def _encloses(outer, inner):
    """Whether a picture covers the paragraph, as a page background does."""
    return outer[0] <= inner[0] + 1 and outer[1] <= inner[1] + 1 \
        and outer[2] >= inner[2] - 1 and outer[3] >= inner[3] - 1


def is_broken_off(head, tail, rules):
    """Whether MuPDF broke a paragraph apart: the rest starts right below,
    in the same column, with no rule between.

    A line box is often taller than the distance to the next baseline, so
    the tail can begin slightly above the head's bottom.
    """
    a, b = head['rect'], tail['rect']
    size = head['style']['size']
    step = b[1] - head['lines'][-1][1]
    if not can_continue(head, tail) or not overlaps(a, b, 0) \
            or not 0.4 * size < step < 2.0 * size:
        return False
    return not any(
        overlaps(rule, b, 0)
        and a[3] - 1 <= (rule[1] + rule[3]) / 2 <= b[1] + 1 for rule in rules)


def set_frames(paragraphs, obstacles, bottom):
    """Give every paragraph a frame: the free space that its translation may
    grow into, to the right for single lines and downwards for all.
    """
    boxes = [paragraph['rect'] for paragraph in paragraphs] + obstacles
    for paragraph in paragraphs:
        if paragraph.get('leader'):
            # A contents row stays on its own line. Growing it would cover
            # the page number or the next entry.
            paragraph['frame'] = list(paragraph['rect'])
            continue
        rect, style = paragraph['rect'], paragraph['style']
        others = [
            box for box in boxes
            if box is not rect and not _encloses(box, rect)]
        x0, y0, x1, y1 = rect
        if len(paragraph['lines']) == 1:
            column = [box for box in others if overlaps(box, rect, 0)]
            column_left = min([x0] + [box[0] for box in column])
            column_right = max([x1] + [box[2] for box in column])
            row = [box for box in others if overlaps(box, rect, 1)]
            right = min([column_right] + [
                box[0] for box in row if box[0] >= x1 - 0.5])
            left = max([column_left] + [
                box[2] for box in row if box[2] <= x0 + 0.5])
            center = (x0 + x1) / 2
            if x0 - column_left > style['size'] \
                    and abs(center - (column_left + column_right) / 2) < 2:
                style['align'] = 'center'
                half = min(center - left, right - center)
                x0, x1 = min(x0, center - half), max(x1, center + half)
            else:
                x1 = max(x1, right)
        # The next line counts even when its box starts above this one.
        # Comparing tops misses it whenever the font box is taller than
        # the line pitch, and the frame is then painted over that line.
        mid = (y0 + y1) / 2
        below = [
            box[1] for box in others
            if box[0] < x1 and box[2] > x0
            and (box[1] + box[3]) / 2 > mid + 0.35 * style['size']]
        limit = min([bottom] + below) - 0.25 * style['size']
        paragraph['frame'] = [x0, y0, x1, max(y1, limit)]


def same_style(a, b):
    return abs(a['size'] - b['size']) <= 0.5 and a['bold'] == b['bold']


def can_continue(head, tail):
    """Whether the text of a paragraph may go on in another one."""
    head_text = plain_text(head['text']).rstrip()
    tail_text = plain_text(tail['text']).lstrip()
    if SENTENCE_END.search(head_text) or not tail_text[:1].islower() \
            or tail.get('item'):
        return False
    return not tail['style']['indent'] \
        and same_style(head['style'], tail['style'])


def is_continuation(head, tail):
    """Whether a paragraph goes on in another column or on another page."""
    return can_continue(head, tail) and (
        tail['page'] != head['page'] or tail['rect'][1] < head['rect'][1])


def chain_paragraphs(paragraphs):
    """Group the paragraphs that continue elsewhere. Running headers,
    footnotes and the like between the two halves are skipped.
    """
    groups, consumed = [], set()
    for i, paragraph in enumerate(paragraphs):
        if i in consumed:
            continue
        group = [paragraph]
        groups.append(group)
        if paragraph['ignored']:
            continue
        skipped = 0
        for j in range(i + 1, len(paragraphs)):
            candidate = paragraphs[j]
            if j in consumed or candidate['ignored']:
                continue
            if is_continuation(group[-1], candidate):
                group.append(candidate)
                consumed.add(j)
                skipped = 0
            elif same_style(group[-1]['style'], candidate['style']):
                break
            else:
                skipped += 1
                if skipped > 2:
                    break
    return groups


def find_cut(text, target, start):
    """A space to cut at. Spaces inside a marker are not spaces to cut at."""
    protected = set()
    for match in MARKER.finditer(text):
        protected.update(range(match.start(), match.end()))
    target = max(start, min(target, len(text)))
    for distance in range(max(target - start, len(text) - target) + 1):
        for position in (target + distance, target - distance):
            if start < position < len(text) and text[position].isspace() \
                    and position not in protected:
                return position
    return target


def _is_hyphen_tail(head, tail):
    """Whether the tail is only the remainder of a word split with a hyphen.

    A longer continuation, such as the rest of a sentence, still gets its
    own part of the translation.
    """
    if not head:
        return False
    previous = plain_text(head.get('text', '')).rstrip()
    fragment = plain_text(tail.get('text', '')).strip().rstrip('.')
    if not previous.endswith('-') and not previous.endswith('\xad'):
        return False
    return bool(fragment) and ' ' not in fragment and len(fragment) <= 12 \
        and fragment[:1].isalpha()


def _fold_hyphen_tails(parts, texts):
    """Keep a one-word hyphen tail with the paragraph it was split from.

    The tail box is only as wide as that fragment. A slice of the translation
    placed there is a stranded word, and a note marker on it belongs at the
    end of the paragraph.
    """
    texts = list(texts)
    for index in range(1, len(parts)):
        if not _is_hyphen_tail(parts[index - 1], parts[index]):
            continue
        texts[index - 1] = (texts[index - 1] + ' ' + texts[index]).strip()
        texts[index] = ''
        moved = [('end', digit) for _place, digit in
                 parts[index].get('notes') or []]
        if moved:
            parts[index - 1].setdefault('notes', []).extend(moved)
            parts[index]['notes'] = []
    return texts


def split_translation(text, weights):
    """Divide a translation among the parts of a paragraph in proportion to
    the lengths of their originals, cutting at spaces where possible.
    """
    total = sum(weights) or 1
    pieces, start, done = [], 0, 0
    for weight in weights[:-1]:
        done += weight
        cut = find_cut(text, round(len(text) * done / total), start)
        pieces.append(text[start:cut].strip())
        start = cut
    pieces.append(text[start:].strip())
    return pieces


class PdfElement(Element):
    """A paragraph of a PDF file. It consists of one or more parts, more
    than one when it goes on in another column or on another page.
    """
    def __init__(self, parts, page_id=None, ignored=False):
        Element.__init__(self, parts, page_id, ignored)
        self.translation = None
        self.runs = {}
        for part in parts:
            for number, run in (part.get('runs') or {}).items():
                self.runs[int(number)] = run

    def get_raw(self):
        return self.get_text()

    def get_text(self):
        text = ''
        for part in self.element:
            text = join_text(text, part['text'])
        return text

    def get_content(self):
        return self.get_text()

    def add_translation(self, translation=None):
        if translation is not None:
            self.translation = translation.strip()

    def get_translation(self):
        return self.translation


def figure_boxes(drawings, images, width, height):
    """Pictures the text may wrap around: images and compact filled drawings.

    A rule, a bullet, a page-sized wash and a wide banner are not pictures.
    A fill that the letters sit on is a background; the caller tells those
    apart by whether the glyphs touch it.
    """
    boxes = []
    for box in images:
        x0, y0, x1, y1 = box
        if x1 - x0 >= 18 and y1 - y0 >= 18:
            boxes.append([float(value) for value in box])
    for drawing in drawings:
        if not drawing.get('fill') or drawing.get('fill_opacity') == 0:
            continue
        rect = drawing.get('rect')
        if not rect:
            continue
        x0, y0, x1, y1 = (float(value) for value in rect)
        wide, tall = x1 - x0, y1 - y0
        if wide < 18 or tall < 18 or wide > 0.85 * width or tall > 0.45 * height:
            continue
        boxes.append([x0, y0, x1, y1])
    return boxes


def _attach_wraps(paragraphs, figures):
    """Remember pictures that sit inside a paragraph but not on its letters."""
    found = []
    for paragraph in paragraphs:
        ink = paragraph.get('ink') or [paragraph['rect']]
        avoid = []
        for figure in figures:
            if not overlaps(figure, paragraph['rect'], 0) \
                    or not overlaps(figure, paragraph['rect'], 1):
                continue
            if any(overlaps(figure, box, 0) and overlaps(figure, box, 1)
                   for box in ink):
                continue
            avoid.append(figure)
        if avoid:
            paragraph['avoid'] = avoid
            found.extend(avoid)
    return found


def get_pdf_elements(path, encoding=None):
    pymupdf = require_pymupdf()
    config = get_config()
    extraction = Extraction(
        [], [], config.get('rule_mode'), config.get('filter_scope'),
        config.get('filter_rules', []), [])
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    pages = []
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise Exception(_('The PDF file is encrypted.'))
        for page in document:
            # rawdict keeps each glyph, so a word printed twice for a fake
            # bold can be reduced to the letters once.
            data = page.get_text('rawdict', flags=flags)
            drawings = page.get_drawings()
            rules, marks = get_rules(drawings)
            # rawdict keeps the letters on the glyphs. The column finder
            # reads span text, so a repeated gap is invisible until the
            # letters are joined.
            for block in data.get('blocks') or []:
                _ensure_span_text(block)
            separators = column_separators(
                drawings, data, page.rect.width, page.rect.height)
            mark_lines(page, data, rules, marks, flags, separators)
            images = [list(info['bbox']) for info in page.get_image_info()]
            obstacles = rules + images
            figures = figure_boxes(
                drawings, images, page.rect.width, page.rect.height)
            pages.append((
                page.number, data, rules, obstacles, figures, page.rect.y1,
                snapshot_links(page)))
    # Translations may grow down to the lowest text line seen in the file.
    margin = min(
        [height - max([block['bbox'][3] for block in data['blocks']
                       if block.get('type') == 0] or [0])
         for _number, data, _rules, _obstacles, _figures, height, _links
         in pages] or [0])
    paragraphs, ids = [], [1]
    for number, data, rules, obstacles, figures, height, links in pages:
        page_paragraphs = get_page_paragraphs(number, data, rules, links, ids)
        wraps = _attach_wraps(page_paragraphs, figures)
        set_frames(page_paragraphs, obstacles + wraps, height - margin)
        paragraphs.extend(page_paragraphs)
    for paragraph in paragraphs:
        element = PdfElement([paragraph])
        paragraph['ignored'] = not extraction.filter_content(element) \
            or element.ignored
    elements = []
    for group in chain_paragraphs(paragraphs):
        elements.append(PdfElement(
            group, 'page-%d' % (group[0]['page'] + 1), group[0]['ignored']))
    if len(elements) == 0:
        log.warning(_(
            'No text was found in the PDF file. Scanned pages need OCR, '
            'which is not supported.'))
    return elements


def font_variants(path):
    """Bold and italic files that sit next to a font and share its name.

    A single font file has no bold or italic face. MuPDF uses these siblings
    when the translation asks for them: ``arial.ttf`` beside ``arialbd.ttf``.
    """
    folder, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    try:
        names = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        names = {}

    def pick(suffixes):
        for suffix in suffixes:
            found = names.get((stem + suffix + ext).lower())
            if found and found.lower() != filename.lower():
                return found
        return None

    return {
        'regular': filename,
        'bold': pick(('bd', 'b', '-bold', ' bold')),
        'italic': pick(('i', '-italic', ' italic', '-oblique')),
        'both': pick(('bi', 'z', '-bolditalic', ' bold italic'))}


def get_font(pymupdf, path):
    """Load a user font, used for all text, in place of the built-in ones."""
    if not path or not os.path.isfile(path):
        return None
    faces = font_variants(path)
    rules = [
        '@font-face {font-family: custom; src: url("%s"); font-weight: %s; '
        'font-style: %s;}' % (name, weight, slant)
        for name, weight, slant in (
            (faces['regular'], 'normal', 'normal'),
            (faces['bold'], 'bold', 'normal'),
            (faces['italic'], 'normal', 'italic'),
            (faces['both'], 'bold', 'italic'))
        if name]
    return {
        'archive': pymupdf.Archive(os.path.dirname(path)),
        'css': '\n'.join(rules) + '\n'}


def get_css(style, scale, font=None):
    rules = [
        'font-family: %s' % (
            'custom' if font else BUILTIN_FONTS[style['family']]),
        'font-size: %.2fpt' % (style['size'] * scale),
        'line-height: %.2f' % style['line_height'],
        'color: %s' % style['color'],
        'text-align: %s' % style['align'],
        'text-indent: %.1fpt' % (style['indent'] * scale),
        'font-weight: %s' % ('bold' if style['bold'] else 'normal'),
        'font-style: %s' % ('italic' if style['italic'] else 'normal'),
        'margin: 0', 'padding: 0']
    return (font['css'] if font else '') + '* {%s}' % '; '.join(rules)


def fits(pymupdf, rect, markup, css, archive=None):
    """Whether the markup fits the rectangle. Story.place() reports a fit
    even when not the first line fits, so the filled height is checked too.
    The margin is the one that Page.insert_htmlbox() adds.
    """
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    story = pymupdf.Story(
        markup, user_css='body {margin:1px;}' + css, archive=archive)
    more, filled = story.place(
        pymupdf.Rect(0, 0, width, height),
        pymupdf.mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW)
    return not more and filled[3] <= height + 0.01


def _marker_tokens(text, runs):
    """Markers split into tokens. A run that starts in another column is
    opened at the beginning, and one that ends in another column is closed
    at the end. Markers the paragraph does not know are dropped.
    """
    known = {int(number) for number in (runs or {})}
    tokens, position = [], 0
    for match in MARKER.finditer(text):
        if match.start() > position:
            tokens.append(('text', text[position:match.start()]))
        kind = 'close' if match.group(1) else 'open'
        # Unknown markers are kept as tokens so the space that separates
        # them from the words is removed, and then nothing is written.
        tokens.append((kind, int(match.group(2))))
        position = match.end()
    if position < len(text):
        tokens.append(('text', text[position:]))
    opened, closed = set(), set()
    for kind, number in tokens:
        if number not in known:
            continue
        if kind == 'open':
            opened.add(number)
        elif kind == 'close':
            closed.add(number)
    tokens = [('open', number) for number in sorted(closed - opened)] \
        + tokens \
        + [('close', number) for number in sorted(opened - closed)]
    trimmed = []
    for index, (kind, value) in enumerate(tokens):
        if kind != 'text':
            trimmed.append((kind, value))
            continue
        previous = tokens[index - 1][0] if index else None
        following = tokens[index + 1][0] if index + 1 < len(tokens) else None
        # The spaces that keep a marker off the words are not part of them.
        if previous == 'open' and value[:1].isspace():
            value = value[1:]
        if following == 'close' and value[-1:].isspace():
            value = value[:-1]
        if value:
            trimmed.append(('text', value))
    return trimmed


def _open_tag(run):
    # A link is not an <a>. insert_htmlbox() would turn it into several
    # overlapping annotations. The link is placed on the written words.
    return '<span style="%s">' % (
        'color:%s; font-weight:%s; font-style:%s' % (
            run['color'], 'bold' if run['bold'] else 'normal',
            'italic' if run['italic'] else 'normal'))


def _escape_with_notes(value):
    """Escape text, and turn a preserved note marker into a superscript."""
    parts, position = [], 0
    for match in _NOTE.finditer(value):
        parts.append(html.escape(value[position:match.start()]))
        parts.append('<sup>%s</sup>' % match.group(1))
        position = match.end()
    parts.append(html.escape(value[position:]))
    return ''.join(parts)


def _with_notes(text, notes):
    """Put note markers back on the words they followed.

    ``end`` hangs the marker on the last word. A word index that the
    translation no longer has hangs it on the last word too.
    """
    if not notes or not text:
        return text
    words = _split_words(text)
    if not words:
        return text
    for place, digit in notes:
        token = '⟦^%s⟧' % digit
        if place == 'end':
            words[-1] += token
        else:
            words[min(int(place), len(words) - 1)] += token
    return ' '.join(words)


def _split_words(text):
    """Words split on spaces that are not inside a marker."""
    protected = set()
    for match in MARKER.finditer(text):
        protected.update(range(match.start(), match.end()))
    words, start = [], 0
    for index, char in enumerate(text):
        if char.isspace() and index not in protected:
            if start < index:
                words.append(text[start:index])
            start = index + 1
    if start < len(text):
        words.append(text[start:])
    return words


def render_markup(text, runs):
    """The translation as HTML, with its markers turned back into style."""
    runs = {int(number): run for number, run in (runs or {}).items()}
    parts, stack = [], []
    for kind, value in _marker_tokens(text, runs):
        if kind == 'text':
            parts.append(_escape_with_notes(value))
            continue
        if kind == 'open' and value in runs:
            stack.append((value, 'span'))
            parts.append(_open_tag(runs[value]))
            continue
        if kind == 'open' or value not in runs:
            continue
        while stack:
            number, tag = stack.pop()
            parts.append('</%s>' % tag)
            if number == value:
                break
    while stack:
        _number, tag = stack.pop()
        parts.append('</%s>' % tag)
    return ''.join(parts)


def _link_phrases(text, runs):
    """The translated words of every run that carries a link."""
    runs = {int(number): run for number, run in (runs or {}).items()}
    active, buffers, order = [], {}, []
    for kind, value in _marker_tokens(text, runs):
        if kind == 'text':
            for number in active:
                buffers[number].append(value)
            continue
        # A merged or altered translation can carry a marker this paragraph
        # never produced. It has no link to place.
        if value not in runs:
            continue
        if kind == 'open':
            active.append(value)
            if value not in buffers:
                buffers[value] = []
                order.append(value)
        else:
            active = [number for number in active if number != value]
    phrases = []
    for number in order:
        run = runs[number]
        if run.get('uri') or run.get('page') is not None:
            phrases.append((run, ''.join(buffers[number])))
    return phrases


def _normalize(text):
    return ' '.join(text.replace('\xa0', ' ').replace('\xad', '').split())


def _span_text(span):
    if span.get('text'):
        return span['text']
    return ''.join(char.get('c', '') for char in span.get('chars') or [])


def _ink_rects(spans, size):
    """Rectangles around the drawn characters.

    Trailing spaces are left out, and a gap wide enough to be a contents
    leader starts a new rectangle instead of underlining the empty space.
    """
    boxes = []
    for span in spans:
        chars = [
            char['bbox'] for char in span.get('chars') or []
            if char.get('c') and not char['c'].isspace()]
        if chars:
            boxes.extend(chars)
        elif _span_text(span).strip():
            boxes.append(span['bbox'])
    if not boxes:
        return []
    gap = 1.25 * (size or 10)
    clusters, current = [], [boxes[0]]
    for box in boxes[1:]:
        # A wrapped line starts further left, or at least a line below.
        # One rectangle around both lines underlines the empty space.
        if box[1] - current[-1][1] > 0.6 * (size or 10) \
                or box[0] - current[-1][2] > gap:
            clusters.append(current)
            current = []
        current.append(box)
    clusters.append(current)

    def union(group):
        return [
            min(box[0] for box in group), min(box[1] for box in group),
            max(box[2] for box in group), max(box[3] for box in group)]

    return [union(group) for group in clusters]


def _phrase_rect(spans, phrase, start):
    """The ink rectangles of ``phrase`` and the index of the next span."""
    target = _normalize(phrase)
    if not target:
        return [], start
    for index in range(start, len(spans)):
        words, count = '', 0
        for span in spans[index:]:
            words = _normalize(words + ' ' + _span_text(span))
            count += 1
            if words == target:
                used = spans[index:index + count]
                size = used[0].get('size') or 10
                return _ink_rects(used, size), index + count
            if not target.startswith(words):
                break
    return [], start


def _link_exists(links, rect, uri=None, page_number=None):
    for link in links:
        if not overlaps(link['from'], rect, 0) \
                or not overlaps(link['from'], rect, 1):
            continue
        if uri and link.get('uri') == uri:
            return True
        if page_number is not None and link.get('page') == page_number:
            return True
    return False


def _link_key(link):
    return tuple(round(float(value), 1) for value in link['from'])


def _link_bars(drawings, links):
    """The drawn underlines of links.

    A link's blue line is a filled rectangle in the page content, not part
    of the annotation. Text redaction leaves it where it was, so it keeps
    the old word's width after the translation gets shorter.
    """
    bars = []
    for drawing in drawings:
        rect = drawing['rect']
        width, height = rect.x1 - rect.x0, rect.y1 - rect.y0
        if width < 8 or height <= 0 or height > 2:
            continue
        fill = drawing.get('fill') or drawing.get('color')
        if not fill:
            continue
        middle = (rect.y0 + rect.y1) / 2
        for link in links:
            box = link['from']
            link_width = box[2] - box[0]
            if link_width <= 0 or not box[1] <= middle <= box[3] + 2:
                continue
            # A rule that only crosses the link is wider than the link.
            if width > link_width * 1.15 + 2:
                continue
            overlap = min(rect.x1, box[2]) - max(rect.x0, box[0])
            if overlap < 0.8 * width:
                continue
            bars.append({
                'rect': [rect.x0, rect.y0, rect.x1, rect.y1],
                'fill': tuple(fill), 'link': _link_key(link)})
            break
    return bars


def _paint_bar(page, rect, fill):
    pymupdf = require_pymupdf()
    page.draw_rect(pymupdf.Rect(rect), fill=fill, color=None, width=0)


def _remove_drawings(page, rects):
    """Remove line art covered by these rectangles, and leave the text."""
    if not rects:
        return
    pymupdf = require_pymupdf()
    for rect in rects:
        page.add_redact_annot(rect)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=pymupdf.PDF_REDACT_TEXT_NONE)


def _bar_on_ink(bar, ink):
    """The same stroke, moved under the letters it now belongs to."""
    height = bar['rect'][3] - bar['rect'][1]
    bottom = ink[3] - 1.15
    return [ink[0], bottom - height, ink[2], bottom]


def _spans_touching(spans, clip):
    """Spans from one page read, limited to the characters inside ``clip``."""
    chosen = []
    for span in spans:
        chars = [
            char for char in span.get('chars') or []
            if char.get('bbox') and overlaps(char['bbox'], clip, 0)
            and overlaps(char['bbox'], clip, 1)]
        if chars:
            copy = dict(span)
            copy['chars'] = chars
            copy.pop('text', None)
            chosen.append(copy)
            continue
        if span.get('text', '').strip() and span.get('bbox') \
                and overlaps(span['bbox'], clip, 0) \
                and overlaps(span['bbox'], clip, 1):
            chosen.append(span)
    return chosen


def _finish_links(page, part, box, text, runs, bars, link_queue):
    """Record a link to place after the page is written, or place it now.

    Placing it now reads the page. A queue lets the whole page be read once.
    """
    if link_queue is None:
        part['replaced_links'] = place_run_links(page, box, text, runs, bars)
        return
    if _link_phrases(text, runs):
        link_queue.append((part, box, text, runs))
    else:
        part['replaced_links'] = set()


def _commit_links(page, queue, bars):
    """Place every queued link from a single reading of the page."""
    if not queue:
        return
    spans = [
        span
        for block in page.get_text('rawdict')['blocks']
        for line in block.get('lines') or []
        for span in line.get('spans') or []]
    live = page.get_links()
    for part, box, text, runs in queue:
        clip = (box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2)
        part['replaced_links'] = place_run_links(
            page, box, text, runs, bars,
            spans=_spans_touching(spans, clip), links=live)


def _coalesce_boxes(boxes):
    """Join stacked line boxes that already cover one column of text.

    A box beside another, or one separated by a picture, stays on its own,
    so the gap between them is not erased.
    """
    pending = [list(box) for box in boxes]
    if len(pending) < 2:
        return pending
    parent = list(range(len(pending)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def stacked(a, b):
        gap = max(a[1], b[1]) - min(a[3], b[3])
        if gap < 0:
            gap = 0
        overlap = min(a[2], b[2]) - max(a[0], b[0])
        narrow = min(a[2] - a[0], b[2] - b[0])
        height = max(a[3] - a[1], b[3] - b[1], 1)
        return gap <= max(4, 0.75 * height) and narrow > 0 \
            and overlap >= 0.55 * narrow

    for i, box in enumerate(pending):
        for j in range(i + 1, len(pending)):
            if stacked(box, pending[j]):
                parent[find(i)] = find(j)
    groups = {}
    for index, box in enumerate(pending):
        groups.setdefault(find(index), []).append(box)
    return [_union_box(group) for group in groups.values()]


def place_run_links(page, box, text, runs, bars=(), spans=None, links=None):
    """Put each linked run's link on the words it was translated into.

    Returns the original rectangles that were successfully replaced.
    ``spans`` and ``links`` skip a fresh reading of the page when the caller
    already has one.
    """
    pymupdf = require_pymupdf()
    if spans is None:
        # Glyphs can extend a fraction outside the box they were written into.
        clip = (box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2)
        spans = [
            span for block in page.get_text('rawdict', clip=clip)['blocks']
            for line in block.get('lines', []) for span in line.get('spans', [])]
    if links is None:
        links = page.get_links()
    start, replaced = 0, set()
    bars_of = {bar['link']: bar for bar in bars}
    for run, phrase in _link_phrases(text, runs):
        rects, start = _phrase_rect(spans, phrase, start)
        if not rects:
            continue
        for rect in rects:
            if run.get('uri') and not _link_exists(links, rect, uri=run['uri']):
                page.insert_link({
                    'kind': pymupdf.LINK_URI, 'from': pymupdf.Rect(rect),
                    'uri': run['uri']})
                links.append({'from': rect, 'uri': run['uri']})
            elif run.get('page') is not None and not _link_exists(
                    links, rect, page_number=run['page']):
                destination = run.get('to') or (rect[0], rect[1])
                page.insert_link({
                    'kind': pymupdf.LINK_GOTO, 'from': pymupdf.Rect(rect),
                    'page': run['page'],
                    'to': pymupdf.Point(destination[0], destination[1])})
                links.append({'from': rect, 'page': run['page']})
        if run.get('from'):
            key = tuple(round(float(v), 1) for v in run['from'])
            replaced.add(key)
            bar = bars_of.get(key)
            if bar:
                for rect in rects:
                    _paint_bar(page, _bar_on_ink(bar, rect), bar['fill'])
    return replaced


def pdf_prompt_extension():
    return (
        'Some words are wrapped in inline markers such as ⟦1⟧ and ⟦/1⟧. '
        'They only record formatting. Translate the words between them. '
        'Keep every marker exactly as it is, immediately beside the same '
        'words. Do not translate, drop, renumber or reorder the markers.'
    )


def _union_box(boxes):
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes)]


def _color_rgb(color):
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _builtin_font(family):
    return {'serif': 'tiro', 'monospace': 'cour'}.get(family, 'helv')


def _ink_after(page, box):
    """The glyph box of the text just written into ``box``."""
    pymupdf = require_pymupdf()
    clip = pymupdf.Rect(box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1)
    boxes = []
    for block in page.get_text('rawdict', clip=clip)['blocks']:
        for line in block.get('lines') or []:
            for span in line.get('spans') or []:
                for char in span.get('chars') or []:
                    if char.get('bbox') and str(char.get('c', '')).strip():
                        boxes.append(char['bbox'])
    if not boxes:
        return None
    return _union_box(boxes)


def _draw_leader(page, gap0, gap1, baseline, style, scale):
    """Draw the dots that take a contents label to its page number."""
    if gap1 - gap0 < style['size']:
        return
    pymupdf = require_pymupdf()
    size = style['size'] * scale
    font = _builtin_font(style.get('family'))
    dot = pymupdf.get_text_length('. ', fontname=font, fontsize=size) or size
    count = int((gap1 - gap0) / dot)
    if count < 2:
        return
    page.insert_text(
        (gap0, baseline), '. ' * count,
        fontsize=size, fontname=font, color=_color_rgb(style['color']))


def _draw_trailer(page, trailer, style, scale):
    """Put the page number back at the end of a contents row."""
    box = trailer.get('box')
    if not box:
        return
    pymupdf = require_pymupdf()
    size = style['size'] * scale
    baseline = box[3] - 0.15 * size
    page.insert_text(
        (box[0], baseline), trailer['text'],
        fontsize=size, fontname=_builtin_font(style.get('family')),
        color=_color_rgb(style['color']))


def _fit_words(pymupdf, box, words, markup_of, css, archive):
    """How many of ``words`` fit in ``box``, from the start."""
    if not words or box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return 0
    low, high = 0, len(words)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(pymupdf, box, markup_of(' '.join(words[:mid])), css, archive):
            low = mid
        else:
            high = mid - 1
    return low


def _write_into(page, box, markup, css, archive, scale_low=1):
    page.insert_htmlbox(
        box, markup, css=css, scale_low=scale_low, archive=archive)


def _line_step(style, scale):
    return style['size'] * scale * style['line_height']


def _flow_slices(
        pymupdf, rect, frame, side, figure, words, markup_of, style, scale,
        font, archive, force=False):
    """One line per step beside the picture, then the rest on the next step.

    A single box as tall as the picture leaves the last line at the top of
    that box and starts the next line under the picture, which opens a hole.
    """
    css = get_css(style, scale, font)
    step = _line_step(style, scale)
    limit = max(frame[3], rect[3])
    slices, index, y, overflow = [], 0, rect[1], False
    for _guard in range(100):
        if index >= len(words) or y > limit - 4:
            break
        if side is None or y >= figure[3] - 0.5:
            box = [rect[0], y, rect[2], max(limit, y + step + 2)]
            rest = ' '.join(words[index:])
            if fits(pymupdf, box, markup_of(rest), css, archive):
                slices.append((box, index, len(words)))
                index = len(words)
            elif force:
                slices.append((box, index, len(words)))
                index = len(words)
                overflow = True
            break
        if y + step <= figure[1] + 0.5:
            box = [rect[0], y, rect[2], y + step + 2]
        else:
            box = [side[0], y, side[1], y + step + 2]
        take = _fit_words(
            pymupdf, box, words[index:], markup_of, css, archive)
        if take == 0:
            break
        slices.append((box, index, index + take))
        index += take
        y += step
    return css, slices, index >= len(words), overflow


def _write_leader(
        page, part, text, markup, style, font, archive, bars, element,
        link_queue=None):
    """Write a contents label, then the dots, then the original page number."""
    rect = part['rect']
    trailer = part.get('trailer') or {}
    box = trailer.get('box')
    size = style['size']
    if box:
        stop = box[0] - 3
    else:
        stop = rect[2] - max(size, len(trailer.get('text', '')) * size * 0.6)
    label = [rect[0], rect[1], max(rect[0] + size, stop), rect[3]]
    used = None
    for scale in FONT_SCALES + FALLBACK_SCALES:
        css = get_css(style, scale, font)
        # scale_low=1 draws only when the line fits, so the trial is the draw.
        spare, _drawn = page.insert_htmlbox(
            label, markup, css=css, scale_low=1, archive=archive)
        if spare >= 0:
            used = scale
            break
    if used is None:
        _write_into(page, label, markup, get_css(style, 0.6, font), archive)
        used = 0.6
    ink = _ink_after(page, label)
    gap0 = (ink[2] + 2) if ink else label[0]
    gap1 = box[0] - 2 if box else label[2]
    baseline = (box[3] - 0.15 * size) if box else rect[3] - 0.2 * size
    _draw_leader(page, gap0, gap1, baseline, style, used)
    _draw_trailer(page, trailer, style, used)
    _finish_links(
        page, part, label, text, getattr(element, 'runs', None), bars,
        link_queue)
    return used


def _beside_box(rect, figure, ink):
    """The free strip beside a picture, on the side the letters already use."""
    right, left = [], []
    for box in ink:
        if not overlaps(box, figure, 1):
            continue
        if box[0] >= figure[2] - 1:
            right.append(box)
        elif box[2] <= figure[0] + 1:
            left.append(box)
    if right:
        return [figure[2] + 2, max(rect[1], figure[1]), rect[2], figure[3]]
    if left:
        return [rect[0], max(rect[1], figure[1]), figure[0] - 2, figure[3]]
    return None


def _write_around(
        page, part, text, style, font, archive, bars, element, runs,
        direction, link_queue=None):
    """Flow a paragraph down the side of a picture, then underneath it.

    The picture is already on the page. The new words use the rectangles
    that the original letters used, so they do not cover it.
    """
    pymupdf = require_pymupdf()
    # The first-line indent is the gap the picture already opened. The side
    # box starts after the picture, so the indent is not added again.
    style = dict(style)
    style['indent'] = 0
    figure = _union_box(part['avoid'])
    rect, frame = part['rect'], part['frame']
    beside = _beside_box(rect, figure, part.get('ink') or [rect])
    side = (beside[0], beside[2]) if beside else None
    words = _split_words(text)

    def markup_of(piece):
        return '<div%s>%s</div>' % (direction, render_markup(piece, runs))

    chosen = None
    for scale in FONT_SCALES + FALLBACK_SCALES:
        css, slices, done, _overflow = _flow_slices(
            pymupdf, rect, frame, side, figure, words, markup_of, style,
            scale, font, archive)
        if done and slices:
            chosen = (scale, css, slices, 1)
            break
    if chosen is None:
        scale = FALLBACK_SCALES[-1]
        css, slices, _done, overflow = _flow_slices(
            pymupdf, rect, frame, side, figure, words, markup_of, style,
            scale, font, archive, force=True)
        chosen = (scale, css, slices, 0 if overflow else 1)
    scale, css, slices, rest_scale = chosen
    placed = []
    for box, start, end in slices:
        low = rest_scale if end == len(words) else 1
        _write_into(
            page, box, markup_of(' '.join(words[start:end])), css, archive,
            scale_low=low)
        placed.append(box)
    box = _union_box(placed) if placed else rect
    _finish_links(page, part, box, text, runs, bars, link_queue)
    return scale


def write_part(page, part, text, element, font=None, bars=(), link_queue=None):
    """Write a translation into the area of its original. A smaller font is
    used only when it fits neither the original area nor the free space.
    Return the scale of the font.
    """
    # Keep the original color. The colored bands stay on the page, so the
    # translation-color setting would turn white text black on top of them.
    style = dict(part['style'])
    text = _with_notes(text, part.get('notes'))
    direction = ' dir="rtl"' if element.target_direction == 'rtl' else ''
    runs = getattr(element, 'runs', None)
    # Not <p>: MuPDF keeps the default margins of <p> whatever the CSS says.
    markup = '<div%s>%s</div>' % (direction, render_markup(text, runs))
    archive = font['archive'] if font else None
    if part.get('leader'):
        return _write_leader(
            page, part, text, markup, style, font, archive, bars, element,
            link_queue)
    if part.get('avoid'):
        return _write_around(
            page, part, text, style, font, archive, bars, element, runs,
            direction, link_queue)
    # The original lines are as tall as their glyphs, while CSS puts half of
    # the line spacing above and below them. Make up for that, and for the
    # margin that Page.insert_htmlbox() adds.
    glyphs = part['lines'][0][3] - part['lines'][0][1]
    pad = max(0, style['size'] * style['line_height'] - glyphs) / 2 + 1
    rect, frame = ([x0 - 1, y0 - pad, x1 + 1, y1 + pad]
                   for x0, y0, x1, y1 in (part['rect'], part['frame']))
    attempts = [(rect, 1.0)]
    attempts += [(frame, scale) for scale in FONT_SCALES + FALLBACK_SCALES]
    used = None
    for box, scale in attempts:
        css = get_css(style, scale, font)
        # One placement both decides and draws. A rejected size leaves the
        # page untouched, so the next size can still be tried.
        spare, _drawn = page.insert_htmlbox(
            box, markup, css=css, scale_low=1, archive=archive)
        if spare >= 0:
            used = (box, scale)
            break
    if used is None:
        log.warning('The translation does not fit its area: %s' % text[:80])
        _spare_height, scale = page.insert_htmlbox(
            frame, markup, css=get_css(style, 1.0, font), scale_low=0,
            archive=archive)
        used = (frame, scale)
    _finish_links(page, part, used[0], text, runs, bars, link_queue)
    return used[1]


def restore_links(page, links, replaced=()):
    """Put back the links that redaction removed.

    ``replaced`` holds the links that were written again on the translated
    words. Those are not put back at their old position.
    """
    def key(link):
        return tuple(round(float(value), 1) for value in link['from'])
    remaining = {key(link) for link in page.get_links()}
    for link in links:
        if key(link) in replaced or key(link) in remaining:
            continue
        page.insert_link(
            {k: v for k, v in link.items() if k not in ('xref', 'id')})


def interleave_pages(pymupdf, input_path, translated, position):
    """Put every translated page next to its original page."""
    original = pymupdf.open(input_path)
    document = pymupdf.open()
    translation_first = position in ('above', 'left')
    for number in range(translated.page_count):
        sources = [translated, original] if translation_first \
            else [original, translated]
        for source in sources:
            document.insert_pdf(source, from_page=number, to_page=number)
    offset = 0 if translation_first else 1
    document.set_toc([
        [level, title, page * 2 - offset]
        for level, title, page in original.get_toc() if page > 0])
    document.set_metadata(original.metadata)
    original.close()
    translated.close()
    return document


def compose_pdf(
        input_path, output_path, elements, position='below', font_path=None):
    pymupdf = require_pymupdf()
    document = pymupdf.open(input_path)
    pages = {}
    for element in elements:
        translation = element.get_translation()
        if element.ignored or not translation:
            continue
        texts = _fold_hyphen_tails(element.element, split_translation(
            translation, [len(part['text']) for part in element.element]))
        for part, text in zip(element.element, texts):
            pages.setdefault(part['page'], []).append((part, text, element))
    font = get_font(pymupdf, font_path)
    scales = []
    for number, items in sorted(pages.items()):
        page = document[number]
        links = page.get_links()
        line_boxes = [
            bbox for part, _text, _element in items for bbox in part['lines']]
        # A page with no links has nothing to underline. Skip its drawings.
        bars = []
        if links:
            bars = [
                bar for bar in _link_bars(page.get_drawings(), links)
                if any(overlaps(bar['rect'], bbox, 0)
                       and overlaps(bar['rect'], bbox, 1)
                       for bbox in line_boxes)]
        # Before the new glyphs are drawn: their boxes cover the old bar.
        _remove_drawings(page, [bar['rect'] for bar in bars])
        for part, _text, _element in items:
            for bbox in _coalesce_boxes(part['lines']):
                page.add_redact_annot(bbox)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        queue = []
        for part, text, element in items:
            if text:
                scales.append(write_part(
                    page, part, text, element, font, bars, queue))
        _commit_links(page, queue, bars)
        replaced = set()
        for part, _text, _element in items:
            replaced.update(part.get('replaced_links') or ())
        for bar in bars:
            if bar['link'] not in replaced:
                _paint_bar(page, bar['rect'], bar['fill'])
        # After writing, so a link that landed on the new words is not also
        # put back on the old ones.
        restore_links(page, links, replaced)
    if position != 'only':
        document = interleave_pages(pymupdf, input_path, document, position)
    # Every inserted text box embeds its own copy of the fonts.
    document.subset_fonts()
    document.save(output_path, garbage=4, deflate=True)
    document.close()
    return Counter(round(scale, 1) for scale in scales)
