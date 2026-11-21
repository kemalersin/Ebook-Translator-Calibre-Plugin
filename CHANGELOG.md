## v2.4.13

1. fix: Footnote numbers in an EPUB are written once. The reserved notes are rebuilt on every extraction, so the second pass no longer appends the same numbers after the translation.
2. fix: A worksheet row of dots without a page number stays one paragraph. It is no longer treated as a contents line.

---

## v2.4.12

1. fix: Bold, italic and the other inline runs in an EPUB keep their tags. The words are translated between the same `⟦n⟧` markers as links, and a dropped marker leaves the translation as plain text.
2. fix: Links in EPUB (and other HTML-based) translations keep their address. The visible words are translated between `⟦n⟧` and `⟦/n⟧` markers and written back inside the link. A marker the engine drops or invents is removed, and the translated words stay.

---

## v2.4.10

1. change: PDF output writes each paragraph in one placement, reads a page once when it restores links, and skips the drawing scan on a page that has none. Stacked lines are cleared together. Wrapping, color, links, underlines and leaders stay the same.

---

## v2.4.9

1. fix: A chapter label and the title beside it on the same contents line stay separate entries. The repeated gap between them is read from the glyphs. A single wide gap in justified text still stays one sentence.

---

## v2.4.8

1. fix: A word printed twice for a fake bold is read once, so the heading is translated as the word on the page.
2. fix: A stored paragraph is replaced when its text changes. The translation follows the current extraction instead of an older one that no longer matches the page.
3. fix: Text keeps the color it was extracted with. The translation-color setting no longer turns light text black on a colored band.
4. fix: Cells in a borderless shaded table, and columns separated by a wide gap, stay separate. A running header on one row stays apart from the title beside it, and a short footnote beside a totals block stays with its paragraph.
5. fix: A contents row keeps its page number and its dot leader. Only the label is translated, and the row does not grow into the next one.
6. fix: Text flows down the free side of a picture and continues underneath it at the same line spacing. A fill the letters sit on stays a background.
7. fix: The tail of a hyphenated word stays with its paragraph, and a raised note number is written back as a superscript.
8. change: In-place PDF translations use the cache suffix `pdf-layout-v2`.

---

## v2.4.7

1. fix: Two short lines, and a long line followed by a short one, are no longer treated as justified. The translation is no longer stretched until the gaps swallow the line.
2. fix: Dot leaders and padding spaces inside a link are not sent for translation. The link and its drawn underline follow the translated words instead of keeping the original width.

---

## v2.4.6

1. fix: A paragraph frame no longer runs through the next line when the line boxes overlap. The continuation is kept in the same paragraph, and the translation uses the free space below it instead of being written on top of the following line. A full-page picture no longer widens a single line to the page edge.

---

## v2.4.5

1. fix: An inline marker that does not belong to the paragraph no longer aborts PDF output. A merged translation can carry a marker such as `⟦1254⟧` from another paragraph; that marker is skipped and the paragraph is still written.

---

## v2.4.4

1. feat: Translate a PDF in place so the output keeps the original page layout. The path stays off unless `pdf_layout.enabled` is on, and it runs only when both the input and the output are PDF. PyMuPDF is installed into the Calibre configuration folder when it is missing.
2. feat: Keep inline color, bold, italic and links. Runs that differ from the paragraph style are sent between `⟦n⟧` and `⟦/n⟧` markers and written back as styled text. A link is placed on the translated words.
3. feat: A custom PDF font also loads a bold, italic or bold-italic face from the same folder when the file name ends with `bd`, `b`, `i`, `bi` or `z`.
4. fix: Google(Free)New keeps a style marker whole when a request is split at the URL limit.
5. change: In-place PDF translations use the cache suffix `pdf-layout-v1`, so they do not reuse a cache built by Calibre's PDF conversion.
6. docs: Add `docs/PDF_LAYOUT.md`, including how to turn the layout on from the CLI.

---

## v2.4.3

1. fix: SRT subtitles are split by timing lines and every cue is translated under its own timestamp. Merged requests carry numbered cue markers, so translations no longer pile up under one cue.
2. fix: SRT groups whose markers do not survive the translation are re-translated cue by cue.
3. fix: Blank lines, timing lines and leftover markers are removed from SRT translations; inline tags such as `<i>` and `{\an8}` are preserved.
4. fix: Google(Free)New splits requests that exceed the URL limit instead of failing with HTTP 400, and re-translates paragraphs when the service merges, drops or repeats them.
5. fix: Requests rejected with a permanent error are no longer retried.
6. change: SRT cache ids carry a version suffix (`srt-v2`); earlier SRT caches are not reused.
7. docs: Add `docs/ARCHITECTURE.md`, `docs/SRT_ANALYSIS.md` and `docs/GOOGLE_FREE_NEW.md`.

