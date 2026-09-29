# searchslim

Claude Code gibi ajanların Grep, Glob, `rg`, `grep`, `fd` ve `find` çıktılarını,
kritik kod kanıtını kaybetmeden küçülten drop-in katman. Yeni bir tool eklemez;
çıktı, aracın kendi formatında (`yol:satır:metin`) kalır ve atılan her şey
sondaki tek bir `[searchslim]` notunda sayılır.

## Kurulum (tek komut)

```sh
pip install git+https://github.com/MSelcukAkbas/reranker_devops && searchslim install --user
```

`searchslim install --user` hook'u `~/.claude/settings.json` dosyasına ekler, yani
bütün projelerde açılır. Tek bir proje için `searchslim install <proje-dizini>`.
Mevcut ayarlara dokunmaz, iki kez çalıştırmak sorun değil. Kaldırmak için
`searchslim uninstall --user`. Yeni bir Claude Code oturumunda devreye girer.

Bu repo hook'u kendi `.claude/settings.json` dosyasıyla zaten kullanıyor.

## Ne yapar

- **Bash:** düz `rg`/`grep`/`fd`/`find` komutları `searchslim run --` ile sarılır.
  Her satır `yol:satır` ile gelsin diye dosya adı ve satır numarası bayrakları,
  sonuç kararlı olsun diye `rg --sort=path` eklenir. Pipe, yönlendirme, `$(...)`
  veya `find -exec` gibi yan etkili komutlara dokunulmaz.
- **Grep/Glob:** hook aynı aramayı `rg` ile kendisi yapar. Sonuç bütçeye
  sığıyorsa hiçbir şey yapmaz, gerçek araç çalışır. Sığmıyorsa küçültülmüş
  sonucu modele verir (başında "bu bir hata değil, sonuç" yazar).
- **Kurallar:** tekrarlar atılır, örtüşen satır aralıkları birleşir. Bütçe
  aşılırsa önce bağlam satırları, sonra fazla eşleşmeler, sonra dosyalar atılır.
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
  dosyasını atomik yazar). Claude Code bağlamı sıkıştırınca (`PreCompact`)
  hafıza silinir; alt ajanların (`agent_id`) hafızası ayrıdır.
- Hook bir hata alırsa sessizce çekilir, orijinal çağrı değişmeden çalışır.

Ayarlar (ortam değişkeni):

| değişken | etkisi |
|---|---|
| `SEARCHSLIM=off` | hook'u kapatır (komutun başına da yazılabilir) |
| `SEARCHSLIM_MAX_TOKENS` | bütçe, varsayılan 2000 |
| `SEARCHSLIM_RERANK` | `lexical` (varsayılan), `claude` veya `off` |
| `SEARCHSLIM_SESSION=off` | oturum hafızasını kapatır |
| `SEARCHSLIM_CACHE_DIR` | hafızanın yeri, varsayılan geçici dizinde `searchslim-<kullanıcı>` |

`claude` sıralayıcısı `claude-haiku-4-5` kullanır; `pip install 'searchslim[claude] @ git+https://github.com/MSelcukAkbas/reranker_devops'` ve bir API anahtarı gerekir.

## Elle kullanım

```sh
rg -n -C2 "raise " src | searchslim filter --stats
searchslim run --max-tokens 1500 -- rg -n -C2 "raise " src
searchslim run --rerank lexical --intent "res.redirect varsayılan status'u değiştir" -- rg -n -C2 redirect lib
```

`run`, komutun exit code'unu ve stderr'ini aynen korur.

## Sonuçlar

4 sabit sürümlü repoda (pytest, ripgrep, express, cobra) 21 arama görevi,
34 kritik kanıt satırı. "Görünen": kanıt satırı çıktıda doğrudan var.
"Notta": satır yok ama dosyası notta adıyla geçiyor, bir ek aramayla bulunur.
Token sayımı chars/4 tahmini. Tam tablo:
[benchmark/results/2026-09-29.md](benchmark/results/2026-09-29.md), yöntem:
[docs/benchmark-design.md](docs/benchmark-design.md).

Varsayılan bütçe (2000 token):

| mod | token | ham çıktıya göre | görünen | notta | kayıp | ek arama |
|---|---|---|---|---|---|---|
| raw | 69.4k | %100 | 34/34 | 0 | 0 | 0 |
| rules | 27.5k | %40 | 30/34 | 4 | 0 | 3 |
| rules + sıralama | 29.1k | %42 | 34/34 | 0 | 0 | 0 |

Daha küçük bütçelerde fark büyüyor:

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

Benchmark'ı yeniden koşturmak için:

```sh
python3 benchmark/bench.py run --model-cmd "python3 -m searchslim bench-model"
python3 benchmark/sessions.py run        # çoklu arama oturumları
python3 benchmark/sessions.py latency    # paralel hook gecikmesi
```

## Geliştirme

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

Plan, değişmez kurallar ve modül yapısı için [CLAUDE.md](CLAUDE.md).
