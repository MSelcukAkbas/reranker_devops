# searchslim

Claude Code gibi ajanların Grep, Glob, `rg`, `grep`, `git grep`, `fd`, `find`,
`git ls-files`, `tree` ve `ls -R` çıktılarını,
kritik kod kanıtını kaybetmeden küçülten drop-in katman. Yeni bir tool eklemez;
çıktı, aracın kendi formatında (`yol:satır:metin`) kalır ve atılan her şey
sondaki tek bir `[searchslim]` notunda sayılır.

## Kurulum (tek komut)

```sh
pip install git+https://github.com/MSelcukAkbas/reranker_devops && searchslim install --user
```

Güncellemek için `pip install -U --force-reinstall --no-deps git+https://github.com/MSelcukAkbas/reranker_devops`
(bayraksız pip, aynı sürüm kuruluysa eski kodu bırakır). Kurulu sürüm: `python -m searchslim --version`.

`searchslim install --user` hook'u `~/.claude/settings.json` dosyasına ekler, yani
bütün projelerde açılır. Tek bir proje için `searchslim install <proje-dizini>`.
Mevcut ayarlara dokunmaz, iki kez çalıştırmak sorun değil. Kaldırmak için
`searchslim uninstall --user`. Yeni bir Claude Code oturumunda devreye girer.

Bu repo hook'u kendi `.claude/settings.json` dosyasıyla zaten kullanıyor.

**Windows:** `searchslim: command not found` alırsanız pip'in `Scripts` klasörü
PATH'te değildir. Yerini `python -c "import sysconfig; print(sysconfig.get_path('scripts'))"`
(pip `--user` ile kurduysa `python -m site --user-base` altındaki `Scripts`) gösterir;
o klasörü PATH'e ekleyin ya da `python -m searchslim ...` kullanın. `install`, hook
komutunu hem Git Bash'te hem PowerShell'de çalışacak biçimde yazar
(`C:/Python314/python.exe -m searchslim hook`); eski bir kurulumu düzeltmek için
`searchslim install --user` komutunu yeniden çalıştırmak yeterli. Girdi ve çıktı
her platformda UTF-8'dir; `.\dizin\dosya` yolları notta dizine göre doğru gruplanır.

## Ne yapar

- **Bash:** `rg`, `grep`, `git grep`, `fd`, `find`, `git ls-files`, `tree` ve
  `ls -R` komutları `searchslim run --` ile sarılır. Her satır `yol:satır` ile
  gelsin diye çalışma anında satır numarası ve (birden çok dosyada) dosya adı
  bayrakları, sonuç kararlı olsun diye `rg --sort=path` eklenir. Tek dosyalı
  aramalar dosya adı eklenmeden kalır, böylece küçük çıktılar küçük kalır.
  `2>/dev/null` desteklenir. Arkasından yalnızca satır süzen komutlar gelen
  pipe'lar (`| head`, `| tail -n`, `| sort`, `| uniq`, `| grep -v x`) olduğu gibi
  çalıştırılır, yalnızca son çıktı küçültülür. `tree` ve `ls -R` çıktısında sığan
  en derin seviyeye kadar her şey kalır, daha derindekiler dizin dizin sayılır.
  Diğer yönlendirmeler, `$(...)`, `find -exec` gibi yan etkili komutlar ve
  sistemde kurulu olmayan araçlar (ör. yalnızca alias olan `rg`) değişmez.
- **PowerShell (Windows):** Claude Code'un PowerShell aracıyla yapılan `rg`,
  `Get-ChildItem -Recurse` ve `Select-String` aramaları da sarılır (`rg` için
  `searchslim run`, diğerleri için `| Out-String -Stream | searchslim filter`).
  Değişken, `$(...)`, script bloğu, yönlendirme, `;` veya başka cmdlet içeren
  komutlara dokunulmaz.
