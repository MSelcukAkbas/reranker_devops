# Benchmark tasarımı (Phase 1, adım 4)

Amaç: aynı arama çıktısı üzerinde `raw`, `rules` ve `rules+model` modlarını
token, gecikme, maliyet, ek arama ihtiyacı ve kritik kanıt kaybı açısından
karşılaştırmak. Tüketici bir ajan (Claude Code) olduğu için asıl soru şu:
"Ajan bu çıktıdan işini görebilir mi, yoksa tekrar aramak zorunda mı kalır?"

Görev seti: [`benchmark/tasks.json`](../benchmark/tasks.json).

## 1. Korpus

Dört gerçek açık kaynak repo, sabit tag ve commit ile. Satır numaraları bu
commit'lere göre doğrulandı.

| repo | tag | dil | dosya | neden |
|---|---|---|---|---|
| pytest-dev/pytest | 8.3.3 | Python | 598 | büyük, çok dosyalı; `raise ` gibi geniş aramalar 90+ KB çıktı üretiyor |
| BurntSushi/ripgrep | 14.1.1 | Rust | 213 | çok crate'li yapı, uzun dosyalar |
| expressjs/express | 4.21.1 | JavaScript | 234 | kod + test + `History.md` gürültüsü aynı aramada |
| spf13/cobra | v1.8.1 | Go | 66 | küçük repo ama tek dev dosya (`command.go`) |

Harness repoları `git clone --depth 1 --branch <tag>` ile çeker ve commit
hash'ini doğrular.

## 2. Görevler

Her görev, ajanın gerçekten yapacağı bir alt işe karşılık gelir ve 14 görev
bütün çıktı şekillerini kapsar: `content` (`-n`, `-C2/-C3`), dosya listesi
(`rg -l`, `rg --files`, `find`), sayım (`rg -c`), tek dosyada arama. Küçük
çıktılar da bilerek var: pass-through davranışını ölçmek için.

Görev alanları:

- `repo`, `cmd`: çalıştırılacak arama, argv olarak.
- `intent`: kullanıcının amacı. `subtask`: ajanın o anki alt görevi.
  İkisi de yalnızca `rules+model` modunda modele verilir.
- `evidence`: çıktıda bulunması gereken kanıtlar.
  - `path` + `line` + `text`: belirli bir satır. `text`, satırın commit'teki
    içeriğinde geçmek zorunda; harness her koşuda bunu doğrular, böylece
    kayan satır numarası sessizce yanlış ölçüm üretmez. `line` verilmezse
    `text`'in ilk geçtiği satır kullanılır.
  - yalnızca `path`: dosya listesi ve sayım görevleri için.
  - `critical: true` olanlar asıl metriğe girer; `false` olanlar destekleyici
    kanıttır, ayrı raporlanır.

Yeni görev eklerken kural: kanıt, görevin cevabı için gerçekten gerekli
satırdır; "eşleşen ilk satır" değil. Kritik kanıt ham çıktıda bulunmak
zorunda: `bench.py fetch` ve testler bunu kontrol eder (ham çıktıda olmayan
bir satır için hiçbir mod suçlanamaz).

## 3. Modlar

