# iptv-ts-proxy

HLS (m3u8) IPTV-adásokból **folyamatos MPEG-TS** folyamot készít, amit bármely lejátszó vagy
PVR (NextPVR, Jellyfin, Kodi, VLC, TVHeadend, Plex-tuner bridge-ek) egyszerű „HTTP TS” forrásként
kezel. Mellé ad csatornakatalógust, forrás-ellenőrzést automatikus legjobbforrás-választással,
lejátszás közbeni átváltást (failover), szűrt XMLTV EPG-t és webes admin felületet.

**Miért jó:** sok szolgáltató kapcsolatonként korlátozza a letöltési sebességet. A proxy a
szegmenseket párhuzamos Range-részekben tölti, így akadás nélkül megy a stream ott is, ahol a
lejátszó közvetlenül akadna. Csatornánként egyetlen upstream letöltés megy, akárhány néző van.

> Csak olyan adást használj vele, amihez jogod van. A program nem tartalmaz csatornalistát.

## Követelmények
- Linux **systemd**-del (Debian 11+, Ubuntu 22.04+, Fedora, RHEL/Alma/Rocky 9+, openSUSE, Arch)
- **Python ≥ 3.9** `venv` modullal (Debianon/Ubuntun: `python3-venv`; a telepítő felajánlja)
- internet a telepítéskor (`pip install aiohttp`)
- root jog a telepítéshez (a szolgáltatás saját, jogosultság nélküli felhasználóval fut)

## Telepítés
```bash
tar xzf iptv-ts-proxy-2.1.0.tar.gz
cd iptv-ts-proxy-2.1.0
sudo ./install.sh
```
A varázsló sorban megkérdezi: **port** (alap 8095), elérhető legyen-e a **helyi hálózatról**,
**admin jelszó** (üresen hagyva generál egyet), **induló M3U lista** URL-je, **EPG (XMLTV)** URL,
**logókönyvtár**, és kell-e **külső, tokenes elérés**. A végén elindítja a szolgáltatást,
ellenőrzi, hogy válaszol-e, és kiírja a címeket.

Grafikus (whiptail) felület akkor jelenik meg, ha a `whiptail` telepítve van; különben sima
szöveges kérdéseket kapsz. A szöveges mód kikényszeríthető: `IPTV_UI=plain sudo -E ./install.sh`.

### Telepítés kérdések nélkül (automatizáláshoz)
```bash
sudo TSP_PORT=8095 TSP_LISTEN=0.0.0.0 \
     TSP_SOURCE='https://szolgaltato.example/lista.m3u' \
     TSP_EPG='https://szolgaltato.example/epg.xml.gz' \
     IPTV_EXTERNAL=1 \
     ./install.sh --unattended
```
| Változó | Jelentés | Alap |
|---|---|---|
| `TSP_PORT` | HTTP port | `8095` |
| `TSP_LISTEN` | figyelési cím (`0.0.0.0` = LAN, `127.0.0.1` = csak helyi) | `0.0.0.0` |
| `TSP_ADMIN_PASSWORD` | admin jelszó (min. 8 karakter) | generált |
| `TSP_SOURCE` | induló M3U lista (csak akkor használja, ha még nincs katalógus) | – |
| `TSP_EPG` | XMLTV forrás (`.xml` vagy `.xml.gz`) | – |
| `TSP_LOGOS_DIR` | logókönyvtár (`<csatornanév>.png`), abszolút útvonal, nem `/home` alatt | – |
| `IPTV_EXTERNAL=1` | külső elérés: titkos token generálása | ki |
| `TSP_EXTERNAL_BASE` | külső URL-előtag, pl. `https://tv.example.com/tv` | a kérés címe + `/tv` |
| `TSP_PUBLIC` | belső playlist URL-előtagja | a kérés címe |

Az értékekben nem lehet szóköz és `' " \ $ `` ` karakter (ezek az env fájlban bizonytalanul viselkednek).

## Kezelés
```bash
sudo /opt/iptv-ts-proxy/install.sh status        # verzió, port, systemd állapot
sudo /opt/iptv-ts-proxy/install.sh reconfigure   # a varázsló újra, a meglévő értékekkel
sudo ./install.sh update                          # az ÚJ csomag mappájából: frissítés
sudo /opt/iptv-ts-proxy/install.sh uninstall     # eltávolítás, a beállítások és adatok maradnak
sudo /opt/iptv-ts-proxy/install.sh uninstall --purge   # mindent töröl (katalógus is!)
journalctl -u iptv-ts-proxy -f                    # napló
```
- **Frissítés:** a beállítások és a katalógus megmaradnak. Az előző programverzió a
  `/opt/iptv-ts-proxy/backup-<idő>/` mappába kerül (az utolsó 3 marad). Ha az új verzió nem
  indul el 30 mp alatt, a telepítő automatikusan visszaállítja az előzőt.
- Az `install.sh` újrafuttatása egy már telepített gépen frissítésként viselkedik, nem kérdez újra.

## Címek
| Cím | Mi | Védelem |
|---|---|---|
| `http://<gép>:8095/admin` | admin felület (katalógus, ellenőrzés, statisztika, beállítások, napló) | jelszó (Basic auth, felhasználónév bármi) |
| `http://<gép>:8095/playlist.m3u` | M3U lista az élő csatornákkal (`?all=1`: mind, `?group=Sport,Film`: szűrés) | nincs |
| `http://<gép>:8095/epg.xml.gz` | a katalógusra szűrt EPG | nincs |
| `http://<gép>:8095/ch/<id>.ts` | egy csatorna élő adása | nincs |
| `http://<gép>:8095/tv/<token>/playlist.m3u` | külső változat (ha be van kapcsolva) | titkos token |

