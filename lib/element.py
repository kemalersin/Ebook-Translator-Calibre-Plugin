import re
import json
import copy
from typing import Any

from lxml import etree  # type: ignore

from calibre import prepare_string_for_xml as xml_escape  # type: ignore

from .utils import (
    log, ns, uid, trim, sorted_mixed_keys, open_file, css_to_xpath,
    create_xpath)
from .config import get_config


def get_string(element, remove_ns=False):
    element.text = element.text or ''  # prevent auto-closing empty elements
    markup = trim(etree.tostring(
        element, encoding='utf-8', with_tail=False).decode('utf-8'))
    return re.sub(r'\sxmlns="[^"]+"', '', markup) if remove_ns else markup


def get_name(element):
    return etree.QName(element).localname


# The same markers the PDF path uses. They contain no words, so an engine
# has nothing to translate, and the spaces keep the words detachable.
_LINK_MARKER = re.compile(r'⟦\s*(/)?\s*(\d+)\s*⟧')
# Inline formatting whose words are translated and whose tag comes back.
# sub, sup and code stay reserved instead: a note number or a code span is
# kept verbatim.
_INLINE_TAGS = ('em', 'i', 'strong', 'b', 'u', 'mark', 'del', 'ins', 'cite')
# Quotes and the other leading marks are not part of the word. A leftover
# {{id_00000}} must not donate its letters to a drop cap.
_XML_ENTITY = re.compile(
    r'&(?:#x[0-9A-Fa-f]+|#\d+|[A-Za-z][A-Za-z0-9]+);')


class Element:
    def __init__(self, element, page_id=None, ignored=False):
        self.element = element
        self.page_id = page_id
        self.ignored = ignored

        self.placeholder: tuple = ()
        self.reserve_elements = []
        self.original = []
        self.column_gap = None

        self.position = None
        self.target_direction = None
        self.translation_lang = None
        self.original_color = None
        self.translation_color = None

        self.remove_pattern = None
        self.reserve_pattern = None
        self.links = {}
        self.drop_cap = None

    def _element_copy(self):
        return copy.deepcopy(self.element)

    def set_ignored(self, ignored):
        self.ignored = ignored

    def set_placeholder(self, placeholder):
        self.placeholder = placeholder

    def set_column_gap(self, values):
        self.column_gap = values

    def set_position(self, position):
        self.position = position

    def set_target_direction(self, direction):
        self.target_direction = direction

    def set_translation_lang(self, lang):
        self.translation_lang = lang

    def set_original_color(self, color):
        self.original_color = color

    def set_translation_color(self, color):
        self.translation_color = color

    def set_remove_pattern(self, pattern):
        self.remove_pattern = pattern

    def set_reserve_pattern(self, pattern):
        self.reserve_pattern = pattern

    def get_name(self) -> str | None:
        return None

    def get_attributes(self) -> str | None:
        return None

    def delete(self):
        return None

    def get_raw(self):
        raise NotImplementedError()

    def get_text(self):
        raise NotImplementedError()

    def get_content(self):
        raise NotImplementedError()

    def add_translation(self, translation=None):
        raise NotImplementedError()

    def get_translation(self) -> str | None:
        return None


SRT_TIMING_RE = re.compile(
    r'^\s*(\d{1,2}:\d{1,2}:\d{1,2}(?:[,.]\d{1,3})?)\s*-->\s*'
    r'(\d{1,2}:\d{1,2}:\d{1,2}(?:[,.]\d{1,3})?)(.*)$')
SRT_TAG_RE = re.compile(r'<[^<>\n]+>|\{\\[^{}\n]*\}')
SRT_CUE_MARKER_ID = r'c\s*\d+'


class SrtElement(Element):
    def get_raw(self):
        return self.element[2]

    def get_text(self):
        return SRT_TAG_RE.sub('', self.get_raw())

    def get_content(self):
        self.reserve_elements = []
        if not self.placeholder:
            return self.get_raw()

        def reserve(match):
            self.reserve_elements.append(match.group(0))
            return self.placeholder[0].format(
                format(len(self.reserve_elements) - 1, '05'))
        return SRT_TAG_RE.sub(reserve, self.get_raw())

    def _clean_translation(self, translation):
        translation = translation.replace('\r\n', '\n').replace('\r', '\n')
        if self.placeholder:
            for rid, tag in enumerate(self.reserve_elements):
                pattern = self.placeholder[1].format(
                    r'\s*'.join(format(rid, '05')))
                translation = re.sub(pattern, lambda _: tag, translation)
            leftover = re.compile('%s|%s' % (
                self.placeholder[1].format(r'\d{5}'),
                self.placeholder[1].format(SRT_CUE_MARKER_ID)))
            translation = leftover.sub('', translation)
        # A blank line ends a cue and a timing line starts one, so neither may
        # survive inside the text of a cue.
        lines = [line.rstrip() for line in translation.split('\n')
                 if line.strip() and not SRT_TIMING_RE.match(line)]
        return '\n'.join(lines).strip()

    def add_translation(self, translation=None):
        if translation is None:
            return
        translation = self._clean_translation(translation)
        if translation == '':
            return
        if self.position == 'only' or not self.element[2]:
            self.element[2] = translation
        elif self.position in ('below', 'right'):
            self.element[2] += '\n%s' % translation
        else:
            self.element[2] = '%s\n%s' % (translation, self.element[2])

    def get_translation(self):
        return '\n'.join(part for part in self.element if part)


