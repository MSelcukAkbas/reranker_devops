# searchslim

Claude Code gibi ajanların Grep, Glob, `rg`, `fd` ve `find` çıktılarını,
kritik kod kanıtını kaybetmeden küçülten drop-in katman. Yeni bir tool eklemez;
çıktı, aracın kendi formatında kalır.

## Durum

Phase 1'in ilk iki adımı hazır: ortak veri modeli ve ayrıştırıcılar, ve
deterministik kural katmanı. Sırada drop-in entegrasyon, benchmark ve
opsiyonel model ile sıralama var. Ayrıntılar ve değişmez kurallar için
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
