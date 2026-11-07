# Változások

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
- Exportált M3U: EPG-cím nélkül nincs üres `x-tvg-url` a fejlécben.