---

## v2.4.2

1. feat: Move cache path from temp to a persistent folder. Credit to @Sansui233.
2. feat: Add toggleable horizontal/vertical review layout. Credit to @lazydao.
3. feat: Add support for SOCKS5 proxy. @Credit to Milor123.
4. feat: add Japanese UI language. Credit to @Utakata.
5. feat: Add a button to toggle word wrap. Credit to @lazydao.
6. fix: Update Microsoft Edge translate.
6. fix: incorrect output format for PGN files.
7. fix: failed to fetch GenAI models list.
8. All Bug fixes and feature enhancements. (8e91461...d56b4d4)

---

## v2.4.1

Added features and fixed bugs as follows:

1. feat: Add line numbers and synced scrolling in review editor.
2. feat: Add Persian language support for Gemini.
3. feat: Improve the clarity of the model fetching alert.
4. fix: Fix the bug that caused incorrect configuration migration.
5. fix: Fix a bug that prevents converting PNG and SRT files.
6. All Bug fixes and feature enhancements. (aefc9c1...75804a4)

---

## v2.4.0

Added features and fixed bugs as follows:

1. feat: Automatically retrieve available GenAI models.
2. feat: Add a new version of the Google Translate API to the engines.
3. feat: Add ChatGPT batch translation feature.
4. feat: Add DeepSeek to the built-in engines. Credit to @jcodingSpace.
5. feat: Change the Gemini API version to v1beta.
6. feat: Remove all Python 2.x-compatible and PyQt5-compatible code.
7. feat: Store the plugin's window settings in the Calibre plugin folder.
8. feat: Improve prompts for various GenAI models. Credit to @seidnerj.
9. feat: Update Spanish UI translation. Credit to @Dunhill69.
10. feat: Support Gemini Flash. Credit to @Kentoseth.
11. feat: Update supported languages for the Google Translate engine.
12. feat: Update supported languages for DeepL.
13. fix: Fix an XML namespace bug that was preventing ebook output.
14. fix: Fix an issue with line-break paragraph translation.
15. fix: Restore special characters in Google Translate ADC results.
16. Other Bug fixes and feature enhancements. (c4808c7...d2583ba)

---

## v2.3.5

Added features and fixed bugs as follows:

1. feat: Added the option to enable/disable metadata translation.
2. feat: Updated Turkish translation. Credit to @DogancanYr
3. fix: Fixed the bug that caused output failure due to the lack of namespaces.
4. fix: Fixed the bug that prevented retry failure translation.
5. fix: Fixed the saved config display bug for the custom engine.

---

## v2.3.4

Added features and fixed bugs as follows:

1. feat: Added filtering feature to Advanced mode.
2. feat: Supports reserving elements using CSS selectors.
3. feat: Supports setting priority elements for extraction.
4. feat: Added translation status indicator. Credit to @Andy-AO.
5. feat: Added non-aligned items counter in Advanced mode. Credit to @Andy-AO.
6. feat: Marks failed translations with red color.
7. feat: Shows last modification time for caches on cache manager.
8. feat: Supports disabling and auto-hiding notifications.
9. feat: Added keyboard shortcut and mnemonics features.
10. feat: Allows users to choose the text direction for translation.
11. feat: Separated translation progress from conversion progress.
12. fix: Fixed bug preventing setting original text color.
13. fix: Fixed bug when adding translation for line breaks.
14. fix: Fixed bug causing error when deleting rows.
15. fix: Properly shows item status after saving edits.

---

## v2.3.3

Added features and fixed bugs as follows:

1. feat: Added Claude as a translation engine. #232
2. feat: Optimized adding translation feature. #244
3. feat: Added Turkish UI translation. Credit to @DogancanYr
4. feat: Show details while extracting the content of an ebook.
5. feat: Support choosing encoding when translating plain text.
6. fix: Fixed the bug causing output errors on Windows. #242
7. fix: Fixed the bug preventing cache deletion on Windows. #246

---

## v2.3.2

Fixed bugs as follows:

1. Fixed the bug preventing output when file lacks metadata. #234, #233
2. Fixed the bug in processing srt/pgn formats in lower versions of Calibre.
3. Fixed the bug causing disorderly translation when translating ebooks partially.

---

## v2.3.1

Fixed bugs as follows:

1. Fixed the bug to be compatible with lower versions of Calibre.
2. Fixed the freezing issue when using the cache with multiple threads.

---

## v2.3.0

Added features:

