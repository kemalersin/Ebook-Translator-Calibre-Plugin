import json
import unittest
from unittest.mock import patch, Mock

from ...lib.cache import Paragraph, get_cache_id
from ...lib.utils import uid
from ...lib.element import SrtElement, get_element_handler
from ...lib.translation import Translation, Glossary
from ...lib.srt import (
    SRT_CACHE_VERSION, parse_srt, compose_srt, cue_marker, strip_cue_markers,
    parse_marked_text, srt_prompt_extension, SrtElementHandler,
    SrtElementHandlerMerge, repair_srt_groups)
from ...engines.base import Base
from ...engines.deepl import DeeplTranslate


module_name = 'calibre_plugins.ebook_translator.lib.srt'

T1 = '00:00:01,000 --> 00:00:02,000'
T2 = '00:00:03,000 --> 00:00:04,000'
T3 = '00:00:05,000 --> 00:00:06,000'


def srt(*cues):
    return '\n\n'.join(cues) + '\n'


class TestParseSrt(unittest.TestCase):
    def test_standard(self):
        elements = parse_srt(srt(
            '1\n%s\nHello.\nHow are you?' % T1, '2\n%s\nFine.' % T2))
        self.assertEqual(
            ['1', T1, 'Hello.\nHow are you?'], elements[0].element)
        self.assertEqual(['2', T2, 'Fine.'], elements[1].element)

    def test_bom_and_crlf(self):
        content = '\ufeff1\r\n%s\r\na\r\n\r\n2\r\n%s\r\nb\r\n' % (T1, T2)
        elements = parse_srt(content)
        self.assertEqual(['1', T1, 'a'], elements[0].element)
        self.assertEqual(['2', T2, 'b'], elements[1].element)

    def test_whitespace_only_line(self):
        elements = parse_srt('1\n%s\na\n   \n2\n%s\nb\n' % (T1, T2))
        self.assertEqual(2, len(elements))
        self.assertEqual('a', elements[0].get_raw())
        self.assertEqual(['2', T2, 'b'], elements[1].element)

    def test_extra_blank_lines(self):
        elements = parse_srt('1\n%s\na\n\n\n\n2\n%s\nb\n\n\n' % (T1, T2))
        self.assertEqual(['1', T1, 'a'], elements[0].element)
        self.assertEqual(['2', T2, 'b'], elements[1].element)

    def test_blank_line_inside_cue(self):
        elements = parse_srt('1\n%s\na\n\nb\n\n2\n%s\nc\n' % (T1, T2))
        self.assertEqual('a\nb', elements[0].get_raw())
        self.assertEqual('c', elements[1].get_raw())

    def test_missing_index(self):
        elements = parse_srt('%s\na\n\n%s\nb\n' % (T1, T2))
        self.assertEqual(['1', T1, 'a'], elements[0].element)
        self.assertEqual(['2', T2, 'b'], elements[1].element)

    def test_empty_cue(self):
        elements = parse_srt(srt('1\n%s' % T1, '2\n%s\nb' % T2))
        self.assertEqual(['1', T1, ''], elements[0].element)
        self.assertEqual(['2', T2, 'b'], elements[1].element)

    def test_numeric_text(self):
        elements = parse_srt(srt('1\n%s\n42' % T1, '2\n%s\nb' % T2))
        self.assertEqual('42', elements[0].get_raw())
        self.assertEqual('2', elements[1].element[0])

    def test_no_timing_line(self):
        self.assertRaises(ValueError, parse_srt, 'a\n\nb\n')

    def test_compose_srt(self):
        content = srt('1\n%s\na' % T1, '2\n%s\nb' % T2)
        self.assertEqual(content, compose_srt(parse_srt(content)))


class TestCueMarker(unittest.TestCase):
    def setUp(self):
        self.placeholder = Base.placeholder

    def test_cue_marker(self):
        self.assertEqual('{{id_c3}}', cue_marker(Base.placeholder, 3))
        self.assertEqual(
            '<m id=c3 />', cue_marker(DeeplTranslate.placeholder, 3))

    def test_strip_cue_markers(self):
        self.assertEqual(
            ' a  b', strip_cue_markers('{{id_c0}} a {{ id_c1 }} b',
                                       self.placeholder))

    def test_parse_marked_text(self):
        self.assertEqual(
            {0: 'A', 1: 'B\nC'},
            parse_marked_text(
                '{{id_c0}} A\n{{id_c1}} B\nC', self.placeholder, [0, 1]))

    def test_parse_marked_text_tolerant_spacing(self):
        self.assertEqual(
            {4: 'A', 5: 'B'},
            parse_marked_text(
                '{ id_c 4 } A\n{{id _ c5}}B', self.placeholder, [4, 5]))

    def test_parse_marked_text_deepl(self):
        self.assertEqual(
            {0: 'A', 1: 'B'},
            parse_marked_text(
                '<m id=c0 /> A <m id=c1 /> B',
                DeeplTranslate.placeholder, [0, 1]))

    def test_parse_marked_text_invalid(self):
        for text in (
                None,
                '{{id_c0}} A B',                       # missing marker
                '{{id_c1}} B\n{{id_c0}} A',            # reordered
                '{{id_c0}} A\n{{id_c0}} A\n{{id_c1}} B',  # duplicated
                'Note: {{id_c0}} A\n{{id_c1}} B',      # leading text
                '{{id_c0}}\n{{id_c1}} B',              # empty cue
                'A B'):                                # all markers lost
            self.assertIsNone(
                parse_marked_text(text, self.placeholder, [0, 1]), text)

    def test_parse_marked_text_single_without_marker(self):
        self.assertEqual(
            {7: 'A'}, parse_marked_text(' A ', self.placeholder, [7]))
        self.assertIsNone(parse_marked_text(' ', self.placeholder, [7]))

    def test_srt_prompt_extension(self):
        self.assertIn('{{id_c0}}', srt_prompt_extension(self.placeholder))


