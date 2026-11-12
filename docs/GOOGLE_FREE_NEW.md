# Google(Free)New Motoru: Anatomi, Saptamalar ve Çözüm Önerileri

Bu doküman eklentinin varsayılan çeviri motoru `Google(Free)New`'i (`GoogleFreeTranslateNew`) uçtan uca inceler. Kapsamı:

- Motorun nasıl çalıştığı.
- Birleştirme (merge) açıkken metnin kesilme riski.
- Ölçümle doğrulanan sorunlar.
- Test edilmiş çözüm kodu.

> İlgili dokümanlar: genel mimari için [`ARCHITECTURE.md`](ARCHITECTURE.md), SRT altyazı çevirisi için [`SRT_ANALYSIS.md`](SRT_ANALYSIS.md). Bu dokümandaki çözüm, SRT dokümanındaki çözümle birlikte uygulanabilir (bkz. [Bölüm 10](#10-srt-dokümanıyla-ilişki)).

## İçindekiler

1. [Özet](#1-özet)
2. [Kapsam ve yöntem](#2-kapsam-ve-yöntem)
3. [Motorun anatomisi](#3-motorun-anatomisi)
4. [Birleştirme ve kesilme riski](#4-birleştirme-ve-kesilme-riski)
5. [Saptamalar](#5-saptamalar)
6. [Çözüm mimarisi](#6-çözüm-mimarisi)
7. [Uygulama yönergeleri (dosya dosya)](#7-uygulama-yönergeleri-dosya-dosya)
8. [Testler](#8-testler)
9. [Doğrulama sonuçları](#9-doğrulama-sonuçları)
10. [SRT dokümanıyla ilişki](#10-srt-dokümanıyla-ilişki)
11. [Kabul kriterleri](#11-kabul-kriterleri)
12. [Sınırlamalar ve isteğe bağlı iyileştirmeler](#12-sınırlamalar-ve-isteğe-bağlı-iyileştirmeler)
13. [Uygulama sırası (kontrol listesi)](#13-uygulama-sırası-kontrol-listesi)

---

## 1. Özet

- **Prompt yok.** `Google(Free)New` bir LLM değil, Google Translate'in klasik makine çevirisi uç noktası. Sağlayıcıya yalnızca metin, kaynak dil kodu ve hedef dil kodu gider. Sınıf, LLM motorlarının ortak atası olan `GenAI`'den değil doğrudan `Base`'den türer. Prompt, model ve sıcaklık (temperature) gibi alanları yoktur.
- **Birleştirme metni kesmez.** Paragraflar `merge_length` sınırına göre gruplanırken hiçbir paragraf bölünmez; son paragraf da yarıda kalmaz. Sınıra sığmayan paragraf bir sonraki gruba geçer. Tek başına sınırı aşan paragraf, sınırı aşarak kendi grubunda gider.
- **Gerçek riskler başka yerde.** Bu motor için ölçümle doğrulananlar:
  1. **URL sınırı (~16 KB).** Metin URL'ye yüzde kodlamayla eklenir. Japonca/Çince bir karakter 9 bayt tutar, bu yüzden varsayılan `merge_length` olan 1800 karakter bile Japoncada **HTTP 400** alır.
  2. **Hata yönetimi.** 400 yanıtı geçici hata sanılıp boşuna 3 kez yeniden deneniyor. Ardışık hata sayacı hızla dolup bütün işi iptal edebiliyor.
  3. **Paragraf tekrarı ve kaybı.** Birleştirilmiş grupta, kelimeye yapışık yer tutucular olduğunda Google bir paragrafı iki kez yazabiliyor ya da iki paragrafı birleştirebiliyor. Bu, eklentinin hizalamasını bozuyor: çeviriler yanlış paragrafların altına yazılıyor. Davranış tekrarlanabilir.
  4. **Yer tutucu kaybı.** Kelimeye yapışık yer tutucuları (dipnot, satır içi görsel) Google çoğunlukla siliyor. Eklenti bu durumda korunan öğeyi çeviriden **sessizce düşürüyor**.
- **Çözüm (test edildi).**
  - URL bütçesine göre metni paragraf, satır, cümle ve kelime sınırlarından parçalama.
  - 400 yanıtını kalıcı hata sayma.
  - Paragraf sayısı tutmazsa grubu paragraf paragraf yeniden çevirme.
  - Kaybolan korunmuş öğeleri çevirinin sonuna geri ekleme.
  - Yanıt sıkıştırmasını kapatma.
- **Doğrulama.** Mevcut 268 testin hepsi geçiyor; 14 yeni test eklendi, toplam 282 test başarılı. Önceden 400 alan bütün metinler ve paragraf tekrarı senaryosu gerçek uç noktada doğru sonuç verdi.

---

## 2. Kapsam ve yöntem

- **İncelenen kod:** `engines/google.py`, `engines/base.py`, `lib/utils.py` (`request`), `lib/translation.py`, `lib/handler.py`, `lib/element.py`, `lib/config.py`, `setting.py`, `components/engine.py`.
- **Yerel ölçüm:** Eklentinin kendi `request()` fonksiyonu yerel bir HTTP sunucusuna yönlendirildi. Giden yöntem, yol ve gövde kaydedildi.
- **Canlı ölçüm:** Gerçek uç noktaya (`translate-pa.googleapis.com`) toplam birkaç düzine küçük istek atıldı. Ölçülenler: yanıt biçimi, sıkıştırma, uzunluk sınırı, yer tutucu ve paragraf korunması.
- **Çözüm:** Önce ayrı bir kopyada test edildi, ardından ana repoya uygulandı ve orada yeniden doğrulandı ([Bölüm 9](#9-doğrulama-sonuçları)). Bölüm 3 ve 5'teki kod alıntıları, değişiklik öncesi hali (commit `d312d6d`) gösterir.
- **Test çalıştırma:** [`SRT_ANALYSIS.md` Bölüm 2](SRT_ANALYSIS.md#2-kapsam-ve-yöntem) içindeki komutla aynı (`run_tests.py`).

---

## 3. Motorun anatomisi

### 3.1 Kimlik

| Özellik | Değer | Not |
|---|---|---|
| `name` / `alias` | `Google(Free)New` / `Google (Free) - New` | `name` ayar anahtarıdır |
| Uç nokta | `https://translate-pa.googleapis.com/v1/translate` | Google'ın web ve tarayıcı çevirisinin kullandığı genel uç nokta; belgelenmiş Cloud Translation API'si değil |
| Kimlik doğrulama | Koda gömülü genel anahtar ve `params.client=gtx` | `need_api_key = False`; kullanıcı anahtar girmez, anahtar değiştirme devreye girmez |
| Üst sınıf | `Base` | `GenAI` değil |
| Dil kodları | 243 kaynak, 243 hedef (`engines/languages.py` → `google`) | "Auto detect" seçilirse kaynak kodu `auto` |
| `support_html` | `False` | Satır içi etiketler yer tutucuya dönüşür, HTML olarak gönderilmez |
| `stream` | `False` | Yanıt tek parça gelir |

### 3.2 Kod

Değişiklik öncesi (`d312d6d`, `engines/google.py:18-52`) motorun tamamı 35 satırdı:

```python
class GoogleFreeTranslateNew(Base):
    name = 'Google(Free)New'
    alias = 'Google (Free) - New'
    free = True
    lang_codes = Base.load_lang_codes(google)
    endpoint = 'https://translate-pa.googleapis.com/v1/translate'
    need_api_key = False

    def get_headers(self):
        return {
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 '
            'Safari/537.36',
        }

    def get_body(self, text):
        self.method = 'GET'
        return {
            'params.client': 'gtx',
            'query.source_language': self._get_source_code(),
            'query.target_language': self._get_target_code(),
            'query.display_language': 'en-US',
            'data_types': 'TRANSLATION',
            # 'data_types': 'SENTENCE_SPLITS',
            # 'data_types': 'BILINGUAL_DICTIONARY_FULL',
            'key': 'AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA',
            'query.text': text,
        }

    def get_result(self, response):
        return json.loads(response)['translation']
```

Motor yalnızca başlıkları, istek parametrelerini ve yanıt çözümlemesini tanımlar. Geri kalan her şey miras alınır:

- İstek gönderme, proxy ve hata sarmalama: `Base.translate`.
- Yeniden deneme ve ardışık hata sayacı: `Translation.translate_text`.
- Eşzamanlılık: `Handler`.

### 3.3 Neden varsayılan motor

| Konum | Davranış |
|---|---|
| `lib/config.py:19` | `translate_engine` varsayılanı `None` |
| `lib/translation.py:276-277` | `get_engine_class`, adı bulamazsa `GoogleFreeTranslateNew` döndürür |
| `lib/config.py:173-174` | Ayar yükseltmesi, motor tercihi boşsa bu motoru ekler |
| `components/engine.py:260`, `306` | Özel motor silinince varsayılan bu motora döner |

### 3.4 Bir çevirinin yolculuğu

```mermaid
sequenceDiagram
    participant T as Translation.translate_paragraph
    participant G as Glossary
    participant B as Base.translate
    participant E as GoogleFreeTranslateNew
    participant R as utils.request (mechanize)
    participant S as translate-pa.googleapis.com
    T->>G: replace(original): terimler → {{id_000000}}
    T->>B: translate_text → translator.translate(text)
    B->>E: get_body(text): method = GET
    B->>E: get_headers()
    B->>R: request(url, dict, headers, GET, timeout=10, proxy)
    R->>S: GET /v1/translate?params.client=gtx&...&query.text=...
    S-->>R: {"translation": "...", "sourceLanguage": "en"}
    R-->>B: UTF-8 metin
    B->>E: get_result → json['translation']
    B-->>T: çeviri
    T->>G: restore(çeviri): {{id_000000}} → hedef terim
    T->>T: strip(); birleştirme açıksa do_aligment('\n\n')
```

Ölçümle doğrulanan ayrıntılar:

- **İstek:** `data` bir sözlük ve `method='GET'`. mechanize sözlüğü sorgu dizgisine (query string) çevirir. Metin URL'de taşınır, gövde 0 bayttır. `Content-Type` başlığı GET isteğinde etkisizdir.
- **Yanıt:** Sıkıştırmasız JSON gelir: `{"translation": "Merhaba dünya. Nasılsınız?", "sourceLanguage": "en"}`. Eklenti `sourceLanguage` alanını kullanmaz.
- **Hata yanıtı:** 400 hatası JSON değil HTML olarak gelir: `Error 400 (Bad Request)!!1 … Your client has issued a malformed or illegal request.` Eklentiye ulaşan hata metninde `HTTP Error 400: Bad Request` geçer.
- **Proxy:**
  - HTTP proxy mechanize'a verilir.
  - SOCKS5 proxy, `socket` modülü geçici olarak değiştirilerek uygulanır (`lib/utils.py:207`).
  - Proxy ayarı yoksa ortam değişkenlerindeki proxy kullanılır.

### 3.5 Miras alınan çalışma ayarları

| Ayar | Değer | Etki |
|---|---|---|
| `concurrency_limit` | `0` | `Handler` bunu paragraf sayısı kadar görev olarak yorumlar (`lib/handler.py:18`); gerçek sınırı Python'un varsayılan thread havuzu belirler |
| `request_interval` | `0` | İstekler arasında bekleme yok |
| `request_attempt` | `3` | Hatada 5, 10 ve 15 saniye bekleyerek 3 kez yeniden dener |
| `request_timeout` | `10` sn | |
| `max_error_count` | `10` | Art arda 10 hata olursa bütün iş iptal edilir |
| `api_key_errors` | `['401']` | Hata metninde ayrı bir "401" geçerse iş hemen iptal edilir |

Ayarlar ekranı bu değerleri yalnızca kullanıcı değiştirirse kaydeder (`setting.py:731-754`). Kaydedilmemişse sınıf varsayılanı kullanılır.

### 3.6 Prompt olmaması ne anlama geliyor

| Konu | Google(Free)New | ChatGPT / Claude / Gemini |
|---|---|---|
| Prompt | Yok | Var, kullanıcı düzenleyebilir; `<tlang>` zorunlu |
| Birleştirme ipucu | Yok | Prompt'a "`{{id_\d+}}` yer tutucularını koru" cümlesi eklenir |
| Sözlük (glossary) | Var: terimler `{{id_000000}}` yer tutucusuna çevrilip sonra geri konur | Aynı mekanizma |
| Etiket koruma | Satır içi etiketler `{{id_00000}}` yer tutucusuna dönüşür | Aynı |
| Akış | Yok | Var |
| Arayüz | Prompt alanı gizli (`setting.py:761-763`, yalnızca `GenAI` alt sınıfları) | Prompt, model, sampling vb. |

Sonuç: Bu motora yapıyı korumasını söyleyecek bir kanal yok. Yer tutucuların ve paragraf ayırıcılarının korunması tamamen Google'ın modeline kalmış. Bu yüzden eklentinin **çıktıyı doğrulaması** gerekiyor (bkz. [Saptama G3 ve G4](#g3-birleştirilmiş-grupta-paragraf-tekrarı-ve-kaybı)).

---

## 4. Birleştirme ve kesilme riski

> **Soru:** Paragraflar birleştirilerek gönderilirken, birleştirilecek karakter sayısı parametresine göre sona eklenen paragrafın yarıda ya da uygunsuz bir noktada kesilme riski var mı?

### 4.1 Kısa cevap

**Girdi tarafında hayır.** Eklenti paragrafları bütün olarak gruplar; hiçbir paragraf `merge_length` yüzünden bölünmez. Sınıra sığmayan paragraf bir sonraki grubu başlatır. Riskler başka yerlerde:

| Risk | Bu motorda | Bölüm |
|---|---|---|
| Girdinin paragraf ortasından kesilmesi | **Yok** | 4.2–4.3 |
| Sınırı aşan tek paragrafın sınırsız boyutta gönderilmesi | **Var**; URL sınırını aşınca 400 alır (kesilmez, **reddedilir**) | 4.4, [G1](#g1-url-uzunluk-sınırı-16-kb) |
| Sağlayıcının yanıtı sondan kesmesi | Bu motorda gözlenmedi; LLM motorlarında `max_tokens` nedeniyle var | 4.5 |
| Sağlayıcının paragrafları birleştirmesi veya tekrarlaması | **Var, ölçüldü** | [G3](#g3-birleştirilmiş-grupta-paragraf-tekrarı-ve-kaybı) |
| Bir cümlenin iki gruba bölünmesi (bağlam kaybı) | Var; yapıyı bozmaz, kaliteyi etkiler | 4.5 |

### 4.2 Gruplama kodu

```787:821:lib/element.py
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
```

Algoritma açgözlü (greedy) çalışır:

1. Her paragrafın içeriğine `\n\n` eklenir.
2. Mevcut grup + yeni paragraf `merge_length`'ten **kısaysa** paragraf gruba eklenir.
3. Değilse mevcut grup kapatılır ve yeni paragraf **bütün olarak** yeni grubu başlatır.
4. `content` hiçbir yerde dilimlenmez (`[:n]` gibi bir kesme yok).

### 4.3 Ölçüm

Gerçek `ElementHandlerMerge` kodu, `merge_length=1800` ile uzunlukları farklı yedi paragrafla çalıştırıldı:

| Grup | Uzunluk | İçerik (paragraf, karakter) |
|---|---|---|
| 0 | 1404 | A (700), B (700) |
| 1 | 702 | C (700) |
| 2 | **2502** | D (**2500**) — tek başına sınırı aşıyor |
| 3 | 302 | E (300) |
| 4 | **1801** | F (1799) — ayırıcıyla birlikte sınırı aşıyor |
| 5 | 3 | G (1) |

Her paragraf tam olarak tek bir grupta ve bölünmeden yer aldı.

### 4.4 İncelikler

- **Sınır "yumuşak".** Karşılaştırma `<` ile yapılır ve yalnızca birden fazla paragrafı aynı grupta toplarken uygulanır. Tek başına `merge_length`'ten uzun bir paragraf (D) sınır gözetilmeden gönderilir. Birleştirme **kapalıyken** hiçbir uzunluk sınırı yoktur.
- **Sayılan şey karakter, bayt ya da token değil.** Bu motorda belirleyici olan URL'deki **bayt** sayısıdır. Aynı 1800 karakter İngilizcede ~1,8 KB, Türkçede ~4–8 KB, Japoncada ~16 KB URL üretir.
- **Uzunluğa sayılan diğer şeyler.**
  - Ayırıcı (`\n\n`) uzunluğa dahildir.
  - Her korunan öğe (görsel, dipnot, `<br>`) 12 karakterlik bir yer tutucuya (`{{id_00000}}`) dönüşür ve o da sayılır.
  - Sözlük terimleri ise gruplamadan **sonra** yer tutucuya çevrildiği için gruplama hesabına girmez.
- **Önbellek.** `merge_length` önbellek kimliğinin parçasıdır. Değer değişince çeviriler yeniden yapılır.

### 4.5 Gerçek kesilme ve "uygunsuz nokta" riskleri

1. **URL sınırı (bu motor).** Uzun grup ya da uzun paragraf kesilmez, Google tarafından **reddedilir** (HTTP 400). Çeviri hiç gelmez. Ayrıntı: [G1](#g1-url-uzunluk-sınırı-16-kb) ve [G2](#g2-400-yanıtının-yanlış-yönetimi).
2. **Çıktının sondan kesilmesi (LLM motorları).** Claude'da `max_tokens: 4096` sabit ve `stop_reason` kontrolü yok. Uzun bir grubun çevirisi sondan kesilirse son paragraf yarım kalır. Hizalama da bloğun tamamını son paragrafa yığar. Bu risk girdiyle değil çıktıyla ilgilidir; ayrıntılar [`SRT_ANALYSIS.md` kök neden K6](SRT_ANALYSIS.md#42-kök-nedenler) ve [Bölüm 11, iyileştirme 1](SRT_ANALYSIS.md#11-sınırlamalar-ve-isteğe-bağlı-iyileştirmeler) içinde. `Google(Free)New`'de yanıtın sondan kesildiği görülmedi: tekrarsız 5.000 karakterlik Japonca metinde 216 cümlenin hepsi, son cümle dahil, çevrildi ([Bölüm 9.3](#93-ana-repoda-canlı-doğrulama)). Aynı cümlenin art arda tekrarlandığı metinde ise Google tekrarları kendisi tek cümleye indiriyor; bu kesilme değil, ama çıktı girdiden çok kısa görünür.
3. **Cümlenin iki gruba bölünmesi.**
   - Grup sınırı her zaman iki paragraf arasına düşer.
   - Ama kaynakta bir cümle iki öğeye bölünmüş olabilir. Örneğin sayfa sonunda ikiye ayrılmış `<p>` ya da iki SRT altyazısına yayılan cümle.
   - Bu iki öğe farklı gruplara düşerse ayrı ayrı çevrilir ve bağlam kaybolur. Yapı bozulmaz, yalnızca çeviri kalitesi etkilenir.
   - SRT için bkz. [`SRT_ANALYSIS.md` Bölüm 11, "Blok sınırında bağlam"](SRT_ANALYSIS.md#11-sınırlamalar-ve-isteğe-bağlı-iyileştirmeler).
4. **Sağlayıcının paragraf yapısını bozması.** Kesilme değil ama sonucu benzer: çeviriler yanlış paragraflara düşer. Ayrıntı: [G3](#g3-birleştirilmiş-grupta-paragraf-tekrarı-ve-kaybı).

### 4.6 Önerilen çözümde metin nerede bölünür

Önerilen çözüm ([Bölüm 7.4](#74-enginesgooglepy)) bu motor için ilk kez **bilinçli bir bölme** ekler, ama yalnızca bir istek URL bütçesini (14.000 bayt) aşarsa. Bölme sırası:

1. Paragraf sınırı (`\n\n` ve daha uzun boş satırlar).
2. Satır sonu (`\n`).
3. Cümle sonu (`. ! ? ; :` ve sonrasındaki boşluk; CJK için `。！？；`).
4. Kelime arası boşluk.
5. Son çare olarak karakter. Yer tutucular (`{{id_…}}`) bu aşamada bile bölünmez.

Parçalar, aralarındaki ayırıcılar korunarak yeniden birleştirilir; `''.join(parça + ayırıcı) == metin` her zaman sağlanır. Birleştirilmiş bir grup önce paragraf sınırlarından bölündüğü için paragraf sayısı ve hizalama korunur. Bir paragrafın ortasından bölme ancak **tek bir paragraf** bütçeyi aşarsa yapılır: Japoncada ~1500, İngilizcede ~14.000 karakter üzeri. O durumda da önce cümle sınırı denenir.

---

## 5. Saptamalar

### G1. URL uzunluk sınırı (~16 KB)

Metin GET sorgu dizgisinde taşındığı için yüzde kodlamasıyla büyür: Latin harfi 1 bayt, Türkçe özel harf 6 bayt, Kiril 6 bayt, CJK 9 bayt. Gerçek uç noktada ölçülen sonuçlar:

| Kaynak dil | Karakter | URL uzunluğu (bayt) | Sonuç |
|---|---|---|---|
| İngilizce | 15.000 | 15.224 | OK |
| Rusça | 3.000 | 15.609 | OK |
| Türkçe | 6.000 | 14.009 | OK |
| Japonca | 1.500 | 13.724 | OK |
| **Japonca** | **1.800** | **16.424** | **HTTP 400** |
| Japonca | 2.000 | 18.224 | HTTP 400 |
| Türkçe | 9.000 | 20.894 | HTTP 400 |

Sınır 15.609 ile 16.424 bayt arasında; büyük olasılıkla 16 KB.

**Etki:** Varsayılan `merge_length` 1800 (`lib/config.py:46`). Japonca, Çince ya da Korece kaynakta birleştirme açılınca varsayılan ayarla bile istekler reddedilir. Birleştirme kapalıyken de ~1700 karakteri aşan tek bir CJK paragrafı aynı hatayı alır.

### G2. 400 yanıtının yanlış yönetimi

`Base.translate` her hatayı `UnexpectedResult` olarak sarar. `Translation.translate_text` de hatanın türüne bakmadan yeniden dener (değişiklik öncesi, `d312d6d`, `lib/translation.py:134-156`):

```python
        except Exception as e:
            if self.cancel_request() or self.need_stop():
                raise TranslationCanceled(_('Translation canceled.'))
            self.abort_count += 1
            message = _('Failed to retrieve data from translate engine API.')
            if retry >= self.translator.request_attempt:
                raise TranslationFailed('{}\n{}'.format(message, str(e)))
            retry += 1
            interval += 5
            # Logging any errors that occur during translation.
            logged_text = text[:200] + '...' if len(text) > 200 else text
            error_messages = [
                sep(), _('Original: {}').format(logged_text), sep('┈'),
                _('Status: Failed {} times / Sleeping for {} seconds')
                .format(retry, interval), sep('┈'), _('Error: {}')
                .format(traceback_error())]
            if row >= 0:
                error_messages.insert(1, _('Row: {}').format(row))
            self.log('\n'.join(error_messages), True)
            if self.translator.match_error(str(e)):
                raise TranslationCanceled(_('Translation canceled.'))
            time.sleep(interval)
            return self.translate_text(row, text, retry, interval)
```

**Etki:**

- Aynı uzun istek 4 kez gönderilir ve toplam 30 saniye beklenir.
- Her deneme `abort_count` sayacını bir artırır. Bu sayaç bütün paralel görevler arasında ortaktır ve yalnızca başarılı bir istekte sıfırlanır.
- Yaklaşık 3 uzun grup üst üste gelirse sayaç 10'a ulaşır ve **bütün çeviri işi iptal edilir**. Toplu modda iş "Translation failed" hatasıyla biter.

### G3. Birleştirilmiş grupta paragraf tekrarı ve kaybı

Birleştirme açıkken eşleme, çevirideki `\n\n` sayısına göre yapılır. Bu mekanizmanın ayrıntılı eleştirisi [`SRT_ANALYSIS.md` kök neden K1](SRT_ANALYSIS.md#42-kök-nedenler) içinde; aynı mekanizma EPUB, DOCX ve diğer formatlarda da geçerli. Bu motorda gerçek uç noktayla ölçülen sonuçlar (12 paragraflık birleştirilmiş grup):

| Senaryo | Dönen paragraf | Yer tutucu |
|---|---|---|
| Yer tutucu yok (İngilizce, Japonca); 40 paragraf | 12/12, 40/40 | — |
| Yer tutucu paragraf sonunda (İngilizce, Japonca) | 12/12 | 12/12 |
| Yer tutucu iki kelime arasında (İngilizce) | **11/12** | **0/12** |
| Yer tutucu kelimeye yapışık, dipnot gibi (İngilizce) | **13/12** | **2/12** |
| Aynı cümle üç kez + sonda yer tutucu (Japonca) | **8/12** | 6/12 |

Dipnot senaryosu iki denemede de aynı sonucu verdi. Google "He was waiting for his friend at the station." paragrafının çevirisini iki kez yazdı. Eklentinin hizalaması sonucu şöyle dağıttı (son dört paragraf):

```
The weather was lovely{{id_00000}}, so we went f -> Hava çok güzeldi, bu yüzden parkta yürüyüşe çıktık.
He was waiting for his friend at the station{{id -> Arkadaşını istasyonda bekliyordu.
Yes{{id_00000}}.                                 -> Arkadaşını istasyonda bekliyordu.
It started to rain{{id_00000}}, so we went home. -> Evet{{id_00000}}. ¶ Yağmur yağmaya başladı{{id_00000}}, ...
```

"Yes." paragrafının altına bir önceki paragrafın çevirisi yazıldı; son paragraf ise iki çeviriyi birden aldı. Parça eksik çıktığında (11/12) ise bloğun **tamamı** son paragrafa yığılır. Bu davranış `SRT_ANALYSIS.md` [Bölüm 4.1, senaryo B](SRT_ANALYSIS.md#41-deney-sonuçları-mevcut-kod) ile aynıdır.

### G4. Yer tutucu kaybı ve korunan öğelerin düşmesi

Kelimeye yapışık yer tutucuları Google, birleştirme kapalıyken tek paragrafta da siliyor (`The weather was lovely{{id_00000}}, so…` → `Hava çok güzeldi, bu yüzden…`). Eklenti çeviride bulamadığı yer tutucunun öğesini geri koymuyordu (değişiklik öncesi, `d312d6d`, `lib/element.py:294-302`):

```python
        # Escape the markups (<m id=1 />) to replace escaped markups.
        translation = xml_escape(translation)
        for rid, element in enumerate(self.reserve_elements):
            pattern = self.placeholder[1].format(
                r'\s*'.join(format(rid, '05')))
            # Prevent processe any backslash escapes in the replacement.
            translation = re.sub(
                xml_escape(pattern), lambda _: element, translation)
        translation = self._polish_translation(translation)
```

**Etki:**

- Varsayılan korunan öğeler şunlar: `img`, `code`, `br`, `hr`, `sub`, `sup`, `kbd`, `abbr`, `wbr`, `var`, `canvas`, `svg`, `script`, `style`, `math` (`lib/element.py:731-733`).
- İki dilli modda bu öğeler orijinal paragrafta kalır, yalnızca çeviride eksiktir.
- **"Yalnızca çeviri" modunda** ise kitaptan tamamen kaybolurlar: dipnot bağlantıları, satır içi görseller, `<br>` satır sonları.

**Yer tutucu biçimini değiştirmek çözüm değil.** Aynı 12 paragraflık testte yedi farklı biçim denendi; hiçbiri her iki dilde ve her iki konumda güvenilir çıkmadı:

| Biçim | İngilizce, dipnot | İngilizce, sonda | Japonca, dipnot | Japonca, sonda |
|---|---|---|---|---|
| `{{id_N}}` (mevcut) | 0/12 | 12/12 | 0/12 | 12/12 |
| `<m id=N />` (DeepL biçimi) | 0/12 | 12/12 | 0/12 | 1/12 |
| `[[N]]` | 12/12 | 12/12 | 0/12 | 12/12 |
| `{N}` | 0/12 | 0/12 | 0/12 | 12/12 |
| `__N__` | 12/12 | 12/12 | 0/12 | 0/12 |
| `#N#` | 0/12 | 0/12 | 0/12 | 12/12 |
| `⟦N⟧` | 12/12 | 2/12 | 2/12 | 12/12 |

Bu yüzden önerilen çözüm biçimi değiştirmez; kaybı tespit edip öğeyi kurtarır ([Bölüm 7.5](#75-libelementpy)).

### G5. SRT çevirisinde bu motorun durumu

- **Prompt eki çalışmaz.** SRT dokümanının önerdiği prompt eki ([`SRT_ANALYSIS.md` Bölüm 6.5](SRT_ANALYSIS.md#65-motorlar-srt-prompt-eki)) yalnızca GenAI motorlarında etkilidir. Bu motorda tek güvence işaretçi doğrulaması ve onarım akışıdır ([`SRT_ANALYSIS.md` Bölüm 5](SRT_ANALYSIS.md#5-çözüm-mimarisi)).
- **Canlı test.** SRT işaretçileri (`{{id_c0}}`, `{{id_c1}}`, `{{id_c2}}`) satır başındayken korundu. Ama Google cümleyi bütün olarak çevirip kelimeleri işaretçiler arasında taşıdı:

  | İşaretçi | Orijinal | Çeviri |
  |---|---|---|
  | `c0` | I was going to | Mağazaya gidecektim, |
  | `c1` | the store, but | ama |
  | `c2` | it was closed. | kapalıydı. |

  Doğrulama bunu geçer; zaman damgaları kaymaz ama anlam altyazılar arasında hafifçe yer değiştirebilir. Bu bir NMT sınırlaması, kodla düzeltilemez.
- **G3 bu motorda SRT'de de geçerliydi.** Çözüm öncesi kodla (`d312d6d`) birleştirme açık SRT çevirisinde G3 aynen yaşanıyordu. SRT çözümü artık uygulandığı için SRT grupları `\n\n` değil işaretçi kullanır ve G3'ten etkilenmez.

### G6. Yanıt sıkıştırması

- `Accept-Encoding: gzip, deflate, br` başlığı gönderiliyor. Ancak `request()` yanıtı açmadan doğrudan `decode('utf-8')` ile çözüyor (`lib/utils.py:196`).
- Google şu an sıkıştırmasız yanıt veriyor (`Content-Encoding` yok), bu yüzden sorun görünmüyor.
- Sunucu bir gün sıkıştırırsa her istek "Can not parse returned response" hatası verir.
- Aynı başlık `Google(Free)Html` ve `Google(Free)` motorlarında da var.

### G7. Sınırsız eşzamanlılık (koddan çıkarım)

- `concurrency_limit = 0` ve `request_interval = 0` ile büyük kitaplarda onlarca paralel istek gider.
- Resmî olmayan bir uç nokta için bu, HTTP 429 (çok fazla istek) ya da geçici engelleme riski taşır.
- 429 hatası geçici sayılıp yeniden denenir ve G2'deki ortak hata sayacına eklenir.
- Testlerde 429 gözlenmedi; bu yüzden bu madde öneri düzeyinde ([Bölüm 12](#12-sınırlamalar-ve-isteğe-bağlı-iyileştirmeler)).

### G8. Küçük notlar

- **Resmî olmayan uç nokta.** Gömülü anahtar ve `gtx` istemcisi Google tarafından her an kısıtlanabilir ya da kapatılabilir; hizmet seviyesi garantisi yoktur.
- **`Content-Type` başlığı** GET isteğinde etkisizdir.
- **`sourceLanguage` yanıt alanı** kullanılmıyor. "Auto detect" modunda algılanan dil loglanabilirdi.

---

## 6. Çözüm mimarisi

Çözüm üç katmanda savunma kurar:

```mermaid
flowchart TD
    A["Translation.translate_text"] --> B["GoogleFreeTranslateNew.translate"]
    B --> C["_translate_chunks:<br/>split_text ile URL bütçesine göre parçala<br/>(paragraf → satır → cümle → kelime → karakter)"]
    C --> D["Base.translate (parça başına GET)"]
    D -->|"HTTP 400"| E["RequestRejected<br/>(yeniden denenmez)"]
    D -->|"diğer hatalar"| F["UnexpectedResult<br/>(eskisi gibi yeniden denenir)"]
    D --> G{"Birleştirme açık mı ve<br/>paragraf sayısı tuttu mu?"}
    G -->|"Tuttu / kapalı"| H["Çeviri"]
    G -->|"Tutmadı"| I["Grubu paragraf paragraf<br/>yeniden çevir"]
    I --> H
    H --> J["PageElement.add_translation:<br/>kaybolan korunan öğeleri sona ekle"]
```

| Katman | Değişiklik | Çözdüğü saptama |
|---|---|---|
| Motor | URL bütçesine göre parçalama | G1 |
| Motor | Paragraf sayısı tutmazsa paragraf paragraf yeniden çevirme | G3 |
| Motor | `Accept-Encoding: identity`, `Content-Type` kaldırıldı | G6, G8 |
| Motor + `Base` | `rejected_errors` ve `RequestRejected` istisnası | G2 |
| `Translation` | `RequestRejected` yeniden denenmez | G2 |
| Öğe (`PageElement`) | Kaybolan korunan öğeler çevirinin sonuna eklenir | G4 |

Tasarım kararları:

- **Bütçe 14.000 bayt.** Ölçülen sınır ~16 KB. Aradaki pay, mechanize ile `urllib` arasındaki kodlama farklarına ve olası başlık büyümesine karşı güvenlik payıdır.
- **Paragraf düzeyinde onarım motorun içinde.** Bu kontrol, sağlayıcıdan bağımsız olan `do_aligment`'tan önce çalışır. Paragraf sayısı tuttuğunda `do_aligment` hiçbir şey yapmaz. Onarım yalnızca uyuşmazlıkta tetiklenir; normal akışta ek istek yoktur.
- **Kalıcı hata türle belirtilir.** `Translation`, kalıcı hatayı translator üzerinde bir metotla değil, istisnanın türüyle (`isinstance`) tanır. Böylece `Mock` translator kullanan mevcut testler etkilenmez. Diğer motorlar `rejected_errors` listesini doldurarak aynı mekanizmayı kullanabilir.
- **Kaybolan öğeler sona eklenir.** Yer tutucunun konumu bilinmediği için öğe en makul yere, paragraf sonuna eklenir. Dipnot bağlantısı ya da görsel yanlış konumda olsa bile kaybolmaz.

---

## 7. Uygulama yönergeleri (dosya dosya)

| Dosya | Değişiklik |
|---|---|
| `lib/exception.py` | `RequestRejected` istisnası |
| `engines/base.py` | `rejected_errors`, `is_rejected`, `translate` içinde istisna seçimi |
| `lib/translation.py` | `RequestRejected` yeniden denenmez |
| `engines/google.py` | `GoogleFreeTranslateNew`: parçalama, paragraf onarımı, başlıklar, `rejected_errors` |
| `lib/element.py` | `PageElement.add_translation`: kaybolan korunan öğeler |
| `tests/lib/test_engine.py` | `TestGoogleFreeTranslateNew` (12 test) |
| `tests/lib/test_translation.py` | `test_translate_text_rejected_request` |
| `tests/lib/test_element.py` | `test_add_translation_keeps_dropped_reserved_elements` |

### 7.1 `lib/exception.py`

`UnexpectedResult` sınıfının hemen altına ekleyin:

```python
class RequestRejected(UnexpectedResult):
    """The service rejected the request itself, so retrying cannot help."""
```

`UnexpectedResult`'tan türediği için bu istisnayı yakalayan mevcut kodlar (`except UnexpectedResult`) aynen çalışır.

### 7.2 `engines/base.py`

**(a)** Import:

```python
from ..lib.exception import UnexpectedResult, RequestRejected
```

**(b)** Sınıf alanı (`api_key_errors` satırının altına):

```python
    api_key_errors = ['401']
    # Regular expressions matching errors that retrying cannot fix.
    rejected_errors: list[str] = []
    separator = '\n\n'
```

**(c)** `match_error` metodunun altına:

```python
    def is_rejected(self, error_message):
        return any(
            re.search(pattern, error_message)
            for pattern in self.rejected_errors)
```

**(d)** `translate` metodunun `except` bloğunda istisna seçimi:

```python
            if self.need_swap_api_key(error_message) and self.swap_api_key():
                return self.translate(content)
            error_class = RequestRejected \
                if self.is_rejected(error_message) else UnexpectedResult
            raise error_class(
                _('Can not parse returned response. Raw data: {}')
                .format('\n\n' + error_message))
```

`rejected_errors` boş olduğu için diğer motorların davranışı değişmez.

### 7.3 `lib/translation.py`

**(a)** Import:

```python
from .exception import (
    TranslationFailed, TranslationCanceled, RequestRejected)
```

**(b)** `translate_text` içinde yeniden deneme koşulu:

```python
            message = _('Failed to retrieve data from translate engine API.')
            if retry >= self.translator.request_attempt \
                    or isinstance(e, RequestRejected):
                raise TranslationFailed('{}\n{}'.format(message, str(e)))
```

Reddedilen istek ardışık hata sayacını yalnızca **bir kez** artırır, bekleme yapılmaz ve log yazılmaz. Hata, paragrafın `error` alanına `TranslationFailed` olarak düşer ve gelişmiş modda görünür.

### 7.4 `engines/google.py`

**(a)** Importlar (dosyanın başı):

```python
import os
import re
import sys
import time
import json
from html import unescape
from subprocess import Popen, PIPE
from urllib.parse import urlencode, quote_plus
```

**(b)** `GoogleFreeTranslateNew` sınıfının tamamını aşağıdakiyle değiştirin:

<!-- code:engines/google.py:GoogleFreeTranslateNew -->
```python
class GoogleFreeTranslateNew(Base):
    name = 'Google(Free)New'
    alias = 'Google (Free) - New'
    free = True
    lang_codes = Base.load_lang_codes(google)
    endpoint = 'https://translate-pa.googleapis.com/v1/translate'
    need_api_key = False
    rejected_errors = [r'HTTP Error 400\b']
    # The text travels in the query string. Google rejects URLs longer than
    # about 16 KB, and a non-Latin character takes up to 9 bytes once
    # percent-encoded.
    max_url_length = 14000
    split_patterns = (
        r'\n{2,}', r'\n', r'(?<=[.!?;:。！？；])\s+|(?<=[。！？；])', r'\s+')
    atom_pattern = re.compile(r'\{\{\s*id\s*_\s*\w+\s*\}\}|.', re.S)

    def get_headers(self):
        return {
            'Accept': '*/*',
            # The response is decoded as plain UTF-8 text.
            'Accept-Encoding': 'identity',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 '
            'Safari/537.36',
        }

    def get_url_length(self, text):
        return len(self.endpoint) + 1 + len(urlencode(self.get_body(text)))

    def split_text(self, text):
        """Split the text into (chunk, delimiter) pairs whose request URLs fit
        the limit. Joining every chunk with its delimiter gives the text back.
        """
        budget = self.max_url_length - self.get_url_length('')
        return self._split(text, budget, 0)

    def _split(self, text, budget, level):
        if len(quote_plus(text)) <= budget:
            return [(text, '')]
        if level == len(self.split_patterns):
            return self._split_atoms(text, budget)
        parts = re.split('(%s)' % self.split_patterns[level], text)
        chunks: list[tuple[str, str]] = []
        current: tuple[str, str] | None = None
        for index in range(0, len(parts), 2):
            segment = parts[index]
            delimiter = parts[index + 1] if index + 1 < len(parts) else ''
            if current is not None:
                joined = current[0] + current[1] + segment
                if len(quote_plus(joined)) <= budget:
                    current = (joined, delimiter)
                    continue
                chunks.append(current)
                current = None
            if len(quote_plus(segment)) <= budget:
                current = (segment, delimiter)
                continue
            pieces = self._split(segment, budget, level + 1)
            chunks.extend(pieces[:-1])
            current = (pieces[-1][0], pieces[-1][1] + delimiter)
        if current is not None:
            chunks.append(current)
        return chunks

    def _split_atoms(self, text, budget):
        """Split by characters, keeping placeholders whole."""
        chunks: list[tuple[str, str]] = []
        current = ''
        for atom in self.atom_pattern.findall(text):
            if current and len(quote_plus(current + atom)) > budget:
                chunks.append((current, ''))
                current = ''
            current += atom
        return chunks + [(current, '')]

    def _translate_chunks(self, content):
        chunks = self.split_text(content)
        if len(chunks) == 1:
            return Base.translate(self, content)
        return ''.join(
            (Base.translate(self, chunk) if chunk.strip() else chunk)
            + delimiter for chunk, delimiter in chunks)

    def _count_paragraphs(self, text):
        pattern = re.compile('%s+' % self.separator)
        return len(pattern.sub(self.separator, text).strip().split(
            self.separator))

    def translate(self, content):
        translation = self._translate_chunks(content)
        if not self.merge_enabled:
            return translation
        # The service sometimes merges, drops or repeats paragraphs, which
        # would shift every translation of the merged group.
        paragraphs = content.strip().split(self.separator)
        if len(paragraphs) < 2 or \
                self._count_paragraphs(translation) == len(paragraphs):
            return translation
        return self.separator.join(
            self._translate_chunks(paragraph) for paragraph in paragraphs)

    def get_body(self, text):
        self.method = 'GET'
        return {
            'params.client': 'gtx',
            'query.source_language': self._get_source_code(),
            'query.target_language': self._get_target_code(),
            'query.display_language': 'en-US',
            'data_types': 'TRANSLATION',
            # 'data_types': 'SENTENCE_SPLITS',
            # 'data_types': 'BILINGUAL_DICTIONARY_FULL',
            'key': 'AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA',
            'query.text': text,
        }

    def get_result(self, response):
        return json.loads(response)['translation']
```

Notlar:

- **`split_text`**
  - `(parça, ayırıcı)` çiftleri döndürür. Ayırıcı, parçanın ardından gelen orijinal metindir (`\n\n`, `\n`, boşluk ya da boş dizgi).
  - Bütçe, metin boşken oluşan URL uzunluğu çıkarılarak hesaplanır. Böylece dil kodları ve sabit parametreler de hesaba girer.
- **`_split`**
  - Her seviyede metni o seviyenin ayırıcısıyla böler ve parçaları açgözlü biçimde bütçeye sığacak kadar birleştirir.
  - Tek başına sığmayan parça bir alt seviyeye iner.
  - `re.split` deseni tek bir yakalama grubuyla çağrıldığı için ayırıcılar kaybolmaz.
- **Cümle deseni**
  - Latin noktalamasından sonra boşluk ister.
  - CJK noktalamasından (`。！？；`) sonra boşluk beklemez ve sıfır genişlikli bir bölme yapar.
- **`_split_atoms`** yalnızca boşluksuz ve noktalamasız çok uzun dizgilerde çalışır. Yer tutucuyu tek bir "atom" sayar ve ortasından bölmez.
- **`_translate_chunks`**
  - Tek parçalı metinde eskisi gibi tek istek atar.
  - Yalnızca boşluktan oluşan parçaları sağlayıcıya göndermez.
- **`translate`**
  - Paragraf sayımını `ElementHandlerMerge.align_paragraph` ile aynı kuralla yapar: art arda gelen ayırıcılar tek sayılır.
  - Böylece "sayı tuttu" kararı hizalamanın göreceği sonuçla birebir aynıdır.
- **`Base.translate` içindeki özyinelemeli `self.translate` çağrısı** (API anahtarı değiştirme) bu motorda tetiklenmez (`need_api_key = False`). Tetiklense bile metin bütçeye sığdığı için güvenlidir.

### 7.5 `lib/element.py`

`PageElement.add_translation` içinde yer tutucu geri yükleme döngüsünü aşağıdakiyle değiştirin:

```python
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
        translation = self._polish_translation(translation)
```

Bu değişiklik motordan bağımsızdır; yer tutucuyu silen her motor için öğeyi kurtarır. Yer tutucusu korunan öğelerin davranışı değişmez. Mevcut `TestPageElement` testlerinin hepsi geçiyor.

---

## 8. Testler

### 8.1 `tests/lib/test_engine.py`

**(a)** Importları genişletin:

```python
from ...lib.exception import (
    UnexpectedResult, UnsupportedModel, RequestRejected)
```

```python
from ...engines.google import GeminiTranslate, GoogleFreeTranslateNew
```

**(b)** `TestDeepl` sınıfından sonra, `TestChatgptTranslate` sınıfından önce ekleyin:

<!-- code:tests/lib/test_engine.py:TestGoogleFreeTranslateNew -->
```python
class TestGoogleFreeTranslateNew(unittest.TestCase):
    def setUp(self):
        GoogleFreeTranslateNew.set_config({})
        self.translator = GoogleFreeTranslateNew()
        self.translator.set_source_lang('English')
        self.translator.set_target_lang('Turkish')

    def limit(self, size):
        """Leave room for `size` bytes of encoded text in the URL."""
        self.translator.max_url_length = \
            self.translator.get_url_length('') + size

    def assertChunks(self, text, size):
        self.limit(size)
        chunks = self.translator.split_text(text)
        self.assertEqual(text, ''.join(c + d for c, d in chunks))
        for chunk, _ in chunks:
            self.assertLessEqual(
                self.translator.get_url_length(chunk),
                self.translator.max_url_length)
        return chunks

    def test_get_body(self):
        body = self.translator.get_body('Hello')
        self.assertEqual('GET', self.translator.method)
        self.assertEqual('en', body['query.source_language'])
        self.assertEqual('tr', body['query.target_language'])
        self.assertEqual('Hello', body['query.text'])

    def test_get_headers(self):
        headers = self.translator.get_headers()
        self.assertEqual('identity', headers['Accept-Encoding'])
        self.assertNotIn('Content-Type', headers)

    def test_split_text_short(self):
        self.assertEqual(
            [('Hello world.', '')], self.translator.split_text('Hello world.'))

    def test_split_text_paragraphs(self):
        text = 'aaaa bbbb.\n\ncccc dddd.\n\neeee ffff.'
        chunks = self.assertChunks(text, 26)
        self.assertEqual(
            [('aaaa bbbb.\n\ncccc dddd.', '\n\n'), ('eeee ffff.', '')], chunks)

    def test_split_text_sentences(self):
        text = 'One two three. Four five six! Seven eight nine?'
        chunks = self.assertChunks(text, 20)
        self.assertEqual(
            ['One two three.', 'Four five six!', 'Seven eight nine?'],
            [chunk for chunk, _ in chunks])

    def test_split_text_cjk(self):
        # Each CJK character takes 9 bytes once percent-encoded.
        text = '今日は晴れです。明日は雨です。'
        chunks = self.assertChunks(text, 72)
        self.assertEqual(
            ['今日は晴れです。', '明日は雨です。'],
            [chunk for chunk, _ in chunks])

    def test_split_text_words(self):
        self.assertChunks('alpha beta gamma delta epsilon zeta eta', 12)

    def test_split_text_keeps_placeholders(self):
        text = 'a' * 10 + '{{id_00000}}' + 'b' * 10
        chunks = self.assertChunks(text, 30)
        self.assertEqual(
            ['a' * 10 + '{{id_00000}}', 'b' * 10],
            [chunk for chunk, _ in chunks])

    @patch(module_name + '.base.request')
    def test_translate(self, mock_request):
        mock_request.return_value = '{"translation": "Merhaba"}'
        self.assertEqual('Merhaba', self.translator.translate('Hello'))
        mock_request.assert_called_once()
        self.assertEqual('GET', mock_request.call_args.kwargs['method'])

    @patch(module_name + '.base.request')
    def test_translate_long_text(self, mock_request):
        def request(**kwargs):
            text = kwargs['data']['query.text']
            return json.dumps({'translation': text.upper()})
        mock_request.side_effect = request
        self.limit(26)
        text = 'aaaa bbbb.\n\ncccc dddd.\n\neeee ffff.'
        self.assertEqual(
            'AAAA BBBB.\n\nCCCC DDDD.\n\nEEEE FFFF.',
            self.translator.translate(text))
        self.assertEqual(2, mock_request.call_count)

    @patch(module_name + '.base.request')
    def test_translate_merged_paragraphs(self, mock_request):
        responses = {
            'One.\n\nTwo.\n\nThree.': 'Bir. İki.\n\nÜç.',
            'One.': 'Bir.', 'Two.': 'İki.', 'Three.': 'Üç.'}
        mock_request.side_effect = lambda **kwargs: json.dumps({
            'translation': responses[kwargs['data']['query.text'].strip()]})
        content = 'One.\n\nTwo.\n\nThree.\n\n'

        self.assertEqual('Bir. İki.\n\nÜç.', self.translator.translate(content))
        self.assertEqual(1, mock_request.call_count)

        mock_request.reset_mock()
        self.translator.set_merge_enabled(True)
        self.assertEqual(
            'Bir.\n\nİki.\n\nÜç.', self.translator.translate(content))
        self.assertEqual(4, mock_request.call_count)

        mock_request.reset_mock()
        responses['One.\n\nTwo.\n\nThree.'] = 'Bir.\n\n\n\nİki.\n\nÜç.'
        self.assertEqual(
            'Bir.\n\n\n\nİki.\n\nÜç.', self.translator.translate(content))
        self.assertEqual(1, mock_request.call_count)

    @patch(module_name + '.base.request')
    def test_translate_rejected(self, mock_request):
        mock_request.side_effect = Exception(
            'httperror_seek_wrapper: HTTP Error 400: Bad Request')
        with self.assertRaises(RequestRejected):
            self.translator.translate('Hello')

        mock_request.side_effect = Exception(
            'httperror_seek_wrapper: HTTP Error 429: Too Many Requests')
        with self.assertRaises(UnexpectedResult) as cm:
            self.translator.translate('Hello')
        self.assertNotIsInstance(cm.exception, RequestRejected)
```

Kapsam:

- **Parçalama:** paragraf, cümle, CJK, kelime ve yer tutucu sınırları. Her testte iki şey doğrulanıyor: birleştirince metnin aynen geri gelmesi ve her parçanın URL bütçesine sığması.
- **Uzun metin:** ayırıcıları koruyarak çok istekli çeviri.
- **Birleştirme onarımı:** Uyuşmazlıkta tetikleniyor; birleştirme kapalıyken ya da sayı tuttuğunda tetiklenmiyor. Art arda gelen ayırıcılar da tek sayılıyor.
- **Hata ayrımı:** 400 kalıcı, 429 geçici.

### 8.2 `tests/lib/test_translation.py`

Import:

```python
from ...lib.exception import (
    TranslationCanceled, TranslationFailed, RequestRejected)
```

`test_translate_text_retry_failed_translation` testinin altına:

```python
    @patch.object(Translation, 'need_stop', lambda self: False)
    @patch(f'{module_name}.time')
    def test_translate_text_rejected_request(self, mock_time):
        self.translation.translator.translate.side_effect = RequestRejected(
            'HTTP Error 400: Bad Request')
        self.translation.log = self.log
        self.translation.cancel_request = self.cancel_request
        self.translator.request_attempt = 5

        with self.assertRaises(TranslationFailed):
            self.translation.translate_text(0, 'text')

        self.translator.translate.assert_called_once_with('text')
        self.log.assert_not_called()
        mock_time.sleep.assert_not_called()
        self.assertEqual(1, self.translation.abort_count)
```

### 8.3 `tests/lib/test_element.py`

`TestPageElement` sınıfında `test_add_translation_with_placeholder` testinin üstüne:

```python
    def test_add_translation_keeps_dropped_reserved_elements(self):
        xhtml = etree.XML(
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b'<p>a<sup>[1]</sup> b <img src="x.jpg"/> c</p></body></html>')
        element = PageElement(xhtml.find('.//x:p', namespaces=ns), 'p1')
        element.reserve_pattern = create_xpath(('sup', 'img'))
        element.set_placeholder(Base.placeholder)
        element.position = 'only'
        self.assertEqual('a{{id_00000}} b {{id_00001}} c', element.get_content())
        element.add_translation('A B {{id_00001}} C')

        elements = xhtml.findall('.//x:p', namespaces=ns)
        self.assertEqual(1, len(elements))
        self.assertEqual(
            '<p dir="auto">A B <img src="x.jpg"/> C<sup>[1]</sup></p>',
            get_string(elements[0], True))
```

---

## 9. Doğrulama sonuçları

### 9.1 Test paketi

| Durum | Sonuç |
|---|---|
| Orijinal repo | 268 test, hepsi başarılı |
| Çözüm uygulandı, yeni testler hariç | 268 test, hepsi başarılı (hiçbir mevcut test kırılmadı) |
| Çözüm + 14 yeni test (kopya) | **282 test, hepsi başarılı** |
| Ana repoya uygulandı (`d312d6d` üzerine) | **282 test, hepsi başarılı** |

### 9.2 Gerçek uç noktada düzeltilmiş motor

| Metin | Eski motor | Yeni motor |
|---|---|---|
| Japonca 1.800 karakter | HTTP 400 (ölçüldü) | 2 parça, OK |
| Japonca 5.000 karakter | HTTP 400 (sınırdan çıkarım) | 4 parça, OK |
| Japonca 3.000 karakter, noktalamasız | HTTP 400 (sınırdan çıkarım) | 2 parça, OK |
| Türkçe 9.000 karakter | HTTP 400 (ölçüldü) | 2 parça, OK |
| 12 birleştirilmiş paragraf, dipnot yer tutucuları | 13 paragraf döndü, çeviriler kaydı | 1 + 12 istek, **12 paragraf, doğru eşleme** |
| Kısa İngilizce metin (`Accept-Encoding: identity`) | OK | OK |

G3'teki dipnot senaryosunun yeni motorla sonucu (son dört paragraf):

```
The weather was lovely{{id_00000}}, so we went f -> Hava çok güzeldi, bu yüzden parkta yürüyüşe çıktık.
He was waiting for his friend at the station{{id -> Arkadaşını istasyonda bekliyordu.
Yes{{id_00000}}.                                 -> Evet{{id_00000}}.
It started to rain{{id_00000}}, so we went home. -> Yağmur yağmaya başladı, bu yüzden eve gittik.
```

Son satırdaki yer tutucuyu Google yine sildi. Bu durumda [Bölüm 7.5](#75-libelementpy)'teki kurtarma, dipnot öğesini çevirinin sonuna ekler.

### 9.3 Ana repoda canlı doğrulama

Çözüm ana repoya uygulandıktan sonra aynı senaryolar gerçek uç noktada yeniden çalıştırıldı ve ek kontroller yapıldı:

| Kontrol | Sonuç |
|---|---|
| Bölüm 9.2'deki uzun metinler (Japonca 1.800 / 5.000 / 3.000 noktalamasız, Türkçe 9.000, kısa İngilizce) | Hepsi OK; 2, 4, 2, 2 ve 1 parça |
| Dipnotlu 12 paragraf, onarım kapalı / açık | 13 / **12** paragraf; onarım açıkken eşleme doğru |
| URL'si sınırı aşan istek, `Translation.translate_text` üzerinden | 0,4 sn'de `TranslationFailed`; tek istek, bekleme yok, log yok, `abort_count` = 1 |
| Aynı istek doğrudan `engine.translate` ile | `RequestRejected` |
| Tekrarsız 5.000 karakter Japonca (216 cümle, 3 parça) | 216/216 cümle; son cümle ("217番…" → "217th…") yerinde |
| Aynı cümlenin tekrarı (1.800 karakter, 69 cümle) | Parça başına ayrı istekte de 58 → 3 ve 11 → 1 cümle: tekrarları Google birleştiriyor, parçalamadan kaynaklanmıyor |
| İngilizce → Japonca, kelimeye yapışık dipnot/görsel (5 örnek) | Google 4 örnekte yer tutucuları sildi; 5 örneğin hepsinde öğeler çıktıda korundu (silinenler paragraf sonunda) |

Kurtarmanın canlı örneği:

```
google: 彼は何も言いませんでした。
output: <p dir="auto">彼は何も言いませんでした。<sup>3</sup><sup>4</sup><sup>5</sup></p>
```

---

## 10. SRT dokümanıyla ilişki

**Birlikte uygulanabilirlik.** İki çözüm farklı noktalara dokunur. Üç yönlü birleştirmede (`git merge-file`) hiçbir dosyada çakışma çıkmadı. Birleşik kodda 318 testin hepsi geçti; SRT senaryoları ([`SRT_ANALYSIS.md` Bölüm 8.2](SRT_ANALYSIS.md#82-eski-senaryoların-yeni-kodla-tekrarı)) da aynı sonuçları verdi.

**Durum:** İki çözüm de artık repoda uygulanmış durumda ve 318 testin hepsi geçiyor. Bu motorla gerçek bir bölüm üzerinde yapılan SRT doğrulaması [`SRT_ANALYSIS.md` Bölüm 8.4](SRT_ANALYSIS.md#84-gerçek-dosyayla-canlı-doğrulama) içinde. O denemede 14 grubun 8'inde işaretçiler bozuldu ve SRT onarım akışıyla düzeltildi. Cümle iki altyazıya yayıldığında Google işaretçiyi siliyor ya da tekrarlıyor.

| Dosya | SRT çözümü | Bu çözüm |
|---|---|---|
| `engines/base.py` | `prompt_extension`, `set_prompt_extension` | `rejected_errors`, `is_rejected`, `translate` içinde istisna seçimi |
| `engines/google.py` | `GeminiTranslate._prompt` | `GoogleFreeTranslateNew` |
| `lib/translation.py` | `alignment`, `set_alignment`, `fork`, `translate_paragraph` koşulu | `translate_text` yeniden deneme koşulu |
| `lib/element.py` | `SrtElement`, `get_srt_elements`, `get_element_handler` | `PageElement.add_translation` |

**SRT akışında bu motorun davranışı (iki çözüm birlikte uygulandığında):**

- **Paragraf onarımı tetiklenmez.** SRT grupları altyazıları `\n` ile ayırır ve işaretçi taşır ([`SRT_ANALYSIS.md` Bölüm 5.3](SRT_ANALYSIS.md#53-veri-biçimleri)). İçerikte `\n\n` olmadığı için motorun paragraf onarımı devreye girmez; doğrulama ve onarım SRT katmanında yapılır.
- **İşaretçiler parçalanmaz.** URL bütçesini aşan uzun bir SRT grubu önce `\n` sınırlarından bölünür. İşaretçiler satır başında olduğu için her altyazı işaretçisiyle birlikte aynı parçada kalır.
- **SRT onarım istekleri** tek altyazılık ve kısa olduğu için tek parça gider.
- **Prompt eki** bu motorda etkisizdir (bkz. [G5](#g5-srt-çevirisinde-bu-motorun-durumu)).

**Yalnızca bu çözüm uygulanırsa (SRT çözümü olmadan):** Birleştirme açık SRT çevirisinde paragraf onarımı, bu motor için [`SRT_ANALYSIS.md` senaryo B, C ve D](SRT_ANALYSIS.md#41-deney-sonuçları-mevcut-kod)'deki kaymaları büyük ölçüde engeller, çünkü altyazı sayısı tutmazsa altyazılar tek tek çevrilir. Ancak ayrıştırıcı, çeviri temizliği ve etiket sorunları (K4, K5, K9) yalnızca SRT çözümüyle giderilir.

---

## 11. Kabul kriterleri

1. Kaynak dil ve `merge_length` ne olursa olsun, bu motor HTTP 400 "malformed request" hatası almaz. Uzun metinler parçalanarak eksiksiz çevrilir.
2. Bir paragraf yalnızca tek başına URL bütçesini aşıyorsa bölünür; bölme önce cümle sınırında yapılır. Yer tutucular hiçbir zaman bölünmez.
3. Birleştirme açıkken çeviri, gruptaki paragraf sayısıyla aynı sayıda paragraf içerir. Tutmadığında grup paragraf paragraf yeniden çevrilir; normal akışta ek istek yoktur.
4. HTTP 400 alan bir istek yeniden denenmez, beklenmez ve ardışık hata sayacını yalnızca bir artırır. Diğer hatalar (429, ağ hatası) eskisi gibi yeniden denenir.
5. Sağlayıcının sildiği korunan öğeler (görsel, dipnot, `<br>`) çeviride kaybolmaz.
6. Diğer motorların davranışı değişmez; mevcut testlerin tümü geçer.

---

## 12. Sınırlamalar ve isteğe bağlı iyileştirmeler

**Bilinen sınırlamalar**

- **Onarım maliyeti.** Paragraf sayısı tutmayan grup, içindeki paragraf sayısı kadar ek istek üretir. Bağlam da paragraf düzeyine iner. Bu motor ücretsiz ve hızlı olduğu için maliyet süreyle sınırlı.
- **Sayının tuttuğu ama içeriğin kaydığı durum.** Google bir paragrafı birleştirip başka birini bölerse sayı tutar ve hata yakalanmaz. Testlerde gözlenmedi ama teorik olarak mümkün. Tam güvence, SRT dokümanındaki gibi paragraf başına işaretçi gerektirir ([`SRT_ANALYSIS.md` Bölüm 5.1](SRT_ANALYSIS.md#51-ilkeler)).
- **Kurtarılan öğenin konumu.** Kaybolan öğe paragraf sonuna eklenir. Bir dipnot bağlantısı cümle sonuna kayabilir.
- **Bütçe sabit.** 14.000 bayt deneysel bir değer. Google sınırı değiştirirse `max_url_length` güncellenmeli.
- **Paragraf ortasında bölme.** Tek bir paragraf bütçeyi aştığında parçalar ayrı çevrilir, cümleler arası bağlam azalır. Bu, eski davranıştaki (hiç çeviri yok) duruma göre kabul edilebilir bir ödünleşim.
- **Tekrarlanan cümlelerin birleştirilmesi.** Google art arda gelen aynı cümleleri tek cümleye indirir. Bu, sağlayıcının davranışıdır ve eklenti tarafından düzeltilmez; gerçek kitap metninde nadirdir.

**İsteğe bağlı iyileştirmeler (bu çözümün parçası değil)**

1. **Varsayılan eşzamanlılık sınırı.** `GoogleFreeTranslateNew.concurrency_limit = 4` gibi bir sınıf varsayılanı, hız sınırı (429) riskini azaltır. Ayarı hiç değiştirmemiş kullanıcıları etkiler ve çeviri süresini uzatabilir. Bu yüzden ölçümle karar verilmeli.
2. **Paragraf onarımını genelleştirmek.** Aynı kontrol `Base` içine taşınıp prompt'suz bütün motorlarda (DeepL, Microsoft, Google Cloud) kullanılabilir.
3. **Diğer Google motorları.** `Google(Free)Html` ve `Google(Free)` için `Accept-Encoding` düzeltmesi aynen uygulanmalı. `Google(Free)Html`'in HTML modunun etiketleri yer tutuculardan daha iyi koruyup korumadığı test edilmeli (bu çalışmada test edilmedi).
4. **Algılanan dilin loglanması.** "Auto detect" modunda yanıttaki `sourceLanguage` değeri loglanabilir.
5. **Kırpılmış LLM yanıtlarını hata saymak.** Bkz. [`SRT_ANALYSIS.md` Bölüm 11, iyileştirme 1](SRT_ANALYSIS.md#11-sınırlamalar-ve-isteğe-bağlı-iyileştirmeler). Bu motoru değil LLM motorlarını ilgilendirir.

---

## 13. Uygulama sırası (kontrol listesi)

1. [x] `lib/exception.py`: `RequestRejected` (7.1).
2. [x] `engines/base.py`: import, `rejected_errors`, `is_rejected`, istisna seçimi (7.2).
3. [x] `lib/translation.py`: import ve yeniden deneme koşulu (7.3).
4. [x] `engines/google.py`: importlar ve `GoogleFreeTranslateNew` sınıfı (7.4).
5. [x] `lib/element.py`: `PageElement.add_translation` kurtarma döngüsü (7.5).
6. [x] Testleri ekle (8.1, 8.2, 8.3).
7. [x] Test paketini çalıştır: 282 test başarılı. SRT çözümüyle birlikte uygulanırsa toplam 318 (iki çözüm üç yönlü birleştirmeyle çakışmasız uygulanıp bu sayı doğrulandı).
8. [ ] Calibre arayüzünden elle doğrulama (motor düzeyindeki karşılıkları Bölüm 9.3'te canlı olarak doğrulandı):
   - Birleştirme açıkken (`merge_length` 1800 ve 3000) bir Japonca EPUB'u bu motorla çevir. Logda "HTTP Error 400" olmamalı.
   - Dipnotlu bir İngilizce kitabı "yalnızca çeviri" modunda çevir. Dipnot bağlantıları çıktıda bulunmalı.
9. [ ] Sürüm notuna ekle: "Google(Free)New: uzun ve CJK metinler parçalanarak çevriliyor; birleştirilmiş paragrafların kayması önlendi; kaybolan dipnot ve görseller korunuyor."
