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
    mark_lines, LIST_ITEM)
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


class TestGetLines(unittest.TestCase):
    def test_keep_spaces_between_spans(self):
        item = line('Hello', 0, 10, 30)
        item['spans'] += [span(' ', y=10), span('world', y=10)]
        lines = get_lines(block(item))
        self.assertEqual('Hello world', lines[0]['text'])
        self.assertEqual(2, len(lines[0]['spans']))

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

    def test_write_part_fits(self):
        scale, page = self.write('Kısa metin')
        self.assertEqual(1.0, scale)
        self.assertEqual(1, page.get_text().count('Kısa metin'))

    def test_write_part_uses_free_space(self):
        scale, page = self.write(
            'Bu çeviri özgün satırdan çok daha uzun bir metin',
            frame=[40, 40, 200, 100])
        self.assertEqual(1.0, scale)

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

    def test_write_part_color(self):
        scale, page = self.write('Renkli', translation_color='#ff0000')
        spans = page.get_text('dict')['blocks'][0]['lines'][0]['spans']
        self.assertEqual(0xff0000, spans[0]['color'])

    @unittest.skipUnless(
        os.path.isfile('C:/Windows/Fonts/arial.ttf'), 'Arial is missing.')
    def test_custom_font(self):
        elements = self.translate(get_pdf_elements(self.source))
        compose_pdf(self.source, self.output, elements, 'only',
                    'C:/Windows/Fonts/arial.ttf')
        with pymupdf.open(self.output) as document:
            fonts = [font[3] for font in document[0].get_fonts()]
            self.assertTrue(any('Arial' in font for font in fonts), fonts)