- **Grep/Glob:** araç normal çalışır. Ardından PostToolUse hook'u aynı aramayı
  `rg` ile kendisi yapar (Grep'in content, files_with_matches ve count modları,
  -A/-B/-C, multiline, glob/type, head_limit/offset). Sonuç bütçeye sığıyorsa
  hiçbir şey yapmaz. Sığmıyorsa aracın çıktısını küçültülmüş sonuçla değiştirir
  (`updatedToolOutput`), yani model bunu "hook error" olarak değil, aracın
  kendi sonucu olarak görür. Bu alanı desteklemeyen eski Claude Code
  sürümlerinde `SEARCHSLIM_GREP_MODE=deny` eski davranışı geri getirir
  (çağrı reddedilir, küçültülmüş sonuç gerekçede gelir).
- **Test/derleme çıktısı (0.5):** Bash/PowerShell komutunun çıktısı (stdout ve
  stderr) PostToolUse'ta incelenir. Çıktının kendisi pytest, jest, vitest,
  mocha, go test, cargo test, dotnet test özeti ya da çok sayıda cargo/pip/maven/
  dotnet ilerleme satırı içeriyorsa geçen ve atlanan testlerin satırları ile
  ilerleme satırları atılır; hatalar, traceback'ler, assert farkları, uyarılar
  ve özet sayılar olduğu gibi kalır. Zaman damgası dışında aynı olan 5+ ardışık
  log satırı ilk ve son satırıyla kalır. Sonda tek bir `[searchslim] not shown:
  ...` satırı neyin atıldığını sayar. Komuta değil çıktıya bakılır, yani
  modelin kendi yazdığı özet betikleri ve diğer çıktılar değişmez. Yalnızca
  2000 token üstünde ve en az %20 kazanç varsa devreye girer
  (`SEARCHSLIM_COMPACT_TRIGGER_TOKENS`, kapatmak için `SEARCHSLIM_COMPACT=off`).
  Not: pytest'in varsayılan çıktısı ve jest/vitest'in terminal dışı varsayılan
  çıktısı zaten kısa; kazanç `-v`/`--verbose`, `-rA`, `go test -v` ve
  `cargo test` gibi test başına satır basan çalıştırmalarda. Elle:
  `pytest -v | searchslim compact --stats`.
  0.5.1: sade test komutları (`pytest`, `python -m pytest`, `npx jest/vitest`,
  `npm test`, `go test`, `cargo test`, `dotnet test`; önünde `cd x &&`,
  `cd x;` ya da PowerShell'de `Set-Location x;` olabilir) PreToolUse'ta
  `searchslim run --compact -- <komut>` ile sarılır; çıkış kodu aynı kalır.
  Sebep: kalan testte (exit ≠ 0) Claude Code PostToolUse çalıştırmıyor, 30 KB
  üstü çıktıyı da hook'a kesik veriyor. Sarılmayan komutlarda PostToolUse,
  `persistedOutputPath` varsa tam çıktıyı o dosyadan okur.
- **Kayıpsız görünüm (0.6, varsayılan):** arama çıktısı hiçbir eşleşme
  atılmadan küçültülür; yalnızca tekrar çıkarılır. Yol her dosya için bir kez
  yazılır (rg'nin kendi `--heading` biçimi), örtüşen `-C` pencereleri tek kez
  basılır, tekrarlanan satırlar atılır, çok yerde geçen aynı satır (ör. aynı
  import) bir kez yazılıp yerleri listelenir. Dosya listeleri (Glob, fd, find)
  dizin başlığı altında gruplanır:

  ```
  src/auth/token.ts
  41:export function refreshToken() {
  44:const token = ...

  src/auth/logout.ts:20:import { revokeToken } from "./token"

  [searchslim] 5 matches are this same line: const timeout = process.env.TIMEOUT;
    src/a.ts:20,44
    src/b.ts:18
  ```

  0.6.1: aynı dizindeki dosyalar bir `dizin/` satırının altında girintili yazılır;
  satır numarası olmayan çıktı (Grep `-n: false`) artık hiç değiştirilmeden geçer
  (0.6.0'da bu biçim yanlış ayrıştırılıp satır kaybediyordu).
  0.6.2: hiçbir kayıpsız biçim bütçeye sığmasa da en küçüğü bütçenin 3 katına
  kadar kullanılır (her eşleşmenin yeri korunur); sıralamalı görünüm yalnızca
  bunun da üstünde devreye girer. 0.6.3: bütçe 4800 token (~19k karakter), çünkü
  Claude Code ~20k karakterden büyük Grep sonucunu dosyaya yazıp modele yalnızca
  ~2 KB önizleme gösteriyor; projeksiyonda ad listesi sona alındı ki önizlemede
  yerler görünsün.
  0.6.5: `rg -c` sayım listeleri de dizin altında gruplanır; PowerShell
  `Select-String -Context` çıktısı (`> ` eşleşme, iki boşluk bağlam) doğru
  ayrıştırılır; projeksiyon tanıyıcılarına tanımlar (`def`/`func`/`fn`/`class`...),
  HTTP route'ları, config anahtarları ve sürümüyle bağımlılıklar eklendi; eşik
  1000 token ve %15 kazanç. `tests/test_invariants.py` her fixture'da (Windows
  mutlak yollu kopyasıyla da) eşleşme konumlarının ham çıktıyla aynı kaldığını denetler.
  1000 token üstündeki çıktılara ve yalnızca en az %15 kazanç varsa uygulanır,
  yoksa çıktı aynen geçer. Sonuç 4800 tokenı (`SEARCHSLIM_MAX_TOKENS`, Claude
  Code'un Grep sonucunu satır içinde gösterdiği ~20k karakterin altı) hâlâ aşarsa sırayla:
  bağlam satırları bırakılır (her eşleşme kalır); arama bilinen bir liste türüyse
  (`process.env`/`os.environ`/`getenv`, `require`, `import`/`from`/`using`, tanımlar,
  route'lar, config anahtarları, bağımlılıklar) her
  eşleşen satır adı ve yeriyle yazılır (projeksiyon); o da sığmazsa aşağıdaki
  kapsam görünümü ve sıralama devreye girer. Offline benchmark'ta varsayılanlarla
  76.6k → 42.9k token, 36/36 kritik satır (`benchmark/results/2026-09-30-lossless.md`).
  `SEARCHSLIM_VIEW=coverage` 0.4'e döner.
- **Kurallar:** tekrarlar atılır, örtüşen satır aralıkları birleşir. Bütçe
  aşılırsa önce bağlam satırları, sonra fazla eşleşmeler, sonra dosyalar atılır.
- **Kapsam görünümü (0.4; 0.6'da yalnızca çok büyük çıktılar için):** bütçeyi aşan bir içerik araması
  "şu kadarı gösterilmedi" diye bitmez. Önce eşleşen her dosyanın dizini gelir
  (eşleşme sayısı, satır aralığı, ilk tanım satırı, kaçının açıldığı), sonra
  seçilen kanıt blokları aracın kendi biçiminde:

  ```
  [searchslim] 84 matches in 17 files. Coverage: 17/17 matching files indexed (matches, line span). Evidence: 12 blocks from 7 files expanded below.
    src/auth/token.ts  18 matches  L41-210  def L41  (5 expanded)
    src/auth/logout.ts  7 matches  L73-89
  src/auth/token.ts:41:export function refreshToken(...) {
  ```

  Dosya çoksa kanıtı olan dosyalar tek tek, diğerleri dizin dizin sayılır; her
  dosya bir satırda sayılmış olur. Amaç, ajanın aramanın tamamını bildiğini
  görüp gerekirse tek dosyaya inmesi, aynı aramayı tekrarlamaması.
  `SEARCHSLIM_VIEW=notes` 0.3'teki sondaki nota döner.
- **Sıralama (varsayılan açık):** kurallar bir şey atmak zorunda kaldığında
  bloklar kullanıcının amacına göre sıralanır, bütçeye en alakalıları girer.
  Amaç oturum kaydındaki son kullanıcı mesajından okunur. Model yalnızca skor
  döndürür; çıktıdaki her satır ham çıktıdan gelir, kod yeniden yazılmaz.
- **Oturum hafızası (çoklu arama):** aynı oturumdaki aramalar, daha önce
  gösterilmiş satırları hatırlar. Bütçeyi aşan bir arama bu satırları tekrar
  basmaz; notta `yol:satır` aralıklarıyla anar ve bütçeyi yeni satırlara
  harcar. Aynı aramayı tekrarlamak böylece sonraki sayfayı getirir. Metni
  değişen satır yeni sayılır, küçük çıktılar yine aynen geçer. Paralel araç
  çağrıları kilitsiz ve Windows'ta da güvenli çalışır (her çağrı kendi
  dosyasını atomik yazar). İki ayrı önbellek tutulur: dosya içeriği önbelleği
  (dosya başına içerik hash'i; diskteki gerçeği anlatır, sıkıştırmada kalır) ve
  modelin gördüğü kanıt önbelleği. Claude Code bağlamı sıkıştırınca
  (`PreCompact`) yalnızca ikincisi silinir; Edit/Write/MultiEdit/NotebookEdit
  o dosyanın satırlarını geçersiz kılar, dosya başka yoldan değişirse (Bash
  `sed -i`, checkout) hash tutmadığı için onlar da düşer. Kalıcı dosyaya
  yazılan büyük sonuçlardan yalnızca modelin gördüğü ~2 KB önizleme sayılır.
  Kayıpsız görünüm hiçbir satırı "gösterildi" diye atlamaz (yalnızca son
  seviyesi olan kapsama görünümü atlar). Alt ajanların (`agent_id`) hafızası
  ayrıdır.
- Hook bir hata alırsa sessizce çekilir, orijinal çağrı değişmeden çalışır.

Ayarlar (ortam değişkeni):

| değişken | etkisi |
|---|---|
| `SEARCHSLIM=off` | hook'u kapatır (komutun başına da yazılabilir) |
| `SEARCHSLIM_MAX_TOKENS` | bunun üstünde bir şey atılır; varsayılan 4800 (`coverage`/`notes` görünümünde 2000) |
| `SEARCHSLIM_TRIGGER_TOKENS` | yalnızca bundan büyük çıktılara dokunulur; varsayılan 1000 (`coverage`/`notes` görünümünde 6000) |
| `SEARCHSLIM_VIEW` | `lossless` (varsayılan, 0.6), `coverage` (0.4: dosya dizini + seçilmiş kanıt) veya `notes` (0.3'teki sondaki not) |
| `SEARCHSLIM_RERANK` | `lexical` (varsayılan), `claude` veya `off` |
| `SEARCHSLIM_GREP_MODE` | `post` (varsayılan) veya `deny` (Grep/Glob için eski PreToolUse davranışı) |
| `SEARCHSLIM_SESSION=off` | oturum hafızasını kapatır |
| `SEARCHSLIM_CACHE_DIR` | hafızanın yeri, varsayılan geçici dizinde `searchslim-<kullanıcı>` |

`claude` sıralayıcısı `claude-haiku-4-5` kullanır; `pip install 'searchslim[claude] @ git+https://github.com/MSelcukAkbas/reranker_devops'` ve bir API anahtarı gerekir.

## Elle kullanım

```sh
rg -n -C2 "raise " src | searchslim filter --stats
searchslim run --max-tokens 1500 -- rg -n -C2 "raise " src
searchslim run --rerank lexical --intent "res.redirect varsayılan status'u değiştir" -- rg -n -C2 redirect lib
```

`run` ve `filter` de varsayılan olarak alaka sıralaması yapar (`--rerank off` veya
`SEARCHSLIM_RERANK=off` kapatır); `run` arama desenini komuttan alır.
`run`, komutun exit code'unu ve stderr'ini aynen korur.

## Sonuçlar

4 sabit sürümlü repoda (pytest, ripgrep, express, cobra) 27 arama görevi
(içerik aramaları, tek dosya aramaları, Glob/`find` listeleri, sayımlar, iki
adımlı aramalar), 36 kritik kanıt satırı. "Görünen": kanıt satırı çıktıda doğrudan var.
"Notta": satır yok ama dosyası notta adıyla geçiyor, bir ek aramayla bulunur.
Token sayımı chars/4 tahmini. Tam tablo:
[benchmark/results/2026-09-29-tasks27.md](benchmark/results/2026-09-29-tasks27.md), yöntem:
[docs/benchmark-design.md](docs/benchmark-design.md).

Varsayılan bütçe (2000 token):

| mod | token | ham çıktıya göre | görünen | notta | kayıp | ek arama |
|---|---|---|---|---|---|---|
| raw | 76.6k | %100 | 36/36 | 0 | 0 | 0 |
| rules | 30.6k | %40 | 32/36 | 4 | 0 | 3 |
| rules + sözcüksel sıralama | 32.3k | %42 | 36/36 | 0 | 0 | 0 |

Daha küçük bütçelerde fark büyüyor (ilk 21 görev, 34 satır):

| bütçe | rules token | rules görünen | sıralama token | sıralama görünen |
|---|---|---|---|---|
| 1000 | 15.0k | 18/34 | 15.5k | 29/34 |
| 1200 | 18.4k | 24/34 | 18.3k | 30/34 |
| 1500 | 22.0k | 26/34 | 22.5k | 31/34 |

Kurallar arama başına ~4 ms, sözcüksel sıralama süreç içinde ~5-15 ms ekliyor
(benchmark'taki ~120 ms, sıralayıcının ayrı bir Python süreci olarak başlatılmasından).

### Çoklu arama oturumları

Aynı görevde 4-5 arama yapan 5 ajan oturumu (daralt, `-C` ile genişlet,
tekrarla), varsayılan bütçe. Tam tablo ve gecikme ölçümü:
[benchmark/results/2026-09-29-sessions.md](benchmark/results/2026-09-29-sessions.md).

| hafıza | gönderilen token | tekrar gönderilen satır | görülen farklı satır | kritik kanıt |
|---|---|---|---|---|
| kapalı | 33.7k | 1010 | 817 | 9/9 |
| açık | 33.5k | 600 | 1209 (+%48) | 9/9 |

Aynı token ile ajan %48 daha fazla farklı kod satırı görüyor; yeni satır başına
maliyet 41 tokendan 28 tokena iniyor. 8 paralel büyük Grep çağrısında hook
en fazla ~1 sn sürüyor (öncesinde parser darboğazıyla ~1.6 sn).

### Canlı Claude Code koşusu

Claude Code'u (`claude -p`) her görevin amacıyla 3 kez koşturduk: hook yok, hook
yalnızca kurallarla, hook kurallar + sözcüksel sıralamayla (varsayılan). Tam tablo:
[benchmark/results/2026-09-29-live.md](benchmark/results/2026-09-29-live.md).

| mod | doğru cevap | arama çağrısı | okunan arama sonucu | ortalama girdi token | toplam $ |
|---|---|---|---|---|---|
| hook yok | 70/81 | 138 | 49.6k | 101k | 4.68 |
| rules | 74/81 | 133 | 42.3k | 105k | 4.66 |
| rules + sıralama | 72/81 | 133 | 42.9k | 103k | 4.73 |

- Ajanın okuduğu arama sonucu %14 azalıyor; doğruluk ve arama sayısı değişmiyor
  (70-74/81 farkı gürültü aralığında).
- Toplam girdi token'ı ve maliyet değişmiyor, çünkü bu görevlerde bir oturumun
  ~95k token'ı sistem istemi ve araç tanımları; arama çıktısı küçük bir pay.
- Claude kendi aramalarını zaten dar tutuyor: ~135 aramanın yalnızca 6-7'si
  bütçeyi aştı ve hook'a düştü. Kazanç, geniş aramaların (`raise `, tüm repo
  `find`, büyük dosyada `rg`) sık olduğu uzun oturumlarda ortaya çıkar;
  yukarıdaki çevrimdışı tablo bu durumu ölçüyor.

### Faz 2: model ile sıralama

Faz 1 modelsizdir: kurallar ve sözcüksel (BM25) sıralama, bağımlılık yok. Faz 2'de
kuralların eleyeceği metin ve aranan şey (kullanıcı amacı, alt görev, sorgu) hafif
bir sıralama modeline (Jev benzeri) verilecek; model yalnızca mevcut blokları
sıralayıp seçecek, kod yazmayacak. Benchmark buna hazır: `bench.py run --model-cmd`
herhangi bir sıralayıcıyı aynı görevlerde raw, rules ve rules + sözcüksel ile
karşılaştırır ve ham girdide olmayan satır üreten çıktıyı geçersiz sayar.

Benchmark'ı yeniden koşturmak için:

```sh
python3 benchmark/bench.py run --model-cmd "python3 -m searchslim bench-model --scorer lexical"
python3 benchmark/sessions.py run        # çoklu arama oturumları
python3 benchmark/sessions.py latency    # paralel hook gecikmesi
python3 benchmark/live.py --repeat 3 --jobs 6   # canlı Claude Code koşusu (gerçek API harcar)
```

### Canlı Windows testinden çıkan sonuç (2026-09-29)

arvis_code üzerinde headless Claude Code ile yapılan testlerde (0.3.0–0.3.2) net kazanç
yalnızca çok büyük çıktılarda görülüyor. Claude'un Grep çağrıları çoğunlukla 4k tokenın
altında kalıyor. Bunları kırpmak bağlamı küçültse bile ajan eksik kısmı ek aramalarla
(Grep, PowerShell ile dosya okuma) geri almaya çalıştı: 2000 eşiğinde tur ve maliyet
arttı (ör. 4 → 5 tur, $0.283 → $0.288). Not metnini "gerekirse daralt" yerine
yönlendirici olmayacak şekilde değiştirmek bunu tam önlemedi. Bu yüzden varsayılan eşik
6000 token (`SEARCHSLIM_TRIGGER_TOKENS`): orta boy sonuçlar olduğu gibi geçer, not da
yalnızca tarafsız bir sayım içerir (ne gösterilmedi, kaç tane, nerede).

0.4 bu sorunu notu değil çıktının biçimini değiştirerek deniyor: kapsam görünümü
(yukarıda) eksik olanı değil, aramanın tamamının dizinini gösterir. Offline
benchmark'ta 2000 bütçede kanıt kaybı değişmedi (rules+model 36/36,
`benchmark/results/2026-09-30-coverage.md`). Ajanın tekrar arayıp aramadığı ancak
canlı testte ölçülebilir; tekrar arama durursa eşik düşürülebilir.

Canlı ölçüm (2026-09-30, 0.4.0, 2000 eşik, 4 görev × 3): toplam maliyet hook
kapalıyla aynı ($3.035 → $3.041); "hepsini listele" (`process.env`) görevinde ajan
dizinde açılmamış dosyaları yeniden aradı, %23 daha pahalı. 0.6 bu yüzden varsayılanı
kayıpsız görünüme çevirdi: eşleşme atılmadığı için yeniden aranacak eksik de yok.

## Geliştirme

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

Plan, değişmez kurallar ve modül yapısı için [CLAUDE.md](CLAUDE.md).
