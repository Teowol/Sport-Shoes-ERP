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
- Redis uyumlu Memurai Developer 4.1.2: `127.0.0.1:56379`.
  Veriler `.local/redis-data` içindedir.
- Django geliştirme sunucusu, Windows için `solo` havuzuyla Celery worker ve Celery beat.
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