class PgnElement(Element):
    def get_raw(self):
        return self.element[0]

    def get_text(self):
        return self.get_raw().strip('{}')

    def get_content(self):
        return self.get_text()

    def add_translation(self, translation=None):
        if translation is not None:
            if self.position == 'only':
                self.element[1] = translation
            else:
                content = (self.get_content(), translation)
                if self.position not in ('below', 'right'):
                    content = tuple(reversed(content))
                self.element[1] = ' | '.join(content)

    def get_translation(self):
        if self.element[1] is None:
            return self.element[0]
        return '{%s}' % self.element[1]


class MetadataElement(Element):
    def get_raw(self):
        return self.element.content

    def get_text(self):
        return self.element.content

    def get_content(self):
        return self.element.content

    def add_translation(self, translation=None):
        if translation is not None and translation != self.get_content() \
                and not self.ignored:
            if self.position == 'only':
                self.element.content = translation
            elif self.position in ['above', 'left']:
                self.element.content = '%s %s' % (
                    translation, self.element.content)
            else:
                self.element.content = '%s %s' % (
                    self.element.content, translation)


class TocElement(Element):
    def get_raw(self):
        return self.element.title

    def get_text(self):
        return self.element.title

    def get_content(self):
        return self.element.title

    def add_translation(self, translation=None):
        if translation is not None:
            items = [self.element.title, translation]
            self.element.title = items[-1] if self.position == 'only' else \
                ' '.join(reversed(items) if self.position in ('above', 'left')
                         else items)