class TestSrtElement(unittest.TestCase):
    def setUp(self):
        self.element = SrtElement(['1', T1, '<i>Hello</i>\n{\\an8}world'])
        self.element.set_placeholder(Base.placeholder)
        self.element.set_position('below')

    def test_get_text(self):
        self.assertEqual('Hello\nworld', self.element.get_text())

    def test_get_content_reserves_tags(self):
        self.assertEqual(
            '{{id_00000}}Hello{{id_00001}}\n{{id_00002}}world',
            self.element.get_content())

    def test_add_translation_restores_tags(self):
        self.element.get_content()
        self.element.add_translation(
            '{{id_00000}}Merhaba{{id_00001}}\n{{ id_00002 }}dünya')
        self.assertEqual(
            '<i>Hello</i>\n{\\an8}world\n<i>Merhaba</i>\n{\\an8}dünya',
            self.element.get_raw())

    def test_add_translation_cleans_structure(self):
        self.element.set_position('only')
        self.element.get_content()
        self.element.add_translation(
            '\n{{id_c1}} Merhaba\n\n%s\n{{id_99999}}dünya\n' % T2)
        self.assertEqual('Merhaba\ndünya', self.element.get_raw())

    def test_add_translation_empty(self):
        self.element.add_translation('\n \n')
        self.assertEqual('<i>Hello</i>\n{\\an8}world', self.element.get_raw())

    def test_get_translation(self):
        element = SrtElement(['1', T1, ''])
        self.assertEqual('1\n' + T1, element.get_translation())


class TestSrtElementHandler(unittest.TestCase):
    def setUp(self):
        self.elements = parse_srt(srt(
            '1\n%s\nHello.' % T1, '2\n%s' % T2, '3\n%s\nBye.' % T3))
        self.handler = SrtElementHandler(Base.placeholder, '\n\n', 'below')

    def test_prepare_original(self):
        originals = self.handler.prepare_original(self.elements)
        self.assertEqual(3, len(originals))
        self.assertEqual(
            (0, uid('0Hello.'), 'Hello.', 'Hello.', False, None, None),
            originals[0])
        self.assertTrue(originals[1][4])

    def test_add_translations(self):
        self.handler.prepare_original(self.elements)
        self.handler.add_translations([
            Paragraph(0, '', '', '', translation='Merhaba.'),
            Paragraph(2, '', '', '', translation='Hoşça kal.')])
        self.assertEqual(
            srt('1\n%s\nHello.\nMerhaba.' % T1, '2\n%s' % T2,
                '3\n%s\nBye.\nHoşça kal.' % T3),
            compose_srt(self.elements))


class TestSrtElementHandlerMerge(unittest.TestCase):
    def setUp(self):
        self.elements = parse_srt(srt(
            '1\n%s\nHello.' % T1, '2\n%s\nYes.' % T2, '3\n%s\nBye.' % T3))
        self.handler = SrtElementHandlerMerge(
            Base.placeholder, '\n\n', 'only')
        self.handler.set_merge_length(1000)

    def get_paragraphs(self):
        return [Paragraph(*original)
                for original in self.handler.prepare_original(self.elements)]

    def test_prepare_original(self):
        originals = self.handler.prepare_original(self.elements)
        self.assertEqual(1, len(originals))
        self.assertEqual(
            '{{id_c0}} Hello.\n{{id_c1}} Yes.\n{{id_c2}} Bye.',
            originals[0][3])
        self.assertEqual({'cues': [0, 1, 2]}, json.loads(originals[0][5]))

    def test_prepare_original_merge_length(self):
        self.handler.set_merge_length(40)
        originals = self.handler.prepare_original(self.elements)
        self.assertEqual(
            [[0, 1], [2]],
            [json.loads(original[5])['cues'] for original in originals])

    def test_add_translations(self):
        paragraphs = self.get_paragraphs()
        paragraphs[0].translation = \
            '{{id_c0}} Merhaba.\n{{id_c1}} Evet.\n{{id_c2}} Hoşça kal.'
        self.handler.add_translations(paragraphs)
        self.assertEqual(
            srt('1\n%s\nMerhaba.' % T1, '2\n%s\nEvet.' % T2,
                '3\n%s\nHoşça kal.' % T3),
            compose_srt(self.elements))

    @patch(module_name + '.log')
    def test_add_translations_misaligned(self, mock_log):
        paragraphs = self.get_paragraphs()
        paragraphs[0].translation = \
            '{{id_c0}} Merhaba. Evet.\n{{id_c2}} Hoşça kal.'
        self.handler.add_translations(paragraphs, {1: 'Evet!'})
        self.assertEqual(
            srt('1\n%s\nHello.' % T1, '2\n%s\nEvet!' % T2, '3\n%s\nBye.' % T3),
            compose_srt(self.elements))
        mock_log.warn.assert_called_once()

    def test_get_element_handler(self):
        with patch(module_name + '.get_config') as mock_get_config:
            mock_get_config().get.side_effect = lambda key: {
                'merge_enabled': True, 'merge_length': 100,
                'translation_position': 'after'}.get(key)
            handler = get_element_handler(
                Base.placeholder, '\n\n', 'ltr', 'srt')
        self.assertIsInstance(handler, SrtElementHandlerMerge)
        self.assertEqual('below', handler.position)
        self.assertEqual(100, handler.get_merge_length())