1. Added new translation engine of Google Gemini Pro.
2. Added support for SubRip Text (.srt) translation. #162
3. Added support for Portable Game Notation (.pgn) translation. #207
4. Added support for side-by-side translation position. Credit to @tiropictor
5. Added customization for changing the original color.
6. Added feature to translate ebook metadata. #216
7. Added a new UI language for French. Credit to @miiPoP
8. Added a new UI language for Portuguese. Credit to Marcelo Duarte
9. Added the context menu to cache manager for deleting caches.
10. Added support for specifying a custom model for ChatGPT. #167
11. Added support to count item and character in Advanced mode. #130

Enhanced features:

1. Supported to restore user-adjusted geometry and size for the Setting window.
2. Changed the separator to dual-newline for the "Merge to Translate" feature.
3. Removed all potential non-printable characters that interfere translation. #189
4. Highlighted inconsistency between the original and translation lines. #82
5. Enabled Google(Free) to handle the long content that exceeds length limit.


Fixed bugs:

1. Fixed bugs that caused deadlock freezes. #170, #182, #186
2. Fixed the bug of removing all subjects from the metadata. #171
3. Fixed malformed request bug in custom engine. #189

Check all the bug fixes and feature enhancements: 682f73f...a871b7a

---

## v2.2.0

Fixed bugs and added features:

1. feat: Added the ability to translate the TOC in the NCX file. #96, #105
2. feat: Added feature to preserve all attributes from the original element. #104
3. fix: Resolved the conflict on saving the engine settings.
4. fix: Enhanced the feature of processing headings as other priority elements. #128
5. fix: Fixed the bug preventing proxy access from the OS. #129
6. fix: Added a user agent to the request header of ChatGPT. #133
7. Other bug fixing and feature enhancement. cb764a4...8b39d86

---

## v2.1.4

Fixed bugs and added feature:

1. Fixed the bug that caused malformed requests due to language variants.
2. Fixed the bug that caused unexpected termination of the translation process.
3. Added the cache status indicator for the advanced mode.

---

## v2.1.3

Fixed bugs as follows:

1. Fixed BOM-caused glossary's first word bug. #67
2. Fixed a bug that prevented cache removal and deletion. #90
3. Fixed a bug that prevented the proper storage of translated ebooks. #106
4. Fixed an asyncio-related bug that was crashing the translation process. #110
5. Removed special characters from translation. #112
6. Fixed the bug where the queue couldn't get the event loop. #115
7. Fixed the bug where settings could not be displayed properly. #116
8. Speed up cache storage on windows.

---

## v2.1.2

Enhanced features and fixed bugs:

1. Terminate translation if error occurs in batch mode.
2. Keep hyperlinks when choosing translation position as "Add without original."
3. Fixed a cache-related bug that caused the plugin to crash.
4. Fixed a bug that prevented the restoration of words in the glossary.

---

## v2.1.1

1. Fixed the bug that caused the saving of setting to not work.

---

## v2.1.0

Added/enhanced features and fixed bugs as follows:

1. Optimized concurrency to speed up the translation process.
2. Added the concurrency feature for lower versions of Calibre.
3. Added Cache Manager for cache management (moving/deleting cache).
4. Optimized the glossary feature to be compatible with multiple platforms.
5. Fixed the bug that was unable to parse DeepL responses.
6. Other bug fixing and feature enhancement. 969659e...deac58c

---

## v2.0.3

Fixed bugs and improved features as follows:

1. Changed the setting of HTTP Request from general to engine-specific.
2. Modified the Request Interval from a random value to a fixed value.
3. Fixed the bug that was unable to disable translation logging.
4. Fixed the bug that special characters in ebook titles caused an output error.
5. Fixed the bug in the restoration of the default endpoint for ChatGPT.
6. Fixed the bug that caused conversion interrupts due to invalid escapes.
7. Fixed the bug that caused subsequent jobs to fail after a failed job.
8. Changed the model names for Azure ChatGPT.

---

## v2.0.2

Fixed bugs, improved, and added features as follows:

1. Added Microsoft Edge Translator to built-in engines. PR by @wanghaosjtu
2. Added support to set Target Language and subjects for ebook metadata.
3. Optimized the performance of filtering elements.
4. Fixed the bug that cannot save custom engine data properly.
5. Removed ignored paragraphs during merge translation.

---


## v2.0.1

Optimized features and fixed bugs as follows:

