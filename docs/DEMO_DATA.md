# Geliştirme/test demo verisi

Yalnızca geliştirme/test veritabanında, migration'lar uygulandıktan sonra çalıştırın:

```powershell
.\.venv\Scripts\python.exe manage.py seed_demo_data --settings=config.settings_local
```

Komut `DEBUG=True` gerektirir (`DJANGO_DEBUG=true`); `DEBUG=False` durumunda
veritabanına yazmadan hata verir. Canlı ortamda kullanmak için DEBUG açmayın.

Oluşan küçük veri kümesi:

- 2 `ShoeModel`, her biri için 2 `ProductVariant`.
- Varyantların zorunlu bire bir `Product` ilişkisi nedeniyle 4 mamul ve reçeteler
  için ayrıca 1 hammadde.
- 1 demo depo, 3 lot ve doğrulanmış stok girişi (100 kg hammadde, iki mamulden
  12'şer çift). Stok miktarlarını `StockMovement.create_verified_movement` hesaplar.
- 2 planlanmış üretim emri; aktif reçete ve rota, hat, iş merkezi, malzeme
  ihtiyaçları ve operasyonlar. Emir kalemleri mevcut `create_components_from_bom`
  ve `create_operations_from_routing` metotlarıyla oluşturulur.
- 2 müşteri, her müşteriye 2 kalemli bir taslak satış siparişi. Satır tutarları
  `SalesOrderLine.save()` tarafından hesaplanır.

Üretim emirlerinin zorunlu `created_by` alanı için giriş yapamayan, yetkisiz,
parolası kullanılamayan `DEMO-SEED` kullanıcısı oluşturulur. Müşteri e-postaları
`example.invalid` kullanır. Üretim ve satış emirleri bağımsız örneklerdir.

Demo kodları/SKU'ları `DEMO-` ile başlar. Mevcut açıklama/not/adres alanlarında
`seed_demo_data:v1` işareti kullanılır; bu alanları olmayan renk ve iş merkezi
demo adlarıyla, diğer alt kayıtlar doğrulanan demo ilişkileriyle ayırt edilir.
Numaralar da mevcut numaralarla karışmamak için `DEMO-40` ve `DEMO-41` olarak
saklanır. Model veya migration değişikliği gerekmez.

Tüm işlem `transaction.atomic()` içindedir. Benzersiz alanlarda
`get_or_create` kullanılır; mevcut kayıtların değerleri ve iş akışı durumları
korunur. Tekrar çalıştırmak tüketilen stoğu doldurmaz veya sipariş kalemlerini
sıfırlamaz. Kod, demo işareti veya ilişki çakışması tüm işlemi geri alır.
Silme/temizleme seçeneği yoktur. `save()` ve sinyaller normal şekilde çalışır.

Doğrulama:

```powershell
.\.venv\Scripts\python.exe manage.py test core.test_seed_demo_data --settings=config.settings_local
```

Testler Django'nun ayrı test veritabanında çalışır; oluşturulan verileri, domain
kurallarını, tekrarlı çalıştırmayı, mevcut verinin korunmasını, çakışmaları,
transaction rollback davranışını ve DEBUG kontrolünü doğrular.
