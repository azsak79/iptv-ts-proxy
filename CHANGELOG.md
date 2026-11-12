# Változások

## 2.2.0 – 2026-11-12
Az üzemi 2.10.1-es verzió javításainak és új funkcióinak átvitele. A disztro-specifikus
alapértékek változatlanok (üres külső URL/EPG/relayek, `ip_only` és `write_legacy_files` ki,
`hdhr_port80` ki).

- **Javítva a folyam sérülése induláskor és forrásváltáskor:** a kezdőpont előtti szegmensek
  visszafelé kerültek a folyamba (élő méréssel bizonyított: `timestamp discontinuity`,
  `DTS out of order`, `Packet corrupt` — a Plex tuner-streamje ettől szakadt meg).
  Monoton media-sequence őr; a sequence újraindulását is kezeli.
- **Zökkenőmentes átváltás:** azonos számozású forráson a folyam ott folytatódik, ahol az
  előző abbamaradt (nincs ismétlés/lyuk); a régi forrás folyamatban lévő letöltései
  megszakadnak.
- **Gyorsabb halott-forrás észlelés:** nem válaszoló playlistet idő alapján ismeri fel, a
  halott hostot kihagyja a jelöltek közül, az első jó, azonos slugú jelöltet azonnal elfogadja
  (lefagyott host: 43,5 → 29,4 mp). Teljesen halott forrásnál a siker-cooldown helyett a
  `failover_retry` érvényes (a második kiesés így nem „fekete lyuk”).
- **Pacer javítások:** a kliens nem ragad szegmensen (lezárási versenyhiba); a pufferből kiesett
  szegmenst nem ugorja át; a kimeneti bitráta becslése az EXTINF-hosszból megy.
- **Naplózás beállítható:** `log_enabled`, `log_level` (DEBUG–ERROR), kategóriánként
  (`log_stream/health/access/hdhr/epg/admin/diag`, a `diag` alapból ki); a Napló fülön szint-
  és kategóriaszűrő.
- **Új funkciók az üzemből:** HDHomeRun-tuner emuláció (`hdhr.py`: Plex Élő TV/DVR, bináris
  TLV + SSDP felderítés, HDHR-EPG), kimenet-simítás (pacer, null-csomag kitöltés), slug-változat
  keresés minden relay hoston, `upstream_ua`, EPG-feldolgozás külön szálon (a többi adás
  nem áll meg az EPG-elemzés alatt), hozzáférési napló javítások.
- **Tesztek:** mock-relay alapú forgatókönyv-teszt (8 hibaforgatókönyv), TS-folytonosság-
  ellenőrző és ffmpeg-dekódoló a `tests/`-ben.

## 2.1.0 – 2026-11-07
Az első általánosan telepíthető kiadás. A saját üzemi 2.0-s verzióból készült, a lejátszási, katalógus-
és ellenőrzési logika változatlan.

- Új: `install.sh` telepítő varázsló (whiptail / szöveges / `--unattended`), `update` automatikus
  visszaállítással, `reconfigure`, `status`, `uninstall [--purge]`, `--root` staging mód.
- Új: dedikált `iptv-ts-proxy` szolgáltatásfelhasználó, Python venv, sandboxolt systemd unit, titkok `600`-as env fájlban.
- Az alapértékekből kikerültek a telepítésfüggő értékek: külső URL, EPG-cím és relay hostok üresek;
  `ip_only` és `write_legacy_files` alapból ki.
- Üres `external_base` esetén a külső playlist a kérés címéből (`…/tv`) épül.
- EPG-forrás nélkül nincs óránkénti hibaüzenet.
- Az elérhetetlen induló M3U nem dönti le az indulást (üres katalógussal indul, a következő indításkor újrapróbálja).
- A telepítő a CA-tárat is ellenőrzi, és hiány esetén telepíti a `ca-certificates` csomagot.
- Interaktív telepítésnél a hiányzó `whiptail` is települ (sikertelen telepítésnél szöveges mód).
- Exportált M3U: EPG-cím nélkül nincs üres `x-tvg-url` a fejlécben.
