import re
import json

from .utils import log, uid, dummy, open_file
from .config import get_config
from .cache import Paragraph
from .element import SrtElement, ElementHandler, SRT_TIMING_RE as TIMING_RE


SRT_CACHE_VERSION = 'srt-v2'

INDEX_RE = re.compile(r'^\s*\d+\s*$')


def parse_srt(content: str) -> list[SrtElement]:
    """Split subtitles by their timing lines instead of by blank lines, so
    that missing indexes, extra blank lines, whitespace-only lines and empty
    cues cannot leak an index or a timing line into the translatable text.
    """
    content = content.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    lines = content.split('\n')
    timing_rows = [row for row, line in enumerate(lines) if TIMING_RE.match(line)]
    if not timing_rows:
        raise ValueError('No valid SRT timing line was found.')
    elements = []
    for number, row in enumerate(timing_rows):
        is_last = number + 1 == len(timing_rows)
        end = len(lines) if is_last else timing_rows[number + 1]
        body = lines[row + 1:end]
        # The index of the next cue sits right before its timing line and
        # after a blank line (or directly after the timing line).
        if not is_last and body and INDEX_RE.match(body[-1]) and (
                len(body) == 1 or body[-2].strip() == ''):
            body = body[:-1]
        index = ''
        if row > 0 and INDEX_RE.match(lines[row - 1]) and (
                row == 1 or lines[row - 2].strip() == ''
                or TIMING_RE.match(lines[row - 2])):
            index = lines[row - 1].strip()
        text = '\n'.join(line.rstrip() for line in body if line.strip())
        elements.append(SrtElement(
            [index or str(number + 1), lines[row].strip(), text]))
    return elements


def get_srt_elements(path, encoding):
    return parse_srt(open_file(path, encoding))


def compose_srt(elements) -> str:
    return '\n\n'.join(element.get_translation() for element in elements) \
        + '\n'


def cue_marker(placeholder, cue_id: int) -> str:
    return placeholder[0].format('c%d' % cue_id)


def cue_marker_pattern(placeholder):
    return re.compile(placeholder[1].format(r'c\s*(?P<cue>\d+)'))


def strip_cue_markers(text, placeholder):
    return cue_marker_pattern(placeholder).sub('', text)


def parse_marked_text(text, placeholder, expected) -> dict[int, str] | None:
    """Map a marked text back to its cues. Return None unless every expected
    cue marker appears exactly once, in order, with non-empty text, and
    nothing precedes the first marker.
    """
    if text is None:
        return None
    expected = list(expected)
    matches = list(cue_marker_pattern(placeholder).finditer(text))
    if not matches:
        if len(expected) == 1 and text.strip():
            return {expected[0]: text.strip()}
        return None
    if text[:matches[0].start()].strip():
        return None
    found = [int(match.group('cue')) for match in matches]
    if found != expected:
        return None
    result = {}
    for number, match in enumerate(matches):
        end = len(text) if number + 1 == len(matches) \
            else matches[number + 1].start()
        chunk = text[match.end():end].strip()
        if not chunk:
            return None
        result[found[number]] = chunk
    return result


def srt_prompt_extension(placeholder):
    return (
        'The content is a sequence of subtitle cues. Each cue begins with a '
        'marker such as %s. Translate the text of every cue and keep each '
        'marker exactly once, unchanged and in the original order, at the '
        'beginning of its translation. Never merge, split, drop or reorder '
        'cues, even when a sentence continues into the next cue. Do not add '
        'blank lines.' % cue_marker(placeholder, 0))


class SrtElementHandler(ElementHandler):
    """Translate one cue per request and map translations by cue id."""

    def __init__(self, placeholder, separator, position):
        ElementHandler.__init__(self, placeholder, separator, position)
        self.cues: list[SrtElement] = []

    def _setup_element(self, element):
        element.set_placeholder(self.placeholder)
        element.set_position(self.position)
        element.set_target_direction(self.target_direction)
        element.set_translation_lang(self.translation_lang)

    def _prepare_cues(self, elements):
        self.cues = list(elements)
        for element in self.cues:
            self._setup_element(element)
            if element.get_text().strip() == '':
                element.set_ignored(True)

    def prepare_original(self, elements):
        self._prepare_cues(elements)
        for cue_id, element in enumerate(self.cues):
            content = element.get_content()
            self.originals.append((
                cue_id, uid('%s%s' % (cue_id, content)), element.get_raw(),
                content, element.ignored, None, None))
        return self.originals

    def add_translations(self, paragraphs, overrides=None):
        for paragraph in paragraphs:
            if paragraph.translation is None:
                continue
            element = self.cues[paragraph.id]
            if not element.ignored:
                element.add_translation(paragraph.translation)