1. Fixed the bug that could not split the translation by ID properly.
2. Fixed the bug that could not handle incomplete read of the stream response.
3. Fixed the bug where the auto-switch API Keys feature did not work properly.
4. Fixed the bug where the original was not removed when turned on "translation only".
5. Fixed the bug that could not remove the Ruby Fallback Parenthesis element.
6. Fixed the bug where translation engines were breaking the glossary markup.
7. Fixed the bug that could not filter out empty content.
8. Optimized the cache and glossary features.

---

## v2.0.0

Added/improved features and fixed bugs:

1. Added "Advanced Mode" for translation fine-grain control. resolved #18, resolved #24
2. Added more options such as endpoint/sampling/model to ChatGPT. resolved #34, resolved #46
3. Supported multiple API keys for all built-in translation engines. resolved #37
4. Added "Ignore Element" feature for excluding unneeded elements. resolved #41
5. Enhanced cache feature to include more information for reviewing. resolved #42
6. Added preferred input/output formats and preferred source language options. resolved #44
7. Added Azure ChatGPT to built-in translation engines. resolved #49
8. Added "Preferred Mode" for clicking icon button in general settings.
9. Added "Timeout" option to "HTTP Request" setting.
10. Optimized ChatGPT prompt for more accurate translating.
11. Fixed the bug that cannot filter paragraphs properly. fixed #41, fixed #45
12. Optimized user experiences, improved features and fix bugs.

---

## v1.3.8

Fixed the following bugs:

1. Fixed the bug that prevented the storage of "Ignore Paragraph" rules.
2. Fixed the bug that caused paragraph translations to be skipped or ignored.
3. Fixed the bug that caused translation interruptions while handling certain content. fixed #40

---

## v1.3.7

Added and improved features as follows:

1. Supported concurrent requests (only for Calibre 5.0 or higher).
2. Added filter scope options to the "Ignore Paragraph" feature.
3. Supported to ignore the translation of pre/code elements.

---

## v1.3.6

Improved functionality and fixed bugs:

1. Removed the old approach for extracting elements to avoid missing translate.
2. Limited the length of filenames to 200 characters to prevent storage errors.
3. Fixed bug when updating plugin to latest version from lower version.
4. Made the plugin compatible with lower versions of Calibre.

---

## v1.3.5

Improved functionality and added test suites:

1. "Merge to Translate" was allowed in the custom translation engine.
2. Optimized "Merge to Translate" for ChatGPT and the custom translator.
3. Added some test cases for key functionality like elements extraction.

---

## v1.3.4

1. Fixed the bug that ChatGPT couldn't retain marks on "Merge to Translate".

---

## v1.3.3

Added features and fixed bug as follows:

1. Added new translation engines: Google (Basic), Google (Advanced).
2. Changed request method to GET for Google (Free) due to "Too Many Requests" error.
3. Added retry timestamp in translation log.

---

## v1.3.2

Fixed the following bugs:

1. Bug that affected plugin usability when handling images.
2. Bug that caused the engine setting to display an incorrect name.
3. Bug that prevented inputting URI scheme into the proxy host.

---

## v1.3.1

1. Remove the restriction on inputting key for Baidu.

---

## v1.3.0

Added the following new features and enhanced functionality:

1. Added "Merge to Translate (Beta)" feature.
2. Supported to set preferred target language.
3. Supported to use glossary file to define precise translation.
4. Added public version of DeepL translation engine.
5. Added server-sent events support for ChatGPT.

---

## v1.2.2

1. Optimized the feature of custom translation engine.

---

## v1.2.1

1. Fixed the bug that caused DeepL to not work properly. fixed #16

---

## v1.2.0

Added the following features:

1. Added custom translation engine feature. resolved #6, resolved #13
2. Added translation engine testing feature.
3. Added the display of usage information for some translation engine. resolved #8

---

## 1.1.0

Added/improved features and fixed bugs:

1. Supported to customize translation text position and delete original content. resolved #7
2. Supported to exclude original content by keyword and regular expression. resolved #2
3. Added Baidu and Youdao translation engines. resolved #3
4. Changed to save translated ebooks as a new book in Calibre library.
5. Supported to customize the color of translation text.
6. Supported to customize ChatGPT prompt word. resolved #4
7. Ignored to translate phonetic symbols (e.g. Japanese). fixed #3
8. Added Spanish as supported interface language. resolved #5
9. Added diagnosis information to log.
10. Added "lang" attribute at translation element.
11. Fixed plugin icon disappearance when changing Calibre interface language.
12. Improved the functionality to extract original text.

---

## v1.0.2

1. Resolved translation loss during mobi to mobi conversion.
2. Optimized the attempt intervals for failed requests.
3. Changed output file extension from uppercase to lowercase.

---

## v1.0.1

Fixed the bug on clicking YES to open ebook on finish notification.

---

## v1.0.0

First release.
