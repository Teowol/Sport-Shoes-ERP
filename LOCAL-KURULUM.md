# Yerel çalıştırma

Uygulama: http://127.0.0.1:8000/

Yönetici hesabının kullanıcı adı ve parolası `.local/admin-login.txt` dosyasındadır.
Bulut verileri aktarılmadı; yeni yerel veritabanı oluşturuldu.

Proje klasöründe PowerShell ile başlatın:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\local.ps1 start
```

Durdurmak veya durumunu görmek için son parametreyi `stop` veya `status` yapın.
Kod değişikliklerinden sonra `stop` ve `start` çalıştırın.

## Kurulan bileşenler

- Python 3.12 sanal ortamı: `.venv`. Uygulama ve geliştirme bağımlılıkları
  `requirements.txt` dosyasından kurulur.
- PostgreSQL 16.15: `127.0.0.1:55432`. Veriler `.local/pgdata` içinde saklanır.
- pgvector 0.8.6: native PostgreSQL 16.15 için derlenen `vector` uzantısı.
  Python/Django entegrasyonu `pgvector==0.5.0` paketini kullanır.
- Redis uyumlu Memurai Developer 4.1.2: `127.0.0.1:56379`.
  Veriler `.local/redis-data` içindedir.
- Django geliştirme sunucusu, genel Celery worker, ayrı embeddings worker ve Celery beat.
  İki worker da Windows'ta `solo` havuzu ve `concurrency=1` ile çalışır.
- Projede sabitlenmiş multilingual-e5-small modeli `.local/ai-models` içine
  indirildi ve kodda tanımlı SHA-256 değerleriyle doğrulandı.
- PDF üretimi için DejaVu fontları ve lisansı `static/fonts` içine eklendi.

Docker/WSL bu çalıştırma yöntemi için gerekli değildir. Memurai Developer yerel
geliştirme/test içindir ve 10 günlük kesintisiz çalışmadan sonra yeniden
başlatılmalıdır. Kaynak: https://www.memurai.com/get-memurai

## Ayarlar ve günlükler

Yerel ayarlar `.env` ve `config/settings_local.py` dosyalarındadır.
E-postalar yerelde uygulama günlüklerine yazılır. Yapay zekâ sohbeti için
`.env` dosyasındaki `OPENAI_API_KEY` alanına kendi anahtarınızı ekleyip
uygulamayı yeniden başlatın. Yerel belge gömme modeli anahtar gerektirmez.

Servis günlükleri `.local/*.log` dosyalarındadır. `.env`, `.venv` ve `.local`
Git dışında tutulur. `.local` klasörü veritabanını ve giriş bilgilerini içerir;
verileri korumak için silmeyin.

Django komutlarını elle çalıştırmak için:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_local'
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe manage.py check
```

## pgvector kurulumu ve doğrulama

`ai.0003_enable_vector` migration'ı veritabanında `vector` uzantısını etkinleştirir.
Migration'dan önce PostgreSQL kurulumunda `lib/vector.dll`,
`share/extension/vector.control` ve uzantının SQL dosyaları bulunmalıdır.
Yalnızca Python paketini kurmak PostgreSQL uzantısını kurmaz.

Bu makinede pgvector 0.8.6 resmî kaynak kodundan, Visual Studio 2022 x64 C++
araçları ve PostgreSQL 16.15 arşivindeki aynı sürüme ait başlık/kütüphanelerle
derlendi. Derleme sırasında `PGROOT`, projenin `.local/pgsql` klasörüdür.
Yeni kurulumlarda [pgvector Windows yönergelerini](https://github.com/pgvector/pgvector#windows)
izleyin; mevcut veritabanını değiştirmeden önce yedek alın ve geri yüklemeyi doğrulayın.

Yerel ayarlarla migration ve uzantı testleri:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_local'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py test ai.test_pgvector --noinput
```

Adım 1 yedeği ve veri karşılaştırma raporları Git dışında `.local/backups` altında
tutulur. Yedekler ve `globals.sql` hassas veriler içerir; paylaşmayın veya Git'e eklemeyin.
GitHub Actions test veritabanı da uzantıyı içeren `pgvector/pgvector:0.8.6-pg16`
image'ını kullanır. Bu migration dokümanlara henüz embedding alanı eklemez.

## Doküman embedding işleme

`ai.0004_documentchunk_embeddings`, chunk başına 384 boyutlu vektör ile model,
boyut, profil SHA-256 ve oluşturulma zamanı alanlarını ekler. Vektör ve metadata
birlikte yazılır; embed edilmemiş chunk'ların bu alanları boştur.

Doküman yükleme → `celery` kuyruğunda metin çıkarma/chunking → veritabanı commit'i
→ `embeddings` kuyruğunda yerel embedding üretimi → PostgreSQL'e batch kaydı.
`passage: ` önekini mevcut yerel servis ekler. Genel worker model yüklemez veya
embedding hesaplamaz; model ayrı worker'da ilk görevde yüklenir.

- Genel worker: `erp-local@...`, yalnızca `celery` kuyruğu.
- Embeddings worker: `erp-embeddings@...`, yalnızca `embeddings` kuyruğu;
  `--pool=solo --concurrency=1 --prefetch-multiplier=1`.
- Embedding logları: `.local/embeddings.out.log` ve `.local/embeddings.err.log`.
- `local.ps1 start`, `stop` ve `status` iki worker'ı da yönetir.

Profil hash'i model/revision, beklenen model dosyası hash'leri, boyut, prefix,
pencereleme, normalizasyon ve embedding çalışma zamanı paket sürümlerini içerir.
Profil oluşturmak model dosyalarını yüklemez. Worker, kuyruğa gönderilmiş profilin,
yüklü model profilinin ve mevcut vektör metadata'sının uyumunu doğrular; uyumsuz
vektörleri otomatik olarak değiştirmez. Algoritma değişirse profildeki
`algorithm_version` artırılmalıdır.

Batch'ler ayrı transaction'larda kaydedilir. Tekrar teslim edilen görevler tamamlanmış
chunk'ları atlar. Doküman satır kilidi eşzamanlı işlemleri sıraya alır; chunk grubu
kimliği, yeniden işlenen dokümana eski görevlerin yazmasını önler. Geçici hatalar
en fazla iki kez yeniden denenir; profil/model/içerik doğrulama hataları denenmez.

`Document.status=ready` metin çıkarma ve chunking'in tamamlandığını belirtir.
Embedding'in tamamlanması ayrıca chunk'ların vektör ve metadata alanlarından
kontrol edilir. Kuyruk veya embedding hataları yönetim panelindeki işleme hatasına
`Embedding:` önekiyle yazılır. Broker'a gönderim başarısızsa chunk'lar korunur;
yönetim panelinden yeniden işleme başlatılabilir. Backfill komutu ayrı Adım 4'tür.