class SrtElementHandlerMerge(SrtElementHandler):
    """Send several cues per request, each prefixed with a numbered marker,
    and only accept a translation whose markers all survived.
    """

    def prepare_original(self, elements):
        self._prepare_cues(elements)
        groups: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        size = 0
        for cue_id, element in enumerate(self.cues):
            if element.ignored:
                continue
            unit = '%s %s' % (
                cue_marker(self.placeholder, cue_id), element.get_content())
            if current and size + len(unit) + 1 > self.merge_length:
                groups.append(current)
                current, size = [], 0
            current.append((cue_id, unit))
            size += len(unit) + 1
        if current:
            groups.append(current)
        for group_id, group in enumerate(groups):
            cue_ids = [cue_id for cue_id, _ in group]
            text = '\n'.join(unit for _, unit in group)
            raw = '\n\n'.join(self.cues[cue_id].get_raw() for cue_id in cue_ids)
            self.originals.append((
                group_id, uid('%s%s' % (group_id, text)), raw, text, False,
                json.dumps({'cues': cue_ids}), None))
        return self.originals

    def add_translations(self, paragraphs, overrides=None):
        overrides = overrides or {}
        skipped = 0
        for paragraph in paragraphs:
            cue_ids = paragraph.get_attributes().get('cues') or []
            mapping = parse_marked_text(
                paragraph.translation, self.placeholder, cue_ids)
            if mapping is None:
                mapping = {
                    cue_id: overrides[cue_id] for cue_id in cue_ids
                    if cue_id in overrides}
                skipped += len(cue_ids) - len(mapping)
            for cue_id, text in mapping.items():
                self.cues[cue_id].add_translation(text)
        if skipped > 0:
            log.warn(
                '%d subtitle cue(s) were left untranslated because their '
                'markers could not be matched.' % skipped)


def get_srt_element_handler(placeholder, separator, direction):
    config = get_config()
    position_alias = {'before': 'above', 'after': 'below'}
    position = config.get('translation_position') or 'below'
    position = position_alias.get(position) or position
    handler: SrtElementHandler
    if config.get('merge_enabled'):
        handler = SrtElementHandlerMerge(placeholder, separator, position)
        handler.set_merge_length(config.get('merge_length'))
    else:
        handler = SrtElementHandler(placeholder, separator, position)
    handler.set_target_direction(direction)
    return handler


def repair_srt_groups(translation, paragraphs, placeholder, save=dummy):
    """Re-translate, one request per cue, every merged group whose markers
    did not survive. Complete groups are rebuilt and saved; the returned
    mapping also covers cues of groups that could only be partially repaired.
    """
    if translation.cancel_request() or translation.need_stop():
        return {}
    broken = []
    for paragraph in paragraphs:
        cue_ids = paragraph.get_attributes().get('cues')
        if cue_ids is not None and parse_marked_text(
                paragraph.translation, placeholder, cue_ids) is None:
            broken.append(paragraph)
    if not broken:
        return {}
    units = []
    for paragraph in broken:
        cue_ids = paragraph.get_attributes()['cues']
        originals = parse_marked_text(paragraph.original, placeholder, cue_ids)
        if originals is None:
            continue
        for cue_id in cue_ids:
            text = originals[cue_id]
            units.append(Paragraph(
                cue_id, uid('repair', str(cue_id), text), text, text,
                attributes=json.dumps({'cues': [cue_id]})))
    if not units:
        return {}
    log.info('Re-translating %d cue(s) of %d misaligned group(s) one by one.'
             % (len(units), len(broken)))
    fallback = translation.fork()
    fallback.handle(units)
    repaired = {}
    for unit in units:
        mapping = parse_marked_text(unit.translation, placeholder, [unit.id])
        if mapping is not None:
            repaired.update(mapping)
    for paragraph in broken:
        cue_ids = paragraph.get_attributes()['cues']
        if all(cue_id in repaired for cue_id in cue_ids):
            paragraph.translation = '\n'.join(
                '%s %s' % (cue_marker(placeholder, cue_id), repaired[cue_id])
                for cue_id in cue_ids)
            paragraph.error = None
            save(paragraph)
    return repaired