- `raw`: aracın çıktısı olduğu gibi.
- `rules`: `searchslim` deterministik kurallar, varsayılan `Config`.
- `rules+model`: yalnızca kuralların bir şey attığı çıktılarda devreye girer.
  Model, `intent`, `subtask`, sorgu ve **ham çıktıdaki bloklar** ile çalışır
  ve aynı token bütçesi içinde hangi blokların tutulacağını ve sırasını seçer.
  Harness, model çıktısının ham blokların bir alt kümesi olduğunu doğrular;
  değilse koşu başarısız sayılır (CLAUDE.md'deki "yeniden yazma, yeni sonuç
  üretme" kuralı).

## 4. Ölçüm

İki katman var. A ucuz ve deterministik, her PR'da koşabilir. B pahalı, adım 3
(hook) bittikten sonra ara sıra koşar.

### A. Çevrimdışı replay

Her görevin ham çıktısı bir kez alınır ve `benchmark/fixtures/<id>.txt` olarak
saklanır; üç mod da aynı girdiyle koşar. Bu şart: `rg` dosya sırası paralel
çalıştığı için koşudan koşuya değişiyor (bkz. bulgu 1). Yakalama `rg --sort path`
ile yapılır; sırasız davranış ayrı bir kararlılık testiyle ölçülür (aynı
görevi sırasız 10 kez koştur, kanıt durumunun kaç kez değiştiğini say).

Metrikler, görev başına:

| metrik | tanım |
|---|---|
| `tokens` | ajanın okuyacağı metnin token sayısı. Gerçek tokenizer: Anthropic `count_tokens` API. Ağ yoksa chars/4 tahmini, ayrı kolonda. |
| `latency_ms` | katmanın eklediği süre. `rules`: süreç içi 20 koşunun medyanı, ayrıca `searchslim run -- <cmd>` ile ham komut arasındaki uçtan uca fark. `rules+model`: model çağrısının p50/p95'i. |
| `cost_usd` | `tokens × ajan modelinin girdi fiyatı` + (model modunda) `reranker girdi/çıktı token × fiyatı`. Fiyatlar tek bir config dosyasında. Arama çıktısı bağlamda kaldığı için sonraki her turda tekrar okunur; `k` kalan tur sayısı parametresiyle `k=1` ve `k=10` raporlanır. |
| `evidence` | her kritik kanıt için bir durum: **kept**: tam `path:line` gövdede eşleşme veya bağlam satırı olarak var (liste görevlerinde yol var). **recoverable**: satır gövdede yok ama dosya gövdede başka bir satırla ya da sondaki `[searchslim]` notunda adıyla (liste görevlerinde üst dizinle) geçiyor; ajan tek bir daraltılmış aramayla bulur. **lost**: hiçbiri değil. |
| `extra_searches` | tahmini ek arama: recoverable kritik kanıtların bulunduğu farklı dosya sayısı. `lost` kanıt ek aramayla da garanti bulunamayacağı için ayrıca sayılır. |

Toplu rapor: toplam token ve azalma yüzdesi, `kept/recoverable/lost` oranları,
görev başına ortalama `extra_searches`, `lost > 0` olan görev sayısı.

### B. Ajan döngüsü

Claude Code headless (`claude -p`) her görevin `intent`'i ile pinli repoda
koşar: hook kapalı (raw), hook açık (rules), hook açık + model. Ajanın cevabı
kritik kanıtların `path:line`'ını anmak zorunda; kontrol otomatik. Her hücre
3 kez koşar (ajan varyansı). Oturum kaydından: toplam girdi token'ı, maliyet,
süre, Grep/Glob/Bash arama çağrısı sayısı (gerçek `extra_searches`) ve
cevabın doğruluğu. A'daki tahmini ek arama sayısı burada gerçeğiyle
karşılaştırılır; ikisi ayrışıyorsa A'nın tanımı düzeltilir.

### Kabul kriterleri

- `rules`: `lost` = 0 (kritik kanıt), token azalması büyük çıktılarda (>2000
  token) en az %50, eklenen gecikme p95 < 50 ms.
- `rules+model`: ancak `rules`'a göre `kept` oranını artırıyor veya
  `extra_searches`'i düşürüyorsa ve kazandırdığı token maliyeti modelin
  maliyetinden fazlaysa (`k=1`'de) tutulur.

## 5. İlk sonuçlar (rules, katman A, chars/4)

`python3 benchmark/bench.py run --tokenizer chars`, varsayılan `Config`:

| görev | raw tok | rules tok | ms | kritik kept | recoverable | lost |
|---|---|---|---|---|---|---|
| pytest-usage-error-p-option | 24214 | 1872 | 11.2 | 1/1 | 0 | 0 |
| pytest-raises-impl | 5558 | 1694 | 2.1 | 2/2 | 0 | 0 |
| pytest-fixture-scope | 1827 | 173 | 1.0 | 0/2 | 2 | 0 |
| pytest-warnings-importers | 266 | 266 | 0.0 | 2/2 | 0 | 0 |
| pytest-fixture-tests-file | 1866 | 1806 | 0.2 | 1/1 | 0 | 0 |
| pytest-src-files-find | 460 | 459 | 0.1 | 2/2 | 0 | 0 |
| pytest-test-counts | 1413 | 1412 | 0.2 | 2/2 | 0 | 0 |
| ripgrep-max-columns | 6603 | 1624 | 1.9 | 2/2 | 0 | 0 |
| ripgrep-search-fns | 447 | 447 | 0.1 | 1/1 | 0 | 0 |
| ripgrep-unwrap-counts | 374 | 374 | 0.1 | 1/1 | 0 | 0 |
| express-redirect-status | 4557 | 1260 | 1.7 | 1/2 | 1 | 0 |
| express-req-query | 303 | 303 | 0.1 | 1/1 | 0 | 0 |
| cobra-persistent-flags | 5844 | 1255 | 2.3 | 0/1 | 1 | 0 |
| cobra-execute | 2820 | 1119 | 1.0 | 0/2 | 2 | 0 |
| **toplam** | **56552** | **14064 (%25)** | p95 11.2 | **16/22** | **6** | **0** |

Maliyet (Opus 5.5 girdi fiyatıyla, `k=1`): raw $0.226, rules $0.056. Token
tarafı hedefin üstünde, sıralı girdide kayıp yok; ama kritik kanıtların 6/22'si
ek arama gerektiriyor (tahmini 4 ek arama). Bulgular:

1. **Sıra kararsızlığı gerçek kayba yol açıyor.** `bench.py stability --runs 10`:
   sırasız `rg` ile 11 görevin 11'inde rules çıktısı koşudan koşuya değişiyor ve
   `pytest-usage-error-p-option`'da kritik kanıt 10 koşunun 8'inde `lost`:
   dosya, notta adı yazılan ilk 10 dosyanın dışında kalıyor ("+15 more files").
   Kurallar deterministik ama girdi değil. Öneri: dosya listesi kesilince
   kalanlar dizin bazında özetlensin; hook `rg`'ye `--sort path` eklemeyi düşünsün.
2. **Tek dosya aramasında format ve bütçe sorunu.** `rg -n scope file.py`
   çıktısında yol yok. `--default-path` olmadan parser 0 satır görüp çıktıyı
   aynen geçiriyor; verilince her satıra yol ekleniyor (aracın formatı
   değişiyor), bu da 1827 token'lık çıktıyı bütçenin üstüne itiyor ve 133
   eşleşmeden 8'i kalıyor. Not "aramayı daralt" diyor ama arama zaten tek
   dosyada. Öneri: bütçe orijinal metin üzerinden hesaplansın, yol eklenmesin,
   tek dosyada dosya başı sınır uygulanmasın.
3. **Dosya başı 8 eşleşme sınırı büyük dosyayı cezalandırıyor.** `cobra-execute`'ta
   küçük dosyaların tüm eşleşmeleri kalırken `command.go`'nun 113 eşleşmesinden
   8'i kaldı; `Execute()` ve `ExecuteC()` gitti. Kuralların sıralama tahmini
   yapmadan çözemeyeceği durum; `rules+model` modunun kapatması gereken boşluk bu.

Bulgular burada düzeltilmedi; kural katmanı için ayrı iş.

## 6. Kullanım

```sh
python3 benchmark/bench.py fetch          # repoları çek, fixture'ları yakala, çapaları doğrula
python3 benchmark/bench.py run            # modları fixture'lar üzerinde koştur (ağ gerekmez)
python3 benchmark/bench.py run --jsonl results.jsonl --model-cmd "python3 my_reranker.py"
python3 benchmark/bench.py stability --runs 10
```

- `benchmark/tasks.json`: korpus ve görevler. `benchmark/fixtures/`: `rg --sort path`
  ile yakalanmış ham çıktılar (commit'li, `run` bunlarla çevrimdışı çalışır).
  Repolar `~/.cache/searchslim-bench` altına klonlanır (`SEARCHSLIM_BENCH_CACHE`).
- Token: `anthropic` paketi ve kimlik bilgisi varsa `count_tokens`, yoksa
  chars/4; tablo hangisinin kullanıldığını yazar. Kural katmanı gibi harness'ın
  da zorunlu bağımlılığı yok.
- `rules+model`: `--model-cmd` stdin'den JSON (`intent`, `subtask`, `cmd`, `raw`,
  `rules`, `max_tokens`) alır, küçültülmüş çıktıyı stdout'a yazar; stderr'in son
  satırına `{"input_tokens": N, "output_tokens": M}` yazarsa maliyeti Haiku 4.5
  fiyatıyla sayılır. Gövdede ham girdide olmayan satır varsa koşu geçersiz
  sayılır. Kuralların hiçbir şey atmadığı görevlerde model çağrılmaz.
- Katman B, adım 3'teki Claude Code hook'u birleşince eklenir.
