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
yönetim panelinden yeniden işleme başlatılabilir veya aşağıdaki backfill komutu kullanılabilir.

## Mevcut chunk'lar için embedding backfill

Yerel servisler ve ayrı `embeddings` worker çalışırken:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_local'
.\.venv\Scripts\python.exe manage.py embed_pending_chunks --dry-run
.\.venv\Scripts\python.exe manage.py embed_pending_chunks --batch-size 100 --limit 500
```

- `--batch-size`: bir taramada okunacak doküman sayısı; varsayılan 100.
- `--limit`: bu çalıştırmada kuyruğa gönderilecek en fazla doküman sayısı;
  verilmezse başlangıçtaki en yüksek doküman kimliğine kadar taranır.
- `--dry-run`: aynı seçim ve profil kontrollerini yapar; veri değiştirmez,
  görev göndermez veya model yüklemez. `--limit` önizlemeye de uygulanır.

Yalnızca `ready` durumunda, `embedding IS NULL` chunk içeren dokümanlar seçilir.
Her doküman için mevcut embedding görevi `embeddings` kuyruğuna gönderilir.
Komutun başarılı bitmesi gönderimin tamamlandığını gösterir; vektörleri ayrı worker
üretir. Modelin chunk batch boyutu mevcut `AI_EMBEDDING_BATCH_SIZE` ayarıdır.
Worker ilerlemesi/hataları `.local/embeddings.err.log` ve dokümanın chunk
metadata'sından izlenebilir.

`Ctrl+C` yeni gönderimleri durdurur ve çıkış kodu 130 döner; önceden kuyruğa
gönderilmiş görevler çalışmaya devam eder. Aynı komutu tekrar çalıştırarak devam
edin. Kaydedilmiş vektörler ve zaman damgaları korunur; yarım kalan dokümanın
yalnızca boş chunk'ları hesaplanır. Worker bitmeden komutu yeniden çalıştırmak
aynı görevi tekrar kuyruğa koyabilir; worker'ın satır kilidi ve boş vektör kontrolü
aynı chunk'ın tekrar hesaplanmasını önler. Ayrı bir ilerleme dosyası gerekmez.

Broker/veritabanı hatası veya mevcut vektörlerde profil uyuşmazlığı komutu hatayla
durdurur; önceki gönderimler geri alınmaz. Uyuşmayan vektörler otomatik silinmez
veya dönüştürülmez. Yeni yüklenen dokümanlar normal pipeline ile işlenir;
tarama sırasında yeniden işlenen kayıtlar gerekirse sonraki komutla tekrar taranır.

## Dokümanlarda anlamsal arama (Adım 5)

`ai.tools.search_documents(user, query, limit=5)` salt-okuma aracıdır. En fazla
10 chunk döndürür; pozitif tam sayı olan daha büyük limitler 10'a indirilir.
Sorgu en fazla 2000 karakter olabilir; yerel servisin 512 token sınırı da geçerlidir.
Boş sorgu veya uygun embedding bulunmaması başarılı, boş sonuç verir.

Erişim mevcut özel doküman kuralına dayanır: aktif superuser, Buyer grubunda
olmayan staff veya FactoryOwner erişebilir. Buyer + staff / FactoryOwner
birleşimleri engellenir; superuser istisnası korunur. Anonim, pasif ve rolü
olmayan kullanıcılar engellenir. `uploaded_by` bir sahiplik/erişim alanı değildir;
yetkili kullanıcılar tüm hazır dokümanlarda arama yapabilir. Modelde doküman
bazında paylaşım alanı bulunmadığı için müşterilere doküman erişimi açılmaz.

SQL sorgusu sıralama ve limitten önce doküman durumunu (`ready`), dolu vektörü
ve model/boyut/profil uyumunu filtreler. Yerel `embed_query` servisi `query: `
önekini kendisi ekler; sorgu embedding'i kaydedilmez. Arama çağrısını yapan
süreç, uygun chunk varsa yerel CPU modelini yükler; Celery genel worker'ına
görev gönderilmez. pgvector cosine distance küçükten büyüğe sıralanır; sonuçlar
bir doğruluk garantisi veya ilgililik eşiği değil, en yakın chunk'lardır.

Yanıt alanları yalnızca `source_type=document`, `document_name`, `content`,
`page_number`, `section_title`, `cosine_distance` değerleridir. İç ID/UUID,
dosya yolu, yükleyen, hash, embedding vektörü, maliyet/fiyat alanları eklenmez.
`content` yetkili dokümanın metnidir; metin içindeki hassas bilgileri otomatik
sansürleyen bir mekanizma yoktur. Bilinmeyen sayfa/bölüm bilgisi uydurulmaz.

Geçersiz sorgu/limit `invalid_tool_arguments`, yetkisiz erişim `access_denied`,
model/profil/veritabanı hatası `tool_unavailable` ile boş veri döndürür. Uyumsuz
profildeki kayıtlar aramaya katılmaz ve hiçbir vektör otomatik değiştirilmez.

## Asistanda doküman RAG akışı (Adım 6)

`search_documents` yedinci function-calling aracıdır. Şema yalnızca doküman
arama yetkisi olan kullanıcılara sunulur; çağrı yürütülürken de yetki kontrol
edilir. Mevcut altı ERP aracının veri sözleşmeleri ve rol kuralları korunur.
Doküman araması da mevcut üç tur sınırına ve tekrar çağrı engeline tabidir;
baştaki/sondaki boşluklar ve varsayılan `limit=5` tekrar kontrolünden önce
normalleştirilir. Model veya API değiştirilmemiştir. Araç sonuçları mevcut
[OpenAI function-calling akışı](https://developers.openai.com/api/docs/guides/function-calling)
ile ilgili `tool_call_id` üzerinden modele geri verilir.

Her istek içinde dönen chunk'lara `[D1]`, `[D2]` gibi geçici kaynak etiketleri
atanır; bunlar veritabanı ID'leri değildir. Model, kullandığı doküman bilgisini
bu etiketle işaretler. Uygulama yalnızca atıf yapılan sonuçların gerçek doküman
adı ve varsa sayfa/bölüm bilgisini yanıtın sonuna ekler. Eksik konum bilgisi
uydurulmaz. Aynı chunk farklı aramalarda tekrar gelirse etiketi korunur.
Etiketler ve kaynak eşlemesi yalnızca o isteğin belleğinde tutulur; sohbet
geçmişi veya kalıcı hafıza oluşturulmaz.

Araç sonuçları tamamlandıktan sonra modele kullanılabilir doküman ve ERP
etiketleri sistem mesajıyla hatırlatılır. Bu mesaj yalnızca uygulamanın ürettiği
etiketleri içerir; doküman metni, başlığı veya bölüm bilgisi sistem talimatına
dönüştürülmez. Hatırlatma ek model turu açmaz ve kaynak doğrulamasını gevşetmez.

Örnek yanıt biçimi:

```text
Doküman bilgisi: Uygunsuz ürün karantinaya alınır. [D1]
ERP canlı verisi: Depoda 12 adet bulunmaktadır. [ERP:get_stock_by_product]

Doküman kaynakları:
- [D1] Kalite talimatı — sayfa 4 — bölüm: Taban kontrolü
ERP canlı veri kaynakları:
- [ERP:get_stock_by_product] get_stock_by_product
```

Doküman sonuçları geldiği halde atıf yoksa, kaynak etiketi uydurulmuşsa veya
birlikte kullanılan ERP sonucuna atıf yapılmamışsa doğrulanamayan yanıt yerine
kısa hata mesajı gösterilir. Ek model çağrısıyla üç tur sınırı aşılmaz. Boş ya da
başarısız aramadan kaynak listesi üretilmez. Doküman metni ve metadata'sındaki
talimatlar sistem yetkisi taşımaz; modele bunların yalnızca kanıt olarak
kullanılacağı belirtilir. Etiket doğrulaması, her cümlenin kaynak metinden
mantıksal olarak çıktığını otomatik ispatlayan bir denetim değildir.

Embedding üretimi ve vektör araması yerel kalır. Yanıt üretmek için seçilen
doküman parçaları, mevcut ERP araç çıktıları gibi yapılandırılmış OpenAI
modeline gönderilir. Sohbet ekranı kaynakları mevcut düz metin gösterimiyle
sunar; HTML çalıştırılmaz.
