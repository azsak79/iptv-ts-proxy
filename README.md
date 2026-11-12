# iptv-ts-proxy

**HLS → folyamatos MPEG-TS IPTV proxy** csatornakatalógussal, automatikus forrás-ellenőrzéssel,
lejátszás közbeni átváltással, szűrt EPG-vel és webes admin felülettel. Linuxra, systemd
szolgáltatásként, telepítő varázslóval.

> Csak olyan adást használj vele, amihez jogod van. A program nem tartalmaz csatornalistát és
> nem kerüli meg a szolgáltatói védelmet (DRM, titkosítás).

- **Telepítés lépésről lépésre:** [`docs/TELEPITES.md`](docs/TELEPITES.md)
- **Gyors telepítés:** `sudo ./install.sh` a kicsomagolt mappában (részletek lent)
- **Változások:** [`CHANGELOG.md`](CHANGELOG.md)
- **Hibajelentés, ötlet:** [Issues](https://github.com/azsak79/iptv-ts-proxy/issues). Írd meg a disztrót, a
  verziót (`install.sh status`) és a napló releváns sorait (`journalctl -u iptv-ts-proxy -n 50`).
  Szolgáltatói URL-t és jelszót ne tegyél be.

---

## Tartalom
1. [Mire jó](#1-mire-jó)
2. [Funkciók](#2-funkciók)
3. [Gyors telepítés](#3-gyors-telepítés)
4. [Hogyan működik](#4-hogyan-működik)
5. [Végpontok](#5-végpontok)
6. [Admin felület](#6-admin-felület)
7. [Beállítások](#7-beállítások)
8. [Katalógus](#8-katalógus)
9. [Forrás-ellenőrzés és átváltás](#9-forrás-ellenőrzés-és-átváltás)
10. [EPG és logók](#10-epg-és-logók)
11. [Értesítés (webhook)](#11-értesítés-webhook)
12. [JSON API](#12-json-api)
13. [Biztonság](#13-biztonság)
14. [Fájlok és mentés](#14-fájlok-és-mentés)
15. [Korlátok](#15-korlátok)
16. [Fejlesztés és teszt](#16-fejlesztés-és-teszt)

---

## 1. Mire jó
Sok IPTV-szolgáltató HLS-ben (m3u8 + rövid `.ts` szegmensek) ad, és **kapcsolatonként korlátozza a
letöltési sebességet**. A lejátszó egyetlen kapcsolaton tölt, ezért a stream akad, lassan indul,
a PVR-szoftverek (NextPVR, TVHeadend) pedig gyakran eldobják.

Az iptv-ts-proxy a szolgáltató és a lejátszó közé áll:
- a szegmenseket **párhuzamos Range-részekben** tölti, így a kapcsolatonkénti korlát nem számít;
- a lejátszó **egyetlen, folyamatos MPEG-TS** folyamot kap (`/ch/<id>.ts`), amit minden PVR és
  lejátszó „HTTP TS” forrásként kezel;
- csatornánként **egy upstream letöltés** megy, akárhány néző van;
- a csatornák forrásait karbantartja: méri, a legjobbat választja, kiesésnél átvált.

```
szolgáltató (HLS)  ──►  iptv-ts-proxy  ──►  /ch/<id>.ts     ──►  NextPVR / Jellyfin / Kodi / VLC / TVHeadend
                         │                  /playlist.m3u
                         │                  /epg.xml.gz
                         └─ admin: /admin (katalógus, ellenőrzés, statisztika, beállítások)
```

## 2. Funkciók
- **HLS → TS proxy:** on-demand indulás, több néző egy letöltésből (fan-out), gyűrűpuffer,
  párhuzamos szegmens- és Range-letöltés, master playlistnél a legjobb minőség.
- **Csatornakatalógus:** M3U-importtal indul, a webes felületen szerkeszthető. Csatornánként
  több forrás-URL, csoportok, csatornaszám, logó, engedélyezés, rögzített forrás. Export JSON-ba
  és M3U-ba, visszaállítható mentések. Stabil csatorna-URL: szerkesztéskor sem változik.
- **Forrás-ellenőrzés:** napi automatikus mérés (a szegmensletöltés sebessége a szegmenshosszhoz
  képest). A legjobb forrás kerül előre, hiszterézissel a pattogás ellen.
- **Élő átváltás (failover):** hiba vagy megakadás esetén lejátszás közben másik forrásra vált,
  a kliens kapcsolata nem szakad meg; több relay hoston slug-változatokkal keresi ugyanazt a
  csatornát, a folyam az új forrásról zökkenőmentesen folytatódik.
- **HDHomeRun-tuner (Plex):** a proxyt a Plex natívan HDHomeRun-tunerként találja meg (Élő TV +
  DVR, automatikus felderítés, HDHomeRun-EPG) – részletek a 11b. fejezetben.
- **Kimenet-simítás:** a letöltött adat real-time ütemezéssel engedődik ki, szegmensközi
  elhallgatásnál null-csomag tölti a folyamatot (a Plex DVR-segmentere így nem „idle” az adás).
- **EPG:** XMLTV forrás (`.xml` / `.xml.gz`), a katalógus csatornáira szűrve. „Most / következő”
  műsor az admin felületen.
- **Admin felület:** műszerfal (élő nézők, sebesség), katalógus-szerkesztő, ellenőrzés, események,
  nézettségi statisztika, beállítások, napló. Jelszóval védett.
- **Külső elérés:** titkos tokenes `/tv/<token>/…` útvonal reverse proxy vagy tunnel mögé.
- **Értesítés:** webhook (JSON POST) kiesésről, visszatérésről, átváltásról, pl. Home Assistantnak.
- **Telepítő varázsló:** függőségek telepítése, saját szolgáltatásfelhasználó, sandboxolt systemd
  unit, frissítés automatikus visszaállítással, eltávolítás.

## 3. Gyors telepítés
Követelmény: Linux **systemd**-del, **Python ≥ 3.9**, internet a telepítéskor. A hiányzó
csomagokat (`python3`, `python3-venv`, `ca-certificates`, `whiptail`) a telepítő felteszi.

```bash
V=2.2.0
curl -fL https://github.com/azsak79/iptv-ts-proxy/releases/download/v$V/iptv-ts-proxy-$V.tar.gz | tar xz
cd iptv-ts-proxy-$V
sudo ./install.sh
```
A varázsló után megnyitható az admin felület: `http://<gép-IP>:8095/admin`. A lejátszóba a
`http://<gép-IP>:8095/playlist.m3u` listát és az `http://<gép-IP>:8095/epg.xml.gz` EPG-t kell
megadni.

Részletes útmutató (kérdések nélküli telepítés, lejátszók, reverse proxy, frissítés, mentés,
hibaelhárítás): **[`docs/TELEPITES.md`](docs/TELEPITES.md)**.

Kezelés:
```bash
sudo /opt/iptv-ts-proxy/install.sh status|reconfigure|uninstall [--purge]
sudo ./install.sh update        # az új verzió mappájából
journalctl -u iptv-ts-proxy -f
```

## 4. Hogyan működik
**Lejátszás.** Ha egy kliens lekéri a `/ch/<id>.ts` címet, a proxy elindítja a csatorna
munkamenetét: a playlistet a szegmenshossz felénként olvassa, a szegmenseket `threads` (3)
párhuzamos feladat tölti. Mindegyik `range_chunk` (512 KB) méretű részekben megy, `range_conc` (8)
párhuzamos Range-kéréssel, és a régebbi szegmens részei elsőbbséget kapnak. A szegmenseket
sorrendben, folyamatosan adja ki. Indulás `start_back` (2) szegmenssel az élő adás mögött;
a memóriában `keep` (6) szegmens marad. Néző nélkül `idle` (20) mp után a csatorna leáll.

**Hibatűrés.** A 404-es szegmenst 3-szor próbálja újra. Ha a szolgáltató a részletek letöltése
közben eltünteti a szegmenst, a hiányzó részt egyetlen, Range nélküli kéréssel pótolja. A lemaradt
kliens a legfrissebb szegmensre ugrik. A klienseknek a proxy saját, folyamatos szegmens-sorszámot
ad, ezért egy forrásváltás nem töri meg a folyamot.

**Range nélküli szolgáltató.** Ha a szolgáltató nem támogatja a Range-kérést, a teljes szegmenst
tölti le, így a párhuzamosság szegmensszintű marad.

## 5. Végpontok
| Útvonal | Mit ad | Védelem |
|---|---|---|
| `/playlist.m3u`, `/playlist.m3u8` | M3U a proxy `/ch/<id>.ts` címeivel. Alapból csak az elérhető csatornák. `?all=1`: mind; `?group=Sport,Film`: csoportszűrés | nincs |
| `/epg.xml`, `/epg.xml.gz` | a katalógus csatornáira szűrt XMLTV | nincs |
| `/ch/<id>.ts` | egy csatorna élő adása (`video/MP2T`) | nincs |
| `/logo/<id>.png` | csatornalogó a logókönyvtárból | nincs |
| `/health` | `ok` (monitorozáshoz) | nincs |
| `/status` | JSON: csatornák száma, aktív adások, EPG kora | nincs |
| `/tv/<token>/playlist.m3u` | külső playlist (a stream-URL-ek is `/tv/<token>/ch/…`) | token |
| `/tv/<token>/epg.xml[.gz]`, `/tv/<token>/ch/<id>.ts`, `/tv/<token>/logo/<id>.png` | külső változatok | token |
| `/`, `/admin` | admin felület | jelszó |
| `/api/…` | JSON API (12. fejezet) | jelszó |

A playlist `#EXTINF` sorai: `tvg-id`, `tvg-name`, `tvg-logo`, `tvg-chno`, `group-title`, a fejlécben
`url-tvg` és `x-tvg-url` az EPG címével. Rossz vagy hiányzó tokennél a `/tv/` útvonal 404-et ad.

## 6. Admin felület
Cím: `http://<gép>:8095/admin`. Basic auth: a felhasználónév bármi lehet, a jelszó a
telepítéskor megadott vagy generált érték (`TSP_ADMIN_PASSWORD`).

| Fül | Tartalom |
|---|---|
| **Műszerfal** | aktív adások: nézők (IP, kliens), aktuális forrás, bejövő sebesség és 2 perces grafikon, letöltött MB, hibák. **Leállít** / **kirúg**; forrásállapot-összesítő, EPG kora |
| **Csatornák** | keresés, csoport- és állapotszűrő; állapot, aktív forrás, most futó műsor, nézők. **Szerkeszt** (adatok és forráslista: átrendezés, címke, tiltás, rögzítés, **Mérés most**), **Teszt** (8 mp-es próbaletöltés: első bájt, Mbps, TS-szinkron, megakadások), **VLC** (egycsatornás M3U), URL másolása; tömeges műveletek, **+ Új csatorna**, **Import M3U**, **Export**, **Mentések**, **Újraszámozás** |
| **Ellenőrzés** | **Teljes mérés most** / **Gyors ellenőrzés**, folyamatjelző, a következő automatikus futás, a problémás csatornák listája |
| **Események** | kiesés, visszatérés, forrásváltás, átváltás, ellenőrzés vége; típusszűrővel |
| **Statisztika** | 1/7/30/90 nap: nézési idő és forgalom csatornánként, kliens-IP-nként, naponta |
| **Beállítások** | az összes futásidejű beállítás típusos mezőkkel, „módosítva” jelölés, „alap” gomb |
| **Napló** | az utolsó 800 naplósor, szint szerinti szűrés, követés |

## 7. Beállítások
Két forrásból jönnek: az **env fájlból** (`/etc/iptv-ts-proxy/iptv-ts-proxy.env`, `TSP_<NÉV>` alakban)
és az admin felületen mentett **`settings.json`**-ból, ami felülírja az env fájlt. A futásidejű
beállítások az admin felületen azonnal érvényesek, újraindítás nem kell.

**Csak az env fájlban** (módosítás után `sudo systemctl restart iptv-ts-proxy`, vagy
`sudo /opt/iptv-ts-proxy/install.sh reconfigure`):

| Változó | Jelentés | Alap |
|---|---|---|
| `TSP_PORT` | HTTP port | `8095` |
| `TSP_LISTEN` | figyelési cím | `0.0.0.0` |
| `TSP_DATA` | adatkönyvtár | `/var/lib/iptv-ts-proxy` |
| `TSP_ADMIN_PASSWORD` | admin jelszó (üresen: nyitott admin, figyelmeztetéssel) | telepítéskor generált |
| `TSP_EXTERNAL_TOKEN` | külső elérés tokenje (üresen: a `/tv/` útvonal kikapcsolva) | – |
| `TSP_SOURCE` | induló M3U: csak akkor használja, ha még nincs katalógus | – |
| `TSP_CATALOG` | a katalógusfájl egyedi útvonala | `<data>/katalog.json` |

**Futásidejű** (admin → Beállítások; env-ben `TSP_<NÉV nagybetűvel>`, pl. `TSP_EPG_REFRESH`):

| Név | Alap | Jelentés |
|---|---|---|
| *Hálózat és külső elérés* | | |
| `external_base` | üres | külső URL-előtag token nélkül, pl. `https://tv.example.com/tv`; üresen a kérés címe + `/tv` |
| `external_streams` | be | külső playlist: az adás a proxyn át (be) vagy közvetlenül a szolgáltatótól (ki) |
| `public` | üres | a belső playlist URL-előtagja; üresen a kérés címe |
| `max_sessions` | 0 | egyszerre aktív csatornák korlátja (0 = nincs); felette 503 |
| *EPG és logók* | | |
| `epg_source` (`TSP_EPG`) | üres | XMLTV forrás URL |
| `legacy_epg_url` | üres | tartalék EPG-cím, ha az `epg_source` üres; az exportált M3U fejlécébe is ez kerül |
| `epg_refresh` | 3600 | EPG újraolvasás (mp) |
| `logos_dir` | üres | logókönyvtár (`<csatornanév>.png`) |
| *Letöltés és lejátszás* | | |
| `threads` | 3 | párhuzamos szegmensek csatornánként |
| `range_conc` | 8 | párhuzamos Range-kérések csatornánként |
| `range_chunk` | 524288 | Range-rész mérete bájtban (0 = kikapcsolva) |
| `start_back` | 2 | ennyi szegmenssel az élő adás mögött indul |
| `keep` | 6 | memóriában tartott szegmensek (≥ `start_back` + 1) |
| `idle` | 20 | néző nélkül ennyi mp után leáll a csatorna |
| *Katalógus és kimenet* | | |
| `playlist_only_alive` | be | a playlistben csak az elérhető csatornák |
| `ip_only` | ki | csak IP-címes forrás-URL használható (egyes szolgáltatóknál a domaines URL lassabb) |
| `write_legacy_files` | ki | régi pipeline-kompatibilis fájlok írása (csak `TSP_PIPELINE_DIR` mellett) |
| *Forrás-ellenőrzés és átváltás* | | |
| `slug_variants` / `slug_variants_max` | be / 6 | slug-változatok mérése minden relay hoston (a szolgáltató IP-nként eltérő slugot használhat) |
| `probe_reuse` / `probe_connect` | 12 óra / 4 mp | friss sikeres mérés újrahasznosítása / gyors kapcsolódási időkorlát |
| `failover_candidates` / `failover_workers` / `failover_fresh` | 24 / 4 / 600 mp | átváltási jelöltek száma / párhuzamossága / cache-frissessége |
| *HDHomeRun-tuner (Plex)* | | |
| `hdhr_enabled` / `hdhr_discovery` | be / be | HDHomeRun-emuláció (Plex Élő TV/DVR) / felderítés: bináris TLV + SSDP (UDP 65001, 1900) |
| `hdhr_port80` / `hdhr_tuners` / `hdhr_name` | ki / 16 / üres | port-80-as tükör (CAP_NET_BIND_SERVICE kell hozzá) / jelentett tunerek / megjelenő név |
| *Naplózás* | | |
| `log_enabled` | be | naplózás be/ki (journal és a Napló fül) |
| `log_level` | `INFO` | legalacsonyabb naplózott szint: DEBUG / INFO / WARNING / ERROR |
| `log_stream` / `log_health` / `log_access` | be | adások / ellenőrzés és események / HTTP hozzáférés |
| `log_hdhr` / `log_epg` / `log_admin` | be | Plex-felderítés / EPG / admin műveletek |
| `log_diag` | **ki** | 5 mp-enkénti adás-diagnosztika (csak hibakereséshez) |
| `history_keep` | 60 | katalógus-mentések száma |
| *Forrás-ellenőrzés* | | |
| `check_enabled` | be | napi automatikus ellenőrzés |
| `check_time` | `03:40` | az ellenőrzés időpontja (a kihagyottat 2 órán belül pótolja) |
| `check_workers` | 8 | párhuzamosan ellenőrzött csatornák |
| `host_conc` | 4 | egyidejű kérések egy szolgáltatói hostra |
| `host_gap` | 0.1 | szünet két kérés között ugyanarra a hostra (mp) |
| `probe_timeout` | 6 | kérés-időkorlát ellenőrzéskor (mp) |
| `seg_time_cap` | 6.0 | egy szegmens mérésének időkorlátja (mp) |
| `min_margin` | 1.2 | ez alatt nem stabil a forrás (szegmenshossz / letöltési idő) |
| `switch_factor` | 1.5 | ennyiszer jobbnak kell lennie az új forrásnak a váltáshoz |
| `parallel_probe` | be | gyenge forrásnál párhuzamos (Range) mérés is |
| `auto_candidates` | be | ugyanazon csatorna keresése a relay hostokon (`/live/<slug>/index.m3u8`) |
| `relays` | üres | relay hostok, soronként egy (`http://host`) |
| *Élő átváltás* | | |
| `failover_enabled` | be | átváltás lejátszás közben |
| `failover_errors` | 3 | ennyi egymás utáni hiba után vált |
| `failover_stall` | 45 | ennyi mp új szegmens nélkül → vált |
| `failover_cooldown` | 600 | két átváltás között legalább ennyi mp (csatornánként) |
| *Értesítés* | | |
| `webhook_url` | üres | webhook URL (JSON POST) |
| `webhook_events` | `dead,recovered,failover,failover_failed,switched` | értesítendő események |
| *Statisztika* | | |
| `stats_enabled` | be | nézettségi statisztika gyűjtése |
| `stats_days` | 90 | megőrzés (nap) |

## 8. Katalógus
Fájl: `/var/lib/iptv-ts-proxy/katalog.json`.
```json
{"updated": "…", "channels": [
  {"id": "m1.hu", "chno": "1", "name": "M1", "group": "Közszolgálati", "tvg_id": "m1.hu",
   "logo": "", "urls": ["http://…/index.m3u8", "http://…/index.m3u8"],
   "enabled": true, "pin": null, "url_meta": {"http://…": {"label": "tartalék", "disabled": false}}}
]}
```
- **`id`**: a csatorna-URL azonosítója (`/ch/<id>.ts`). A `tvg-id`-ből készül (ütközésnél `-2`
  utótaggal), és **szerkesztéskor sem változik**, így a PVR-ben tárolt csatornák megmaradnak.
- **`urls`**: a sorrend számít, az első engedélyezett az aktív forrás. Az ellenőrzés ezt a
  sorrendet frissíti.
- **`enabled: false`**: kikapcsolt csatorna (nincs playlistben, nincs adás).
- **`pin`**: rögzített forrás, ezt sem az ellenőrzés, sem az átváltás nem váltja le.
- **`url_meta`**: forrásonként címke és tiltás.

**Import** (admin → Csatornák → Import M3U, URL-ből vagy beillesztett szövegből): „csak új
csatornák” vagy „új csatornák + új forrás-URL-ek az ismertekhez” (összefésülés név / `tvg-id`
alapján). **Export:** JSON, illetve M3U (összes vagy csak élő). Minden módosítás előtt pillanatkép
készül (`catalog-history/`), ez a **Mentések** ablakból visszaállítható.

## 9. Forrás-ellenőrzés és átváltás
**Mérés.** Minden jelöltnél (a csatorna nem tiltott URL-jei, plusz a relay hostokon talált
párok) letölti a playlistet, master playlistnél a legjobb variánst, majd az utolsó teljes
szegmenst. A mérőszám a **margin = szegmenshossz / letöltési idő**. Ha ez gyenge, párhuzamos
Range-részekkel is megméri, mert a lejátszás is így tölt. Az effektív érték a kettő közül a
jobb.

| Állapot | Feltétel |
|---|---|
| `jo` | effektív margin ≥ 1,5 |
| `hatar` | ≥ `min_margin` |
| `instabil` | él, de gyenge vagy nem mérhető |
| `halott` | egyik jelölt playlistje sem érvényes |
| `ismeretlen` | még nem mérték |

**Kiválasztás.** A legjobb effektív margin kerül előre. Csak akkor vált, ha az új `switch_factor`-szor
jobb, vagy a mostani nem elég jó. Rögzített forrásnál nem vált.
**Módok:** `measure` (teljes mérés és kiválasztás) vagy `quick` (csak él-e). Automatikusan naponta
`check_time`-kor fut, kézzel az **Ellenőrzés** fülön vagy egy csatornára a szerkesztőből.
**Udvariasság:** hostonként legfeljebb `host_conc` egyidejű kérés, `host_gap` szünettel.

**Élő átváltás.** Ha egy nézett csatornán `failover_errors` egymás utáni hiba történik, vagy
`failover_stall` mp-ig nem jön új szegmens, a proxy a következő működő jelöltre vált. Ha nincs 12
óránál frissebb mérés, azonnal mér. A katalógus sorrendjét is frissíti, a futó adás pedig
megszakítás nélkül az új forrásról folytatódik.

## 10. EPG és logók
Az `epg_source` XMLTV-t `epg_refresh` másodpercenként olvassa (gzip is lehet), és **csak az
engedélyezett csatornák `tvg-id`-jére** szűri. A katalógus `tvg_id` mezőjének egyeznie kell az
XMLTV `<channel id="…">` értékével. A katalógus módosításakor az EPG magától újraszűrődik.

Logó: a katalógus `logo` URL-je, ha van. Ha nincs, a `logos_dir/<csatornanév>.png` fájl a
`/logo/<id>.png` címen. A logókönyvtár ne a `/home` alatt legyen, mert a szolgáltatás onnan nem
olvashat.

## 11. Értesítés (webhook)
Ha a `webhook_url` be van állítva, a `webhook_events` eseményeiről JSON POST megy:
```json
{"i": 42, "t": 1762531200.5, "type": "failover", "id": "m1.hu", "name": "M1", "detail": "…"}
```
Típusok: `dead`, `recovered`, `switched`, `failover`, `failover_failed`, `failover_blocked`,
`check_done`. Home Assistantban egy webhook-triggeres automatizmus fogadja
(`https://<ha>/api/webhook/<webhook_id>`).

## 11b. HDHomeRun-tuner (Plex)

A proxy **HDHomeRun-tunerként** is elérhető, így a Plex (Plex Pass) natívan, M3U-tuner
megkerülésével használja: Élő TV + DVR, automatikus felderítéssel.

| Végpont | Mi |
|---|---|
| `/discover.json` | eszközinformáció (DeviceID, TunerCount = `hdhr_tuners`) |
| `/lineup.json` | csatornák: `GuideNumber` = katalógus `chno`, stream = `/ch/<id>.ts` |
| `/hdhr/epg.xml(.gz)` | XMLTV guide: csatorna-id = csatornaszám |
| UDP 65001 / 1900 | bináris TLV-felderítés és SSDP (`hdhr_discovery`) |

Plexben: Beállítások → Élő TV és DVR → a tuner magától megtalálja, vagy kézzel
`<IP>:8095` (porttal!). Guide-nak a `/hdhr/epg.xml` adható meg.

**Tapasztalatok éles használatból:** az Élő TV lejátszásához a Plex **átkódolása be** kell
legyen kapcsolva; a minőség-beállítás kliensenkénti (gyenge TV-kliensen a Direct Play
kényszerítése a legjobb). A `Unable to find title for item of type 5` és az üres
`Unknown metadata type:` sorok a Plex naplójában ártalmatlan belső üzenetek.

## 12. JSON API
Minden végpont `/api/…` alatt van, Basic auth-tal. A POST-hoz `Content-Type: application/json`
kell (CSRF-védelem).

| Metódus és út | Mi |
|---|---|
| `GET status` | állapot, verzió, aktív adások |
| `GET catalog` | katalógus |
| `POST catalog/channel` | új csatorna |
| `POST catalog/channel/<id>/update`, `…/delete` | módosítás, törlés |
| `POST catalog/reorder`, `catalog/renumber`, `catalog/bulk` | sorrend, újraszámozás, tömeges művelet |
| `POST catalog/import` | M3U import (`{"url"…}` vagy `{"text"…}`, `"mode":"add_new"\|"merge"`) |
| `GET catalog/export?format=json\|m3u&scope=all\|alive` | export |
| `GET catalog/history`, `POST catalog/restore` | mentések |
| `GET health`, `GET health/channel/<id>` | mérési eredmények |
| `POST health/run` | ellenőrzés indítása: `{"mode":"quick\|measure","ids":[…]}` |
| `POST health/check/<id>` | egy csatorna mérése (szinkron) |
| `GET events`, `GET stats?days=N` | események, statisztika |
| `GET config`, `POST config` | beállítások (`{"set":{…}}`, `{"reset":[…]}`) |
| `POST reload` | `{"what":"epg\|catalog"}` |
| `POST channel/<id>/stop`, `POST channel/<id>/kick/<kliens>` | adás leállítása, néző kirúgása |
| `POST channel/<id>/probe`, `GET channel/<id>.m3u` | próbaletöltés, egycsatornás M3U |
| `GET logs?after=<n>` | napló |

Példa: teljes mérés parancssorból
```bash
curl -u x:JELSZÓ -H 'Content-Type: application/json' -d '{"mode":"measure"}' http://127.0.0.1:8095/api/health/run
```

## 13. Biztonság
- A lejátszói végpontok (playlist, EPG, stream) **hitelesítés nélküliek**, mert a lejátszók
  többsége nem tud hitelesíteni. Ezért a portot **ne nyisd ki az internetre**. Külső eléréshez a
  tokenes `/tv/` útvonalat használd, és csak azt engedd ki reverse proxyn vagy tunnelen.
- Az admin és az API jelszóval védett. Rossz jelszónál 0,5 mp a késleltetés, és a POST csak JSON
  lehet.
- A szolgáltatás saját, jogosultság nélküli felhasználóval fut (`iptv-ts-proxy`), sandboxolt
  systemd unitban: `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`, csak
  az adatkönyvtár írható, `MemoryMax=512M`.
- A titkok a `/etc/iptv-ts-proxy/iptv-ts-proxy.env` fájlban vannak (root, `600`).
- A `/status` végpont nyitott, és a nézett csatornák nevét mutatja (néző-IP nélkül).

## 14. Fájlok és mentés
| Útvonal | Tartalom |
|---|---|
| `/opt/iptv-ts-proxy/app/` | a program |
| `/opt/iptv-ts-proxy/venv/` | Python környezet (`aiohttp`) |
| `/opt/iptv-ts-proxy/install.sh` | a telepítő másolata (kezeléshez) |
| `/opt/iptv-ts-proxy/backup-<idő>/` | az előző programverziók (az utolsó 3) |
| `/etc/iptv-ts-proxy/iptv-ts-proxy.env` | beállítások és titkok |
| `/var/lib/iptv-ts-proxy/` | `katalog.json`, `catalog-history/`, `settings.json`, `health.json`, `events.jsonl`, `usage.jsonl`, `epg.xml.gz` |
| `/etc/systemd/system/iptv-ts-proxy.service` | systemd unit |
| `/var/log/iptv-ts-proxy-install.log` | a telepítő naplója |

**Mentéshez** elég a `/etc/iptv-ts-proxy/` és a `/var/lib/iptv-ts-proxy/` mappa. Lásd:
[`docs/TELEPITES.md` → Mentés és visszaállítás](docs/TELEPITES.md#9-mentés-és-visszaállítás).

## 15. Korlátok
- Csak **titkosítatlan, MPEG-TS szegmenses HLS** támogatott. AES-128, SAMPLE-AES, DRM és fMP4/CMAF nem.
- Nincs átkódolás: a stream bitre azonos a forrással.
- A felület magyar nyelvű.
- Egy példány egy gépen fut, nincs fürtözés.

## 16. Fejlesztés és teszt
```
app/            proxy.py (HLS→TS, végpontok, életciklus), catalog.py, health.py, admin.py, admin.html
install.sh      telepítő varázsló
tests/          e2e.sh (végponttól végpontig, root nélkül), fake_upstream.py (hamis HLS-szolgáltató),
                mock_relay.py + run_scenarios.py (8 hibaforgatókönyv: kiesés, lefagyás, IP-váltás),
                ts_check.py (TS-folytonosság), ffmpeg_check.py (valódi dekódolás)
docs/           TELEPITES.md
make-release.sh kiadási csomag: dist/iptv-ts-proxy-<verzió>.tar.gz + .sha256
```
```bash
tests/e2e.sh        # staging telepítés (--root), indítás, stream/EPG/auth/token, frissítés, eltávolítás
tests/run_scenarios.py   # 8 hibaforgatókönyv mock relayeken: TS-folytonosság, IP-váltás, failover
tests/ts_check.py <url> --mbps 3   # egy folyamon: szegmens-sorrend, lyukak, elakadások
tests/ffmpeg_check.py <url> --secs 45  # dekódolás: első kép, figyelmeztetések
./make-release.sh   # tiszta git munkakönyvtárból
```
Helyi futtatás telepítés nélkül:
```bash
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt
TSP_ADMIN_PASSWORD=valami TSP_SOURCE=https://…/lista.m3u .venv/bin/python -I app/proxy.py --port 8095 --data ./data
```