class PageElement(Element):
    def get_name(self):
        return get_name(self.element)

    def get_raw(self):
        return get_string(self.element, True)

    def get_text(self):
        return trim(''.join(self.element.itertext()))

    def get_attributes(self):
        attributes = dict(self.element.attrib.items())
        return json.dumps(attributes) if attributes else None

    def _safe_remove(self, element, replacement=''):
        previous, parent = element.getprevious(), element.getparent()
        if previous is not None:
            previous.tail = (previous.tail or '') + replacement
            previous.tail += (element.tail or '')
        else:
            parent.text = (parent.text or '') + replacement
            parent.text += (element.tail or '')
        element.tail = None
        parent.remove(element)

    def get_content(self):
        # Rebuilt on every call. add_translations() extracts again, and the
        # links and reserved elements have to belong to that extraction.
        # Keeping the previous reserves would append every footnote twice.
        self.links = {}
        self.reserve_elements = []
        self.drop_cap = None
        element_copy = self._element_copy()
        if self.remove_pattern is not None:
            for noise in element_copy.xpath(
                    self.remove_pattern, namespaces=ns):
                self._safe_remove(noise)
        elements = []
        if self.reserve_pattern is not None:
            elements = element_copy.xpath(self.reserve_pattern, namespaces=ns)
        elements = self._hoist_textless_links(element_copy, elements)
        for eid, element in enumerate(elements):
            replacement = self.placeholder[0].format(format(eid, '05'))
            if get_name(element) in ('sub', 'sup'):
                parent = element.getparent()
                if parent is not None and get_name(parent) == 'a' and \
                        parent.text is None and element.tail is None and \
                        len(parent.getchildren()) == 1:
                    elements[eid] = element = parent
            self.reserve_elements.append(get_string(element, True))
            self._safe_remove(element, replacement)
        self._detach_drop_cap(element_copy)
        self._mark_runs(element_copy)
        return trim(''.join(element_copy.itertext()))

    def _textless_link_ancestor(self, node, root):
        """An ancestor link that has an address but no words of its own."""
        parent = node.getparent()
        found = None
        while parent is not None and parent is not root:
            if get_name(parent) == 'a' and parent.get('href') \
                    and ''.join(parent.itertext()).strip() == '':
                found = parent
            parent = parent.getparent()
        return found

    def _hoist_textless_links(self, root, elements):
        """Keep an image link whole. It has no words to translate."""
        if get_name(root) == 'a':
            return elements
        chosen, seen = [], set()
        for element in elements:
            anchor = self._textless_link_ancestor(element, root)
            if anchor is None:
                chosen.append(element)
                continue
            if id(anchor) in seen:
                continue
            seen.add(id(anchor))
            chosen.append(anchor)
        return chosen

    def _text_before(self, node, root):
        parts = []

        def walk(current):
            if current is node:
                return True
            if not isinstance(getattr(current, 'tag', None), str):
                return False
            if current.text:
                parts.append(current.text)
            for child in current:
                if walk(child):
                    return True
                if child.tail:
                    parts.append(child.tail)
            return False

        walk(root)
        return ''.join(parts)

    def _word_continues(self, text):
        return bool(text) and text[0].isalpha()

    def _continuation(self, node, root):
        """The text that keeps going as the same word, with no space between.

        The rest of the word may sit in this element's tail, in the next
        element's text, or on an ancestor when the letter is wrapped again.
        """
        current = node
        while current is not None and current is not root:
            if self._word_continues(current.tail):
                return current, 'tail'
            if current.tail:
                return None
            sibling = current.getnext()
            if sibling is not None:
                if isinstance(getattr(sibling, 'tag', None), str) \
                        and self._word_continues(sibling.text):
                    return sibling, 'text'
                return None
            current = current.getparent()
        return None

    def _could_be_drop_cap(self, node, root):
        """One styled letter glued to the rest of its word.

        "I said" stays a word: a space follows the letter. A picture letter
        has no character to move, so it is left alone.
        """
        if not self._is_marked_run(node, root) or get_name(node) == 'a':
            return False
        letter = ''.join(node.itertext())
        if len(letter) != 1 or not letter.isalpha():
            return False
        return self._continuation(node, root) is not None

    def _detach_drop_cap(self, root):
        """Give the engine the whole word, and remember the letter's tag.

        The translated word lends its first letter back to that tag.
        """
        chosen = None
        for node in root.iter():
            if node is root or not isinstance(getattr(node, 'tag', None), str):
                continue
            if any(char.isalnum() for char in self._text_before(node, root)):
                break
            if self._could_be_drop_cap(node, root):
                chosen = node
        if chosen is None:
            return
        found = self._continuation(chosen, root)
        if found is None:
            return
        owner, kind = found
        letter = ''.join(chosen.itertext())
        self.drop_cap = self._open_tag(chosen)
        if kind == 'text':
            owner.text = letter + owner.text
            self._safe_remove(chosen)
        elif owner is chosen:
            self._safe_remove(chosen, letter)
        else:
            owner.tail = letter + owner.tail
            self._safe_remove(chosen)

    def _apply_drop_cap(self, translation):
        """Wrap the translated word's first letter in the saved tag.

        Quotes in front of it stay where they are. With no letter to wrap,
        the translation is left untouched.
        """
        open_tag = self.drop_cap
        if not open_tag or not translation:
            return translation
        close_tag = self._close_tag(open_tag)
        if not close_tag:
            return translation
        index = self._first_letter_index(translation)
        if index is None:
            return translation
        letter = translation[index]
        return ''.join((
            translation[:index], open_tag, letter, close_tag,
            translation[index + 1:]))

    def _first_letter_index(self, translation):
        index, length = 0, len(translation)
        while index < length:
            char = translation[index]
            if char.isspace() or (not char.isalnum() and char not in '<&{'):
                index += 1
                continue
            if translation.startswith('{{', index):
                end = translation.find('}}', index + 2)
                if end == -1:
                    return None
                index = end + 2
                continue
            if char == '{':
                index += 1
                continue
            if char == '&':
                entity = _XML_ENTITY.match(translation, index)
                if entity is None:
                    return None
                index = entity.end()
                continue
            if char == '<':
                end = translation.find('>', index)
                if end == -1:
                    return None
                index = end + 1
                continue
            if char.isalpha():
                return index
            return None
        return None

    def _element_from_translation(self, text):
        text = self._polish_translation(self._apply_drop_cap(text))
        return self._create_new_element(get_name(self.element), text), text

    def _is_marked_run(self, node, root):
        """A link or an inline run whose tag should wrap the translation.

        The paragraph element itself is not marked. Its tag is copied onto
        the translation, so marking it would wrap the words twice.
        """
        if node is root or not isinstance(getattr(node, 'tag', None), str):
            return False
        name = get_name(node)
        if name == 'a':
            return bool(node.get('href')) and get_name(root) != 'a'
        if name in _INLINE_TAGS:
            return True
        return name == 'span' and bool(node.get('class') or node.get('style'))

    def _mark_runs(self, root):
        """Send each link and formatted run between a pair of markers.

        Children are marked first. An italic link therefore keeps both the
        emphasis and the address, and the words between the markers are what
        the engine translates.
        """
        nodes = [
            node for node in root.iter() if self._is_marked_run(node, root)]
        number = 1
        for node in reversed(nodes):
            if node.getparent() is None:
                continue
            body = ' '.join(''.join(node.itertext()).split())
            if body == '':
                continue
            self.links[number] = self._open_tag(node)
            for child in list(node):
                node.remove(child)
            node.text = '⟦%d⟧ %s ⟦/%d⟧' % (number, body, number)
            number += 1

    def _open_tag(self, node):
        nsmap = {}
        current = node
        while current is not None:
            for prefix, uri in (current.nsmap or {}).items():
                if prefix and uri and prefix not in nsmap:
                    nsmap[prefix] = uri
            current = current.getparent()
        parts, declared = [], set()
        for key, value in node.attrib.items():
            if key.startswith('{'):
                qualified = etree.QName(key)
                prefix = next(
                    (name for name, uri in nsmap.items()
                     if uri == qualified.namespace), None)
                if not prefix:
                    prefix = 'ns'
                local = '%s:%s' % (prefix, qualified.localname)
                if prefix not in declared:
                    uri = nsmap.get(prefix, qualified.namespace)
                    parts.append('xmlns:%s="%s"' % (prefix, xml_escape(uri)))
                    declared.add(prefix)
            else:
                local = key
            if local == 'id' and self.position != 'only':
                continue
            escaped = xml_escape(value).replace('"', '&quot;')
            parts.append('%s="%s"' % (local, escaped))
        name = get_name(node)
        return '<%s %s>' % (name, ' '.join(parts)) if parts else '<%s>' % name

    def _close_tag(self, open_tag):
        match = re.match(r'<\s*([^\s>/]+)', open_tag or '')
        return '</%s>' % match.group(1) if match else ''

    def _restore_links(self, translation):
        """Turn marker pairs back into links and inline formatting. A marker
        this paragraph did not create, or a pair the engine left incomplete,
        is removed. The words on either side stay.
        """
        if '⟦' not in translation:
            return translation
        links = self.links or {}
        tokens, position = [], 0
        for match in _LINK_MARKER.finditer(translation):
            if match.start() > position:
                tokens.append(('text', translation[position:match.start()]))
            kind = 'close' if match.group(1) else 'open'
            tokens.append((kind, int(match.group(2))))
            position = match.end()
        if position < len(translation):
            tokens.append(('text', translation[position:]))
        opened, closed = set(), set()
        for kind, number in tokens:
            if kind == 'open':
                opened.add(number)
            else:
                closed.add(number)
        valid = opened & closed & set(links)
        parts, stack = [], []
        for index, (kind, value) in enumerate(tokens):
            if kind == 'text':
                previous = tokens[index - 1][0] if index else None
                following = tokens[index + 1][0] \
                    if index + 1 < len(tokens) else None
                if previous == 'open' and value[:1].isspace():
                    value = value[1:]
                if following == 'close' and value[-1:].isspace():
                    value = value[:-1]
                parts.append(value)
                continue
            if value not in valid:
                continue
            if kind == 'open':
                if not stack or stack[-1] != value:
                    stack.append(value)
                    parts.append(links[value])
                continue
            if value not in stack:
                continue
            while stack:
                inner = stack.pop()
                parts.append(self._close_tag(links.get(inner)))
                if inner == value:
                    break
        while stack:
            inner = stack.pop()
            parts.append(self._close_tag(links.get(inner)))
        return ''.join(parts)

    def _polish_translation(self, translation):
        translation = translation.replace('\n', '<br/>')
        # Condense a run of the same letter down to four (some engines emit
        # repetition artifacts). Digits and underscores are excluded so that
        # legitimate content such as "100000000" is not silently corrupted.
        return re.sub(r'(([^\W\d_])\2{3})\2*', r'\1', translation)

    def _create_new_element(
            self, name, content='', copy_attrs=True, excluding_attrs=[]):
        # Copy the namespaces from the original namespaces to the new ones.
        namespaces = ' '.join(
            'xmlns%s="%s"' % ('' if name is None else ':' + name, value)
            for name, value in self.element.nsmap.items())
        new_element = etree.XML('<{0} {1}>{2}</{0}>'.format(
            name, namespaces, trim(content)))
        # Preserve all attributes from the original element.
        if copy_attrs:
            for name, value in self.element.items():
                if (name == 'id' and self.position != 'only') or \
                        name in excluding_attrs:
                    continue
                new_element.set(name, value)
        # Set the attributes for the new element.
        new_element.set('dir', self.target_direction or 'auto')
        if self.translation_lang is not None:
            new_element.set('lang', self.translation_lang)
        if self.translation_color is not None:
            new_element.set('style', 'color:%s' % self.translation_color)
        return new_element

    def add_translation(self, translation=None):
        # self.element.tail = None  # Make sure the element has no tail
        if self.original_color is not None:
            for element in self.element.iter():
                if element.text is not None or len(list(element)) > 0:
                    # Some users encountered errors when trying to set the
                    # style on certain child elements, e.g., comment nodes,
                    # so we simply skip those elements. Checking the type may
                    # be a better approach, e.g., isinstance(element.tag, str),
                    # since comment and processing instruction nodes do not
                    # have string tags, e.g., etree.Comment and etree.PI.
                    try:
                        element.set('style', 'color:%s' % self.original_color)
                    except TypeError:
                        log.warn(
                            'Failed to set style on element:',
                            get_string(element))
        if translation is None:
            if self.position in ('left', 'right'):
                self.element.addnext(self._create_table())
                self._safe_remove(self.element)
            return

        # Escape the markups (<m id=1 />) to replace escaped markups.
        translation = xml_escape(translation)
        missing = []
        for rid, element in enumerate(self.reserve_elements):
            pattern = self.placeholder[1].format(
                r'\s*'.join(format(rid, '05')))
            # Prevent processe any backslash escapes in the replacement.
            translation, count = re.subn(
                xml_escape(pattern), lambda _: element, translation)
            if count == 0:
                missing.append(element)
        # Keep reserved elements (images, footnote links, line breaks) that
        # the engine dropped instead of losing them silently.
        if missing:
            translation += ''.join(missing)
        prepared = translation
        linked = self._restore_links(prepared)
        try:
            new_element, translation = self._element_from_translation(linked)
        except Exception:
            # A broken marker must not discard the translation. If the drop
            # cap tag itself cannot be parsed, the words still stay.
            log.warning(
                'Could not restore inline formatting in the translation. '
                'The text was kept without that formatting.')
            plain = _LINK_MARKER.sub('', prepared)
            try:
                new_element, translation = self._element_from_translation(
                    plain)
            except Exception:
                translation = self._polish_translation(plain)
                new_element = self._create_new_element(
                    get_name(self.element), translation)
        element_name = get_name(self.element)

        # Add translation for table elements.
        group_elements = ('li', 'th', 'td', 'caption')
        if element_name in group_elements:
            if self.position == 'only':
                self.element.addnext(new_element)
                self._safe_remove(self.element)
            new_element = self._create_new_element(
                'span', translation, excluding_attrs=['class'])
            if self.position in ['left', 'above']:
                if self.element.text is not None:
                    if self.position == 'above':
                        br = etree.SubElement(self.element, 'br')
                        br.tail = self.element.text
                        self.element.insert(0, br)
                    else:
                        new_element.tail = ' ' + self.element.text
                    self.element.text = None
                self.element.insert(0, new_element)
            else:
                if self.position == 'below':
                    self.element.append(etree.SubElement(self.element, 'br'))
                else:
                    children = self.element.getchildren()
                    if len(children) > 0:
                        element = children[-1]
                        if element.tail is not None:
                            element.tail += ' '
                        else:
                            element.tail = ' '
                    else:
                        self.element.text += ' '
                self.element.append(new_element)
            return

        text_elements = (
            'a', 'em', 'strong', 'small', 's', 'cite', 'q', 'time', 'samp',
            'i', 'b', 'u', 'mark', 'span', 'data', 'del', 'ins')
        is_text_element = element_name in text_elements

        # Add translation for left or right position.
        if self.position in ('left', 'right') and not is_text_element:
            self.element.addnext(self._create_table(new_element))
            self._safe_remove(self.element)
            return

        # Add translation for line breaks.
        line_break_tag = '{%s}br' % ns['x']
        original_br_list = list(self.element.iterdescendants(line_break_tag))
        translation_br_list = list(new_element.iterchildren(line_break_tag))
        if (len(original_br_list) == len(translation_br_list) > 0) and \
                self.position in ('below', 'above'):
            self._add_translation_for_line_breaks(
                new_element, original_br_list, translation_br_list)
            return

        parent_element = self.element.getparent()
        is_table_descendant = parent_element is not None and \
            get_name(parent_element) in group_elements

        if self.position == 'only':
            self.element.addnext(new_element)
            self._safe_remove(self.element)
            return

        # new_element.tag = 'span'
        if self.position in ('left', 'above'):
            # Add translation next to the element.
            self.element.addprevious(new_element)
            # # Add translation at the start of the element.
            # new_element.tail = self.element.text
            # self.element.text = None
            # self.element.insert(0, etree.SubElement(self.element, 'br'))
            # self.element.insert(0, new_element)
            if is_text_element and is_table_descendant:
                new_element.addnext(etree.SubElement(self.element, 'br'))
            elif is_text_element:
                new_element.tail = ' '
        else:
            # Add translation next to the element.
            self.element.addnext(new_element)
            # # Added translation at the end of the element.
            # self.element.append(etree.SubElement(self.element, 'br'))
            # self.element.append(new_element)
            if is_text_element and is_table_descendant:
                self.element.addnext(etree.SubElement(self.element, 'br'))
            elif is_text_element:
                if self.element.tail is not None:
                    new_element.tail = self.element.tail
                self.element.tail = ' '

    def _add_translation_for_line_breaks(
            self, new_element, original_br_list, translation_br_list):
        text = new_element.text
        tail = None
        if self.position == 'below':
            for index, br in enumerate(original_br_list):
                translation_br = translation_br_list[index]
                wrapper = self._create_new_element(
                    'span', copy_attrs=False, excluding_attrs=['class'])
                # Get preceding siblings in reverse document order.
                for sibling in translation_br.itersiblings(preceding=True):
                    if get_name(sibling) == 'br':
                        break
                    wrapper.insert(0, sibling)
                wrapper.text = text if index == 0 else tail
                tail = translation_br.tail
                if wrapper.text or len(list(wrapper)) > 0:
                    new_br = etree.SubElement(self.element, 'br')
                    br.addprevious(new_br)
                    new_br.addnext(wrapper)
                # Handle the last br element in the translation simultaneously.
                if br == original_br_list[-1]:
                    # Ignore the last barely br element.
                    if translation_br.getnext() is None and (
                            tail is None or tail.strip() == ''):
                        continue
                    wrapper = self._create_new_element(
                        'span', copy_attrs=False, excluding_attrs=['class'])
                    for sibling in translation_br.itersiblings():
                        wrapper.append(sibling)
                    wrapper.text = tail
                    new_br = etree.SubElement(self.element, 'br')
                    self.element.append(new_br)
                    new_br.addnext(wrapper)
        else:
            for index, br in enumerate(original_br_list):
                translation_br = translation_br_list[index]
                wrapper = self._create_new_element(
                    'span', copy_attrs=False, excluding_attrs=['class'])
                for sibling in translation_br.itersiblings():
                    if get_name(sibling) == 'br':
                        break
                    wrapper.insert(0, sibling)
                wrapper.text = translation_br.tail
                if wrapper.text or len(list(wrapper)) > 0:
                    new_br = etree.SubElement(self.element, 'br')
                    new_br.tail = br.tail
                    br.tail = None
                    br.addnext(new_br)
                    new_br.addprevious(wrapper)
                if br == original_br_list[0]:
                    wrapper = self._create_new_element(
                        'span', copy_attrs=False, excluding_attrs=['class'])
                    if translation_br.getprevious() is None and (
                            text is None or text.strip() == ''):
                        continue
                    for sibling in translation_br.itersiblings(preceding=True):
                        wrapper.insert(0, sibling)
                    wrapper.text = new_element.text
                    new_br = etree.SubElement(self.element, 'br')
                    new_br.tail = self.element.text
                    self.element.text = None
                    self.element.insert(0, new_br)
                    new_br.addprevious(wrapper)

    def _create_table(self, translation=None):
        # table = self.element.makeelement('table', attrib={'width': '100%'})
        original = self._element_copy()
        table = etree.XML(
            '<table xmlns="{}" width="100%"></table>'.format(ns['x']))
        tr = etree.SubElement(table, 'tr')
        td_left = etree.SubElement(tr, 'td', attrib={'valign': 'top'})
        td_middle = etree.SubElement(tr, 'td')
        td_right = etree.SubElement(tr, 'td', attrib={'valign': 'top'})
        if self.column_gap is None:
            td_left.set('width', '45%')
            td_middle.set('width', '10%')
            td_right.set('width', '45%')
        else:
            unit, value = self.column_gap
            if unit == 'percentage':
                width = '%s%%' % round((100 - value) / 2)
                td_left.set('width', width)
                td_middle.set('width', '%s%%' % value)
                td_right.set('width', width)
            else:
                td_left.set('width', '50%')
                td_middle.text = '\xa0' * value
                td_right.set('width', '50%')
        if self.position == 'left':
            if translation is not None:
                td_left.append(translation)
            td_right.append(original)
        if self.position == 'right':
            td_left.append(original)
            if translation is not None:
                td_right.append(translation)
        return table