class TestCompatibility(unittest.TestCase):
    def test_is_alignment_with_cues(self):
        paragraph = Paragraph(
            0, '', '', '', attributes='{"cues": [0, 1]}',
            translation='{{id_c0}} A\n\n{{id_c1}} B')
        self.assertTrue(paragraph.is_alignment('\n\n', Base.placeholder))
        paragraph.translation = '{{id_c0}} A B'
        self.assertFalse(paragraph.is_alignment('\n\n', Base.placeholder))

    def test_get_cache_id(self):
        self.assertEqual(
            uid('/a.epubGoogleTurkish2000'),
            get_cache_id('/a.epub', 'Google', 'Turkish', '2000', 'UTF-8',
                         'epub'))
        self.assertEqual(
            uid('/a.srtGoogleTurkish2000gbk' + SRT_CACHE_VERSION),
            get_cache_id('/a.srt', 'Google', 'Turkish', '2000', 'GBK', 'srt'))


class TestRepairSrtGroups(unittest.TestCase):
    def setUp(self):
        self.elements = parse_srt(srt(
            '1\n%s\nHello.' % T1, '2\n%s\nYes.' % T2, '3\n%s\nBye.' % T3))
        self.handler = SrtElementHandlerMerge(
            Base.placeholder, '\n\n', 'only')
        self.handler.set_merge_length(1000)
        self.paragraphs = [
            Paragraph(*original)
            for original in self.handler.prepare_original(self.elements)]

        self.requests = []
        self.translator = Mock(
            placeholder=Base.placeholder, separator='\n\n', merge_enabled=True,
            concurrency_limit=1, request_interval=0, max_error_count=10,
            request_attempt=0)
        self.translator.name = 'Fake'
        self.translator.get_target_lang.return_value = 'Turkish'
        self.translator.match_error.return_value = False
        self.translator.translate.side_effect = self.translate
        self.translation = Translation(
            self.translator, Glossary(Base.placeholder))
        self.translation.set_alignment(False)

    def translate(self, text):
        self.requests.append(text)
        if '{{id_c' in text:
            # Merge the first two cues, as a GenAI engine might do.
            return '{{id_c0}} Merhaba, evet.\n{{id_c2}} Hoşça kal.'
        return {'Hello.': 'Merhaba.', 'Yes.': 'Evet.', 'Bye.': 'Hoşça kal.'
                }[text]

    def test_repair(self):
        saved = []
        self.translation.handle(self.paragraphs)
        repaired = repair_srt_groups(
            self.translation, self.paragraphs, Base.placeholder, saved.append)
        self.assertEqual(
            {0: 'Merhaba.', 1: 'Evet.', 2: 'Hoşça kal.'}, repaired)
        self.assertEqual(4, len(self.requests))
        self.assertEqual(self.paragraphs, saved)
        self.assertEqual(
            '{{id_c0}} Merhaba.\n{{id_c1}} Evet.\n{{id_c2}} Hoşça kal.',
            self.paragraphs[0].translation)

        self.handler.add_translations(self.paragraphs, repaired)
        self.assertEqual(
            srt('1\n%s\nMerhaba.' % T1, '2\n%s\nEvet.' % T2,
                '3\n%s\nHoşça kal.' % T3),
            compose_srt(self.elements))

    def test_nothing_to_repair(self):
        self.paragraphs[0].translation = \
            '{{id_c0}} Merhaba.\n{{id_c1}} Evet.\n{{id_c2}} Hoşça kal.'
        translation = Mock()
        translation.cancel_request.return_value = False
        translation.need_stop.return_value = False
        self.assertEqual(
            {}, repair_srt_groups(
                translation, self.paragraphs, Base.placeholder))
        translation.fork.assert_not_called()

    def test_canceled(self):
        translation = Mock()
        translation.cancel_request.return_value = True
        self.assertEqual(
            {}, repair_srt_groups(
                translation, self.paragraphs, Base.placeholder))
        translation.fork.assert_not_called()
