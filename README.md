# searchslim

Claude Code gibi ajanların Grep, Glob, `rg`, `fd` ve `find` çıktılarını,
kritik kod kanıtını kaybetmeden küçülten drop-in katman. Yeni bir tool eklemez;
çıktı, aracın kendi formatında kalır.

## Durum

Phase 1'in adımları hazır: ortak veri modeli ve ayrıştırıcılar, deterministik
kural katmanı, Claude Code hook'u ile drop-in entegrasyon ve modelle sıralama
(rules+model). Benchmark ayrı PR'da. Ayrıntılar ve değişmez kurallar için
[CLAUDE.md](CLAUDE.md).

## Kullanım

```sh
python3 -m pip install -e '.[dev]'

# stdin'den gelen çıktıyı küçült
rg -n -C2 "raise " src | searchslim filter --stats

# komutu çalıştırıp çıktısını küçült (exit code ve stderr aynen korunur)
searchslim run --max-tokens 1500 -- rg -n -C2 "raise " src
searchslim run -- fd -e py
```

Bütçe aşılırsa sırasıyla bağlam satırları, dosya başına fazla eşleşmeler ve
sondaki dosyalar atılır. Atılan her şey sondaki tek `[searchslim] ...` notunda
dosya/dizin bazında sayılır, böylece ajan aramayı daraltıp tekrar çalıştırabilir.

Örnek: pytest kaynak kodunda `rg -n -C2 "raise "` 171 KB çıktı üretiyor;
varsayılan 2000 token bütçesiyle 7,8 KB'a iniyor ve atlanan 281 eşleşme
dosya bazında notta listeleniyor.

## Claude Code'a bağlama

Bu repo kendi hook'unu `.claude/settings.json` ile zaten kullanıyor. Başka bir
projede kullanmak için paketi kurup o projenin `.claude/settings.json`
dosyasına şunu ekle:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Grep|Glob",
        "hooks": [{ "type": "command", "command": "python3 -m searchslim hook", "timeout": 30 }]
      }
    ]
  }
}
```

- **Bash:** düz `rg`/`grep`/`fd`/`find` komutları `searchslim run --` ile sarılır.
  Pipe, yönlendirme veya yan etkili bayrak içeren komutlara dokunulmaz.
- **Grep/Glob:** hook aynı aramayı `rg` ile kendisi yapar. Sonuç bütçeye
  sığıyorsa hiçbir şey yapmaz, gerçek araç çalışır. Sığmıyorsa küçültülmüş
  sonucu modele verir.
- Kapatmak için `SEARCHSLIM=off`, bütçe için `SEARCHSLIM_MAX_TOKENS`.

## Modelle sıralama (rules+model)

Kurallar bir şey atmak zorunda kaldığında, bloklar kullanıcının amacına göre
sıralanır ve bütçeye en alakalı olanlar girer. Model yalnızca skor ya da blok
numarası döndürür; çıktıdaki her satır ham çıktıdan gelir.

```sh
searchslim run --rerank lexical --intent "res.redirect varsayılan status'u değiştir" -- rg -n -C2 redirect lib
SEARCHSLIM_RERANK=lexical   # hook'ta açmak için; amaç oturum kaydından okunur
```

- `lexical` (varsayılan): bağımlılıksız, deterministik.
- `claude`: `claude-haiku-4-5` ile sıralar; `pip install 'searchslim[claude]'` ve API anahtarı gerekir.
