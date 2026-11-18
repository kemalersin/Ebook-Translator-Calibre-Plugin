import io
import os
import sys
import json
import shutil
import hashlib
import zipfile
import tempfile
import unittest
from unittest.mock import patch, Mock

from ...lib.cache import Paragraph, get_cache_id
from ...lib.utils import uid
from ...lib.element import ElementHandler
from ...lib.conversion import extract_item, convert_item
from ...lib.pdf import (
    PDF_CACHE_VERSION, FALLBACK_SCALES, fits, join_text, get_lines,
    split_block, get_page_paragraphs,
    get_style, get_family, make_paragraph, set_frames, is_continuation,
    chain_paragraphs, split_translation, find_wheel, install_pymupdf,
    get_installed_version, load_pymupdf, is_pdf_layout, PdfElement,
    get_pdf_elements, compose_pdf, write_part, get_css, get_rules,
    mark_lines, column_separators, LIST_ITEM, render_markup, font_variants,
    pdf_prompt_extension, _link_phrases, _ink_rects, _fold_hyphen_tails,
    _attach_wraps, _ensure_span_text, _coalesce_boxes)
from ...engines.base import Base


module_name = 'calibre_plugins.ebook_translator.lib.pdf'
pymupdf = load_pymupdf()


def span(text, size=10, font='Times-Roman', flags=4, color=0, x=0, y=0):
    return {
        'text': text, 'size': size, 'font': font, 'flags': flags,
        'color': color, 'origin': (x, y)}


def line(text, x0, baseline, x1, size=10, **kwargs):
    return {
        'dir': (1, 0), 'bbox': (x0, baseline - size, x1, baseline + size / 4),
        'spans': [span(text, size, x=x0, y=baseline, **kwargs)]}


def block(*lines):
    return {'type': 0, 'lines': list(lines)}


def texts(paragraphs):
    return [' '.join(line['text'] for line in group) for group in paragraphs]


def paragraph(text, page=0, rect=(40, 100, 380, 140), size=10, bold=False,
              indent=0, ignored=False, lines=3):
    return {
        'page': page, 'rect': list(rect), 'frame': list(rect),
        'lines': [list(rect)] * lines, 'text': text, 'ignored': ignored,
        'style': {
            'size': size, 'bold': bold, 'indent': indent, 'align': 'left',
            'family': 'serif', 'italic': False, 'color': '#000000',
            'line_height': 1.2}}


def config(**values):
    return Mock(get=lambda key, default=None: values.get(key, default))


class TestJoinText(unittest.TestCase):
    def test_space(self):
        self.assertEqual('a b', join_text('a', 'b'))

    def test_hyphenated_word(self):
        self.assertEqual('capstan', join_text('cap-', 'stan'))

    def test_compound_name(self):
        self.assertEqual('Jean-Paul', join_text('Jean-', 'Paul'))

    def test_dash_after_number(self):
        self.assertEqual('1914- 1918', join_text('1914-', '1918'))

    def test_soft_hyphen(self):
        self.assertEqual('example', join_text('exam\xad', 'ple'))

    def test_cjk(self):
        self.assertEqual('今日は晴れ', join_text('今日は', '晴れ'))

    def test_empty(self):
        self.assertEqual('a', join_text('', 'a'))
        self.assertEqual('a', join_text('a', ''))


