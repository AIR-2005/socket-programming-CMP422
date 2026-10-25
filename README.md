# CMP422 Socket Programming

Python ile TCP üzerinden çalışan bir P2P messaging ve file exchange uygulaması.
Central server yalnızca online peer kayıtlarını ve userlist dağıtımını yönetir.
Message ve file içeriği doğrudan peer'lar arasında taşınır.

## Gereksinimler

- Python 3.10 veya üzeri.
- GUI için Tkinter. Kontrol: `python3 -c "import tkinter; print(tkinter.TkVersion)"`.
- Testte kullanılacak bilgisayarlar arasında TCP erişimi. Aynı Wi-Fi/hotspot kullanılabilir.

Üçüncü taraf Python paketi gerekmez. Proje dizininde çalıştırın.

## Çalıştırma

Server bilgisayarında:

```bash
python3 server 5000
```

Aynı bilgisayarda iki client ile denemek için ayrı terminallerde:

```bash
python3 client 127.0.0.1 5000 Baris
```

```bash
python3 client 127.0.0.1 5000 Ayse
```

Başka bir bilgisayardan bağlanırken `127.0.0.1` yerine server bilgisayarının local IPv4
address'ini yazın. Linux'ta adresleri `hostname -I` ile görebilirsiniz.
Örneğin server'ın local IP address'i `192.168.1.10` ise:

```bash
python3 client 192.168.1.10 5000 Ayse
```

Her client farklı username kullanmalıdır. Parametre sırası:
`python3 client <server-ip-address> <server-port-number> <user-name>`.
`client.py` ve `server.py` dosyaları da doğrudan çalıştırılabilir.

Server bütün local IPv4 interface'lerinde bağlantı bekler. Server port'unu CLI'dan alır;
client'lar her girişte kullanılabilir yüksek port'lardan rastgele bir P2P listening port seçer.
Bilgisayarın firewall'u ve hotspot ayarları cihazlar arası TCP bağlantılarına izin vermelidir.

## GUI ve commands

Online users listesinden bir peer seçin. Output alanı seçilen peer'ın conversation'ını
gösterir. Başka bir peer'dan gelen message kendi history'sine eklenir ve user list'te unread
sayısı görünür. History uygulama belleğinde tutulur; client kapatılınca silinir.

Commands alanına her satırda bir command yazıp Execute düğmesine basın:

```text
sndmsg Merhaba, nasılsın?
sndf "/home/kullanici/belgeler/ornek.pdf"
sndmsg File transfer devam ederken message da gönderebilirim.
```

`sndf` için kendi bilgisayarınızdaki gerçek full file path'i kullanın. Boşluk içeren paths
desteklenir; çift, tek veya tipografik çift tırnak kullanılabilir. Command'lar önce doğrulanır.
Message'lar kendi gönderim sıralarını korur; file transfer ayrı worker'da başlar ve sonrasındaki
message'ları bekletmez.

Alıcı, filename ve size içeren bir onay penceresi görür. Kabul edilene kadar file içeriği
gönderilmez. Kabul edilen files, **alıcı client'ın current working directory'sindeki**
`download/` dizinine kaydedilir. Aynı filename varsa `(1)`, `(2)` gibi ekler kullanılır.
Yarım kalan transfer'ın oluşturduğu eksik file temizlenir. Disconnect socket'leri kapatır.

## Logs

PDF'in örnek formatlarını kullanan loglar:

- `server.log`: `IP PORT connected` veya `IP PORT disconnected`.
- `client-USERNAME.log`: başarılı message ve file transfer kayıtları.

Server log'undaki port, peer'ın bildirdiği **P2P listening port** olarak yorumlanmıştır.
PDF, bu alanı source port'tan açıkça ayırmaz.

Diğer işlemler ve hatalar strict log formatını değiştirmeyen ayrı dosyalara yazılır:

- `server-detailed.log`: username, userlist dağıtımları, registration ve connection hataları.
- `client-USERNAME-detailed.log`: bağlantı/kayıt, userlist, commands, teklifler, kabul/ret,
  başarılı işlemler, hatalar ve kapanış. Bu kayıtlarda timestamp ve log level de bulunur.

Log filename'inde path ayırıcıları gibi karakterler `_` ile değiştirilir. Logs, `download/`,
Python cache ve local ayarlar `.gitignore` ile Git dışında tutulur.

## Kodun yapısı

| Dosya | Sorumluluk |
|---|---|
| `client.py` | GUI, conversation history, commands ve server connection |
| `server.py` | Peer kayıtları ve client başına worker/queue ile userlist dağıtımı |
| `messaging.py` | P2P message/file connection'ları ve client logging |
| `file_transfer.py` | Onay kararı, chunks, download oluşturma ve eksik file temizliği |
| `protocol.py` | UTF-8 JSON packets ve 4-byte length prefix |
| `client`, `server` | CLI entrypoint'leri |

JSON control packets en fazla 1 MiB olabilir. Bu sınır file size sınırı değildir:
file içeriği JSON'a dönüştürülmeden, en fazla 64 KiB'lık chunks halinde taşınır.
Onay bekleme süresinde deadline yoktur; aktif file transfer'da her socket işlemi için
30 saniyelik timeout vardır. Client'lar IPv4 kullanır.

## Testler

```bash
python3 -B -m unittest discover -s tests -v
```

Testler standart `unittest` kullanır; ek paket veya açık bir GUI gerekmez. Local socket
erişimi gerekir. Geçici dizinlerde ve otomatik seçilen port'larda kendi server/client'larını
açar; mevcut `5000` port'undaki server'a ve çalışma dizininizdeki file'lara dokunmaz.

Kapsam: parçalı TCP verisi ve UTF-8, commands, conversation/unread ayrımı, registration,
yeniden giriş, okumayan bir client varken userlist dağıtımı, message sırası, file kabul/ret,
transfer sırasında iki yönlü messaging, binary bütünlüğü, boş/tekrarlı/yarım files ve loglar.
GUI onay callback'i mock ile kontrol edilir; pencerenin görsel davranışı manuel denenmelidir.

Manuel GUI kontrolü:

1. Üç farklı username ile client açın; online listelerinin güncellendiğini kontrol edin.
2. Bir peer seçiliyken diğerinden message gönderin. Conversation değişmemeli, unread artmalıdır.
3. Diğer peer'ı seçin; history görünmeli ve unread sıfırlanmalıdır.
4. File teklifini önce reddedin, sonra tekrar gönderip kabul edin.
5. Büyük bir file aktarılırken iki yönde `sndmsg` çalıştırın.
6. Bir client'ı kapatın; diğerlerinin online listesinde kaldırıldığını kontrol edin.