A lejátszói végpontok szándékosan jelszó nélküliek, mert a legtöbb lejátszó nem tud
hitelesíteni. **Ne nyisd ki a portot az internetre** (port forward). Külső eléréshez a tokenes
`/tv/` útvonalat használd reverse proxy vagy tunnel mögött.

## Lejátszók beállítása
- **VLC:** Média → Hálózati adatfolyam megnyitása → `http://<gép>:8095/playlist.m3u`
- **Kodi:** PVR IPTV Simple Client → M3U: `…/playlist.m3u`, EPG: `…/epg.xml.gz`
- **Jellyfin:** Vezérlőpult → Élő TV → Tuner: M3U Tuner `…/playlist.m3u`; TV-műsorújság: XMLTV `…/epg.xml.gz`
- **NextPVR:** Settings → Devices → Add IPTV → M3U `…/playlist.m3u`, EPG: XMLTV `…/epg.xml.gz`
- **TVHeadend:** IPTV Automatic Network → URL `…/playlist.m3u`; EPG: XMLTV grabber az `…/epg.xml.gz`-re

A csatorna-URL (`/ch/<id>.ts`) állandó: a katalógus szerkesztésekor sem változik, így a PVR-ben
tárolt csatornák nem vesznek el.

## Külső elérés (reverse proxy)
Csak a `/tv/` útvonalat engedd ki, az admin felületet és az `/api/`-t soha. nginx példa:
```nginx
location /tv/ {
    proxy_pass http://127.0.0.1:8095;
    proxy_buffering off;            # élő stream: ne pufferelje
    proxy_read_timeout 1h;
    proxy_set_header Host $host;
}
```
Utána állítsd be a külső előtagot (`TSP_EXTERNAL_BASE=https://tv.example.com/tv`, vagy az
admin felület **Beállítások** fülén az `external_base` értéket). Cloudflare Tunnelnél ugyanígy
csak a `/tv/*` útvonalat publikáld; a valós néző-IP a `CF-Connecting-IP` fejlécből jön.

## Hogyan működik
- **On-demand:** egy csatornát csak akkor tölt, ha nézik; 20 mp néző nélkül leáll.
  Több néző esetén is egyetlen letöltés megy, gyűrűpufferből szolgálja ki őket.
- **Katalógus:** az induló M3U-ból jön létre, utána az admin felületen szerkeszthető (források,
  sorrend, csoportok, import/export, visszaállítható mentések).
- **Forrás-ellenőrzés:** naponta (alap 03:40) megméri a forrásokat, és a legjobbat teszi előre.
  Lejátszás közben hiba vagy megakadás esetén másik működő forrásra vált, a kliens kapcsolata
  nem szakad meg.
- **Webhook:** kiesésről, visszatérésről, átváltásról JSON POST-ot küld (pl. Home Assistantnak).

A finomhangolás (párhuzamosság, puffer, ellenőrzés időpontja, failover küszöbök, relay hostok,
webhook) az admin felület **Beállítások** fülén van. Az ott mentett értékek felülírják az env
fájl értékeit.

## Fájlok
| Útvonal | Tartalom |
|---|---|
| `/opt/iptv-ts-proxy/` | program (`app/`), Python venv, `install.sh`, mentések |
| `/etc/iptv-ts-proxy/iptv-ts-proxy.env` | beállítások és titkok (root, 600) |
| `/var/lib/iptv-ts-proxy/` | katalógus, mentések, mérések, események, statisztika, `settings.json` |
| `/etc/systemd/system/iptv-ts-proxy.service` | systemd unit (sandboxolt) |
| `/var/log/iptv-ts-proxy-install.log` | a telepítő naplója |

Mentéshez elég a `/etc/iptv-ts-proxy/` és a `/var/lib/iptv-ts-proxy/` mappa.

## Hibaelhárítás
| Tünet | Teendő |
|---|---|
| `hiányzó függőség: venv` | Debian/Ubuntu: `sudo apt install python3-venv` |
| `pip install nem sikerült` | internet/DNS ellenőrzése; HTTP proxy mögött: `sudo -E env HTTPS_PROXY=… ./install.sh` |
| a szolgáltatás nem indul | `journalctl -u iptv-ts-proxy -n 50`; foglalt port: `sudo ./install.sh reconfigure` |
| üres playlist | nincs katalógus: admin → Csatornák → Import M3U; vagy a csatornák még „halottak”: `?all=1` |
| a playlist megvan, a lejátszás nem indul | admin → Csatornák → **Teszt**; csak titkosítatlan, TS-szegmenses HLS támogatott |
| nincs EPG | admin → Beállítások → `epg_source`; a csatornák `tvg-id`-jének egyeznie kell az XMLTV-vel |
| nem látszanak a logók | a logókönyvtár ne a `/home` alatt legyen (a unit `ProtectHome=true`) |
| a LAN-ról nem érhető el | `TSP_LISTEN=0.0.0.0` és tűzfal (a telepítő kiírja az ufw/firewalld parancsot, de nem futtatja) |

## Korlátok
- Csak titkosítatlan, MPEG-TS szegmenses HLS támogatott (AES-128 és fMP4/CMAF nem).
- A felület magyar nyelvű.

## Fejlesztés, teszt
```bash
tests/e2e.sh          # root és systemd nélkül: staging telepítés + élő teszt egy hamis HLS-szolgáltatóval
./make-release.sh     # dist/iptv-ts-proxy-<verzió>.tar.gz + .sha256
```