class TestMarkers(unittest.TestCase):
    def paragraph(self, spans, links=()):
        return make_paragraph(0, [{
            'dir': (1, 0), 'bbox': (40, 80, 300, 100),
            'baseline': 96, 'text': ''.join(item['text'] for item in spans),
            'item': False, 'spans': spans}], links)

    def test_plain_paragraph_has_no_marker(self):
        made = self.paragraph([span('Please share our books.')])
        self.assertEqual('Please share our books.', made['text'])
        self.assertEqual({}, made['runs'])

    def test_colored_bold_words(self):
        made = self.paragraph([
            span('Please share these '),
            span('books', color=0x008000, flags=20, font='Times-Bold'),
            span(' with your friends now.')])
        self.assertEqual(
            'Please share these ⟦1⟧ books ⟦/1⟧ with your friends now.',
            made['text'])
        self.assertEqual('#008000', made['runs'][1]['color'])
        self.assertTrue(made['runs'][1]['bold'])
        self.assertFalse(made['runs'][1]['italic'])

    def test_link_drops_leader_padding(self):
        linked = span('Chapter 1 . . . . . 12')
        linked['bbox'] = (40, 80, 400, 96)
        made = self.paragraph([linked], links=[{
            'from': [40, 80, 400, 96], 'page': 3}])
        self.assertEqual('⟦1⟧ Chapter 1 ⟦/1⟧', made['text'])
        self.assertEqual('12', made['trailer']['text'])
        self.assertEqual(3, made['runs'][1]['page'])
        padded = span('Dedication          ')
        padded['bbox'] = (40, 80, 400, 96)
        made = self.paragraph([padded], links=[{
            'from': [40, 80, 400, 96], 'uri': 'https://example.com/a'}])
        self.assertEqual('⟦1⟧ Dedication ⟦/1⟧', made['text'])

    def test_link_ink_ignores_trailing_space(self):
        spans = [{
            'text': 'İthaf   ', 'size': 12, 'bbox': (40, 80, 200, 94),
            'chars': [
                {'c': 'İ', 'bbox': (40, 80, 48, 94)},
                {'c': 'f', 'bbox': (52, 80, 58, 94)},
                {'c': ' ', 'bbox': (58, 80, 200, 94)}]}]
        self.assertEqual([[40, 80, 58, 94]], _ink_rects(spans, 12))
        spans[0]['chars'] = [
            {'c': 'A', 'bbox': (40, 80, 50, 94)},
            {'c': ' ', 'bbox': (50, 80, 140, 94)},
            {'c': 'B', 'bbox': (140, 80, 150, 94)}]
        self.assertEqual(
            [[40, 80, 50, 94], [140, 80, 150, 94]], _ink_rects(spans, 12))
        spans[0]['chars'] = [
            {'c': 'E', 'bbox': (78, 190, 88, 205)},
            {'c': 'r', 'bbox': (170, 190, 180, 205)},
            {'c': 'B', 'bbox': (78, 206, 88, 221)}]
        self.assertEqual(
            [[78, 190, 88, 205], [170, 190, 180, 205], [78, 206, 88, 221]],
            _ink_rects(spans, 11))

    def test_link_on_its_own_words(self):
        linked = span('share', color=0x008000, flags=4)
        linked['bbox'] = (80, 84, 120, 98)
        plain = span('Please ')
        plain['bbox'] = (40, 84, 80, 98)
        made = self.paragraph([plain, linked], links=[{
            'from': [80, 84, 120, 98], 'uri': 'https://example.com/'}])
        self.assertEqual('https://example.com/', made['runs'][1]['uri'])
        self.assertIn('⟦1⟧ share ⟦/1⟧', made['text'])

    def test_small_link_does_not_mark_the_line(self):
        whole = span('Squire Trelawney asked me to write')
        whole['bbox'] = (40, 80, 300, 94)
        made = self.paragraph([whole], links=[{
            'from': [40, 80, 90, 94], 'uri': 'https://example.com/'}])
        self.assertEqual({}, made['runs'])

    def test_render_markup(self):
        markup = render_markup(
            'Please ⟦1⟧ share our books ⟦/1⟧ now.',
            {1: {'color': '#008000', 'bold': True, 'italic': False,
                 'uri': 'https://example.com/a&b'}})
        self.assertNotIn('⟦', markup)
        self.assertIn('>share our books<', markup)
        noted = render_markup('Dipnotlar⟦^1⟧ okunur⟦^2⟧.', None)
        self.assertIn('<sup>1</sup>', noted)
        self.assertIn('<sup>2</sup>', noted)
        self.assertNotIn('⟦', noted)
        self.assertIn('color:#008000', markup)
        self.assertIn('font-weight:bold', markup)
        self.assertNotIn('<a', markup)
        self.assertTrue(markup.startswith('Please <span '))
        self.assertTrue(markup.endswith('</span> now.'))

    def test_render_drops_unknown_markers(self):
        self.assertEqual(
            'Please share.', render_markup('Please ⟦9⟧ share ⟦/9⟧.', {}))

    def test_unknown_marker_is_not_a_link(self):
        run = {1: {'color': '#008000', 'bold': False, 'italic': False,
                   'uri': 'https://example.com/donate'}}
        phrases = _link_phrases(
            '⟦1254⟧ yabancı ⟦/1254⟧ ⟦1⟧ paylaşın ⟦/1⟧', run)
        self.assertEqual([(run[1], 'paylaşın')], phrases)

    def test_render_run_split_across_parts(self):
        run = {1: {'color': '#008000', 'bold': False, 'italic': True}}
        self.assertIn('>share<', render_markup('⟦1⟧ share', run))
        self.assertIn('>our books<', render_markup('our books ⟦/1⟧', run))

    def test_join_across_markers(self):
        self.assertEqual('cap⟦/1⟧⟦2⟧stan', join_text('cap-⟦/1⟧', '⟦2⟧stan'))
        self.assertEqual('a⟦/1⟧ ⟦2⟧b', join_text('a⟦/1⟧', '⟦2⟧b'))

    def test_split_does_not_cut_a_marker(self):
        pieces = split_translation('alpha ⟦ 1 ⟧ beta gamma delta', [1, 1])
        self.assertTrue(all(
            '⟦' not in piece or '⟧' in piece for piece in pieces))

    def test_markers_do_not_hide_sentence_end(self):
        self.assertFalse(is_continuation(
            paragraph('It ended.⟦/1⟧'), paragraph('⟦2⟧ next', page=1)))
        self.assertTrue(is_continuation(
            paragraph('and then⟦/1⟧'), paragraph('⟦2⟧he left.', page=1)))

    def test_font_variants(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        for name in ('arial.ttf', 'arialbd.ttf', 'ariali.ttf', 'arialbi.ttf'):
            open(os.path.join(folder, name), 'w').close()
        faces = font_variants(os.path.join(folder, 'arial.ttf'))
        self.assertEqual('arialbd.ttf', faces['bold'])
        self.assertEqual('ariali.ttf', faces['italic'])
        self.assertEqual('arialbi.ttf', faces['both'])

    def test_prompt_names_the_marker(self):
        self.assertIn('⟦1⟧', pdf_prompt_extension())


class TestGetLines(unittest.TestCase):
    def test_keep_spaces_between_spans(self):
        item = line('Hello', 0, 10, 30)
        item['spans'] += [span(' ', y=10), span('world', y=10)]
        lines = get_lines(block(item))
        self.assertEqual('Hello world', lines[0]['text'])
        self.assertEqual(2, len(lines[0]['spans']))

    def test_overprinted_bold_is_one_word(self):
        def glyph(char, x0, x1):
            return {'c': char, 'bbox': (x0, 57, x1, 94), 'origin': (x0, 88)}

        first = line('InInvoic', 56, 88, 128, size=30)
        first['spans'][0]['chars'] = [
            glyph('I', 56.7, 65.2), glyph('n', 65.2, 81.1),
            glyph('I', 57.4, 65.9), glyph('n', 65.9, 81.8),
            glyph('v', 80.7, 93.8), glyph('o', 93.4, 108.7),
            glyph('i', 108.7, 116.7), glyph('c', 116.7, 128.3)]
        first['spans'][0]['bbox'] = (56.7, 57, 128.3, 94)
        second = line('oice', 94, 88, 143, size=30)
        second['spans'][0]['chars'] = [
            glyph('o', 94.1, 109.4), glyph('i', 109.4, 117.3),
            glyph('c', 117.3, 129.0), glyph('e', 128.2, 142.6)]
        second['spans'][0]['bbox'] = (94.1, 57, 142.6, 94)
        lines = get_lines(block(first, second))
        self.assertEqual(['Invoice'], [item['text'] for item in lines])

    def test_merge_lines_on_same_baseline(self):
        lines = get_lines(block(
            line('Left', 0, 10, 30), line('right', 36, 10, 90)))
        self.assertEqual(1, len(lines))
        self.assertEqual('Left right', lines[0]['text'])
        self.assertEqual(90, lines[0]['bbox'][2])

    def test_baseline_ignores_superscripts(self):
        note = line('Equal contribution and the', 46, 100, 380)
        note['spans'].insert(0, span('*', 6, x=40, y=96))
        lines = get_lines(block(
            note, line('effort to evaluate it.', 40, 112, 200)))
        self.assertEqual(100, lines[0]['baseline'])
        self.assertEqual(['*Equal contribution and the effort to '
                          'evaluate it.'], texts(split_block(lines)))

    def test_merge_wide_gaps_of_justified_text(self):
        lines = get_lines(block(
            line('him', 0, 10, 20), line('down', 60, 10, 90)))
        self.assertEqual(['him down'], [item['text'] for item in lines])

    def test_keep_table_cells_apart(self):
        cell = line('Role', 60, 10, 90)
        cell['cell'] = True
        lines = get_lines(block(line('Name', 0, 10, 30), cell))
        self.assertEqual(['Name', 'Role'], [item['text'] for item in lines])

    def test_list_item(self):
        drawn = line('First', 20, 10, 60)
        drawn['item'] = True
        lines = get_lines(block(drawn, line('• Second', 20, 22, 60),
                                line('third', 20, 34, 60)))
        self.assertEqual([True, True, False], [l['item'] for l in lines])

    def test_skip_blank_lines(self):
        lines = get_lines(block(line('a', 0, 10, 5), line(' ', 0, 22, 5)))
        self.assertEqual(['a'], [item['text'] for item in lines])

    def test_rotated_text(self):
        item = line('Up', 0, 10, 20)
        item['dir'] = (0, -1)
        self.assertIsNone(get_lines(block(item)))


class TestSplitBlock(unittest.TestCase):
    def split(self, *lines):
        return texts(split_block(get_lines(block(*lines))))

    def test_leader_rows_are_not_one_paragraph(self):
        groups = split_block(get_lines(block(
            line('Hyphenation...................1', 40, 100, 220),
            line('Rounded borders ............2', 40, 114, 220))))
        self.assertEqual(2, len(groups))
        made = [
            make_paragraph(0, group) for group in groups]
        self.assertEqual(
            ['Hyphenation', 'Rounded borders'],
            [item['text'] for item in made])
        self.assertEqual(
            ['1', '2'], [item['trailer']['text'] for item in made])

    def test_raised_note_leaves_the_words(self):
        body = span('Footnotes', size=11)
        body.update(bbox=(96, 691, 138, 705), origin=(96, 703))
        note = span('1', size=8)
        note.update(bbox=(138, 688, 144, 698), origin=(138, 697))
        rest = span(' are essential', size=11)
        rest.update(bbox=(148, 691, 340, 705), origin=(148, 703))
        made = make_paragraph(0, [{
            'dir': (1, 0), 'bbox': (96, 688, 340, 705), 'baseline': 703,
            'text': 'Footnotes1 are essential', 'item': False,
            'spans': [body, note, rest]}])
        self.assertEqual('Footnotes are essential', made['text'])
        self.assertEqual([(0, '1')], made['notes'])
        glued = span('are essential', size=11)
        glued.update(bbox=(148, 691, 340, 705), origin=(148, 703))
        made = make_paragraph(0, [{
            'dir': (1, 0), 'bbox': (96, 688, 340, 705), 'baseline': 703,
            'text': 'Footnotes1are essential', 'item': False,
            'spans': [body, note, glued]}])
        self.assertEqual('Footnotes are essential', made['text'])

    def test_single_paragraph(self):
        self.assertEqual(['one two three'], self.split(
            line('one', 40, 100, 380), line('two', 40, 112, 380),
            line('three', 40, 124, 200)))

    def test_first_line_indent(self):
        self.assertEqual(['a b c', 'd e'], self.split(
            line('a', 52, 100, 380), line('b', 40, 112, 380),
            line('c', 40, 124, 380), line('d', 52, 136, 380),
            line('e', 40, 148, 380)))

    def test_centered_lines(self):
        self.assertEqual(['a b c'], self.split(
            line('a', 40, 100, 380), line('b', 42, 112, 378),
            line('c', 150, 124, 270)))

    def test_short_line_with_sentence_end(self):
        self.assertEqual(['a b.', 'c d'], self.split(
            line('a', 40, 100, 380), line('b.', 40, 112, 150),
            line('c', 40, 124, 380), line('d', 40, 136, 380)))

    def test_short_line_without_sentence_end(self):
        self.assertEqual(['a b c'], self.split(
            line('a', 40, 100, 380), line('b', 40, 112, 150),
            line('c', 40, 124, 380)))

    def test_font_size_change(self):
        self.assertEqual(['Title', 'a b'], self.split(
            line('Title', 40, 100, 200, size=16), line('a', 40, 118, 380),
            line('b', 40, 130, 380)))

    def test_bold_change(self):
        bold = line('Note', 40, 100, 380, flags=20)
        self.assertEqual(['Note', 'a b'], self.split(
            bold, line('a', 40, 112, 380), line('b', 40, 124, 380)))

    def test_vertical_gap(self):
        self.assertEqual(['a b', 'c d'], self.split(
            line('a', 40, 100, 380), line('b', 40, 112, 380),
            line('c', 40, 136, 380), line('d', 40, 148, 380)))

    def test_table_cells(self):
        cells = [line('Role', 120, 100, 150), line('Captain', 120, 112, 160)]
        for cell in cells:
            cell['cell'] = True
        self.assertEqual(['Name', 'Role', 'Billy Bones', 'Captain'], texts(
            split_block(get_lines(block(
                line('Name', 40, 100, 80), cells[0],
                line('Billy Bones', 40, 112, 100), cells[1])))))

    def test_footnote_beside_a_total_stays_together(self):
        total = line('Total', 300, 100, 350, size=15)
        total['cell'] = True
        amount = line('$39.60', 420, 101, 490, size=15)
        amount['cell'] = True
        note = [
            line('* Lorem ipsum dolor sit amet, consectetur adipiscing.',
                 20, 100, 280, size=9),
            line('dapibus fermentum. Nunc adipiscing, magna sed cursus.',
                 20, 110, 270, size=9),
            line('lectus dapibus urna, sed facilisis leo dui et ipsum.',
                 20, 120, 250, size=9)]
        self.assertEqual([
            'Total', '$39.60',
            '* Lorem ipsum dolor sit amet, consectetur adipiscing. '
            'dapibus fermentum. Nunc adipiscing, magna sed cursus. '
            'lectus dapibus urna, sed facilisis leo dui et ipsum.'],
            self.split(total, amount, *note))

    def test_list_items(self):
        second = line('Second item.', 40, 112, 110)
        second['item'] = True
        self.assertEqual(['First item of a list.', 'Second item.'], texts(
            split_block(get_lines(block(
                line('First item of a list.', 40, 100, 150), second)))))
        self.assertEqual(['1. one', '2. two two'], texts(
            split_block(get_lines(block(
                line('1. one', 40, 100, 380), line('2. two', 40, 112, 380),
                line('two', 52, 124, 380))))))

    def test_hanging_indent(self):
        self.assertEqual(['• a b', '• c d e'], self.split(
            line('• a', 40, 100, 380), line('b', 52, 112, 300),
            line('• c', 40, 124, 380), line('d', 52, 136, 380),
            line('e', 52, 148, 300)))


class TestRules(unittest.TestCase):
    def test_get_rules(self):
        drawings = [
            {'rect': (10, 10, 200, 60), 'items': [
                ('re', (100, 10, 101, 60)), ('re', (10, 10, 50, 50)),
                ('re', (100, 10, 101, 15))]},
            {'rect': (10, 20, 200, 80), 'items': [
                ('l', (200, 51), (10, 50)), ('c', (0, 0), (9, 9)),
                ('l', (30, 80), (30, 20))]},
            {'rect': (93, 79, 98, 83), 'items': [('c', (93, 81), (98, 81))]}]
        rules, marks = get_rules(drawings)
        self.assertEqual(
            [[100, 10, 101, 60], [10, 50, 200, 51], [30, 20, 30, 80]], rules)
        self.assertEqual([[93, 79, 98, 83]], marks)

    def test_mark_lines(self):
        data = {'blocks': [block(
            line('Name', 40, 100, 80), line('Role', 120, 100, 150),
            line('First', 107, 120, 150), line('Far', 160, 140, 200))]}
        rules = [[100, 85, 100, 105]]
        marks = [[93, 112, 98, 118], [93, 132, 98, 138]]
        mark_lines(None, data, rules, marks, 0)
        lines = data['blocks'][0]['lines']
        self.assertEqual(
            [False, True, False, False],
            [bool(item.get('cell')) for item in lines])
        self.assertEqual(
            [False, False, True, False],
            [bool(item.get('item')) for item in lines])

    def test_shaded_cells_are_columns(self):
        drawings = [
            {'rect': (40, 40, 150, 70), 'fill': (0.8, 0.6, 0.2)},
            {'rect': (150, 40, 260, 70), 'fill': (0.8, 0.6, 0.2)},
            {'rect': (260, 40, 370, 70), 'fill': (0.8, 0.6, 0.2)}]
        data = {'blocks': [block(
            line('Item', 48, 60, 90), line('Price', 170, 60, 220),
            line('Apple', 48, 90, 95), line('$5', 180, 90, 210))]}
        separators = column_separators(drawings, data, 420, 400)
        mark_lines(None, data, [], [], 0, separators)
        self.assertEqual(
            ['Item', 'Price', 'Apple', '$5'],
            [item['text'] for item in get_lines(data['blocks'][0])])

    def test_aligned_gaps_are_columns(self):
        data = {'blocks': [block(
            line('Subtotal', 40, 40, 110), line('$36', 220, 40, 260),
            line('GST', 40, 60, 80), line('$3', 230, 60, 260),
            line('Total', 40, 80, 90), line('$39', 220, 80, 260))]}
        separators = column_separators([], data, 420, 400)
        mark_lines(None, data, [], [], 0, separators)
        self.assertEqual(
            ['Subtotal', '$36', 'GST', '$3', 'Total', '$39'],
            [item['text'] for item in get_lines(data['blocks'][0])])

    def test_running_header_stays_apart(self):
        data = {'blocks': [block(
            line('TRANSFER RULES', 40, 30, 210, size=9),
            line('Page 1', 360, 30, 410, size=9))]}
        separators = column_separators([], data, 500, 800)
        mark_lines(None, data, [], [], 0, separators)
        self.assertEqual(
            ['TRANSFER RULES', 'Page 1'],
            texts(split_block(get_lines(data['blocks'][0]))))

    def test_stacked_redaction_boxes_become_one(self):
        self.assertEqual(
            [[10, 10, 100, 40]],
            _coalesce_boxes([[10, 10, 100, 22], [12, 24, 80, 40]]))

    def test_side_by_side_boxes_stay_apart(self):
        self.assertEqual(
            2, len(_coalesce_boxes([[10, 10, 40, 24], [80, 10, 140, 24]])))

    def test_repeated_gap_is_read_from_glyphs(self):
        def piece(text, x0, x1, y):
            item = span(text, size=15, x=x0, y=y)
            item['bbox'] = (x0, y - 12, x1, y + 4)
            width = max(x1 - x0, 1) / max(len(text), 1)
            item['chars'] = []
            for index, char in enumerate(text):
                cx0 = x0 + index * width
                item['chars'].append({
                    'c': char,
                    'bbox': (cx0, y - 12, cx0 + width * 0.8, y + 4)})
            del item['text']
            return item

        def row(y, left, right):
            item = line(left, 75, y, 270, size=15)
            gap = piece(' ' * 12, 137, 182, y)
            item['spans'] = [
                piece(left, 75, 136, y), gap, piece(right, 182, 270, y)]
            item['bbox'] = (75, y - 12, 270, y + 4)
            return item

        data = {'blocks': [block(
            row(100, 'Chapter 1', 'What I Believe'),
            row(118, 'Chapter 2', 'What Is the Problem?'),
            row(136, 'Chapter 3', 'Where Does It Come From?'))]}
        for item in data['blocks']:
            _ensure_span_text(item)
        separators = column_separators([], data, 612, 792)
        mark_lines(None, data, [], [], 0, separators)
        self.assertEqual(
            ['Chapter 1', 'What I Believe',
             'Chapter 2', 'What Is the Problem?',
             'Chapter 3', 'Where Does It Come From?'],
            [line['text'] for line in get_lines(data['blocks'][0])])

    def test_one_wide_gap_is_not_a_column(self):
        data = {'blocks': [block(
            line('him', 0, 10, 20), line('down', 60, 10, 90))]}
        self.assertEqual([], column_separators([], data, 420, 400))

    def test_spans_split_across_a_column(self):
        def cell(text, x0, x1, y=50):
            item = span(text, x=x0, y=y)
            item['bbox'] = (x0, y - 10, x1, y + 2)
            return item

        def row(y, left, right):
            item = line('row', 40, y, 240)
            item['spans'] = [cell(left, 40, 80, y), cell(right, 200, 240, y)]
            item['bbox'] = (40, y - 10, 240, y + 2)
            return item

        data = {'blocks': [block(row(50, 'Apple', '$5'), row(80, 'Mango', '$9'))]}
        separators = column_separators([], data, 420, 400)
        mark_lines(None, data, [], [], 0, separators)
        self.assertEqual(
            ['Apple', '$5', 'Mango', '$9'],
            [item['text'] for item in get_lines(data['blocks'][0])])

    def test_list_item_pattern(self):
        for text in ('• a', '- a', '* a', '1. a', '12) a', '(3) a', 'a) a',
                     '(b) a'):
            self.assertTrue(LIST_ITEM.match(text), text)
        for text in ('— a', '– a', 'A. Smith', '1914 was', '-5', 'ab) a'):
            self.assertFalse(LIST_ITEM.match(text), text)


class TestStyle(unittest.TestCase):
    def test_family(self):
        self.assertEqual('serif', get_family('TimesNewRomanPSMT', 0))
        self.assertEqual('sans-serif', get_family('Helvetica-Bold', 0))
        self.assertEqual('sans-serif', get_family('DejaVuSans', 4))
        self.assertEqual('monospace', get_family('CourierNewPSMT', 0))
        self.assertEqual('serif', get_family('ABCDEF+F1', 4))
        self.assertEqual('monospace', get_family('ABCDEF+F2', 8))
        self.assertEqual('sans-serif', get_family('ABCDEF+F3', 0))

    def test_dominant_values(self):
        style = get_style([
            span('Bold heading text', 12, 'Arial-BoldMT', 0, 0xff0000),
            span('x', 9, 'Arial-ItalicMT', 2, 0)])
        self.assertEqual(12, style['size'])
        self.assertEqual('#ff0000', style['color'])
        self.assertTrue(style['bold'])
        self.assertFalse(style['italic'])
        self.assertEqual('sans-serif', style['family'])

    def test_italic_by_flag(self):
        self.assertTrue(get_style([span('word', flags=6)])['italic'])

    def test_css(self):
        style = paragraph('a')['style']
        style.update(align='justify', indent=12, bold=True)
        self.assertEqual(
            '* {font-family: Times; font-size: 9.00pt; line-height: 1.20; '
            'color: #000000; text-align: justify; text-indent: 10.8pt; '
            'font-weight: bold; font-style: normal; margin: 0; padding: 0}',
            get_css(style, 0.9))
        self.assertIn('font-family: Helvetica', get_css(
            dict(style, family='sans-serif'), 1))
        self.assertIn('font-family: Courier', get_css(
            dict(style, family='monospace'), 1))
        font = {'css': '@font-face {font-family: custom; src: url("a.ttf");}\n'}
        css = get_css(style, 1, font)
        self.assertTrue(css.startswith('@font-face'))
        self.assertIn('font-family: custom;', css)


class TestMakeParagraph(unittest.TestCase):
    def test_justified_paragraph_with_indent(self):
        lines = get_lines(block(
            line('He was a tall, strong, heavy,', 52, 100, 380),
            line('nut-brown man; his tarry pig-', 40, 112, 380),
            line('tail fell over his coat.', 40, 124, 200)))
        result = make_paragraph(3, lines)
        self.assertEqual(3, result['page'])
        self.assertEqual(
            'He was a tall, strong, heavy, nut-brown man; his tarry '
            'pigtail fell over his coat.', result['text'])
        self.assertEqual([40, 90, 380, 126.5], result['rect'])
        self.assertEqual(3, len(result['lines']))
        self.assertEqual('justify', result['style']['align'])
        self.assertEqual(12, result['style']['indent'])
        self.assertEqual(1.2, result['style']['line_height'])

    def test_justified_paragraph_going_on(self):
        lines = get_lines(block(
            line('a', 40, 100, 380), line('b', 40, 112, 380),
            line('c', 40, 124, 380)))
        self.assertEqual(
            'justify', make_paragraph(0, lines)['style']['align'])

    def test_ragged_paragraph(self):
        lines = get_lines(block(
            line('a', 40, 100, 370), line('b', 40, 112, 380),
            line('c', 40, 124, 300)))
        self.assertEqual('left', make_paragraph(0, lines)['style']['align'])

    def test_short_stacked_lines_are_not_justified(self):
        lines = get_lines(block(
            line('Also by E. Lockhart', 78, 100, 193, size=11),
            line('Title Page', 78, 116, 150, size=11)))
        self.assertEqual('left', make_paragraph(0, lines)['style']['align'])

    def test_wrapped_sentence_is_not_justified(self):
        lines = get_lines(block(
            line('Educators and librarians, for a variety of teaching '
                 'tools, visit us at', 77, 324, 456, size=11),
            line('RHTeachersLibrarians.com', 77, 338, 233, size=11)))
        self.assertEqual('left', make_paragraph(0, lines)['style']['align'])

    def test_single_line(self):
        result = make_paragraph(0, get_lines(block(line('Hi', 40, 100, 60))))
        self.assertEqual('left', result['style']['align'])
        self.assertEqual(0, result['style']['indent'])
        self.assertEqual(1.2, result['style']['line_height'])

    def test_centered_lines(self):
        lines = get_lines(block(
            line('a long centered line', 100, 100, 300),
            line('short one', 150, 112, 250)))
        self.assertEqual(
            'center', make_paragraph(0, lines)['style']['align'])


class TestSetFrames(unittest.TestCase):
    def test_grow_down_to_next_paragraph(self):
        first = paragraph('a', rect=(40, 100, 380, 140))
        second = paragraph('b', rect=(40, 160, 380, 200))
        set_frames([first, second], [], 560)
        self.assertEqual([40, 100, 380, 157.5], first['frame'])
        self.assertEqual([40, 160, 380, 557.5], second['frame'])

    def test_never_shrink(self):
        first = paragraph('a', rect=(40, 100, 380, 140))
        set_frames([first], [], 120)
        self.assertEqual([40, 100, 380, 140], first['frame'])

    def test_image_is_an_obstacle(self):
        first = paragraph('a', rect=(40, 100, 380, 140))
        set_frames([first], [[200, 300, 300, 400]], 560)
        self.assertEqual(297.5, first['frame'][3])

    def test_columns_do_not_block_each_other(self):
        left = paragraph('a', rect=(40, 100, 200, 140))
        right = paragraph('b', rect=(220, 50, 380, 300))
        set_frames([left, right], [], 560)
        self.assertEqual([40, 100, 200, 557.5], left['frame'])

    def test_single_line_grows_right_until_neighbour(self):
        body = paragraph('body', rect=(40, 200, 380, 300))
        label = paragraph('label', rect=(40, 100, 90, 110), lines=1)
        value = paragraph('value', rect=(150, 100, 200, 110), lines=1)
        set_frames([body, label, value], [], 560)
        self.assertEqual(150, label['frame'][2])
        self.assertEqual(380, value['frame'][2])

    def test_centered_single_line(self):
        body = paragraph('body', rect=(40, 200, 380, 300))
        title = paragraph('title', rect=(180, 100, 240, 116), lines=1)
        set_frames([body, title], [], 560)
        self.assertEqual('center', title['style']['align'])
        self.assertEqual([40, 100, 380], title['frame'][:3])

    def test_overlapping_line_boxes_do_not_share_a_frame(self):
        first = paragraph(
            'They need', rect=(355, 165.3, 697, 233.9), size=18, lines=3)
        second = paragraph(
            'message', rect=(358, 230.1, 689, 255.5), size=18, lines=1)
        set_frames([first, second], [[0, 0, 792, 484]], 580)
        self.assertEqual(233.9, first['frame'][3])

    def test_background_image_does_not_widen_a_line(self):
        line = paragraph(
            'They need', rect=(178, 232, 621, 258), size=18, lines=1)
        set_frames([line], [[0, 0, 792, 612]], 580)
        self.assertEqual([178, 232, 621], line['frame'][:3])


class TestChainParagraphs(unittest.TestCase):
    def test_across_pages(self):
        head = paragraph('as he came plodding to the inn door, his')
        number = paragraph('1', rect=(200, 560, 210, 570), ignored=True)
        tail = paragraph('sea-chest following him.', page=1)
        groups = chain_paragraphs([head, number, tail])
        self.assertEqual([[head, tail], [number]], groups)

    def test_across_columns(self):
        head = paragraph('and then', rect=(40, 400, 200, 540))
        tail = paragraph('he left.', rect=(220, 40, 380, 100))
        self.assertTrue(is_continuation(head, tail))

    def test_same_column_below(self):
        head = paragraph('and then', rect=(40, 100, 380, 140))
        tail = paragraph('he left.', rect=(40, 160, 380, 200))
        self.assertFalse(is_continuation(head, tail))

    def test_broken_off_block(self):
        data = {'blocks': [
            block(line('Provided proper attribution, it grants the', 40, 100,
                       380),
                  line('right to use the tables in journalistic or', 42, 112,
                       378)),
            block(line('scholarly works.', 170, 124, 250)),
            block(line('next block', 40, 150, 380))]}
        paragraphs = get_page_paragraphs(0, data)
        self.assertEqual(
            ['Provided proper attribution, it grants the right to use the '
             'tables in journalistic or scholarly works.', 'next block'],
            [item['text'] for item in paragraphs])
        self.assertEqual(3, len(paragraphs[0]['lines']))
        self.assertEqual('center', paragraphs[0]['style']['align'])
        self.assertEqual([40, 90, 380, 126.5], paragraphs[0]['rect'])
        self.assertEqual(3, len(get_page_paragraphs(
            0, data, [[40, 114.5, 380, 115]])))

    def test_broken_off_when_line_boxes_overlap(self):
        # The chores book: each line box is taller than the line pitch, so
        # the next line starts above the previous line's bottom.
        data = {'blocks': [
            block(line('They need to be done and depending on',
                       359, 183, 694, size=18),
                  line('your outlook can be accomplished easily.',
                       356, 205, 697, size=18),
                  line('Doing My Chores is intended to impart a',
                       355, 226, 697, size=18)),
            block(line('message of importance in accomplishing',
                       358, 248, 689, size=18)),
            block(line("one's given tasks.", 447, 270, 611, size=18))]}
        paragraphs = get_page_paragraphs(0, data)
        self.assertEqual(
            ['They need to be done and depending on your outlook can be '
             'accomplished easily. Doing My Chores is intended to impart a '
             "message of importance in accomplishing one's given tasks."],
            [item['text'] for item in paragraphs])

    def test_not_continued(self):
        cases = [
            (paragraph('It ended.'), paragraph('next', page=1)),
            (paragraph('and then'), paragraph('Next', page=1)),
            (paragraph('and then'), paragraph('next', page=1, indent=12)),
            (paragraph('and then'), paragraph('next', page=1, size=12)),
            (paragraph('and then'), paragraph('next', page=1, bold=True)),
            (paragraph('and then'), dict(paragraph('next', page=1),
                                         item=True)),
        ]
        for head, tail in cases:
            self.assertFalse(is_continuation(head, tail), tail['text'])

    def test_skip_footnote(self):
        head = paragraph('as he came')
        footnote = paragraph('1. A note.', size=8, rect=(40, 520, 380, 540))
        tail = paragraph('to the door.', page=1)
        groups = chain_paragraphs([head, footnote, tail])
        self.assertEqual([[head, tail], [footnote]], groups)

    def test_stop_at_other_body_paragraph(self):
        head = paragraph('as he came')
        other = paragraph('Another one.', page=1)
        tail = paragraph('to the door.', page=1, rect=(40, 300, 380, 340))
        groups = chain_paragraphs([head, other, tail])
        self.assertEqual([[head], [other], [tail]], groups)

    def test_three_parts(self):
        parts = [paragraph('one and', page=0), paragraph('two and', page=1),
                 paragraph('three.', page=2)]
        self.assertEqual([parts], chain_paragraphs(parts))


class TestSplitTranslation(unittest.TestCase):
    def test_single_part(self):
        self.assertEqual(['abc def'], split_translation('abc def', [3]))

    def test_cut_at_nearest_space(self):
        self.assertEqual(
            ['aaaa bbbb', 'cccc dddd'],
            split_translation('aaaa bbbb cccc dddd', [10, 10]))

    def test_keep_every_word(self):
        text = 'one two three four five six seven eight nine ten'
        pieces = split_translation(text, [5, 30, 10])
        self.assertEqual(3, len(pieces))
        self.assertEqual(text, ' '.join(pieces))

    def test_text_without_spaces(self):
        self.assertEqual(
            ['今日は', '晴れです'], split_translation('今日は晴れです', [3, 4]))

    def test_hyphen_tail_stays_with_its_paragraph(self):
        head = paragraph('read the footnotes before they are an-')
        tail = paragraph('chored.', rect=(360, 200, 400, 216), lines=1)
        tail['notes'] = [(0, '2')]
        texts = _fold_hyphen_tails(
            [head, tail], ['Dipnotları okumadan', 'okur.'])
        self.assertEqual(['Dipnotları okumadan okur.', ''], texts)
        self.assertEqual([('end', '2')], head['notes'])
        sentence = paragraph('for demon-')
        rest = paragraph(
            'stration purposes; normally the colors would be close.',
            rect=(40, 200, 340, 240), lines=1)
        kept = _fold_hyphen_tails(
            [sentence, rest], ['gösterim', 'amaçlıdır.'])
        self.assertEqual(['gösterim', 'amaçlıdır.'], kept)

    def test_wrap_figure_is_kept_off_the_letters(self):
        paragraph_box = {
            'rect': [360, 340, 600, 420],
            'ink': [[420, 345, 600, 360], [360, 400, 600, 420]]}
        background = {
            'rect': [360, 340, 600, 370],
            'ink': [[370, 345, 590, 365]]}
        _attach_wraps(
            [paragraph_box, background],
            [[360, 344, 408, 392], [100, 100, 200, 160]])
        self.assertEqual([[360, 344, 408, 392]], paragraph_box['avoid'])
        self.assertNotIn('avoid', background)


class TestPdfElement(unittest.TestCase):
    def test_text(self):
        element = PdfElement([
            paragraph('his sea-chest follow-'), paragraph('ing him')])
        self.assertEqual('his sea-chest following him', element.get_text())
        self.assertEqual(element.get_text(), element.get_raw())
        self.assertEqual(element.get_text(), element.get_content())

    def test_add_translation(self):
        element = PdfElement([paragraph('a')])
        element.add_translation()
        self.assertIsNone(element.get_translation())
        element.add_translation(' b \n')
        self.assertEqual('b', element.get_translation())

    def test_element_handler(self):
        elements = [
            PdfElement([paragraph('Hello')], 'page-1'),
            PdfElement([paragraph('12')], 'page-1', True)]
        handler = ElementHandler(Base.placeholder, Base.separator, 'only')
        originals = handler.prepare_original(elements)
        self.assertEqual(
            [(0, uid('0Hello'), 'Hello', 'Hello', False, None, 'page-1'),
             (1, uid('112'), '12', '12', True, None, 'page-1')], originals)
        handler.add_translations([
            Paragraph(0, uid('0Hello'), 'Hello', 'Hello', translation='Merhaba')])
        self.assertEqual('Merhaba', elements[0].get_translation())
        self.assertIsNone(elements[1].get_translation())


class TestRouting(unittest.TestCase):
    @patch(module_name + '.get_config')
    def test_is_pdf_layout(self, mock_config):
        mock_config.return_value = config(**{'pdf_layout.enabled': True})
        self.assertTrue(is_pdf_layout('pdf', 'pdf'))
        self.assertFalse(is_pdf_layout('pdf', 'epub'))
        self.assertFalse(is_pdf_layout('epub', 'pdf'))
        self.assertFalse(is_pdf_layout('pdf', None))
        mock_config.return_value = config()
        self.assertFalse(is_pdf_layout('pdf', 'pdf'))

    @patch(module_name + '.get_config')
    def test_get_cache_id(self, mock_config):
        mock_config.return_value = config(**{'pdf_layout.enabled': True})
        self.assertEqual(
            uid('/a.pdfGoogleTurkish0' + PDF_CACHE_VERSION),
            get_cache_id('/a.pdf', 'Google', 'Turkish', '0', 'utf-8', 'pdf',
                         'pdf'))
        self.assertEqual(
            uid('/a.pdfGoogleTurkish0'),
            get_cache_id('/a.pdf', 'Google', 'Turkish', '0', 'utf-8', 'pdf',
                         'epub'))
        self.assertEqual(
            uid('/a.pdfGoogleTurkish0'),
            get_cache_id('/a.pdf', 'Google', 'Turkish', '0', 'utf-8', 'pdf'))
        self.assertEqual(
            uid('/a.pdfGoogleTurkish0'),
            get_cache_id('/a.pdf', 'Google', 'Turkish', '0', 'utf-8', 'pdf',
                         'pdf', False))
        mock_config.return_value = config()
        self.assertEqual(
            uid('/a.pdfGoogleTurkish0' + PDF_CACHE_VERSION),
            get_cache_id('/a.pdf', 'Google', 'Turkish', '0', 'utf-8', 'pdf',
                         'pdf', True))

    @patch('calibre_plugins.ebook_translator.lib.conversion.extract_book')
    @patch('calibre_plugins.ebook_translator.lib.conversion.get_pdf_elements')
    @patch(module_name + '.get_config')
    def test_extract_item(self, mock_config, mock_pdf, mock_book):
        mock_config.return_value = config(**{'pdf_layout.enabled': True})
        extract_item('/a.pdf', 'pdf', 'utf-8', output_format='pdf')
        mock_pdf.assert_called_once_with('/a.pdf', 'utf-8')
        mock_book.assert_not_called()
        extract_item('/a.pdf', 'pdf', 'utf-8', output_format='epub')
        mock_book.assert_called_once_with('/a.pdf', 'utf-8')

    @patch('calibre_plugins.ebook_translator.lib.conversion.convert_book')
    @patch('calibre_plugins.ebook_translator.lib.conversion.convert_pdf')
    @patch('calibre_plugins.ebook_translator.lib.conversion.get_translation')
    @patch('calibre_plugins.ebook_translator.lib.conversion.get_cache')
    @patch('calibre_plugins.ebook_translator.lib.conversion'
           '.get_element_handler')
    @patch('calibre_plugins.ebook_translator.lib.conversion.get_translator')
    @patch(module_name + '.get_config')
    def test_convert_item(
            self, mock_config, mock_translator, mock_handler, mock_cache,
            mock_translation, mock_pdf, mock_book):
        mock_config.return_value = config(**{'pdf_layout.enabled': True})
        mock_translator.return_value.name = 'Google'
        mock_handler.return_value.get_merge_length.return_value = 0
        for output_path in ('/b.pdf', '/b.epub'):
            convert_item(
                'Title', '/a.pdf', output_path, 'English', 'Turkish', False,
                False, 'pdf', 'utf-8', 'auto', Mock())
        self.assertEqual('/b.pdf', mock_pdf.call_args[0][1])
        self.assertEqual('/b.epub', mock_book.call_args[0][1])
        self.assertEqual(1, mock_pdf.call_count)
        self.assertEqual(1, mock_book.call_count)
        self.assertNotIn('font_path', mock_book.call_args.kwargs)

        mock_pdf.reset_mock()
        mock_book.reset_mock()
        mock_config.return_value = config()
        convert_item(
            'Title', '/a.pdf', '/c.pdf', 'English', 'Turkish', False,
            False, 'pdf', 'utf-8', 'auto', Mock(),
            font_path='C:/Fonts/arial.ttf')
        mock_book.assert_not_called()
        self.assertEqual(
            'C:/Fonts/arial.ttf', mock_pdf.call_args.kwargs['font_path'])
        self.assertIn('PDF Font: C:/Fonts/arial.ttf', mock_pdf.call_args[0][5])


RELEASE = [{'filename': name, 'url': 'https://files/' + name}
           for name in (
               'pymupdf-2.1-cp311-abi3-macosx_11_0_arm64.whl',
               'pymupdf-2.1-cp311-abi3-manylinux_2_28_aarch64.whl',
               'pymupdf-2.1-cp311-abi3-manylinux_2_28_x86_64.whl',
               'pymupdf-2.1-cp311-abi3-win_amd64.whl',
               'pymupdf-2.1.tar.gz')]


class TestFindWheel(unittest.TestCase):
    def find(self, system, machine, version):
        wheel = find_wheel(RELEASE, system, machine, version)
        return wheel and wheel['filename']

    def test_supported(self):
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-win_amd64.whl',
            self.find('Windows', 'AMD64', (3, 14)))
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-macosx_11_0_arm64.whl',
            self.find('Darwin', 'arm64', (3, 11)))
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-manylinux_2_28_x86_64.whl',
            self.find('Linux', 'x86_64', (3, 12)))
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-manylinux_2_28_aarch64.whl',
            self.find('Linux', 'aarch64', (3, 13)))
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-manylinux_2_28_x86_64.whl',
            self.find('Linux', 'amd64', (3, 12)))
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-manylinux_2_28_aarch64.whl',
            self.find('Linux', 'arm64', (3, 13)))
        self.assertEqual(
            'pymupdf-2.1-cp311-abi3-win_amd64.whl',
            self.find('Windows', 'x86_64', (3, 14)))

    def test_unsupported(self):
        self.assertIsNone(self.find('Windows', 'AMD64', (3, 10)))
        self.assertIsNone(self.find('Darwin', 'x86_64', (3, 14)))
        self.assertIsNone(self.find('Windows', 'x86', (3, 14)))
        self.assertIsNone(self.find('FreeBSD', 'amd64', (3, 14)))

    def test_version_specific_wheel(self):
        files = [{'filename': 'x-1-cp312-cp312-win_amd64.whl'}]
        self.assertIsNotNone(find_wheel(files, 'Windows', 'AMD64', (3, 12)))
        self.assertIsNone(find_wheel(files, 'Windows', 'AMD64', (3, 13)))


