import os
import shutil
import tempfile
import unittest

from ...lib.cache import Paragraph, TranslationCache


class TestParagraph(unittest.TestCase):
    def setUp(self):
        self.paragraph = Paragraph(
            1, 'TEST', 'a\n\nb\n\nc', 'a\n\nb\n\nc\n\n',
            translation='A\n\nB\n\nC', attributes='{"class": "test"}')

    def test_created_paragraph(self):
        self.assertIsInstance(self.paragraph, Paragraph)
        self.assertFalse(self.paragraph.is_cache)
        self.assertIsNone(self.paragraph.error)
        self.assertTrue(self.paragraph.aligned)

    def test_get_attributes(self):
        self.assertEqual({'class': 'test'}, self.paragraph.get_attributes())

    def test_check_translation(self):
        self.assertTrue(self.paragraph.is_alignment('\n\n'))

        self.paragraph.original = 'a\n\nb\n\nc'
        self.paragraph.translation = 'A\n\nB\nC\n\n'
        self.assertFalse(self.paragraph.is_alignment('\n\n'))

    def test_is_alignment(self):
        # Test with empty translation.
        self.paragraph.translation = None
        self.assertTrue(self.paragraph.is_alignment('\n\n'))

        self.paragraph.translation = ''
        self.assertTrue(self.paragraph.is_alignment('\n\n'))

        self.paragraph.translation = '   '
        self.assertTrue(self.paragraph.is_alignment('\n\n'))

        self.paragraph.original = 'a\n\nb\n\nc'
        self.paragraph.translation = 'A\n\nB\n\nC'
        self.assertTrue(self.paragraph.is_alignment('\n\n'))

        # Alignment determination ignores line breaks at the start and end.
        self.paragraph.translation = '\n\nA\n\nB\n\nC\n\n'
        self.assertTrue(self.paragraph.is_alignment('\n\n'))

        self.paragraph.original = 'a\n\nb'
        self.paragraph.translation = 'A\n\nB\n\nC'
        self.assertFalse(self.paragraph.is_alignment('\n\n'))

        self.paragraph.original = 'a\n\nb\n\nc'
        self.paragraph.translation = 'A\n\nB'
        self.assertFalse(self.paragraph.is_alignment('\n\n'))

    def test_do_alignment(self):
        # Test with empty translation
        self.paragraph.translation = None
        self.paragraph.do_aligment('\n\n')
        self.assertIsNone(self.paragraph.translation)

        self.paragraph.translation = ''
        self.paragraph.do_aligment('\n\n')
        self.assertEqual('', self.paragraph.translation)

        self.paragraph.translation = '   '
        self.paragraph.do_aligment('\n\n')
        self.assertEqual('   ', self.paragraph.translation)

        # Test with no alignment needed.
        self.paragraph.original = 'a\n\nb\n\nc'
        self.paragraph.translation = 'A\n\nB\n\nC'
        self.paragraph.do_aligment('\n')
        self.assertEqual('A\n\nB\n\nC', self.paragraph.translation)

        self.paragraph.translation = 'A\n\nB\nC'
        self.paragraph.do_aligment('\n\n')
        self.assertEqual('A\n\nB\n\nC', self.paragraph.translation)


class TestCacheSave(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.paths = (
            TranslationCache.dir_path, TranslationCache.cache_path,
            TranslationCache.temp_path)
        TranslationCache.dir_path = self.directory
        TranslationCache.cache_path = os.path.join(self.directory, 'cache')
        TranslationCache.temp_path = os.path.join(self.directory, 'temp')
        os.makedirs(TranslationCache.cache_path)
        self.cache = TranslationCache('heading', True)

    def tearDown(self):
        self.cache.destroy()
        (
            TranslationCache.dir_path, TranslationCache.cache_path,
            TranslationCache.temp_path) = self.paths
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_changed_paragraph_is_translated_again(self):
        self.cache.save([(0, 'old', '<old>', 'In Invvoic oicee', False)])
        self.cache.update(0, translation='Fatura Fiyatı')

        self.cache.save([(0, 'old', '<old>', 'In Invvoic oicee', False)])
        self.assertEqual(
            'Fatura Fiyatı', self.cache.paragraph(0).translation)

        self.cache.save([(0, 'new', '<new>', 'Invoice', False)])
        stored = self.cache.paragraph(0)
        self.assertEqual('Invoice', stored.original)
        self.assertIsNone(stored.translation)

        self.cache.save([(1, 'other', '<other>', 'Total', False)])
        self.assertIsNone(self.cache.first(id=0))
