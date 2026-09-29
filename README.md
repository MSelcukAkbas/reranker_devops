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

`searchslim install --user` hook'u `~/.claude/settings.json` dosyasına ekler, yani
bütün projelerde açılır. Tek bir proje için `searchslim install <proje-dizini>`.
Mevcut ayarlara dokunmaz, iki kez çalıştırmak sorun değil. Kaldırmak için
`searchslim uninstall --user`. Yeni bir Claude Code oturumunda devreye girer.

Bu repo hook'u kendi `.claude/settings.json` dosyasıyla zaten kullanıyor.

**Windows:** `searchslim: command not found` alırsanız pip'in `Scripts` klasörü
PATH'te değildir. Yerini `python -c "import sysconfig; print(sysconfig.get_path('scripts'))"`
(pip `--user` ile kurduysa `python -m site --user-base` altındaki `Scripts`) gösterir;
o klasörü PATH'e ekleyin ya da `python -m searchslim ...` kullanın. Girdi ve çıktı
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
- **Grep/Glob:** hook aynı aramayı `rg` ile kendisi yapar (Grep'in content,
  files_with_matches ve count modları, -A/-B/-C, multiline, glob/type,
  head_limit/offset). Sonuç bütçeye
  sığıyorsa hiçbir şey yapmaz, gerçek araç çalışır. Sığmıyorsa küçültülmüş
  sonucu modele verir (başında "bu bir hata değil, sonuç" yazar).
- **Kurallar:** tekrarlar atılır, örtüşen satır aralıkları birleşir. Bütçe
  aşılırsa önce bağlam satırları, sonra fazla eşleşmeler, sonra dosyalar atılır.
- **Sıralama (varsayılan açık):** kurallar bir şey atmak zorunda kaldığında
  bloklar kullanıcının amacına göre sıralanır, bütçeye en alakalıları girer.
  Amaç oturum kaydındaki son kullanıcı mesajından okunur. Model yalnızca skor
  döndürür; çıktıdaki her satır ham çıktıdan gelir, kod yeniden yazılmaz.
- Hook bir hata alırsa sessizce çekilir, orijinal çağrı değişmeden çalışır.

Ayarlar (ortam değişkeni):

| değişken | etkisi |
|---|---|
| `SEARCHSLIM=off` | hook'u kapatır (komutun başına da yazılabilir) |
| `SEARCHSLIM_MAX_TOKENS` | bütçe, varsayılan 2000 |
| `SEARCHSLIM_RERANK` | `lexical` (varsayılan), `claude` veya `off` |

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

Benchmark'ı yeniden koşturmak için:

```sh
python3 benchmark/bench.py run --model-cmd "python3 -m searchslim bench-model"
```

## Geliştirme

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

Plan, değişmez kurallar ve modül yapısı için [CLAUDE.md](CLAUDE.md).