def fake_wheel(version='9.9'):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('pymupdf_fake/__init__.py', 'VALUE = 1\n')
        archive.writestr('pymupdf-%s.dist-info/METADATA' % version, '')
    return data.getvalue()


class TestInstallPymupdf(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.library_dir = os.path.join(self.temp_dir, 'pymupdf')
        patcher = patch(module_name + '.get_library_dir',
                        return_value=self.library_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        modules = patch.dict('sys.modules')
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop('pymupdf', None)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_from_file(self):
        path = os.path.join(self.temp_dir, 'fake.whl')
        with open(path, 'wb') as file:
            file.write(fake_wheel())
        self.assertIsNone(get_installed_version())
        self.assertEqual(self.library_dir, install_pymupdf(wheel_path=path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.library_dir, 'pymupdf_fake', '__init__.py')))
        self.assertEqual('9.9', get_installed_version())
        self.assertFalse(os.path.exists(self.library_dir + '.partial'))

    def test_replace_old_version(self):
        path = os.path.join(self.temp_dir, 'fake.whl')
        for version in ('1.0', '2.0'):
            with open(path, 'wb') as file:
                file.write(fake_wheel(version))
            install_pymupdf(wheel_path=path)
        self.assertEqual('2.0', get_installed_version())

    @patch(module_name + '.fetch')
    def test_download(self, mock_fetch):
        wheel = fake_wheel()
        release = {'urls': [{
            'filename': 'pymupdf-2.1-cp311-abi3-win_amd64.whl',
            'url': 'https://files/wheel',
            'digests': {'sha256': hashlib.sha256(wheel).hexdigest()}}]}
        mock_fetch.side_effect = [json.dumps(release).encode(), wheel]
        progress = Mock()
        with patch(module_name + '.find_wheel',
                   return_value=release['urls'][0]):
            install_pymupdf(progress)
        mock_fetch.assert_called_with('https://files/wheel', progress)
        self.assertEqual('9.9', get_installed_version())

    @patch(module_name + '.fetch')
    def test_damaged_download(self, mock_fetch):
        release = {'urls': [{
            'filename': 'pymupdf-2.1-cp311-abi3-win_amd64.whl',
            'url': 'https://files/wheel', 'digests': {'sha256': '0'}}]}
        mock_fetch.side_effect = [json.dumps(release).encode(), fake_wheel()]
        with patch(module_name + '.find_wheel',
                   return_value=release['urls'][0]):
            self.assertRaisesRegex(
                Exception, 'damaged', install_pymupdf)
        self.assertIsNone(get_installed_version())

    @patch(module_name + '.fetch')
    def test_unsupported_system(self, mock_fetch):
        mock_fetch.return_value = json.dumps({'urls': []}).encode()
        self.assertRaisesRegex(
            Exception, 'not available for this system', install_pymupdf)

    def test_in_use(self):
        sys.modules['pymupdf'] = Mock()
        self.assertRaisesRegex(Exception, 'restart', install_pymupdf)


SOURCE_LINES = [
    (90, 40, 'Squire Trelawney asked me to write down the whole'),
    (102, 40, 'story from the beginning to the end, keeping nothing'),
    (114, 40, 'back but the bearings of the island.'),
    (126, 52, 'I remember him as if it were yesterday, as he came'),
    (138, 40, 'plodding to the inn door, his sea-chest following him'),
]


def build_pdf(path):
    document = pymupdf.open()
    first = document.new_page(width=420, height=595)
    first.insert_text((40, 60), 'The Old Sea Dog', fontsize=16,
                      fontname='tibo')
    for baseline, x, text in SOURCE_LINES:
        first.insert_text((x, baseline), text, fontsize=10, fontname='tiro')
    first.insert_text((205, 570), '1', fontsize=9, fontname='tiro')
    first.insert_link({
        'kind': pymupdf.LINK_URI, 'from': pymupdf.Rect(40, 80, 120, 92),
        'uri': 'https://example.com/'})
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    image.set_rect(image.irect, (200, 30, 30))
    first.insert_image(pymupdf.Rect(300, 200, 360, 260), pixmap=image)
    second = document.new_page(width=420, height=595)
    second.insert_text((40, 60), 'in a hand-barrow; a tall, strong man.',
                       fontsize=10, fontname='tiro')
    second.insert_text((205, 570), '2', fontsize=9, fontname='tiro')
    document.set_toc([[1, 'The Old Sea Dog', 1]])
    document.save(path)
    document.close()


def page_text(document, number):
    return ' '.join(document[number].get_text().split())


@unittest.skipIf(pymupdf is None, 'PyMuPDF is not installed.')
class TestPdfLayout(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source = os.path.join(self.temp_dir, 'source.pdf')
        self.output = os.path.join(self.temp_dir, 'output.pdf')
        build_pdf(self.source)
        patcher = patch(module_name + '.get_config', return_value=config(
            rule_mode='normal', filter_scope='text', filter_rules=[]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def translate(self, elements):
        for element in elements:
            if not element.ignored:
                element.add_translation(element.get_text().upper() + ' ĞŞİ')
        return elements

    def test_get_pdf_elements(self):
        elements = get_pdf_elements(self.source)
        self.assertEqual([
            ('The Old Sea Dog', 1, False),
            ('Squire Trelawney asked me to write down the whole story from '
             'the beginning to the end, keeping nothing back but the '
             'bearings of the island.', 1, False),
            ('I remember him as if it were yesterday, as he came plodding '
             'to the inn door, his sea-chest following him in a '
             'hand-barrow; a tall, strong man.', 2, False),
            ('1', 1, True),
            ('2', 1, True),
        ], [(e.get_text(), len(e.element), e.ignored) for e in elements])
        heading, first, second = elements[:3]
        self.assertTrue(heading.element[0]['style']['bold'])
        self.assertEqual(16, heading.element[0]['style']['size'])
        self.assertEqual('serif', first.element[0]['style']['family'])
        self.assertEqual(12, second.element[0]['style']['indent'])
        self.assertEqual([0, 1], [part['page'] for part in second.element])

    def test_borderless_table_keeps_columns(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=300)
        for rect in ((40, 40, 150, 72), (150, 40, 270, 72), (270, 40, 390, 72)):
            page.draw_rect(rect, color=None, fill=(0.86, 0.66, 0.28), width=0)
        for x, y, text in (
                (48, 64, 'Item'), (168, 64, 'Price'), (290, 64, 'Qty'),
                (48, 96, 'Apple'), (178, 96, '$5.00'), (310, 96, '2')):
            page.insert_text((x, y), text, fontsize=11, fontname='tiro')
        document.save(self.source)
        document.close()
        ordered = sorted(
            get_pdf_elements(self.source),
            key=lambda element: (
                element.element[0]['rect'][1], element.element[0]['rect'][0]))
        self.assertEqual(
            ['Item', 'Price', 'Qty', 'Apple', '$5.00', '2'],
            [element.get_text() for element in ordered])
        for previous, element in zip(ordered, ordered[1:]):
            left, right = previous.element[0], element.element[0]
            same_row = min(left['rect'][3], right['rect'][3]) \
                - max(left['rect'][1], right['rect'][1]) > 2
            if same_row:
                self.assertLessEqual(left['frame'][2], right['rect'][0] + 0.5)

    def test_table_cells_joined_by_mupdf(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        page.insert_text((42, 60), 'Billy Bones Captain', fontsize=10,
                         fontname='tiro')
        x = 42 + pymupdf.get_text_length(
            'Billy Bones ', fontname='tiro', fontsize=10) - 1.5
        for start, end in (((40, 48), (40, 64)), ((x, 48), (x, 64)),
                           ((140, 48), (140, 64)), ((40, 48), (140, 48)),
                           ((40, 64), (140, 64))):
            page.draw_line(start, end)
        document.save(self.source)
        document.close()
        elements = get_pdf_elements(self.source)
        self.assertEqual(
            ['Billy Bones', 'Captain'], [e.get_text() for e in elements])
        self.assertLessEqual(elements[0].element[0]['frame'][2], x + 0.01)

    def test_compose_in_place(self):
        elements = self.translate(get_pdf_elements(self.source))
        scales = compose_pdf(self.source, self.output, elements, 'only')
        self.assertEqual(4, sum(scales.values()))
        with pymupdf.open(self.output) as document:
            self.assertEqual(2, document.page_count)
            self.assertEqual(pymupdf.Rect(0, 0, 420, 595), document[0].rect)
            first, second = page_text(document, 0), page_text(document, 1)
            self.assertIn('THE OLD SEA DOG ĞŞİ', first)
            self.assertIn('SQUIRE TRELAWNEY ASKED', first)
            self.assertNotIn('asked me', first)
            self.assertNotIn('plodding', first)
            self.assertIn('STRONG MAN. ĞŞİ', second)
            self.assertNotIn('hand-barrow', second)
            for number in (0, 1):
                self.assertEqual(str(number + 1), document[number].get_text(
                    clip=(190, 555, 230, 580)).strip())
            self.assertEqual(1, len(document[0].get_images()))
            self.assertEqual(
                ['https://example.com/'],
                [link['uri'] for link in document[0].get_links()])
            self.assertEqual([[1, 'The Old Sea Dog', 1]], document.get_toc())
            fonts = {}
            for page in document:
                for font in page.get_fonts():
                    fonts.setdefault(font[3].split('+')[-1], set()).add(
                        font[0])
            self.assertIn('Nimbus Roman Regular', fonts)
            self.assertEqual({1}, {len(xrefs) for xrefs in fonts.values()})

    def test_compose_untranslated(self):
        elements = get_pdf_elements(self.source)
        compose_pdf(self.source, self.output, elements, 'only')
        with pymupdf.open(self.output) as document:
            self.assertIn('asked me to write', page_text(document, 0))

    def test_compose_bilingual(self):
        for position, original, toc_page in (('below', 0, 1), ('above', 1, 2)):
            elements = self.translate(get_pdf_elements(self.source))
            compose_pdf(self.source, self.output, elements, position)
            with pymupdf.open(self.output) as document:
                self.assertEqual(4, document.page_count)
                self.assertIn('asked me', page_text(document, original))
                self.assertIn('ASKED ME', page_text(document, 1 - original))
                self.assertIn('hand-barrow', page_text(document, 2 + original))
                self.assertEqual(
                    [[1, 'The Old Sea Dog', toc_page]], document.get_toc())

    def write(self, text, rect=(40, 40, 200, 60), **kwargs):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        part = paragraph('x', rect=rect, lines=1)
        part['frame'] = kwargs.pop('frame', list(rect))
        element = PdfElement([part])
        for name, value in kwargs.items():
            setattr(element, name, value)
        scale = write_part(page, part, text, element, kwargs.get('font'))
        return scale, page

    def test_write_part_ignores_unknown_marker(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        part = paragraph('x', rect=(40, 40, 380, 90), lines=1)
        element = PdfElement([part])
        element.runs = {1: {
            'color': '#008000', 'bold': True, 'italic': False,
            'uri': 'https://example.com/donate', 'from': [40, 40, 120, 55]}}
        write_part(
            page, part,
            'Lütfen ⟦1254⟧ kitapları ⟦/1254⟧ ⟦1⟧ paylaşın ⟦/1⟧', element)
        document.save(self.output)
        document.close()
        with pymupdf.open(self.output) as saved:
            self.assertIn('paylaşın', saved[0].get_text())
            self.assertNotIn('⟦', saved[0].get_text())
            self.assertEqual(
                ['https://example.com/donate'],
                [link.get('uri') for link in saved[0].get_links()])

    def test_write_part_fits(self):
        scale, page = self.write('Kısa metin')
        self.assertEqual(1.0, scale)
        self.assertEqual(1, page.get_text().count('Kısa metin'))

    def test_write_part_uses_free_space(self):
        scale, page = self.write(
            'Bu çeviri özgün satırdan çok daha uzun bir metin',
            frame=[40, 40, 200, 100])
        self.assertEqual(1.0, scale)

    def test_wrap_keeps_the_rest_under_the_picture(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        figure = [100, 80, 190, 170]
        part = paragraph('x', rect=(100, 80, 340, 280), lines=8)
        part['avoid'] = [figure]
        part['ink'] = [[200, 80, 340, 160], [100, 180, 340, 250]]
        text = ' '.join('kelime%d' % number for number in range(1, 36))
        write_part(page, part, text, PdfElement([part]))
        words = page.get_text('words')
        found = {word[4] for word in words}
        for number in range(1, 36):
            self.assertIn('kelime%d' % number, found)
        for word in words:
            box = word[:4]
            self.assertFalse(
                box[0] < figure[2] - 1 and box[2] > figure[0] + 1
                and box[1] < figure[3] - 1 and box[3] > figure[1] + 1,
                word)
        tops = sorted(word[1] for word in words)
        lines = [tops[0]]
        for top in tops[1:]:
            if top - lines[-1] > 3:
                lines.append(top)
        gaps = [b - a for a, b in zip(lines, lines[1:])]
        self.assertGreater(len(lines), 4)
        self.assertLess(max(gaps), min(gaps) * 1.45, gaps)
        self.assertTrue(any(
            word[0] >= figure[2] and word[3] <= figure[3] + 2 for word in words))
        self.assertTrue(any(word[1] >= figure[3] - 2 for word in words))

    def test_write_part_shrinks_in_steps(self):
        text = 'Bu çeviri özgün satırdan biraz daha uzun'
        _scale, page = self.write(text, rect=(40, 40, 400, 53))
        x0, _y0, x1, _y1 = page.get_text('blocks')[0][:4]
        scale, page = self.write(text, rect=(40, 40, 40 + (x1 - x0) * 0.85, 53))
        self.assertEqual(0.8, scale)
        self.assertEqual(1, page.get_text().count('uzun'))

    def test_write_part_below_minimum(self):
        scale, page = self.write('uzun ' * 60)
        self.assertIn(scale, FALLBACK_SCALES)
        self.assertEqual(60, page.get_text().count('uzun'))

    def test_fits_checks_first_line(self):
        css = '* {font-size: 10pt; line-height: 1.2}'
        self.assertTrue(fits(pymupdf, (0, 0, 400, 14), 'a b', css))
        self.assertFalse(fits(pymupdf, (0, 0, 400, 13), 'a b', css))
        self.assertFalse(fits(pymupdf, (0, 0, 8, 100), 'abcdefgh', css))

    def test_write_part_rtl(self):
        scale, page = self.write('مرحبا بالعالم', target_direction='rtl')
        fonts = [span['font'] for block in page.get_text('dict')['blocks']
                 for line in block['lines'] for span in line['spans']]
        self.assertTrue(any('Arabic' in font for font in fonts), fonts)

    def test_inline_style_round_trip(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        css = '* {font-family: Times; font-size: 14pt; color: #000000}'
        page.insert_htmlbox(
            pymupdf.Rect(40, 40, 380, 80),
            '<div>Please <span style="color:#008000; font-weight:bold">'
            'share our books</span> now.</div>', css=css, scale_low=1)
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines', []):
                for item in line['spans']:
                    if 'share' in item['text']:
                        page.insert_link({
                            'kind': pymupdf.LINK_URI,
                            'from': pymupdf.Rect(item['bbox']),
                            'uri': 'https://example.com/donate'})
        document.save(self.source)
        document.close()
        elements = [item for item in get_pdf_elements(self.source)
                    if not item.ignored]
        text = elements[0].get_text()
        number = next(key for key, run in elements[0].runs.items()
                      if run.get('uri') == 'https://example.com/donate')
        self.assertIn('⟦%d⟧ share our books ⟦/%d⟧' % (number, number), text)
        self.assertTrue(elements[0].runs[number]['bold'])
        elements[0].add_translation(
            text.replace('Please', 'Lütfen').replace('now', 'şimdi').replace(
                'share our books', 'kitaplarımızı paylaşın'))
        compose_pdf(self.source, self.output, elements, 'only')
        with pymupdf.open(self.output) as result:
            self.assertIn('kitaplarımızı paylaşın', result[0].get_text())
            self.assertNotIn('⟦', result[0].get_text())
            colored = [
                item for block in result[0].get_text('dict')['blocks']
                for line in block.get('lines', [])
                for item in line['spans'] if 'kitap' in item['text']]
            self.assertEqual(0x008000, colored[0]['color'])
            self.assertTrue(colored[0]['flags'] & 16)
            links = result[0].get_links()
            self.assertEqual(
                ['https://example.com/donate'], [link.get('uri') for link in links])
            link_box = links[0]['from']
            self.assertLess(link_box.x0, colored[0]['bbox'][2])
            self.assertGreater(link_box.x1, colored[0]['bbox'][0])

    def test_link_underline_follows_the_words(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        css = '* {font-family: Times; font-size: 14pt; color: #000000}'
        page.insert_htmlbox(
            pymupdf.Rect(40, 40, 380, 80),
            '<div>Visit <span style="color:#4c7aa9">GetUnderlined.com</span>'
            ' today.</div>', css=css, scale_low=1)
        span = next(
            item for block in page.get_text('dict')['blocks']
            for line in block.get('lines', []) for item in line['spans']
            if 'GetUnderlined.com' in item['text'])
        box = pymupdf.Rect(span['bbox'])
        page.insert_link({
            'kind': pymupdf.LINK_URI, 'from': box,
            'uri': 'http://getunderlined.com/'})
        page.draw_rect(
            pymupdf.Rect(box.x0, box.y1 - 1.9, box.x1, box.y1 - 1.15),
            fill=(0.298, 0.478, 0.663), color=None, width=0)
        old_width = box.x1 - box.x0
        document.save(self.source)
        document.close()
        elements = [item for item in get_pdf_elements(self.source)
                    if not item.ignored]
        text = elements[0].get_text()
        elements[0].add_translation(text.replace('GetUnderlined.com', 'Site'))
        compose_pdf(self.source, self.output, elements, 'only')
        with pymupdf.open(self.output) as result:
            letters = []
            for block in result[0].get_text('rawdict')['blocks']:
                for line in block.get('lines', []):
                    for item in line.get('spans', []):
                        chars = item.get('chars') or []
                        text = ''.join(char.get('c', '') for char in chars)
                        if 'Site' not in text:
                            continue
                        index = text.index('Site')
                        letters = chars[index:index + 4]
            bars = [
                drawing['rect'] for drawing in result[0].get_drawings()
                if drawing['rect'].width >= 8 and drawing['rect'].height <= 2]
            self.assertEqual(1, len(bars), bars)
            self.assertLess(bars[0].width, old_width - 20)
            self.assertAlmostEqual(bars[0].x0, letters[0]['bbox'][0], delta=1)
            self.assertAlmostEqual(bars[0].x1, letters[-1]['bbox'][2], delta=1)

    def test_write_part_keeps_original_color(self):
        document = pymupdf.open()
        page = document.new_page(width=420, height=595)
        part = paragraph('x', rect=(40, 40, 380, 90), lines=1)
        part['style']['color'] = '#ffffff'
        element = PdfElement([part])
        element.translation_color = '#000000'
        write_part(page, part, 'Beyaz başlık', element)
        spans = page.get_text('dict')['blocks'][0]['lines'][0]['spans']
        self.assertEqual(0xffffff, spans[0]['color'])

    @unittest.skipUnless(
        os.path.isfile('C:/Windows/Fonts/arial.ttf'), 'Arial is missing.')
    def test_custom_font(self):
        elements = self.translate(get_pdf_elements(self.source))
        compose_pdf(self.source, self.output, elements, 'only',
                    'C:/Windows/Fonts/arial.ttf')
        with pymupdf.open(self.output) as document:
            fonts = [font[3] for font in document[0].get_fonts()]
            self.assertTrue(any('Arial' in font for font in fonts), fonts)
