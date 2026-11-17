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


PDF_CACHE_VERSION = 'pdf-layout-v1'
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


def join_text(head, tail):
    """Join two lines of a paragraph, undoing hyphenation at the line end."""
    if head == '' or tail == '':
        return head or tail
    if head.endswith('\xad'):
        return head[:-1] + tail
    if head.endswith('-') and len(head) > 1 and head[-2].isalpha():
        return head[:-1] + tail if tail[0].islower() else head + tail
    if CJK.match(head[-1]) and CJK.match(tail[0]):
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


def mark_lines(page, data, rules, marks, flags):
    """Prepare the lines of a page with what only the drawings tell: a line
    that crosses the border of table cells is cut, a line that is a table
    cell is marked as such, and so is a line after a drawn list bullet.
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


def get_lines(block):
    """Return the visual lines of a text block, or None if it has rotated
    text. PyMuPDF may split a visual line where words are far apart.
    """
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
            'item': bool(line.get('item')) or bool(LIST_ITEM.match(text))}
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


def split_block(lines):
    """Split the lines of a block into paragraphs. PyMuPDF puts paragraphs
    that are not separated by space, as in most novels, into one block.
    """
    if len(lines) < 2:
        return [lines]
    # Lines still side by side are table cells, translated one by one.
    if any(abs(b['baseline'] - a['baseline']) < 1
           for a, b in zip(lines, lines[1:])):
        return [[line] for line in lines]
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
    # The last line is full when the paragraph goes on in the next column.
    if all(abs(line['bbox'][2] - right) < 1 for line in lines[:-1]) \
            and (len(lines) > 2 or lines[-1]['bbox'][2] < right - size):
        return 'justify'
    return 'left'


def make_paragraph(page, lines):
    spans = [span for line in lines for span in line['spans']]
    style = get_style(spans)
    size = style['size']
    left = min(line['bbox'][0] for line in lines)
    text = ''
    for line in lines:
        text = join_text(text, line['text'])
    line_height = 1.2
    if len(lines) > 1:
        steps = [
            b['baseline'] - a['baseline'] for a, b in zip(lines, lines[1:])]
        line_height = min(max(median(steps) / size, 1.0), 2.0)
    indent = lines[0]['bbox'][0] - left
    style.update(
        line_height=round(line_height, 2), align=get_alignment(lines, size),
        indent=round(indent, 1) if len(lines) > 1 and indent > 0.8 * size
        else 0)
    rect = [
        left, min(line['bbox'][1] for line in lines),
        max(line['bbox'][2] for line in lines),
        max(line['bbox'][3] for line in lines)]
    return {
        'page': page, 'rect': rect, 'frame': list(rect),
        'lines': [line['bbox'] for line in lines], 'text': text,
        'style': style, 'item': lines[0]['item'], 'ignored': False}


def get_page_paragraphs(page, data, rules=()):
    groups, paragraphs = [], []
    for block in data['blocks']:
        if block.get('type') != 0:
            continue
        lines = get_lines(block)
        if not lines:
            continue
        for index, group in enumerate(split_block(lines)):
            paragraph = make_paragraph(page, group)
            if index == 0 and paragraphs \
                    and is_broken_off(paragraphs[-1], paragraph, rules):
                paragraphs.pop()
                group = groups.pop() + group
                paragraph = make_paragraph(page, group)
            groups.append(group)
            paragraphs.append(paragraph)
    return paragraphs


def overlaps(a, b, axis):
    """Whether two boxes overlap horizontally (axis 0) or vertically (1)."""
    return a[axis] < b[axis + 2] and a[axis + 2] > b[axis]


def is_broken_off(head, tail, rules):
    """Whether MuPDF broke a paragraph apart: the rest starts right below,
    in the same column, with no rule between.
    """
    a, b = head['rect'], tail['rect']
    if not can_continue(head, tail) or not overlaps(a, b, 0) \
            or not -1 < b[1] - a[3] < 0.5 * head['style']['size']:
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
        rect, style = paragraph['rect'], paragraph['style']
        others = [box for box in boxes if box is not rect]
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
        below = [
            box[1] for box in others
            if box[0] < x1 and box[2] > x0 and box[1] >= y1 - 0.5]
        limit = min([bottom] + below) - 0.25 * style['size']
        paragraph['frame'] = [x0, y0, x1, max(y1, limit)]


def same_style(a, b):
    return abs(a['size'] - b['size']) <= 0.5 and a['bold'] == b['bold']


def can_continue(head, tail):
    """Whether the text of a paragraph may go on in another one."""
    if SENTENCE_END.search(head['text']) or not tail['text'][:1].islower() \
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
    target = max(start, min(target, len(text)))
    for distance in range(max(target - start, len(text) - target) + 1):
        for position in (target + distance, target - distance):
            if start < position < len(text) and text[position].isspace():
                return position
    return target


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
            data = page.get_text('dict', flags=flags)
            rules, marks = get_rules(page.get_drawings())
            mark_lines(page, data, rules, marks, flags)
            obstacles = rules + [
                list(info['bbox']) for info in page.get_image_info()]
            pages.append((page.number, data, rules, obstacles, page.rect.y1))
    # Translations may grow down to the lowest text line seen in the file.
    margin = min(
        [height - max([block['bbox'][3] for block in data['blocks']
                       if block.get('type') == 0] or [0])
         for _number, data, _rules, _obstacles, height in pages] or [0])
    paragraphs = []
    for number, data, rules, obstacles, height in pages:
        page_paragraphs = get_page_paragraphs(number, data, rules)
        set_frames(page_paragraphs, obstacles, height - margin)
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


def get_font(pymupdf, path):
    """Load a user font, used for all text, in place of the built-in ones."""
    if not path or not os.path.isfile(path):
        return None
    return {
        'archive': pymupdf.Archive(os.path.dirname(path)),
        'css': '@font-face {font-family: custom; src: url("%s");}\n'
        % os.path.basename(path)}


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


def write_part(page, part, text, element, font=None):
    """Write a translation into the area of its original. A smaller font is
    used only when it fits neither the original area nor the free space.
    Return the scale of the font.
    """
    pymupdf = require_pymupdf()
    style = dict(part['style'])
    if element.translation_color:
        style['color'] = element.translation_color
    direction = ' dir="rtl"' if element.target_direction == 'rtl' else ''
    # Not <p>: MuPDF keeps the default margins of <p> whatever the CSS says.
    markup = '<div%s>%s</div>' % (direction, html.escape(text))
    archive = font['archive'] if font else None
    # The original lines are as tall as their glyphs, while CSS puts half of
    # the line spacing above and below them. Make up for that, and for the
    # margin that Page.insert_htmlbox() adds.
    glyphs = part['lines'][0][3] - part['lines'][0][1]
    pad = max(0, style['size'] * style['line_height'] - glyphs) / 2 + 1
    rect, frame = ([x0 - 1, y0 - pad, x1 + 1, y1 + pad]
                   for x0, y0, x1, y1 in (part['rect'], part['frame']))
    attempts = [(rect, 1.0)]
    attempts += [(frame, scale) for scale in FONT_SCALES + FALLBACK_SCALES]
    for box, scale in attempts:
        css = get_css(style, scale, font)
        if fits(pymupdf, box, markup, css, archive):
            page.insert_htmlbox(
                box, markup, css=css, scale_low=1, archive=archive)
            return scale
    log.warning('The translation does not fit its area: %s' % text[:80])
    _spare_height, scale = page.insert_htmlbox(
        frame, markup, css=get_css(style, 1.0, font), scale_low=0,
        archive=archive)
    return scale


def restore_links(page, links):
    """Redaction removes the links that overlap the erased text."""
    def key(link):
        return tuple(round(value, 1) for value in link['from'])
    remaining = {key(link) for link in page.get_links()}
    for link in links:
        if key(link) not in remaining:
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
        texts = split_translation(
            translation, [len(part['text']) for part in element.element])
        for part, text in zip(element.element, texts):
            pages.setdefault(part['page'], []).append((part, text, element))
    font = get_font(pymupdf, font_path)
    scales = []
    for number, items in sorted(pages.items()):
        page = document[number]
        links = page.get_links()
        for part, _text, _element in items:
            for bbox in part['lines']:
                page.add_redact_annot(bbox)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        restore_links(page, links)
        for part, text, element in items:
            if text:
                scales.append(write_part(page, part, text, element, font))
    if position != 'only':
        document = interleave_pages(pymupdf, input_path, document, position)
    # Every inserted text box embeds its own copy of the fonts.
    document.subset_fonts()
    document.save(output_path, garbage=4, deflate=True)
    document.close()
    return Counter(round(scale, 1) for scale in scales)
