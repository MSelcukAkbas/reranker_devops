# searchslim

Claude Code gibi ajanların Grep, Glob, `rg`, `fd` ve `find` çıktılarını,
kritik kod kanıtını kaybetmeden küçülten drop-in katman. Yeni bir tool eklemez;
çıktı, aracın kendi formatında kalır.

## Durum

Phase 1'in ilk üç adımı hazır: ortak veri modeli ve ayrıştırıcılar,
deterministik kural katmanı ve Claude Code hook'u ile drop-in entegrasyon.
Sırada benchmark ve opsiyonel model ile sıralama var. Ayrıntılar ve değişmez kurallar için
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