class Extraction:
    # List the necessary non-inline-level elements according to the HTML spec:
    # https://html.spec.whatwg.org/multipage/rendering.html#non-replaced-elements
    non_inline_elements = (
        'address', 'blockquote', 'dialog', 'div', 'figure', 'figcaption',
        'footer', 'header', 'legend', 'main', 'p', 'pre', 'search', 'article',
        'aside', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hgroup', 'nav',
        'section', 'dd', 'dl', 'dt', 'menu', 'ol', 'ul', 'table', 'caption',
        'colgroup', 'col', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'li')

    def __init__(
            self, pages, priority_rules, rule_mode, filter_scope, filter_rules,
            ignore_rules):
        self.pages = pages
        self.priority_rules = priority_rules
        self.rule_mode = rule_mode
        self.filter_scope = filter_scope
        self.filter_rules = filter_rules
        self.ignore_rules = ignore_rules

        self.priority_patterns = []
        self.filter_patterns = []
        self.ignore_patterns = []

        self.load_priority_patterns()
        self.load_filter_patterns()
        self.load_ignore_patterns()

    def load_priority_patterns(self):
        default_selectors = [
            'p', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote']
        self.priority_patterns = css_to_xpath(
            default_selectors + self.priority_rules)

    def load_filter_patterns(self):
        default_filter_rules = (
            r'^[-\d\s\.\'\\"‘’“”,=~!@#$%^&º*|≈<>?/`—…+:–_(){}[\]]+$',)
        patterns = [re.compile(rule) for rule in default_filter_rules]
        for rule in self.filter_rules:
            if self.rule_mode == 'normal':
                rule = re.compile(re.escape(rule), re.I)
            elif self.rule_mode == 'case':
                rule = re.compile(re.escape(rule))
            else:
                rule = re.compile(rule)
            patterns.append(rule)
        self.filter_patterns = patterns

    def load_ignore_patterns(self):
        default_selectors = ['pre', 'code']
        self.ignore_patterns = css_to_xpath(
            default_selectors + self.ignore_rules)

    def get_sorted_pages(self):
        pages = []
        pattern = re.compile(r'\.(xhtml|html|htm|xml|xht)$')
        for page in self.pages:
            if isinstance(page.data, etree._Element) \
                    and pattern.search(page.href):
                pages.append(page)
        return sorted(pages, key=lambda page: sorted_mixed_keys(page.href))

    def get_elements(self):
        elements = []
        for page in self.get_sorted_pages():
            body = page.data.find('./x:body', namespaces=ns)
            elements.extend(self.extract_elements(page.id, body, []))
        return filter(self.filter_content, elements)

    def is_priority(self, element: etree._Element):
        for pattern in self.priority_patterns:
            if element.xpath(pattern, namespaces=ns):
                return True
        return False

    def is_inline_only(self, element: etree._Element):
        """The purpose of this method is to ensure that if the element contains
        only inline-level elements, it is regarded as a whole paragraph; there
        is no need to further separate its children.
        """
        for tag in self.non_inline_elements:
            if element.find(f'./x:{tag}', namespaces=ns) is not None:
                return False
        return True

    def need_ignore(self, element):
        for pattern in self.ignore_patterns:
            if element.xpath(pattern, namespaces=ns):
                return True
        return False

    def extract_elements(self, page_id, root, elements=[]):
        """If the root matches the pattern, return an empty list; otherwise,
        just break the recursion without doing anything.
        """
        if self.need_ignore(root):
            return []
        for element in root.findall('./*'):
            if self.need_ignore(element):
                elements.append(PageElement(element, page_id, True))
                continue
            element_has_content = False
            if self.is_priority(element) or self.is_inline_only(element) or (
                    element.text is not None and trim(element.text) != ''):
                element_has_content = True
            else:
                children = element.findall('./*')
                if children and self.is_priority(element):
                    element_has_content = True
                else:
                    for child in children:
                        if child.tail is not None and trim(child.tail) != '':
                            element_has_content = True
                            break
            if element_has_content:
                elements.append(PageElement(
                    element, page_id, self.need_ignore(element)))
            else:
                self.extract_elements(page_id, element, elements)
        # Return root if all children have no content
        return elements if elements else [
            PageElement(root, page_id, self.need_ignore(root))]

    def filter_content(self, element):
        # Ignore the element contains empty content
        content = element.get_text()
        if content == '':
            return False
        for entity in ('&lt;', '&gt;'):
            content = content.replace(entity, '')
        for pattern in self.filter_patterns:
            if pattern.search(content):
                element.set_ignored(True)
        # Filter HTML according to the rules
        if self.filter_scope == 'html':
            markup = element.get_raw()
            for pattern in self.filter_patterns:
                if pattern.search(markup):
                    element.set_ignored(True)
        return True


class ElementHandler:
    def __init__(self, placeholder, separator, position):
        self.placeholder = placeholder
        self.separator = separator
        self.position = position

        self.merge_length = 0
        self.target_direction = None

        self.translation_lang = None
        self.original_color = None
        self.translation_color = None
        self.column_gap = None

        self.remove_pattern = None
        self.reserve_pattern = None

        self.elements = {}
        self.originals = []

    def set_merge_length(self, length):
        self.merge_length = length

    def get_merge_length(self):
        return self.merge_length

    def set_target_direction(self, direction):
        self.target_direction = direction

    def set_translation_lang(self, lang):
        self.translation_lang = lang

    def set_original_color(self, color):
        self.original_color = color

    def set_translation_color(self, color):
        self.translation_color = color

    def set_column_gap(self, values):
        if isinstance(values, tuple) and len(values) == 2:
            self.column_gap = values

    def load_remove_rules(self, rules=[]):
        default_rules = ('rt', 'rp')
        self.remove_pattern = create_xpath(default_rules + tuple(rules))

    def load_reserve_rules(self, rules=[]):
        # Reserve the <br> element instead of using a line break to prevent
        # conflicts with the mechanism of merge translation.
        default_rules = (
            'img', 'code', 'br', 'hr', 'sub', 'sup', 'kbd', 'abbr', 'wbr',
            'var', 'canvas', 'svg', 'script', 'style', 'math')
        self.reserve_pattern = create_xpath(default_rules + tuple(rules))

    def prepare_original(self, elements):
        count = 0
        for oid, element in enumerate(elements):
            element.set_placeholder(self.placeholder)
            element.set_position(self.position)
            element.set_target_direction(self.target_direction)
            element.set_translation_lang(self.translation_lang)
            element.set_original_color(self.original_color)
            element.set_translation_color(self.translation_color)
            if self.column_gap is not None:
                element.set_column_gap(self.column_gap)
            element.set_remove_pattern(self.remove_pattern)
            element.set_reserve_pattern(self.reserve_pattern)
            raw = element.get_raw()
            content = element.get_content()
            # Make sure the element does not contain empty content because it
            # may only contain ignored elements.
            if content.strip() == '':
                element.set_ignored(True)
            md5 = uid('%s%s' % (oid, content))
            attrs = element.get_attributes()
            if not element.ignored:
                self.elements[count] = element
                count += 1
            self.originals.append((
                oid, md5, raw, content, element.ignored, attrs,
                element.page_id))
        return self.originals

    def prepare_translation(self, paragraphs):
        translations = {}
        for paragraph in paragraphs:
            translations[paragraph.original] = paragraph.translation
        return translations

    def add_translations(self, paragraphs):
        translations = self.prepare_translation(paragraphs)
        for eid, element in self.elements.copy().items():
            if element.ignored:
                element.add_translation()
                continue
            original = element.get_content()
            translation = translations.get(original)
            if translation is None:
                element.add_translation()
                continue
            element.add_translation(translation)
            self.elements.pop(eid)


class ElementHandlerMerge(ElementHandler):
    def prepare_original(self, elements):
        raw = ''
        txt = ''
        oid = 0
        for eid, element in enumerate(elements):
            self.elements[eid] = element
            if element.ignored:
                continue
            element.set_placeholder(self.placeholder)
            element.set_position(self.position)
            element.set_target_direction(self.target_direction)
            element.set_translation_lang(self.translation_lang)
            element.set_original_color(self.original_color)
            element.set_translation_color(self.translation_color)
            if self.column_gap is not None:
                element.set_column_gap(self.column_gap)
            element.set_remove_pattern(self.remove_pattern)
            element.set_reserve_pattern(self.reserve_pattern)
            code = element.get_raw()
            content = element.get_content()
            content += self.separator
            if len(txt + content) < self.merge_length:
                raw += code + self.separator
                txt += content
                continue
            elif txt:
                md5 = uid('%s%s' % (oid, txt))
                self.originals.append((oid, md5, raw, txt, False))
                oid += 1
            raw = code
            txt = content
        md5 = uid('%s%s' % (oid, txt))
        if txt:
            self.originals.append((oid, md5, raw, txt, False))
        return self.originals

    def align_paragraph(self, paragraph):
        # Compatible with using the placeholder as the separator.
        if paragraph.original[-2:] != self.separator:
            pattern = re.compile(
                r'\s*%s\s*' % self.placeholder[1].format(r'(0|[^0]\d*)'))
            paragraph.original = pattern.sub(
                self.separator, paragraph.original)
            if paragraph.translation is not None:
                paragraph.translation = pattern.sub(
                    self.separator, paragraph.translation)
        # Ensure the translation count matches the actual elements count.
        originals = paragraph.original.strip().split(self.separator)
        if paragraph.translation is None:
            return list(zip(originals, [None] * len(originals)))
        pattern = re.compile('%s+' % self.separator)
        translation = pattern.sub(self.separator, paragraph.translation)
        translations: list[Any] = translation.strip().split(self.separator)
        offset = len(originals) - len(translations)
        if offset > 0:
            if self.position in ['left', 'right']:
                addition = [None] * offset
                translations += addition
            else:
                merged_translations = '\n\n'.join(translations)
                translations = [None] * (len(originals) - 1)
                if self.position in ['above']:
                    translations.insert(0, merged_translations)
                else:
                    translations.append(merged_translations)
        elif offset < 0:
            offset = len(originals) - 1
            translations = translations[:offset] + [
                '\n\n'.join(translations[offset:])]
        return list(zip(originals, translations))

    def prepare_translation(self, paragraphs):
        translations = []
        for paragraph in paragraphs:
            translations.extend(self.align_paragraph(paragraph))
        return dict(translations)


def get_srt_elements(path, encoding):
    from .srt import get_srt_elements as _get_srt_elements
    return _get_srt_elements(path, encoding)


def get_pgn_elements(path, encoding):
    pattern = re.compile(r'\{[^}]*[a-zA-z][^}]*\}')
    originals = pattern.findall(open_file(path, encoding))
    return [PgnElement([original, None]) for original in originals]


def get_metadata_elements(metadata):
    config = get_config()
    enable_translation = config.get(
        'ebook_metadata.metadata_translation', False)
    elements = []
    names = (
        'title', 'creator', 'publisher', 'rights', 'subject', 'contributor',
        'description')
    for key in metadata.iterkeys():
        if key not in names:
            continue
        items = getattr(metadata, key)
        for item in items:
            if not any(char.isalpha() for char in item.content):
                continue
            element = MetadataElement(
                item, page_id='content.opf', ignored=not enable_translation)
            elements.append(element)
    return elements


def get_toc_elements(nodes, elements=[]):
    """Be aware that elements should not overlap with existing data."""
    for node in nodes:
        elements.append(TocElement(node, 'toc.ncx'))
        if len(node.nodes) > 0:
            get_toc_elements(node.nodes, elements)
    return elements


def get_page_elements(pages):
    config = get_config()
    priority_rules = config.get('priority_rules')
    rule_mode = config.get('rule_mode')
    filter_scope = config.get('filter_scope')
    filter_rules = config.get('filter_rules', [])
    ignore_rules = config.get('ignore_rules', config.get('element_rules', []))
    extraction = Extraction(
        pages, priority_rules, rule_mode, filter_scope, filter_rules,
        ignore_rules)
    return extraction.get_elements()


def get_element_handler(placeholder, separator, direction, input_format=None):
    if input_format == 'srt':
        from .srt import get_srt_element_handler
        return get_srt_element_handler(placeholder, separator, direction)
    config = get_config()
    position_alias = {'before': 'above', 'after': 'below'}
    position = config.get('translation_position') or 'below'
    position = position_alias.get(position) or position
    handler = ElementHandler(placeholder, separator, position)
    if config.get('merge_enabled'):
        handler = ElementHandlerMerge(placeholder, separator, position)
        handler.set_merge_length(config.get('merge_length'))
    handler.set_target_direction(direction)
    column_gap = config.get('column_gap') or {}
    gap_type = column_gap.get('_type')
    if gap_type is not None and gap_type in column_gap.keys():
        handler.set_column_gap((gap_type, column_gap.get(gap_type)))
    handler.set_original_color(config.get('original_color'))
    handler.set_translation_color(config.get('translation_color'))
    handler.load_remove_rules(
        config.get('ignore_rules', config.get('element_rules', [])))
    handler.load_reserve_rules(config.get('reserve_rules', []))
    return handler
