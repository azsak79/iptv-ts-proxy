#!/usr/bin/env python3
"""IPTV HLS -> folyamatos MPEG-TS proxy, katalógus-kezeléssel, forrás-ellenőrzéssel és EPG-vel.

Csatornánként egy /ch/<id>.ts végpont: az upstream HLS szegmenseit párhuzamosan (Range-részekben)
tölti le, sorrendben egyetlen TS folyamként adja ki. On-demand: csak akkor húz az upstreamből,
ha van néző. A csatornák katalógusa (katalog.json), a források ellenőrzése és az élő átváltás
ugyanebben a folyamatban él; az admin felület a /admin címen érhető el.
"""
import argparse
import asyncio
import calendar
import copy
import datetime
import gzip
import hashlib
import heapq
import hmac
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin

import aiohttp
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # -I módban nincs a path-on
from catalog import Catalog, CatalogError, extinf as cat_extinf, is_ip_url, parse_m3u as cat_parse_m3u  # noqa: E402
from health import Health  # noqa: E402
import hdhr  # noqa: E402

VERSION = "2.3.0"
log = logging.getLogger("tsproxy")
slog = logging.getLogger("tsproxy.stream")     # adás: indulás/leállás, forrásváltás, szegmens- és playlist-hibák
dlog = logging.getLogger("tsproxy.diag")       # 5 mp-enkénti részletes adás-diagnosztika
elog = logging.getLogger("tsproxy.epg")
UA = "VLC/3.0.20 LibVLC/3.0.20"
DEFAULT_RELAYS = ""

# naplókategóriák: logger-név utótag -> leírás (a beállításokban log_<kategória> kapcsolja)
LOG_CATS = {
    "stream": "Adások: indulás, leállás, forrásváltás, kliens-ugrás",
    "health": "Forrás-ellenőrzés és események (kiesés, visszatérés, átváltás)",
    "access": "HTTP hozzáférés-napló (az /api és /admin kérések nélkül)",
    "hdhr": "HDHomeRun / SSDP felderítés (Plex)",
    "epg": "EPG betöltés és frissítés",
    "admin": "Admin műveletek (szerkesztés, beállítás, import)",
    "diag": "Részletes adás-diagnosztika 5 mp-enként (pacer, kliens-bájtok) – csak hibakereséshez",
}
LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


class RingHandler(logging.Handler):
    """Az utolsó N naplósor memóriában, az admin felület naplófüléhez."""

    def __init__(self, n=800):
        super().__init__()
        self.buf = deque(maxlen=n)
        self.n = 0

    def emit(self, rec):
        self.n += 1
        cat = rec.name[8:] if rec.name.startswith("tsproxy.") else ("core" if rec.name == "tsproxy" else rec.name)
        self.buf.append({"i": self.n, "t": rec.created, "lvl": rec.levelname, "cat": cat, "msg": rec.getMessage()})


RING = RingHandler()


def apply_logging(cfg):
    """A naplózási beállítások élő alkalmazása a loggerek szintjén (a kikapcsolt kategória
    üzenetei meg sem formázódnak). Kikapcsolt kategóriában a WARNING és ERROR továbbra is
    megjelenik – kivéve, ha a naplózás teljesen ki van kapcsolva (log_enabled = ki)."""
    lvl = LOG_LEVELS.get(cfg.log_level, logging.INFO) if cfg.log_enabled else logging.CRITICAL + 10
    logging.getLogger().setLevel(lvl)
    log.setLevel(lvl)
    for cat in LOG_CATS:
        on = getattr(cfg, "log_" + cat, True)
        logging.getLogger("tsproxy." + cat).setLevel(lvl if on else max(lvl, logging.WARNING))


def xmltv_ts(s):
    """XMLTV időbélyeg ('20261107120000 +0100') -> unix idő."""
    s = (s or "").strip()
    dt = datetime.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    off = 0
    if len(s) >= 20 and s[15] in "+-":
        off = (1 if s[15] == "+" else -1) * (int(s[16:18]) * 3600 + int(s[18:20]) * 60)
    return calendar.timegm(dt.timetuple()) - off


