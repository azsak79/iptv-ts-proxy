# Telepítési útmutató – iptv-ts-proxy

Lépésről lépésre egy friss Linux géptől a működő lejátszásig. A program leírása:
[`README.md`](../README.md).

## Tartalom
1. [Követelmények](#1-követelmények)
2. [Letöltés](#2-letöltés)
3. [Telepítés a varázslóval](#3-telepítés-a-varázslóval)
4. [Telepítés kérdések nélkül](#4-telepítés-kérdések-nélkül)
5. [Ellenőrzés és első lépések](#5-ellenőrzés-és-első-lépések)
6. [Lejátszók és PVR-ek beállítása](#6-lejátszók-és-pvr-ek-beállítása)
7. [Külső elérés (reverse proxy, tunnel)](#7-külső-elérés-reverse-proxy-tunnel)
8. [Frissítés, újrakonfigurálás, eltávolítás](#8-frissítés-újrakonfigurálás-eltávolítás)
9. [Mentés és visszaállítás](#9-mentés-és-visszaállítás)
10. [Hibaelhárítás](#10-hibaelhárítás)
11. [Gyakori kérdések](#11-gyakori-kérdések)

---

## 1. Követelmények
| Mi | Követelmény |
|---|---|
| Operációs rendszer | Linux **systemd**-del: Debian 11+, Ubuntu 22.04+, Fedora, RHEL / AlmaLinux / Rocky 9+, openSUSE, Arch |
| Python | **3.9 vagy újabb**, `venv` modullal (a telepítő felteszi) |
| Hardver | 1 CPU mag, kb. 100–150 MB RAM (a unit felső korlátja 512 MB), néhány MB lemez a katalógusnak és a statisztikának |
| Hálózat | internet a telepítéskor (`pip install aiohttp`), a szolgáltató elérése, egy szabad TCP port (alap 8095) |
| Jogosultság | `root` vagy `sudo` a telepítéshez; a szolgáltatás utána jogosultság nélkül fut |

Konténerben (LXC, Proxmox CT) is működik, ha a konténerben systemd fut. Docker-konténer nem a
célplatform.

**Amit a telepítő magától felrak, ha hiányzik** (jóváhagyás után; `--unattended` esetén kérdés nélkül):

| Csomagkezelő | Kötelező | A grafikus varázslóhoz |
|---|---|---|
| apt (Debian, Ubuntu) | `python3 python3-venv ca-certificates` | `whiptail` |
| dnf / yum (Fedora, RHEL, Alma, Rocky) | `python3 ca-certificates` | `newt` |
| zypper (openSUSE) | `python3 ca-certificates` | `newt` |
| pacman (Arch) | `python ca-certificates` | `libnewt` |

Az `aiohttp` a program saját környezetébe kerül (`/opt/iptv-ts-proxy/venv`), a rendszer Pythonjához
nem nyúl. Ha a rendszer Pythonja régebbi 3.9-nél (pl. RHEL 8: 3.6), előbb telepíts újabbat
(`sudo dnf install python3.11`); a telepítő megtalálja a `python3.9` … `python3.13` parancsokat.

## 2. Letöltés
A tároló privát, ezért a letöltéshez a GitHub-fióknak hozzáférése kell legyen.

**A) git clone** (SSH-kulccsal vagy tokennel):
```bash
sudo apt install -y git          # ha nincs (Fedora: dnf install git)
git clone git@github.com:azsak79/iptv-ts-proxy.git
cd iptv-ts-proxy
```
Egy adott verzió: `git checkout v2.1.0`.

**B) kiadási csomag** (Releases → `iptv-ts-proxy-<verzió>.tar.gz`). Privát tárolónál böngészőből
töltsd le, vagy a GitHub CLI-vel:
```bash
gh release download v2.1.0 -R azsak79/iptv-ts-proxy -p 'iptv-ts-proxy-*.tar.gz*'
sha256sum -c iptv-ts-proxy-2.1.0.tar.gz.sha256
tar xzf iptv-ts-proxy-2.1.0.tar.gz
cd iptv-ts-proxy-2.1.0
```

## 3. Telepítés a varázslóval
```bash
sudo ./install.sh
```
A varázsló grafikus (whiptail) ablakokban kérdez; ha a `whiptail` hiányzik, előbb felteszi. Ha ez
nem sikerül, vagy kifejezetten szöveges módot akarsz, használd ezt:
`sudo IPTV_UI=plain ./install.sh`. Bármelyik kérdésnél a **Cancel** / `Ctrl+C` megszakítja a
telepítést. Ilyenkor a program, a beállítások és a szolgáltatás nem jön létre (a már felrakott whiptail marad).

A lépések:

| # | Kérdés | Mit adj meg |
|---|---|---|
| 1 | **HTTP port** | alap `8095`. A telepítő ellenőrzi, hogy szabad-e |
| 2 | **Elérhető legyen a helyi hálózatról?** | **Igen** (`0.0.0.0`), ha a lejátszók más gépen vannak (TV, Kodi, NextPVR). **Nem** (`127.0.0.1`), ha csak ezen a gépen vagy egy reverse proxy mögött használod |
| 3 | **Admin jelszó** | legalább 8 karakter, szóköz és `' " \ $ `` ` nélkül. **Üresen hagyva** erős jelszót generál, és a végén kiírja |
| 4 | **Induló csatornalista (M3U URL)** | a szolgáltató M3U listájának címe; ebből jön létre a katalógus. Üresen is hagyható: később az admin felületen importálható. Csak első telepítéskor kérdezi |
| 5 | **EPG (XMLTV) URL** | `.xml` vagy `.xml.gz` műsorújság címe; üresen nincs EPG |
| 6 | **Logókönyvtár** | abszolút útvonal `<csatornanév>.png` fájlokkal, pl. `/srv/logos`. **Ne a `/home` alatt legyen.** Üresen nincs |
| 7 | **Külső elérés?** | **Igen**: titkos tokent generál a `/tv/<token>/…` útvonalhoz (7. fejezet). Ekkor megkérdezi a külső URL-előtagot is (pl. `https://tv.example.com/tv`; üresen a kérés címéből) |
| 8 | **Mehet a telepítés?** | összefoglaló, utána indul |

Mit csinál ezután:
1. felteszi a hiányzó csomagokat;
2. bemásolja a programot a `/opt/iptv-ts-proxy/` mappába, és venv-et készít `aiohttp`-vel;
3. létrehozza az `iptv-ts-proxy` rendszerfelhasználót és a `/var/lib/iptv-ts-proxy/` adatkönyvtárat;
4. megírja a `/etc/iptv-ts-proxy/iptv-ts-proxy.env` fájlt (`600`) és a systemd unitot;
5. bekapcsolja és elindítja a szolgáltatást, majd 30 mp-ig várja, hogy a `/health` válaszoljon;
6. kiírja a címeket, generált jelszó esetén a jelszót is, és ha aktív ufw vagy firewalld tűzfalat
   talál, a port megnyitásához szükséges parancsot. Ezt a parancsot nem futtatja le.

Példa a záró kimenetre:
```
iptv-ts-proxy 2.1.0 telepítve
  Admin felület:  http://192.168.1.50:8095/admin   (felhasználónév: bármi)
  Admin jelszó:   Xy3…   (generált; később: sudo grep PASSWORD /etc/iptv-ts-proxy/iptv-ts-proxy.env)
  Playlist (M3U): http://192.168.1.50:8095/playlist.m3u
  EPG (XMLTV):    http://192.168.1.50:8095/epg.xml.gz
  Egy csatorna:   http://192.168.1.50:8095/ch/<id>.ts
  Napló:          journalctl -u iptv-ts-proxy -f
  Kezelés:        sudo /opt/iptv-ts-proxy/install.sh {status|update|reconfigure|uninstall}
```
A telepítő naplója: `/var/log/iptv-ts-proxy-install.log`.

## 4. Telepítés kérdések nélkül
Automatizáláshoz (Ansible, cloud-init, szkript). A beállításokat környezeti változókban add át:
```bash
sudo TSP_PORT=8095 \
     TSP_LISTEN=0.0.0.0 \
     TSP_ADMIN_PASSWORD='ErosJelszo2026' \
     TSP_SOURCE='https://szolgaltato.example/lista.m3u' \
     TSP_EPG='https://szolgaltato.example/epg.xml.gz' \
     TSP_LOGOS_DIR=/srv/logos \
     IPTV_EXTERNAL=1 \
     TSP_EXTERNAL_BASE='https://tv.example.com/tv' \
     ./install.sh --unattended
```
| Változó | Jelentés | Ha nincs megadva |
|---|---|---|
| `TSP_PORT` | port | `8095` |
| `TSP_LISTEN` | figyelési cím | `0.0.0.0` |
| `TSP_ADMIN_PASSWORD` | admin jelszó | generált (a kimenetben látszik) |
| `TSP_SOURCE` | induló M3U | üres katalógus |
| `TSP_EPG` | XMLTV forrás | nincs EPG |
| `TSP_LOGOS_DIR` | logókönyvtár | nincs |
| `IPTV_EXTERNAL=1` | külső elérés (token generálása) | ki |
| `TSP_EXTERNAL_BASE` | külső URL-előtag | a kérés címéből |
| `TSP_PUBLIC` | belső playlist URL-előtagja | a kérés címéből |

További kapcsolók:

| Kapcsoló | Hatás |
|---|---|
| `--unattended` | nincs kérdés, minden jóváhagyás automatikus |
| `--yes` | interaktív mód, de a jóváhagyó kérdések automatikusan „igen” |
| `--no-start` | telepít, de nem indítja el (indítás: `systemctl enable --now iptv-ts-proxy`) |
| `--no-deps` | nem telepít csomagot; ha valami hiányzik, hibával áll meg |
| `--root DIR` | staging telepítés egy könyvtárba, root, systemd és felhasználó nélkül (csomagoláshoz, teszthez) |

A `--unattended` mód kimenetében a generált jelszó **olvasható formában** szerepel. CI-naplóban
add meg előre a `TSP_ADMIN_PASSWORD`-öt.

## 5. Ellenőrzés és első lépések
```bash
sudo /opt/iptv-ts-proxy/install.sh status
curl -s http://127.0.0.1:8095/health                   # ok
curl -s http://127.0.0.1:8095/playlist.m3u | head       # #EXTM3U … /ch/<id>.ts
journalctl -u iptv-ts-proxy -n 30 --no-pager
```
Utána az admin felületen (`http://<gép>:8095/admin`):
1. **Csatornák:** ellenőrizd az importált listát. Ha üres: **Import M3U** (URL vagy beillesztett
   szöveg).
2. **Ellenőrzés → Teljes mérés most:** megméri a forrásokat, és beállítja az élő/halott állapotot.
   A playlistben alapból csak az élő csatornák szerepelnek, a mérés előtt a `?all=1` mutat mindent.
3. Egy csatornánál a **Teszt** gomb 8 mp-es próbaletöltést végez. A **VLC** gomb egycsatornás
   listát ad, ezzel azonnal kipróbálhatod a lejátszást.
4. **Beállítások:** EPG-forrás, ellenőrzési időpont, webhook stb.

## 6. Lejátszók és PVR-ek beállítása
Mindegyiknél két cím kell (a `<gép>` a proxy IP-je vagy neve):
- **M3U:** `http://<gép>:8095/playlist.m3u`
- **EPG:** `http://<gép>:8095/epg.xml.gz`

Csoportszűrés: `…/playlist.m3u?group=Sport,Film`. Az összes csatorna, a halottakkal együtt:
`…/playlist.m3u?all=1`.

| Lejátszó | Beállítás |
|---|---|
| **VLC** | Média → Hálózati adatfolyam megnyitása → M3U cím |
| **Kodi** | Kiegészítők → PVR IPTV Simple Client → Beállítás: M3U lejátszási lista útvonala = M3U cím; EPG beállítások → XMLTV útvonal = EPG cím |
| **Jellyfin** | Vezérlőpult → Élő TV → Tunereszközök: **M3U Tuner**, fájl vagy URL = M3U cím; TV-műsorújság-szolgáltatók: **XMLTV**, EPG cím |
| **NextPVR** | Settings → Devices → **IPTV** hozzáadása → M3U cím; EPG: XMLTV, EPG cím; utána Scan / Auto Map |
| **TVHeadend** | Configuration → DVB Inputs → Networks → **IPTV Automatic Network**, URL = M3U cím; EPG: XMLTV grabber az EPG címre |
| **Smart TV / telefon** (pl. TiviMate, IPTVnator, OTT Navigator) | Playlist URL = M3U cím, EPG URL = EPG cím |

A csatorna-URL (`/ch/<id>.ts`) a katalógus szerkesztésekor sem változik, így a PVR-ben
hozzárendelt csatornák megmaradnak. Új csatorna felvétele után a PVR-ben újra kell olvastatni a
listát.

## 7. Külső elérés (reverse proxy, tunnel)
A lejátszói végpontok jelszó nélküliek, ezért **a portot ne nyisd ki közvetlenül az internetre**.
Külső eléréshez:
1. a varázslóban kapcsold be a **külső elérést** (vagy `IPTV_EXTERNAL=1` és `reconfigure`), így
   létrejön a token;
2. a reverse proxyn **csak a `/tv/` útvonalat** engedd ki, az `/admin`-t és az `/api/`-t ne;
3. állítsd be a külső előtagot: `TSP_EXTERNAL_BASE`, vagy admin → Beállítások → `external_base`.

A külső playlist címe: `https://tv.example.com/tv/<token>/playlist.m3u`. A token kiolvasása:
```bash
sudo grep TOKEN /etc/iptv-ts-proxy/iptv-ts-proxy.env
```

**nginx:**
```nginx
server {
    listen 443 ssl;
    server_name tv.example.com;
    # ssl_certificate …; ssl_certificate_key …;

    location /tv/ {
        proxy_pass http://127.0.0.1:8095;
        proxy_http_version 1.1;
        proxy_buffering off;          # élő stream: ne pufferelje
        proxy_read_timeout 1h;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
    location / { return 404; }
}
```
**Caddy:**
```
tv.example.com {
    handle /tv/* {
        reverse_proxy 127.0.0.1:8095 {
            flush_interval -1
        }
    }
    respond 404
}
```
**Cloudflare Tunnel:** a public hostname szolgáltatása `http://localhost:8095`. Egy WAF-szabállyal
blokkolj mindent, ami nem `/tv/` kezdetű. A valódi néző-IP-t a proxy a `CF-Connecting-IP`
fejlécből veszi.

A **Beállítások → `external_streams`** kapcsolóval eldöntheted, hogy a külső playlist streamjei
a proxyn át menjenek (be, alap), vagy közvetlenül a szolgáltatóhoz (ki, a proxy feltöltését
kíméli).

## 8. Frissítés, újrakonfigurálás, eltávolítás
**Frissítés** egy újabb verzióra (az új verzió mappájából):
```bash
cd iptv-ts-proxy && git pull            # vagy töltsd le és csomagold ki az új kiadást
sudo ./install.sh update
```
- A beállítások (`/etc/iptv-ts-proxy/`) és az adatok (`/var/lib/iptv-ts-proxy/`) megmaradnak.
- Az előző programverzió a `/opt/iptv-ts-proxy/backup-<idő>/` mappába kerül (az utolsó 3 marad).
- Ha az új verzió 30 mp alatt nem indul el, a telepítő **automatikusan visszaállítja** az előzőt.
- A frissítés újraindítja a szolgáltatást, a futó adások pár másodpercre megszakadnak.
- A `sudo ./install.sh` egy már telepített gépen is frissítésként fut, nem kérdez újra.

**Újrakonfigurálás** (port, jelszó, EPG, külső elérés stb.):
```bash
sudo /opt/iptv-ts-proxy/install.sh reconfigure
```
A varázsló a jelenlegi értékekkel indul. Az env fájl előző változata `.bak` néven megmarad. Az
env fájlba kézzel felvett egyéb `TSP_*` sorokat megtartja.
Kézi szerkesztés is lehetséges: `sudoedit /etc/iptv-ts-proxy/iptv-ts-proxy.env`, utána
`sudo systemctl restart iptv-ts-proxy`.

**Eltávolítás:**
```bash
sudo /opt/iptv-ts-proxy/install.sh uninstall           # program és unit törlése; beállítások, adatok maradnak
sudo /opt/iptv-ts-proxy/install.sh uninstall --purge   # minden törlése, a katalógussal és a felhasználóval együtt
```
A telepítő által felrakott rendszercsomagokat (Python, whiptail stb.) az eltávolítás nem törli.

## 9. Mentés és visszaállítás
**Mentés:**
```bash
sudo tar czf iptv-ts-proxy-mentes-$(date +%F).tar.gz /etc/iptv-ts-proxy /var/lib/iptv-ts-proxy
```
A mentés titkokat tartalmaz (admin jelszó, token), ezért biztonságos helyen tárold.

**Visszaállítás** (új gépen is):
```bash
sudo ./install.sh --unattended --no-start      # telepítés
sudo systemctl stop iptv-ts-proxy 2>/dev/null || true
sudo tar xzf iptv-ts-proxy-mentes-….tar.gz -C /
sudo chown -R iptv-ts-proxy:iptv-ts-proxy /var/lib/iptv-ts-proxy
sudo systemctl enable --now iptv-ts-proxy
```
Csak a katalógus visszaállítása egy korábbi állapotra: admin → Csatornák → **Mentések**.
Hordozható export: **Export → JSON**, ezt egy másik példányon az admin felületről vagy a
`katalog.json` cseréjével lehet betölteni.

## 10. Hibaelhárítás
Első lépés mindig a napló: `journalctl -u iptv-ts-proxy -n 50 --no-pager`. Telepítési hibánál:
`sudo tail -50 /var/log/iptv-ts-proxy-install.log`.

| Tünet | Ok és teendő |
|---|---|
| `root jog kell` | `sudo ./install.sh` |
| `systemd szükséges` | a rendszer nem systemd-s (pl. Alpine OpenRC, Docker); ez nem támogatott |
| `hiányzó függőség: venv` / `python` | `--no-deps` mellett nem telepít; futtasd nélküle, vagy: `apt install python3 python3-venv` |
| `Python >= 3.9 nem található` | régi disztró: telepíts újabb Pythont (`dnf install python3.11`), majd futtasd újra |
| `pip install nem sikerült` | nincs internet vagy DNS. HTTP proxy mögött: `sudo env HTTPS_PROXY=http://proxy:3128 ./install.sh` |
| `a CA-tár még mindig üres` | `sudo update-ca-certificates` (RHEL: `update-ca-trust`) |
| `A(z) 8095 port foglalt` | válassz másik portot, vagy nézd meg, mi használja: `sudo ss -ltnp 'sport = :8095'` |
| `a szolgáltatás 30 mp alatt nem válaszolt` | a kiírt naplósorok mutatják az okot. Frissítésnél a telepítő visszaállította az előző verziót |
| az admin felület nem érhető el más gépről | a figyelési cím `127.0.0.1` → `reconfigure`. Vagy tűzfal: `sudo ufw allow 8095/tcp`, `sudo firewall-cmd --permanent --add-port=8095/tcp && sudo firewall-cmd --reload` |
| elfelejtett admin jelszó | `sudo grep PASSWORD /etc/iptv-ts-proxy/iptv-ts-proxy.env`, vagy új jelszó: `reconfigure` |
| üres a playlist | nincs katalógus (Import M3U), vagy még nem volt mérés, és minden „ismeretlen/halott”: `?all=1`, Ellenőrzés → Teljes mérés |
| `katalógus: az induló M3U nem tölthető le` | rossz vagy elérhetetlen M3U URL; a proxy üres katalógussal fut. Javítsd (`reconfigure`), vagy importálj az admin felületről |
| a lejátszás nem indul, a Teszt hibát ad | a forrás halott, titkosított (AES/DRM) vagy fMP4. Csak titkosítatlan TS-szegmenses HLS megy |
| akad a kép | Beállítások: `start_back` 3, `keep` 8; nézd meg az Ellenőrzés fülön a forrás marginját; próbálj másik forrást |
| nincs EPG / üres a műsor | `epg_source` beállítva? A katalógus `tvg_id`-je egyezik az XMLTV `channel id`-jével? Napló: `epg: N csatorna, M műsor` |
| nem jelennek meg a logók | a logókönyvtár a `/home` alatt van (a sandbox nem engedi), vagy a fájlnév nem a csatorna neve + `.png` |
| a külső playlist 404 | rossz token, vagy a külső elérés nincs bekapcsolva (`TSP_EXTERNAL_TOKEN` üres) |
| a külső playlistben belső IP-k vannak | állítsd be az `external_base`-t a nyilvános címre |
| 503 új csatorna indításakor | elérted a `max_sessions` korlátot |

## 11. Gyakori kérdések
**Mennyi sávszélesség kell?** Csatornánként annyi, amennyi a stream bitrátája (HD jellemzően
4–8 Mbps), függetlenül a nézők számától. Kifelé a nézők számával arányos.

**Hány csatornát bír egyszerre?** Nincs belső korlát (lásd `max_sessions`). A szűk keresztmetszet
jellemzően a szolgáltatói sávszélesség, nem a CPU. Átkódolás nincs, a proxy csak bájtokat
mozgat.

**Átkódol vagy felvesz?** Nem. Felvételhez a PVR (NextPVR, TVHeadend, Jellyfin) használja a proxy
streamjét.

**Ugyanazon a gépen futhat a PVR-rel?** Igen, ez a tipikus elrendezés. Ekkor elég a `127.0.0.1`
figyelési cím, ha más eszköz nem éri el közvetlenül.

**Hol változtatok a finomhangoláson?** Admin → Beállítások. A változás azonnal érvényes, és a
`settings.json`-ba mentődik, ami felülírja az env fájl értékét.