# ---------------------------------------------------------------- beállítások
def _spec():
    S = {}

    def add(name, typ, default, group, help, lo=None, hi=None, env=None, choices=None):
        S[name] = {"type": typ, "default": default, "group": group, "help": help, "min": lo, "max": hi,
                   "env": env or ("TSP_" + name.upper()), "choices": choices}

    g = "Hálózat és külső elérés"
    add("external_base", "str", "", g, "Külső URL-előtag a /tv/ végpontokhoz (token nélkül, pl. https://tv.example.com/tv; üresen: a kérés címe + /tv)")
    add("external_streams", "bool", True, g, "Külső playlist: az adások a proxyn át (be) vagy közvetlenül a szolgáltatóra (ki)")
    add("public", "str", "", g, "Belső playlist URL-előtagja (üresen: a kérés címe)")
    add("max_sessions", "int", 0, g, "Egyszerre aktív csatornák maximuma (0 = nincs korlát)", 0, 200)
    g = "EPG és logók"
    add("epg_source", "str", "", g, "XMLTV forrás URL (üresen: a lenti alapértelmezett)", env="TSP_EPG")
    add("legacy_epg_url", "str", "", g, "Alapértelmezett EPG-cím (az exportált M3U-k fejlécében is ez szerepel; üresen nincs)")
    add("epg_refresh", "int", 3600, g, "EPG újraolvasása (mp)", 60, 86400)
    add("logos_dir", "str", "", g, "Csatornalogók könyvtára (<csatornanév>.png)")
    g = "Letöltés és lejátszás"
    add("threads", "int", 3, g, "Párhuzamos szegmensek csatornánként", 1, 8)
    add("upstream_ua", "str", "Mozilla/5.0", g, "A szolgáltató felé használt User-Agent (egyes relayek a VLC UA-ra nem válaszolnak)")
    add("range_conc", "int", 8, g, "Párhuzamos Range-kérések csatornánként", 1, 32)
    add("range_chunk", "int", 524288, g, "Range-rész mérete bájtban (0 = kikapcsolva)", 0, 4194304)
    add("start_back", "int", 2, g, "Ennyi szegmenssel az élő adás mögött indul", 1, 6)
    add("keep", "int", 6, g, "Memóriában tartott szegmensek száma", 3, 20)
    add("idle", "int", 20, g, "Néző nélkül ennyi mp után leáll a csatorna", 5, 600)
    g = "Katalógus és kimenet"
    add("playlist_only_alive", "bool", True, g, "A playlist csak az elérhető (nem halott) csatornákat tartalmazza")
    add("ip_only", "bool", False, g, "Csak IP-alapú forrás-URL használható (domain-alapú nem kerül a listákba)")
    add("write_legacy_files", "bool", False, g, "A régi fájlok (kimenet/*.m3u, status.json, meres.json) írása")
    add("history_keep", "int", 60, g, "Katalógus-mentések (visszaállítási pontok) száma", 5, 500)
    g = "Forrás-ellenőrzés"
    add("check_enabled", "bool", True, g, "Napi automatikus forrás-ellenőrzés")
    add("check_time", "time", "03:40", g, "Az automatikus ellenőrzés időpontja (HH:MM)")
    add("check_workers", "int", 8, g, "Párhuzamosan ellenőrzött csatornák", 1, 32)
    add("host_conc", "int", 4, g, "Egyszerre legfeljebb ennyi kérés egy szolgáltatói hostra", 1, 16)
    add("host_gap", "float", 0.1, g, "Szünet két kérés között ugyanarra a hostra (mp)", 0, 5)
    add("probe_timeout", "int", 6, g, "Egy kérés időkorlátja ellenőrzéskor (mp)", 2, 30)
    add("seg_time_cap", "float", 6.0, g, "Egy szegmens mérésének legfeljebb ennyi ideje (mp)", 2, 20)
    add("min_margin", "float", 1.2, g, "Ez alatt „nem stabil” a forrás (szegmenshossz / letöltési idő)", 0.5, 5)
    add("switch_factor", "float", 1.5, g, "Ennyiszer jobbnak kell lennie az újnak a forrásváltáshoz", 1, 5)
    add("parallel_probe", "bool", True, g, "Gyenge forrásnál párhuzamos (Range) mérés is: így mér a proxy valójában")
    add("auto_candidates", "bool", True, g, "Ugyanazon csatorna keresése a többi relay hoston is (/live/<slug>/<index|playlist>.m3u8)")
    add("slug_variants", "bool", True, g, "Slug-változatok mérése: a csatorna minden lehetséges slugját megpróbálja minden relay hoston (a provider IP-nként eltérő slugot használhat)")
    add("slug_variants_max", "int", 6, g, "Csatornánként legfeljebb ennyi slug-változat", 1, 20)
    add("probe_reuse", "int", 12, g, "Friss, sikeres mérés újrahasznosítása ennyi óráig az ellenőrzésben (0 = mindig újramérés)", 0, 168)
    add("probe_connect", "int", 4, g, "Kapcsolódási időkorlát ellenőrzéskor (mp) – az elérhetetlen host gyorsan bukjon meg", 1, 15)
    add("relays", "text", DEFAULT_RELAYS, g, "Relay hostok (soronként egy, pl. http://203.0.113.10); üresen nincs relay-keresés")
    g = "Élő átváltás (failover)"
    add("failover_enabled", "bool", True, g, "Lejátszás közbeni automatikus átváltás másik forrásra")
    add("failover_errors", "int", 3, g, "Ennyi egymás utáni szegmenshiba után vált", 1, 20)
    add("failover_stall", "int", 45, g, "Ennyi mp-ig tartó új szegmens nélküli megakadás után vált", 10, 600)
    add("failover_cooldown", "int", 600, g, "Sikeres átváltás után legalább ennyi mp telik el a csatorna következő átváltásáig", 30, 86400)
    add("failover_retry", "int", 60, g, "Sikertelen átváltás után legkorábban ennyi mp múlva próbál újra", 10, 3600)
    add("failover_candidates", "int", 24, g, "Átváltáskor legfeljebb ennyi jelöltet mér (azonos slug a többi hoston előbb)", 6, 100)
    add("failover_workers", "int", 4, g, "Átváltási jelöltek párhuzamos mérése ennyi szállal", 1, 8)
    add("failover_fresh", "int", 600, g, "Átváltáskor csak eddig friss (mp) mérési eredményt fogad el cache-ből", 60, 86400)
    g = "Értesítés"
    add("webhook_url", "str", "", g, "Webhook URL (JSON POST; pl. Home Assistant)")
    add("webhook_events", "str", "dead,recovered,failover,failover_failed,switched", g,
        "Értesítendő események (vesszővel): dead, recovered, switched, failover, failover_failed, failover_blocked, check_done")
    g = "HDHomeRun-tuner (Plex)"
    add("hdhr_enabled", "bool", True, g, "HDHomeRun-emuláció: a Plex élő tunernek látja a proxyt (/discover.json, /lineup.json, /auto/v<szám>, /hdhr/epg.xml)")
    add("hdhr_tuners", "int", 16, g, "A tuner jelentett párhuzamos fogadóinak száma (a Plex ennyi egyidejű streamet enged; nagyobb = a beragadt foglalások nem blokkolnak)", 1, 32)
    add("hdhr_discovery", "bool", True, g, "Automatikus felderítés: bináris TLV + szöveges (UDP 65001) és SSDP (UDP 1900)")
    add("hdhr_port80", "bool", False, g, "Port-80-as tükör: a Plex egyes útvonalai a hirdetett port helyett a 80-at kérdeznek (ehhez CAP_NET_BIND_SERVICE vagy root kell)")
    add("hdhr_name", "str", "", g, "Tuner megjelenő neve (üresen: iptv-ts-proxy@<host>)")
    g = "Csatorna-felderítés"
    add("discovery_enabled", "bool", False, g, "Napi automatikus új-csatorna-keresés a relay hostokon (jelöltlista az admin Ellenőrzés fülén)")
    add("discovery_time", "time", "04:20", g, "Az automatikus felderítés időpontja (HH:MM)")
    add("discovery_epg_names", "bool", True, g, "A forrás-EPG csatornanevei is jelölt slugok legyenek (így találja meg a katalógusból kimaradókat)")
    add("discovery_names", "text", "", g, "Kézi jelöltlista: soronként csatornanév vagy slug, amit a relayeken kipróbál")
    add("discovery_max", "int", 400, g, "Egy futásban legfeljebb ennyi ismeretlen jelölt-slug mérése", 10, 5000)
    g = "Statisztika"
    add("stats_enabled", "bool", True, g, "Nézettségi statisztika gyűjtése")
    add("stats_days", "int", 90, g, "Statisztika megőrzése (nap)", 7, 3650)
    g = "Naplózás"
    add("log_enabled", "bool", True, g, "Naplózás be/ki (journal és a Napló fül); kikapcsolva a hibák sem naplózódnak")
    add("log_level", "choice", "INFO", g, "Legalacsonyabb naplózott szint (DEBUG a legrészletesebb)",
        choices=list(LOG_LEVELS))
    for cat, desc in LOG_CATS.items():
        add("log_" + cat, "bool", cat != "diag", g,
            desc + " (kikapcsolva csak a figyelmeztetések és hibák maradnak)")
    return S


SPEC = _spec()


def env(name, default=""):
    return os.environ.get(name, default)


def _parse_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "igen")


class Cfg:
    def __init__(self, a):
        self.admin_password = env("TSP_ADMIN_PASSWORD")
        self.external_token = env("TSP_EXTERNAL_TOKEN")
        self.source = env("TSP_SOURCE")                 # csak induló katalógus-importhoz (ha nincs katalógus-fájl)
        self.listen, self.port, self.data = a.listen, a.port, a.data
        self.pipeline_dir = a.pipeline_dir
        self.catalog_path = a.catalog or (os.path.join(a.pipeline_dir, "katalog.json") if a.pipeline_dir
                                          else os.path.join(a.data, "katalog.json"))
        for k, s in SPEC.items():
            raw = env(s["env"], None)
            setattr(self, k, self._coerce(k, raw) if raw not in (None, "") else s["default"])
        self.settings_file = os.path.join(self.data, "settings.json")
        self.defaults = {k: s["default"] for k, s in SPEC.items()}
        self.overrides = {}

    @staticmethod
    def _coerce(key, val):
        s = SPEC[key]
        t = s["type"]
        if t == "bool":
            return _parse_bool(val)
        if t == "int":
            return int(val)
        if t == "float":
            return float(val)
        if t == "choice":
            return str(val).strip().upper()
        return str(val).strip() if t != "text" else str(val).replace("\r", "").strip()

    def relay_list(self):
        return [x.strip().rstrip("/") for x in self.relays.splitlines() if x.strip()]

    def validate(self, key, val):
        if key not in SPEC:
            raise ValueError(f"ismeretlen beállítás: {key}")
        s = SPEC[key]
        t = s["type"]
        try:
            v = self._coerce(key, val)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: érvénytelen érték") from None
        if t in ("int", "float"):
            lo, hi = s["min"], s["max"]
            if (lo is not None and v < lo and not (key == "range_chunk" and v == 0)) or (hi is not None and v > hi):
                raise ValueError(f"{key}: {lo}..{hi} között legyen")
            if key == "range_chunk" and 0 < v < 65536:
                raise ValueError("range_chunk: 0 vagy legalább 65536")
        elif t == "time":
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
                raise ValueError(f"{key}: HH:MM formátum kell")
        elif t == "choice":
            if v not in s["choices"]:
                raise ValueError(f"{key}: {'|'.join(s['choices'])} egyike legyen")
        elif t == "text" and key == "relays":
            for line in v.splitlines():
                if line.strip() and not re.match(r"^https?://[^\s/]+$", line.strip().rstrip("/")):
                    raise ValueError(f"relays: érvénytelen host: {line.strip()[:60]}")
        elif t == "str":
            if key in ("epg_source", "external_base", "legacy_epg_url", "webhook_url") and v and not re.match(r"^https?://", v):
                raise ValueError(f"{key}: http(s):// URL kell")
        return v

    def apply(self, changes):
        """Beállítások érvényesítése és élő alkalmazása; csak az alaptól eltérők mentődnek."""
        clean = {k: self.validate(k, v) for k, v in changes.items()}
        new_keep = clean.get("keep", self.keep)
        new_back = clean.get("start_back", self.start_back)
        if new_keep < new_back + 1:
            raise ValueError("keep legyen legalább start_back + 1")
        for k, v in clean.items():
            setattr(self, k, v)
            if v == self.defaults[k]:
                self.overrides.pop(k, None)
            else:
                self.overrides[k] = v
        return clean

    def load_settings(self):
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                data = {k: v for k, v in json.load(f).items() if k in SPEC}
            self.apply(data)
            log.info("mentett beállítások betöltve: %s", ", ".join(self.overrides) or "-")
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("settings.json hibás, kihagyva: %s", e)

    def save_settings(self):
        os.makedirs(self.data, exist_ok=True)
        tmp = self.settings_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.overrides, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.settings_file)


# ---------------------------------------------------------------- csatorna (futásidejű nézet)
class Channel:
    def __init__(self, rec, url):
        self.rec = rec
        self.id = rec["id"]
        self.name = rec["name"]
        self.tvg_id = rec.get("tvg_id", "")
        self.group = rec.get("group", "")
        self.num = rec.get("chno", "")
        self.url = url
        self.session = None

    def logo_path(self, logos_dir):
        if not logos_dir or "/" in self.name or "\\" in self.name:
            return None
        p = os.path.join(logos_dir, self.name + ".png")
        return p if os.path.isfile(p) else None


# ---------------------------------------------------------------- HLS
class PrioSem:
    """Prioritásos szemafor: a kisebb prioritás-érték (régebbi szegmens, kisebb rész) kerül előbb sorra."""

    def __init__(self, n):
        self.free, self.q, self.c = n, [], 0

    def slot(self, prio):
        return _Slot(self, prio)

    async def acquire(self, prio):
        if self.free > 0 and not self.q:
            self.free -= 1
            return
        fut = asyncio.get_running_loop().create_future()
        self.c += 1
        heapq.heappush(self.q, (prio, self.c, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()
            raise

    def release(self):
        while self.q:
            _, _, fut = heapq.heappop(self.q)
            if not fut.done():
                fut.set_result(None)
                return
        self.free += 1


class _Slot:
    def __init__(self, sem, prio):
        self.sem, self.prio = sem, prio

    async def __aenter__(self):
        await self.sem.acquire(self.prio)

    async def __aexit__(self, *exc):
        self.sem.release()


class Segment:
    """Egy szegmens: chunkok listája + állapot; a kliensek sorban olvassák (seq = belső, monoton index)."""

    def __init__(self, seq):
        self.seq = seq
        self.chunks = []
        self.done = False
        self.failed = False
        self.up_done = False       # az upstream e szegmens adatát teljesen pufferelte
        self.in_bytes = 0          # a pacer számára: enqueue-olt bájtok
        self.pending = 0
        self.touched = time.time()  # utolsó kiengedett adat (a kliens az elakadt szegmenst átugorja)
        self.dur = None             # a szegmens hossza az EXTINF szerint (mp)


# HDHomeRun-stílusú null-csomag (PID 0x1FFF): a szegmensközi üresek kitöltésére. A
# lejátszók/transcoderek átdobják, de az adatfolyam nem áll meg — a Plex DVR-segmentere
# az elhallgatást "idle session"-ként kezeli, és killeli a lejátszást.
NULL_PKT = b"\x47\x1f\xff\x10" + b"\xff" * 184
NULL_BLOCK = NULL_PKT * 128            # ~24 KB


class Session:
    """Egy csatorna élő munkamenete (upstream letöltés + kliens fan-out)."""

    def __init__(self, app, ch):
        self.A, self.ch, self.cfg, self.http = app, ch, app.cfg, app.http
        self.ua = app.cfg.upstream_ua
        self.cur_url = ch.url
        self.switch_req = None
        self.segs = deque(maxlen=self.cfg.keep)
        self.cond = asyncio.Condition()
        self.rsem = PrioSem(self.cfg.range_conc)
        self.clients = 0
        self.last_client = time.time()
        self.task = None
        self.started = time.time()
        self.bytes_in = 0
        self.errors = 0
        self.stopped = False
        self.client_list = {}            # id -> {ip, ua, since, bytes, transport}
        self._cid = 0
        self.counter = 0                 # belső szegmens-index (forrásváltáskor is monoton)
        self.fail_streak = 0
        self.pl_streak = 0               # egymás utáni playlist-letöltési hibák
        self.last_pl_ok = time.time()    # utolsó sikeres playlist-letöltés
        self.last_new = time.time()
        self.samples = deque(maxlen=90)  # (idő, bytes_in) 2 mp-enként
        # kimenet-simítás: a letöltés pufferbe megy, a pacer real-time ütemezéssel adja ki
        self.bufq = deque()              # (seg, chunk) FIFO
        self.buf_bytes = 0
        self.rate = 2_500_000.0          # becsült kimenet, bájt/mp (szegmensenként finomítva)
        self.td_cur = 6.0
        self.last_out = time.time()

    def sample(self):
        self.samples.append((time.time(), self.bytes_in))

    def kbps_series(self):
        s = list(self.samples)
        out = []
        for (t0, b0), (t1, b1) in zip(s, s[1:]):
            if t1 > t0:
                out.append(round((b1 - b0) * 8 / 1000 / (t1 - t0)))
        return out

    def kbps(self):
        s = list(self.samples)[-5:]
        if len(s) < 2 or s[-1][0] <= s[0][0]:
            return 0
        return round((s[-1][1] - s[0][1]) * 8 / 1000 / (s[-1][0] - s[0][0]))

    # -- upstream playlist
    async def fetch_text(self, url):
        # sock_connect + rövidebb total: az elérhetetlen/félig halott relay gyorsan bukjon meg,
        # hogy a friss kapcsolódás gyors failover-je a Plex 30 mp-es ablakán belül beérjen
        async with self.http.get(url, headers={"User-Agent": self.ua},
                                 timeout=aiohttp.ClientTimeout(total=8, sock_connect=5)) as r:
            r.raise_for_status()
            return await r.text(errors="replace")

    async def media_url(self):
        url = self.cur_url
        text = await self.fetch_text(url)
        if "#EXT-X-STREAM-INF" not in text:
            return url
        best, best_bw, bw = None, -1, 0
        for line in text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
            elif line.strip() and not line.startswith("#"):
                if bw > best_bw:
                    best, best_bw = urljoin(url, line.strip()), bw
        return best or url

    @staticmethod
    def parse_media(text, base):
        seq, td, segs, dur = 0, 6.0, [], None
        for line in text.splitlines():
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq = int(line.split(":")[1])
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                td = float(line.split(":")[1])
            elif line.startswith("#EXTINF:"):
                m = re.match(r"#EXTINF:\s*([\d.]+)", line)
                dur = float(m.group(1)) if m else None
            elif line.strip() and not line.startswith("#"):
                segs.append((seq + len(segs), urljoin(base, line.strip()), dur))
        return td, segs

    async def publish_data(self, seg, data):
        seg.chunks.append(data)
        self.last_out = seg.touched = time.time()
        async with self.cond:
            self.cond.notify_all()

    async def enqueue(self, seg, data):
        """Letöltött adat pufferbe: a pacer ütemezve engedi ki (folyamatos kimenet)."""
        seg.in_bytes += len(data)
        seg.pending += 1
        self.bytes_in += len(data)       # valódi upstream-bájt (a null-töltés nem számít bele)
        self.bufq.append((seg, data))
        self.buf_bytes += len(data)
        async with self.cond:
            self.cond.notify_all()

    async def pacer(self):
        """Kimenet-ütemező: a pufferből rate bájt/mp-et enged ki, és szegmensközi
        elhallgatásnál null-csomagokkal tölti a folyamatot (a Plex DVR-segmentere
        az üresjáratot idle-ként kezeli és killeli a lejátszást)."""
        try:
            n = 0
            while not self.stopped:
                await asyncio.sleep(0.25)
                budget = self.rate * 0.25
                if self.buf_bytes > 12_000_000:      # túlcsordulás ellen: gyorsítva ki
                    budget = float("inf")
                while budget > 0 and self.bufq:
                    seg, chunk = self.bufq.popleft()
                    self.buf_bytes -= len(chunk)
                    budget -= len(chunk)
                    await self.publish_data(seg, chunk)
                    seg.pending -= 1
                # lezárás: a kijárt szegmens (upstream kész + minden darabja kiengedve) itt zárul,
                # nem a darab kiengedésekor – ha az utolsó darab az up_done jelzés ELŐTT jár ki
                # (lassú upstream, EOF-várakozás, félbeszakadt szegmens), a szegmens különben
                # sosem kapna done-t, és a kliens örökre rajta ragadna
                for seg in self.segs:
                    if seg.up_done and seg.pending <= 0 and not seg.done:
                        if not self.bufq:        # szegmens-végi null-töltés a folytonosságért
                            await self.publish_data(seg, NULL_BLOCK * 4)
                        seg.done = True
                        async with self.cond:
                            self.cond.notify_all()
                if (not self.bufq and self.clients > 0
                        and time.time() - self.last_out > 2.0):
                    seg = Segment(self.counter)
                    self.counter += 1
                    seg.chunks = [NULL_BLOCK] * 8
                    seg.done = True
                    await self.publish(seg)
                    self.last_out = time.time()
                n += 1
                if self.clients and n % 20 == 0 and dlog.isEnabledFor(logging.INFO):   # ~5 mp-enként
                    dlog.info("%s pacer: buf=%.1fMB rate=%.2fMbps last_out=%.1fs pended=%d",
                             self.ch.name, self.buf_bytes / 1e6, self.rate / 125000,
                             time.time() - self.last_out, self.bufq[0][0].pending if self.bufq else 0)
        except asyncio.CancelledError:
            raise

    async def fetch_part(self, url, a, b, prio):
        """Egy bájttartomány letöltése (újrapróbálással). Az rsem prioritás szerint korlátoz."""
        last = None
        for attempt in range(3):
            try:
                async with self.rsem.slot(prio):
                    async with self.http.get(
                            url, headers={"User-Agent": self.ua, "Range": f"bytes={a}-{b}"},
                            timeout=aiohttp.ClientTimeout(total=40, sock_read=15)) as r:
                        if r.status not in (200, 206):
                            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                        data = await r.read()
                        if r.status == 200:      # a szerver nem tud Range-et: a teljes testet kaptuk
                            data = data[a:b + 1]
                        return data
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(0.4 * (attempt + 1))
        raise last

    # -- szegmens letöltés: párhuzamos Range-részek, sorrendben kiadva
    async def download(self, seg, url):
        ch = self.cfg.range_chunk
        if ch <= 0:
            return await self.download_stream(seg, url)
        try:
            async with self.rsem.slot((seg.seq, 0)):
                async with self.http.get(
                        url, headers={"User-Agent": self.ua, "Range": f"bytes=0-{ch - 1}"},
                        timeout=aiohttp.ClientTimeout(total=40, sock_read=15)) as r:
                    if r.status != 206:
                        pass_through = True
                    else:
                        pass_through = False
                        first = await r.read()
                        total = int(r.headers.get("Content-Range", "bytes 0-0/0").rsplit("/", 1)[1] or 0)
            if pass_through:
                return await self.download_stream(seg, url)
            if total <= 0 or total <= len(first):
                await self.enqueue(seg, first)
                seg.up_done = True
                return
            await self.enqueue(seg, first)
            starts = list(range(ch, total, ch))
            tasks = [asyncio.create_task(self.fetch_part(url, a, min(a + ch, total) - 1, (seg.seq, i + 1)))
                     for i, a in enumerate(starts)]
            pos = len(first)
            try:
                for i, t in enumerate(tasks):
                    try:
                        data = await t
                        await self.enqueue(seg, data)
                        pos += len(data)
                    except Exception as e:  # noqa: BLE001
                        # a szolgáltató eltüntette a szegmenst a részek letöltése közben (404):
                        # a hiányzó részt egy teljes (Range nélküli) letöltéssel pótoljuk, hogy ne legyen lyuk
                        slog.warning("%s seg %s: rész %d/%d hiba (%s) – teljes letöltéssel pótlom",
                                    self.ch.name, seg.seq, i + 1, len(tasks), e)
                        if await self.download_tail(seg, url, pos):
                            return
                        raise
                seg.up_done = True
                return
            finally:
                for t in tasks:
                    t.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.errors += 1
            if seg.in_bytes:                     # félig pufferelt szegmens: nem próbáljuk újra
                slog.warning("%s seg %s megszakadt: %s", self.ch.name, seg.seq, e)
                seg.failed = True
                seg.up_done = True
                return
        if seg.in_bytes == 0:
            await self.download_stream(seg, url)  # még semmit nem puffereltünk: teljes letöltéssel újra

    async def download_tail(self, seg, url, skip):
        """A szegmens hátralévő része egyetlen, Range nélküli letöltéssel (a már kiküldött `skip` bájtot átugorva).
        Akkor kell, ha a szolgáltató a párhuzamos részek letöltése közben eltüntette a szegmenst (404)."""
        for attempt in range(2):
            try:
                async with self.http.get(
                        url, headers={"User-Agent": self.ua},
                        timeout=aiohttp.ClientTimeout(total=60, sock_read=15)) as r:
                    if r.status != 200:
                        raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                    seen = 0
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        if seen + len(chunk) <= skip:
                            seen += len(chunk)
                            continue
                        if seen < skip:
                            chunk = chunk[skip - seen:]
                            seen = skip
                        seen += len(chunk)
                        await self.enqueue(seg, chunk)
                    seg.up_done = True
                    return True
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                slog.warning("%s seg %s: teljes pótlás %d. próba hiba: %s", self.ch.name, seg.seq, attempt + 1, e)
                await asyncio.sleep(0.4 * (attempt + 1))
        return False

    async def download_stream(self, seg, url):
        for attempt in range(3):
            try:
                async with self.http.get(
                        url, headers={"User-Agent": self.ua},
                        timeout=aiohttp.ClientTimeout(total=60, sock_read=15)) as r:
                    if r.status != 200:
                        raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        await self.enqueue(seg, chunk)
                    seg.up_done = True
                    return
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                if seg.in_bytes:         # félig pufferelt szegmens: nem próbáljuk újra
                    break
                slog.warning("%s seg %s próba %d: %s", self.ch.name, seg.seq, attempt + 1, e)
                await asyncio.sleep(0.5 * (attempt + 1))
        seg.failed = True
        if seg.in_bytes == 0:
            seg.done = True              # adat nélkül: a pacer nem érinti, le lehet zárni
        else:
            seg.up_done = True           # a pacer engedi ki, aztán zárja le

    async def publish(self, seg):
        async with self.cond:
            self.segs.append(seg)
            self.cond.notify_all()

    async def run(self):
        sem = asyncio.Semaphore(self.cfg.threads)
        hi = None                        # a jelenlegi forrás legnagyobb kiadott media-sequence-e
        hi_prev = None                   # forrásváltáskor a régi forrás hi-ja (folytatáshoz)
        first = True
        inflight = set()
        murl = None

        def switch(url, abort):
            nonlocal murl, first, hi, hi_prev
            self.cur_url, murl, first = url, None, True
            hi_prev, hi = hi, None
            self.fail_streak, self.last_new = 0, time.time()
            if abort:                    # halott forrás: a folyamatban lévő letöltései ne tartsák fel a klienst
                for t in list(inflight):
                    t.cancel()

        async def job(seg, url):
            try:
                async with sem:
                    await self.download(seg, url)
            except asyncio.CancelledError:
                # forrásváltáskor a régi forrás folyamatban lévő letöltései megszakadnak:
                # a szegmens lezárható (a pacer kiengedi, ami már megjött, aztán done)
                seg.failed = seg.up_done = True
                raise
            # kimeneti bitráta finomítása: szegmens-bájtok / a szegmens tényleges hossza (EXTINF)
            if seg.in_bytes and (seg.dur or self.td_cur):
                est = seg.in_bytes / (seg.dur or self.td_cur)
                self.rate = max(100_000.0, 0.6 * self.rate + 0.4 * min(est, 12_000_000.0))
            self.fail_streak = self.fail_streak + 1 if seg.failed else 0
            async with self.cond:
                self.cond.notify_all()

        try:
            pad = asyncio.create_task(self.pacer())
            while not self.stopped:
                if time.time() - self.last_client > self.cfg.idle and self.clients == 0:
                    slog.info("%s: néző nélkül leáll", self.ch.name)
                    break
                # a katalógusban megváltozott aktív forrás (szerkesztés / ellenőrzés): zökkenő nélküli váltás
                if self.switch_req and self.switch_req != self.cur_url:
                    slog.info("%s: forrásváltás (katalógus): %s", self.ch.name, self.switch_req)
                    switch(self.switch_req, abort=False)
                self.switch_req = None
                # élő átváltás másik forrásra, ha elakad az adás
                if self.cfg.failover_enabled and self.clients > 0:
                    errs = self.fail_streak >= self.cfg.failover_errors
                    stall = time.time() - self.last_new > self.cfg.failover_stall
                    # nem válaszoló playlist (lefagyott host): nem várjuk ki a failover_errors × 8 mp-es
                    # időkorlátot – két célszegmensnyi (min. 12 mp) sikertelenség már halott forrás
                    pl_dead = self.pl_streak >= 1 and time.time() - self.last_pl_ok > max(12.0, 2 * self.td_cur)
                    young = time.time() - self.started < 30
                    # friss kapcsolódásnál gyors út: a néző (pl. a Plex) ~30 mp után feladja,
                    # addig minden próbálkozásnak szabad azonnal lennie
                    fast = young and (self.fail_streak >= 1 and self.bytes_in == 0
                                      or self.fail_streak >= 2)
                    if errs or fast or stall or pl_dead:
                        reason = ("első kapcsolódás hibás forrásra" if fast and self.bytes_in == 0 else
                                  "playlist-hibák" if self.pl_streak >= self.cfg.failover_errors or pl_dead else
                                  "szegmenshibák" if errs or fast else "az adás megakadt")
                        # hard: a forrás teljesen halott (nincs playlist / nincs új szegmens) – ekkor a
                        # sikeres váltás utáni cooldown sem tarthatja percekig sötétben a nézőt
                        hard = stall or pl_dead or self.pl_streak >= self.cfg.failover_errors
                        new = await self.A.failover(self, reason, quick=fast, urgent=fast, hard=hard)
                        self.fail_streak, self.last_new = 0, time.time()
                        self.pl_streak, self.last_pl_ok = 0, time.time()
                        if new:
                            switch(new, abort=True)
                            continue
                try:
                    if murl is None:
                        murl = await self.media_url()
                    td, segs = self.parse_media(await self.fetch_text(murl), murl)
                    self.pl_streak, self.last_pl_ok = 0, time.time()
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    self.fail_streak += 1
                    self.pl_streak += 1
                    slog.warning("%s playlist hiba: %s", self.ch.name, e or type(e).__name__)
                    murl = None
                    await asyncio.sleep(1)
                    continue
                if first and segs:
                    last = segs[-1][0]
                    if hi_prev is not None and segs[0][0] - 1 <= hi_prev <= last + 3:
                        # azonos számozás (ugyanaz az origin másik relayen): ott folytatjuk, ahol a
                        # régi forrás abbahagyta – nincs ismétlődő vagy kihagyott szegmens
                        hi = hi_prev
                    else:
                        # rövid (gyors evikciójű) playlistnél az élő fejénél indulunk: a start_back
                        # miatti lemaradás ott már kijárt szegmenst jelent (404 a kapcsolat első másodperceiben).
                        # Idegen számozású új forrásnál is az élő fejénél: a visszalépés ismétlést okozna.
                        n = self.cfg.start_back if len(segs) >= self.cfg.start_back + 3 and hi_prev is None else 1
                        hi = segs[-n][0] - 1
                    first, hi_prev = False, None
                elif segs and hi is not None and segs[-1][0] < hi - len(segs) - 5:
                    slog.info("%s: a forrás media-sequence-e újraindult (%s -> %s)", self.ch.name, hi, segs[-1][0])
                    hi = segs[-1][0] - 1
                # ha az eddigi szegmensek mind hibásak (lejártak), a kliens a következőkre vár
                if segs and self.segs and all(s.failed for s in self.segs):
                    hi = max(hi, segs[-1][0] - 1)
                for src_seq, url, dur in segs:
                    # csak a már kiadottnál újabb szegmens: a régebbiek (a playlist eleje az indulási
                    # pont előtt) különben a következő frissítéskor visszafelé kerülnének a folyamba
                    if src_seq <= hi:
                        continue
                    hi = src_seq
                    self.last_new = time.time()
                    seg = Segment(self.counter)
                    seg.dur = dur or td or self.td_cur      # a tényleges hossz a bitráta-becsléshez
                    self.counter += 1
                    await self.publish(seg)
                    t = asyncio.create_task(job(seg, url))
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
                self.td_cur = td or self.td_cur
                await asyncio.sleep(max(1.0, td / 2))
        finally:
            for t in inflight:
                t.cancel()
            self.stopped = True
            if pad and not pad.done():
                pad.cancel()
            async with self.cond:
                self.cond.notify_all()

    def _stuck(self, seg):
        """A kliens ne ragadjon egy szegmensen, amelyre már nem jön adat: a letöltése régóta nem
        adott semmit, miközben egy KÉSŐBBI szegmens már kapott adatot (a pacer sorban enged ki,
        tehát ez csak elakadt letöltésnél fordul elő). A pufferből kiesett szegmenst NEM ugorjuk
        át: a kliens a saját hivatkozásán keresztül továbbra is megkapja a tartalmát."""
        if seg.done or time.time() - seg.touched < max(20.0, 3 * self.td_cur):
            return False
        return any(s.seq > seg.seq and s.in_bytes for s in self.segs)

    # -- kliens kiszolgálás
    async def serve(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "video/MP2T", "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*"})
        await resp.prepare(request)
        self.clients += 1
        self.last_client = time.time()
        self._cid += 1
        cid = self._cid
        wrote_since_log = [0, time.time()]
        rec = {"id": cid, "ip": request.headers.get("CF-Connecting-IP") or request.remote,
               "ua": request.headers.get("User-Agent", "")[:90],
               "since": time.time(), "bytes": 0, "transport": request.transport}
        self.client_list[cid] = rec
        sent_seq = None
        try:
            # várjuk meg az első szegmenst
            async with self.cond:
                await asyncio.wait_for(
                    self.cond.wait_for(lambda: self.segs or self.stopped), 30)
                if self.segs:
                    sent_seq = list(self.segs)[-self.cfg.start_back:][0].seq - 1
            while not self.stopped or self.segs:
                async with self.cond:
                    nxt = None
                    for s in self.segs:
                        if sent_seq is None or s.seq > sent_seq:
                            nxt = s
                            break
                    if nxt is None:
                        if self.stopped:
                            break
                        await self.cond.wait()
                        continue
                if sent_seq is not None and nxt.seq > sent_seq + 1:
                    slog.info("%s: kliens lemaradt, ugrik %s -> %s", self.ch.name, sent_seq, nxt.seq)
                i = 0
                while True:
                    while i < len(nxt.chunks):
                        # 60 mp-es írási korlát: ha a kliens nem olvassa tovább (beragadt lejátszó,
                        # elhagyott Plex-tuner-slot), a kapcsolat felszabadul, nem foglal örökké
                        await asyncio.wait_for(resp.write(nxt.chunks[i]), 60)
                        rec["bytes"] += len(nxt.chunks[i])
                        wrote_since_log[0] += len(nxt.chunks[i])
                        i += 1
                    if wrote_since_log[0] and time.time() - wrote_since_log[1] > 5:
                        dlog.info("%s kliens %d: %d B/5s, seg %s/%s", self.ch.name, cid,
                                  wrote_since_log[0], nxt.seq, "done" if nxt.done else "folyamatban")
                        wrote_since_log[0], wrote_since_log[1] = 0, time.time()
                    if nxt.done:
                        if i >= len(nxt.chunks):
                            break
                        continue
                    async with self.cond:
                        if i >= len(nxt.chunks) and not nxt.done and not self.stopped:
                            if self._stuck(nxt):
                                slog.info("%s: elakadt szegmens (%s) kihagyva", self.ch.name, nxt.seq)
                                break
                            # 120 mp: élő szünet sosem ennyi (failover ~15-20 s); tovább váró kliens halott
                            await asyncio.wait_for(self.cond.wait(), 120)
                        elif self.stopped and not nxt.done and i >= len(nxt.chunks):
                            break
                sent_seq = nxt.seq
                self.last_client = time.time()
        except (ConnectionError, asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            self.clients -= 1
            self.client_list.pop(cid, None)
            self.last_client = time.time()
            self.A.record_usage(self, rec)
        return resp


# ---------------------------------------------------------------- alkalmazás
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = log
        self.VERSION = VERSION
        self.catalog = Catalog(cfg.catalog_path, os.path.join(cfg.data, "catalog-history"), cfg.history_keep)
        self.health = Health(self)
        self.channels = {}               # id -> Channel (kiszolgálható: engedélyezett + van aktív URL)
        self.http = None
        self.epg_gz = None
        self.epg_xml = None
        self.epg_time = 0
        self.epg_hdhr_xml = None
        self.epg_hdhr_gz = None
        self.started = time.time()
        self.epg_index = {}              # tvg-id -> [(start, stop, cím), ...] rendezve
        self.epg_names = {}              # tvg-id -> display-name-ek (a felderítés jelöltjei)
        self.epg_stats = (0, 0)
        self.epg_used = ""

    # ---- katalógus <-> futó csatornák
    def sync_channels(self):
        new = {}
        for rec in self.catalog.channels:
            if rec.get("enabled") is False:
                continue
            url = self.catalog.active_url(rec, ip_only=self.cfg.ip_only)
            if not url:
                continue
            ch = Channel(rec, url)
            old = self.channels.get(ch.id)
            if old and old.session:
                ch.session = old.session
                old.session.ch = ch
                if old.session.cur_url != url:
                    old.session.switch_req = url
            new[ch.id] = ch
        for cid, old in self.channels.items():
            if cid not in new and old.session:
                old.session.stopped = True
        self.channels = new

    async def failover(self, session, reason, quick=False, urgent=False, hard=False):
        return await self.health.failover(session.ch.id, session.cur_url, reason,
                                          quick=quick, urgent=urgent, hard=hard)

    def write_legacy(self, measured=None, dt=0.0, mode=""):
        """A régi pipeline fájljai (kimenet/*.m3u, status.json, meres.json): az nginx/CF linkek és a refresh.py ezeket olvassák."""
        d, cat = self.cfg.pipeline_dir, self.catalog
        if not d or not self.cfg.write_legacy_files:
            return
        try:
            def wr(path, text):
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)

            out = os.path.join(d, "kimenet")
            os.makedirs(out, exist_ok=True)
            enabled = [c for c in cat.channels if c.get("enabled") is not False]
            chans = [c for c in enabled if cat.active_url(c, ip_only=True)]
            active = [c for c in chans if self.health.alive(c["id"])]
            epg = self.cfg.legacy_epg_url
            wr(os.path.join(out, "hu.m3u"), cat.export_m3u(chans, f"Magyar csatornák — teljes katalógus ({len(chans)})", epg))
            wr(os.path.join(out, "hu-aktiv.m3u"), cat.export_m3u(active, f"Magyar csatornák — aktív ({len(active)})", epg))
            names_alive = [c["name"] for c in enabled if self.health.alive(c["id"])]
            names_dead = [c["name"] for c in enabled if not self.health.alive(c["id"])]
            wr(os.path.join(d, "status.json"), json.dumps({
                "datum": datetime.datetime.now().isoformat(timespec="seconds"), "mode": mode or "proxy",
                "csatorna": len(enabled), "aktiv_szam": len(names_alive), "halott_szam": len(names_dead),
                "aktiv": names_alive, "halott": names_dead, "futas_mp": round(dt)}, ensure_ascii=False, indent=1))
            if measured is not None:
                ossz = {"jo": 0, "hatar": 0, "instabil": 0, "halott": 0}
                cs = {}
                for c in enabled:
                    res = measured.get(c["id"])
                    if res is None:
                        continue
                    state, _, _ = self.health.classify(res)
                    ossz[state if state in ossz else "instabil"] += 1
                    best = self.health.rank(res)
                    urls = c.get("urls") or [None]
                    cs[c["name"]] = {"aktualis": urls[0], "valasztott": best["url"] if best else None,
                                     "eredmenyek": sorted(res, key=lambda r: -(r.get("margin") or 0))}
                wr(os.path.join(d, "meres.json"), json.dumps({
                    "datum": time.strftime("%Y-%m-%dT%H:%M:%S"), "ido_s": dt, "mod": "apply", "osszesito": ossz,
                    "csatornak": cs}, ensure_ascii=False, indent=1))
        except OSError as e:
            log.warning("a régi pipeline-fájlok írása nem sikerült: %s", e)

    # ---- induló katalógus
    async def bootstrap_catalog(self):
        if self.catalog.exists():
            if self.catalog.load():
                self.catalog.save("azonositok")
                log.info("katalógus: új csatornaazonosítók kiosztva és mentve")
        elif self.cfg.source:
            try:
                async with self.http.get(self.cfg.source, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    r.raise_for_status()
                    text = await r.text()
            except Exception as e:  # noqa: BLE001
                # nem mentünk üres katalógust: a következő induláskor újra megpróbálja az importot
                log.error("katalógus: az induló M3U nem tölthető le (%s): %s – üres katalógussal indul", self.cfg.source, e)
                return
            rep = self.catalog.import_m3u(text, "add_new")
            log.info("katalógus: induló import az M3U-ból: %d csatorna", len(rep["added"]))
        else:
            self.catalog.save("uj-katalogus")
            log.warning("nincs katalógus és nincs induló forrás: üres katalógus jött létre")
        log.info("katalógus: %d csatorna (%s)", len(self.catalog.channels), self.catalog.path)

    # ---- EPG
    def now_next(self, tvg_id):
        lst = self.epg_index.get(tvg_id) or []
        now = time.time()
        cur = nxt = None
        for i, (a, b, t) in enumerate(lst):
            if a <= now < b:
                cur = (a, b, t)
                nxt = lst[i + 1] if i + 1 < len(lst) else None
                break
            if a > now:
                nxt = (a, b, t)
                break
        return cur, nxt, len(lst)

    async def load_epg(self):
        t0 = time.time()
        src = self.cfg.epg_source or self.cfg.legacy_epg_url
        elog.info("epg forrás: %s", src)
        async with self.http.get(src, timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            raw = await r.read()
        chans = [dict(c) for c in self.catalog.channels]
        # a több MB-os XMLTV feldolgozása és tömörítése külön szálon: az event loopban másodpercekre
        # megállította volna az összes futó adást (a Plex ezt elhallgatásnak veszi)
        (self.epg_index, self.epg_stats, self.epg_hdhr_xml, self.epg_hdhr_gz,
         self.epg_xml, self.epg_gz, self.epg_names) = await asyncio.to_thread(self._build_epg, raw, chans)
        self.epg_used, self.epg_time = src, time.time()
        elog.info("epg: %d csatorna, %d műsor, %.1fs", *self.epg_stats, time.time() - t0)

    def _build_epg(self, raw, chans):
        """XMLTV szűrése a katalógusra + HDHomeRun-változat; szálon fut, a megosztott állapotot nem írja."""
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        keep = {c.get("tvg_id") for c in chans if c.get("tvg_id") and c.get("enabled") is not False}
        root = ET.fromstring(raw)
        out = ET.Element("tv", root.attrib)
        n_ch = n_pr = 0
        index = {}
        names = {}
        for el in root:
            if el.tag == "channel" and el.get("id") in keep:
                out.append(el)
                n_ch += 1
                # az EPG display-name-jei a csatorna-felderítés slug-jelöltjei
                names.setdefault(el.get("id"), []).extend(
                    [d.text.strip() for d in el.findall("display-name")
                     if d.text and d.text.strip() and not d.text.strip()[:1].isdigit()])
            elif el.tag == "programme" and el.get("channel") in keep:
                out.append(el)
                n_pr += 1
                try:
                    index.setdefault(el.get("channel"), []).append(
                        (xmltv_ts(el.get("start")), xmltv_ts(el.get("stop")), (el.findtext("title") or "").strip()))
                except Exception:  # noqa: BLE001
                    pass
        for lst in index.values():
            lst.sort()
        # a normál EPG-be is kerül „<szám> <név>” display-name: a Plex a tuner GuideNumber-e
        # alapján párosít, és ha valaki a normál EPG-t adja meg guide-nak, így is egyezni fog.
        # A név a KATALÓGUSból jön (lineup GuideName-nel egyező): az EPG-forrás nevei tartalmazhatnak
        # minőségi/24/7 utótagot („(576p)”, „[Not 24/7]”), ami miatt a Plex nem találja a párost
        chno_by_tvg = {c.get("tvg_id"): str(c["chno"]) for c in chans
                       if c.get("tvg_id") and c.get("chno") and c.get("enabled") is not False}
        name_by_chno = {str(c["chno"]): c["name"] for c in chans
                        if c.get("chno") and c.get("enabled") is not False}
        for el in out:
            if el.tag == "channel":
                gn = chno_by_tvg.get(el.get("id"))
                if gn:
                    # a katalógus-néves display-name az ELSŐ helyre kerül: a Plex az elsőt
                    # használja állomásnévként (a forráséban minőségi/24/7 utótag szokott lenni)
                    dn = ET.Element("display-name")
                    dn.text = (f"{gn} {name_by_chno.get(gn) or (el.findtext('display-name') or '').strip()}").strip()
                    el.insert(0, dn)
        # HDHomeRun/Plex EPG: a csatorna-id a katalógus chno-ja, hogy a Plex a tuner lineupjával párosítani tudja
        hmap = {c.get("tvg_id"): str(c["chno"]) for c in chans
                if c.get("tvg_id") and c.get("chno") and c.get("enabled") is not False}
        hdhr_out = ET.Element("tv", root.attrib)
        for el in out:
            el2 = copy.deepcopy(el)
            if el.tag == "channel":
                gn = hmap.get(el.get("id"))
                if gn:
                    el2.set("id", gn)          # a „<szám> <név>” display-name már a normál EPG-ben rajta van
            elif el.tag == "programme":
                gn = hmap.get(el.get("channel"))
                if gn:
                    el2.set("channel", gn)
            hdhr_out.append(el2)
        hxml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(hdhr_out, encoding="utf-8")
        hgz = gzip.compress(hxml)
        os.makedirs(self.cfg.data, exist_ok=True)
        with open(os.path.join(self.cfg.data, "epg-hdhr.xml.gz"), "wb") as f:
            f.write(hgz)
        xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(out, encoding="utf-8")
        gz = gzip.compress(xml)
        with open(os.path.join(self.cfg.data, "epg.xml.gz"), "wb") as f:
            f.write(gz)
        return index, (n_ch, n_pr), hxml, hgz, xml, gz, names

    async def refresher(self):
        last_epg = time.time()
        while True:
            await asyncio.sleep(60)
            if time.time() - last_epg > self.cfg.epg_refresh:
                last_epg = time.time()
                try:
                    await self.load_epg()
                except Exception as e:  # noqa: BLE001
                    elog.warning("epg frissítés hiba: %s", e)

    async def sampler(self):
        """2 mp-enként mintát vesz az aktív csatornák bejövő bájtjaiból (sebesség-grafikon)."""
        while True:
            await asyncio.sleep(2)
            for c in list(self.channels.values()):
                s = c.session
                if s and not s.stopped:
                    s.sample()

    # ---- statisztika, értesítés
    def record_usage(self, session, rec):
        if not self.cfg.stats_enabled:
            return
        secs = time.time() - rec["since"]
        if secs < 3 or rec["ip"] == "127.0.0.1" and rec["ua"].startswith("Python"):
            return
        p = os.path.join(self.cfg.data, "usage.jsonl")
        try:
            if os.path.exists(p) and os.path.getsize(p) > 8 << 20:
                os.replace(p, p + ".1")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": int(time.time()), "id": session.ch.id, "name": session.ch.name,
                                    "ip": rec["ip"], "s": int(secs), "mb": round(rec["bytes"] / 1e6, 1)},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    def usage(self, days):
        cutoff = time.time() - days * 86400
        rows = []
        for p in (os.path.join(self.cfg.data, "usage.jsonl.1"), os.path.join(self.cfg.data, "usage.jsonl")):
            try:
                for line in open(p, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r["t"] >= cutoff:
                            rows.append(r)
                    except (ValueError, KeyError):
                        pass
            except FileNotFoundError:
                pass
        return rows

    def notify(self, ev):
        url = self.cfg.webhook_url
        if not url or ev["type"] not in {x.strip() for x in self.cfg.webhook_events.split(",") if x.strip()}:
            return
        try:
            asyncio.get_running_loop().create_task(self._post_hook(url, ev))
        except RuntimeError:
            pass

    async def _post_hook(self, url, ev):
        try:
            async with self.http.post(url, json=ev, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status >= 400:
                    log.warning("webhook: HTTP %s", r.status)
        except Exception as e:  # noqa: BLE001
            log.warning("webhook hiba: %s", e)

    # ---- teszt (a saját folyamot nézi, mint egy kliens)
    async def deep_probe(self, ch, secs=8):
        t0 = time.time()
        n, first, sync, err, stalls, last = 0, None, False, None, 0, time.time()
        try:
            async with self.http.get(f"http://127.0.0.1:{self.cfg.port}/ch/{ch.id}.ts",
                                     timeout=aiohttp.ClientTimeout(total=secs + 8, sock_read=10)) as r:
                async for chunk in r.content.iter_chunked(65536):
                    now = time.time()
                    if first is None:
                        first = now - t0
                        sync = len(chunk) > 188 and chunk[0] == 0x47 and chunk[188] == 0x47
                    elif now - last > 2.5:
                        stalls += 1
                    last = now
                    n += len(chunk)
                    if now - t0 >= secs or n >= 24_000_000:
                        break
        except Exception as e:  # noqa: BLE001
            err = str(e) or type(e).__name__
        dt = max(time.time() - t0, 0.001)
        return {"first_byte_s": round(first, 2) if first is not None else None, "mb": round(n / 1e6, 1),
                "mbps": round(n * 8 / 1e6 / dt, 1), "ts_ok": sync, "stalls": stalls, "error": err}

    # ---- playlist
    def base(self, request):
        return self.cfg.public or f"{request.scheme}://{request.host}"

    def playlist_items(self, query):
        only_alive = self.cfg.playlist_only_alive and query.get("all") != "1"
        groups = {g.strip() for g in query.get("group", "").split(",") if g.strip()}
        out = []
        for ch in self.channels.values():
            if only_alive and not self.health.alive(ch.id):
                continue
            if groups and ch.group not in groups:
                continue
            out.append(ch)
        return out

    def build_playlist(self, items, base_logo, epg_url, stream_url):
        lines = [f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"']
        for ch in items:
            lg = ch.rec.get("logo") or (f"{base_logo}/logo/{ch.id}.png" if ch.logo_path(self.cfg.logos_dir) else "")
            lines.append(cat_extinf(ch.rec, lg))
            lines.append(stream_url(ch))
        return "\n".join(lines) + "\n"

    async def h_playlist(self, request):
        base = self.base(request)
        txt = self.build_playlist(self.playlist_items(request.query), base, f"{base}/epg.xml.gz",
                                  lambda ch: f"{base}/ch/{ch.id}.ts")
        return web.Response(text=txt, content_type="audio/x-mpegurl", charset="utf-8")

    def ext_check(self, request):
        """Külső (/tv/<token>/…) végpontok: titkos token nélkül 404."""
        tok = self.cfg.external_token
        if not tok or not hmac.compare_digest(request.match_info.get("tok", "").encode(), tok.encode()):
            raise web.HTTPNotFound()

    async def x_playlist(self, request):
        self.ext_check(request)
        base = f"{(self.cfg.external_base or self.base(request) + '/tv').rstrip('/')}/{self.cfg.external_token}"
        direct = not self.cfg.external_streams
        txt = self.build_playlist(self.playlist_items(request.query), base, f"{base}/epg.xml.gz",
                                  (lambda ch: ch.url) if direct else (lambda ch: f"{base}/ch/{ch.id}.ts"))
        return web.Response(text=txt, content_type="audio/x-mpegurl", charset="utf-8", headers={"Cache-Control": "no-store"})

    async def h_epg(self, request):
        if self.epg_gz is None:
            raise web.HTTPServiceUnavailable(text="EPG még nincs kész")
        if request.path.endswith(".gz"):
            return web.Response(body=self.epg_gz, content_type="application/gzip")
        return web.Response(body=self.epg_xml, content_type="application/xml")

    async def h_logo(self, request):
        ch = self.channels.get(request.match_info["cid"])
        p = ch.logo_path(self.cfg.logos_dir) if ch else None
        if not p:
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    async def h_channel(self, request):
        ch = self.channels.get(request.match_info["cid"])
        if not ch:
            raise web.HTTPNotFound()
        s = self._session_for(ch, peer=request.headers.get("CF-Connecting-IP") or request.remote)
        if s is None:
            raise web.HTTPServiceUnavailable(text="elérte az egyidejű csatornák maximumát")
        return await s.serve(request)

    def _session_for(self, ch, peer=None):
        """Meglévő, futó session visszaadása vagy új indítása; max_sessions-t figyelembe véve."""
        s = ch.session
        if s is None or s.stopped:
            mx = self.cfg.max_sessions
            if mx and sum(1 for c in self.channels.values() if c.session and not c.session.stopped) >= mx:
                return None
            s = ch.session = Session(self, ch)
            s.task = asyncio.create_task(s.run())
            slog.info("%s: indul (%s)", ch.name, peer or "-")
        return s

    async def x_epg(self, request):
        self.ext_check(request)
        return await self.h_epg(request)

    async def x_logo(self, request):
        self.ext_check(request)
        return await self.h_logo(request)

    async def x_channel(self, request):
        self.ext_check(request)
        return await self.h_channel(request)

    async def h_status(self, request):
        act = []
        for c in self.channels.values():
            s = c.session
            if s and not s.stopped:
                act.append({"id": c.id, "name": c.name, "clients": s.clients,
                            "uptime_s": int(time.time() - s.started),
                            "mb_in": round(s.bytes_in / 1e6, 1), "errors": s.errors,
                            "segments": len(s.segs)})
        return web.json_response({"channels": len(self.channels), "active": act,
                                  "epg_age_s": int(time.time() - self.epg_time) if self.epg_time else None})

    # ---- életciklus
    async def on_start(self, app):
        self.http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=256, limit_per_host=128, ttl_dns_cache=300))
        await self.bootstrap_catalog()
        self.health.load()
        self.sync_channels()
        try:
            await self.load_epg()
        except Exception as e:  # noqa: BLE001
            elog.warning("epg betöltés hiba: %s", e)
        app["refresher"] = asyncio.create_task(self.refresher())
        app["sampler"] = asyncio.create_task(self.sampler())
        app["scheduler"] = asyncio.create_task(self.health.scheduler())
        await hdhr.start_discovery(self, app)
        await hdhr.start_mirror(self, app)

    async def on_stop(self, app):
        for k in ("refresher", "sampler", "scheduler"):
            app[k].cancel()
        await hdhr.stop_discovery(self, app)
        await hdhr.stop_mirror(self, app)
        for c in self.channels.values():
            if c.session:
                c.session.stopped = True
        await self.http.close()

    def build(self):
        import admin
        ACC = logging.getLogger("tsproxy.access")
        if not ACC.filters:
            class _NoApi(logging.Filter):
                def filter(self, rec):
                    m = rec.getMessage()
                    return "/api/" not in m and "/admin" not in m
            ACC.addFilter(_NoApi())
            ACC.propagate = True
        app = web.Application(middlewares=[admin.make_middleware(self)])
        admin.setup(self, app)
        hdhr.setup(self, app)
        self.access_log = ACC
        app.add_routes([
            web.get("/logo/{cid}.png", self.h_logo),
            web.get("/tv/{tok}/playlist.m3u", self.x_playlist),
            web.get("/tv/{tok}/epg.xml", self.x_epg),
            web.get("/tv/{tok}/epg.xml.gz", self.x_epg),
            web.get("/tv/{tok}/logo/{cid}.png", self.x_logo),
            web.get("/tv/{tok}/ch/{cid}.ts", self.x_channel),
            web.get("/playlist.m3u", self.h_playlist),
            web.get("/playlist.m3u8", self.h_playlist),
            web.get("/epg.xml", self.h_epg),
            web.get("/epg.xml.gz", self.h_epg),
            web.get("/ch/{cid}.ts", self.h_channel),
            web.get("/status", self.h_status),
            web.get("/health", lambda r: web.Response(text="ok")),
        ])
        app.on_startup.append(self.on_start)
        app.on_cleanup.append(self.on_stop)
        return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default=env("TSP_LISTEN", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(env("TSP_PORT", "8095")))
    p.add_argument("--data", default=env("TSP_DATA", "./data"))
    p.add_argument("--pipeline-dir", default=env("TSP_PIPELINE_DIR", ""),
                   help="a régi pipeline könyvtára (katalog.json, kimenet/, status.json, meres.json); üresen a --data alatt él a katalógus")
    p.add_argument("--catalog", default=env("TSP_CATALOG", ""), help="katalógus-fájl útvonala (felülírja az alapértéket)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger().addHandler(RING)
    cfg = Cfg(a)
    cfg.load_settings()
    apply_logging(cfg)
    if not cfg.admin_password:
        log.warning("TSP_ADMIN_PASSWORD nincs beállítva: az admin felület jelszó nélkül elérhető!")
    a = App(cfg)
    webapp = a.build()
    web.run_app(webapp, host=cfg.listen, port=cfg.port,
                access_log=a.access_log, access_log_format='"%r" %a -> %s %bb %Tfs', print=None)


if __name__ == "__main__":
    main()
