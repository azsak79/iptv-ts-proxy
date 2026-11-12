"""Forrás-ellenőrzés, legjobb forrás kiválasztása, élő átváltás, események.

A régi pipeline `best.py` + `refresh.py check_quick` logikájának átvétele (aszinkron), kiegészítve:
  - párhuzamos (Range) méréssel: a proxy párhuzamosan tölt, ezért egy egykapcsolatos mérésnél
    „instabilnak” látszó forrás is használható lehet;
  - élő átváltással lejátszás közben;
  - eseményekkel (kiesés, visszatérés, váltás) és webhook-értesítéssel.
"""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import aiohttp

log = logging.getLogger("tsproxy.health")
PROBE_UA = "Mozilla/5.0"
SEG_SIZE_CAP = 12 << 20
MIN_SEG_BYTES = 200_000


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


def _lines(text):
    return [x.strip() for x in text.splitlines() if x.strip()]


def eff(r):
    """Effektív margin: az egykapcsolatos és a párhuzamos mérés közül a jobb."""
    return max(r.get("margin") or 0.0, r.get("pmargin") or 0.0)


def slug_and_file(url):
    """`/live/<slug>/<index|playlist>.m3u8` -> (slug, fájlnév); egyébként (None, None).

    A fájlnév is kell, hogy a jelölt ugyanazt az alakot használja a többi hoston
    (a `playlist.m3u8`-as forrásoknak eddig nem volt cross-host jelöltje).
    """
    m = re.match(r"^/live/([^/]+)(?:/(index\.m3u8|playlist\.m3u8))?$", urlsplit(url).path)
    if not m:
        return None, None
    return m.group(1), (m.group(2) or "index.m3u8")


def _asc_slug(s):
    """Névtöredék -> kisbetűs, ASCII, aláhúzásos slug-jelölt (Spíler1.TV.hu -> spiler1_tv_hu)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "_", s.lower())
    return re.sub(r"_+", "_", s).strip("_")


SLUG_SUFFIXES = ("_hun", "_hu", "_atk", "_hd", "_tv_hu")


class Health:
    def __init__(self, app):
        self.A = app
        self.results = {}        # cid -> {url: eredmény}
        self.info = {}           # cid -> {state, alive, url, eff, t, mode}
        self.history = {}        # cid -> deque([{t, state, alive, eff, host}])
        self.events = deque(maxlen=1000)
        self.running = False
        self.discovery = {"last_run": {}, "candidates": []}
        self.discovery_running = False
        self.progress = {"done": 0, "total": 0, "mode": "", "started": 0, "source": ""}
        self.last_run = {}
        self.last_failover = {}
        self._sems = {}
        self._gap = {}
        self._ev_n = 0
        self._pm_lock = asyncio.Lock()

    # ------------------------------------------------------------ perzisztencia
    @property
    def cfg(self):
        return self.A.cfg

    def _path(self, name):
        return os.path.join(self.cfg.data, name)

    def load(self):
        try:
            d = json.load(open(self._path("health.json"), encoding="utf-8"))
            self.results = d.get("results", {})
            self.info = d.get("info", {})
            self.history = {k: deque(v, maxlen=120) for k, v in d.get("history", {}).items()}
            self.last_run = d.get("last_run", {})
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("health.json hibás, kihagyva: %s", e)
        try:
            self.discovery = json.load(open(self._path("discovery.json"), encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.discovery = {"last_run": {}, "candidates": []}
        try:
            with open(self._path("events.jsonl"), encoding="utf-8") as f:
                for line in f.readlines()[-1000:]:
                    try:
                        ev = json.loads(line)
                        self._ev_n = max(self._ev_n, ev.get("i", 0))
                        self.events.append(ev)
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass

    def save_discovery(self):
        os.makedirs(self.cfg.data, exist_ok=True)
        tmp = self._path("discovery.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.discovery, f, ensure_ascii=False)
        os.replace(tmp, self._path("discovery.json"))

    def save(self):
        os.makedirs(self.cfg.data, exist_ok=True)
        tmp = self._path("health.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"results": self.results, "info": self.info, "last_run": self.last_run,
                       "history": {k: list(v) for k, v in self.history.items()}}, f, ensure_ascii=False)
        os.replace(tmp, self._path("health.json"))

    # ------------------------------------------------------------ események
    def add_event(self, typ, ch, detail=""):
        self._ev_n += 1
        ev = {"i": self._ev_n, "t": time.time(), "type": typ, "id": ch["id"] if ch else "",
              "name": ch["name"] if ch else "", "detail": detail}
        self.events.append(ev)
        try:
            with open(self._path("events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass
        log.info("esemény: %s %s %s", typ, ev["name"], detail)
        self.A.notify(ev)

    # ------------------------------------------------------------ állapot-lekérdezések
    def alive(self, cid):
        i = self.info.get(cid)
        return True if i is None else bool(i.get("alive", True))

    def uptime(self, cid):
        h = list(self.history.get(cid, []))
        if not h:
            return None
        return round(100 * sum(1 for x in h if x.get("alive")) / len(h))

    def summary(self):
        cnt = {"jo": 0, "hatar": 0, "instabil": 0, "halott": 0, "ismeretlen": 0}
        for c in self.A.catalog.channels:
            if c.get("enabled") is False:
                continue
            st = (self.info.get(c["id"]) or {}).get("state", "ismeretlen")
            cnt[st] = cnt.get(st, 0) + 1
        return {"counts": cnt, "running": self.running, "progress": self.progress, "last_run": self.last_run,
                "next": self.next_scheduled()}

    def next_scheduled(self):
        if not self.cfg.check_enabled:
            return None
        try:
            hh, mm = [int(x) for x in self.cfg.check_time.split(":")]
        except ValueError:
            return None
        now = time.time()
        lt = time.localtime(now)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        if t <= now or self.last_run.get("scheduled_date") == time.strftime("%F", lt):
            t += 86400
        return t

    # ------------------------------------------------------------ hálózat (udvarias)
    def _sem(self, host):
        s = self._sems.get(host)
        if s is None:
            s = self._sems[host] = asyncio.Semaphore(max(1, self.cfg.host_conc))
        return s

    @asynccontextmanager
    async def _polite(self, host):
        async with self._sem(host):
            gap = self._gap.get(host, 0.0) + self.cfg.host_gap - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                yield
            finally:
                self._gap[host] = time.monotonic()

    async def _text(self, url):
        to = self.cfg.probe_timeout
        async with self._polite(urlsplit(url).netloc):
            async with self.A.http.get(url, headers={"User-Agent": PROBE_UA},
                                       timeout=aiohttp.ClientTimeout(total=to,
                                                                     sock_connect=self.cfg.probe_connect)) as r:
                if r.status >= 400:
                    raise HTTPError(r.status)
                body = bytearray()
                async for chunk in r.content.iter_chunked(16384):
                    body += chunk
                    if len(body) >= 512 << 10:
                        break
                return body.decode("utf-8", "replace")

    # ------------------------------------------------------------ egy forrás mérése
    async def probe(self, url, measure=True):
        res = {"url": url, "ok": False, "quick_ok": False, "margin": 0.0, "error": None, "t": time.time()}
        t0 = time.monotonic()
        try:
            body = await self._text(url)
        except HTTPError as e:
            res["error"] = f"http {e.status}"
            return res
        except Exception as e:  # noqa: BLE001
            res["error"] = type(e).__name__
            return res
        if "#EXTM3U" not in body:
            res["error"] = "nem m3u8"
            return res
        res["connect_ms"] = round((time.monotonic() - t0) * 1000)

        media_url, media = url, body
        if "#EXT-X-STREAM-INF" in body:
            best, best_bw = None, -1
            ls = _lines(body)
            for i, l in enumerate(ls):
                if l.startswith("#EXT-X-STREAM-INF"):
                    m = re.search(r"BANDWIDTH=(\d+)", l)
                    bw = int(m.group(1)) if m else 0
                    if bw > best_bw:
                        for nxt in ls[i + 1:]:
                            if not nxt.startswith("#"):
                                best, best_bw = nxt, bw
                                break
            if not best:
                res["error"] = "ures master"
                return res
            var = urljoin(url, best)
            res["variant"] = var
            res["bitrate"] = best_bw or None
            try:
                media = await self._text(var)
            except Exception as e:  # noqa: BLE001
                res["error"] = f"variant {type(e).__name__}"
                return res
            media_url = var

        if "aviso" in media or "/aviso" in (res.get("variant") or ""):
            res["error"] = "placeholder (aviso)"
            res["placeholder"] = True
            return res

        segs, durs, last_dur = [], [], None
        for l in _lines(media):
            if l.startswith("#EXTINF"):
                m = re.search(r"#EXTINF:\s*([\d.]+)", l)
                last_dur = float(m.group(1)) if m else None
            elif not l.startswith("#") and (l.endswith(".ts") or ".ts?" in l or l.endswith(".m4s")):
                segs.append(urljoin(media_url, l))
                durs.append(last_dur or 0.0)
        if not segs:
            res["error"] = "nincs szegmens"
            return res
        res["quick_ok"] = True
        if not measure:
            res["ok"] = None            # nem mért
            return res

        cap = self.cfg.seg_time_cap
        got, dt, seg_dur, seg_url = 0, 0.0, 0.0, None
        for idx in range(len(segs) - 1, max(len(segs) - 2, -1), -1):
            seg_url, seg_dur = segs[idx], durs[idx] or 0.0
            got = 0
            try:
                async with self._polite(urlsplit(seg_url).netloc):
                    t_start = time.monotonic()       # a sorbanállás után indul az időmérés
                    async with self.A.http.get(
                            seg_url, headers={"User-Agent": PROBE_UA},
                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.cfg.probe_connect,
                                                          sock_read=self.cfg.probe_timeout)) as r:
                        if r.status >= 400:
                            raise HTTPError(r.status)
                        async for chunk in r.content.iter_chunked(1 << 16):
                            got += len(chunk)
                            if got >= SEG_SIZE_CAP or time.monotonic() - t_start > cap:
                                break
                dt = time.monotonic() - t_start
            except Exception as e:  # noqa: BLE001
                res["error"] = f"segment {type(e).__name__}"
                res["seg_bytes"] = got
                return res
            if got >= MIN_SEG_BYTES:
                break
        dt = max(dt, 0.001)
        if got < MIN_SEG_BYTES:
            res["error"] = f"rovid szegmens ({got} B)"
            res["seg_bytes"] = got
            return res
        seg_dur = seg_dur or 6.0
        margin = seg_dur / dt
        res.update(ok=True, margin=round(margin, 2), seg_bytes=got, seg_duration=round(seg_dur, 2),
                   download_s=round(dt, 2), speed_kbps=round(got / dt / 1024, 1))
        if margin < self.cfg.min_margin and self.cfg.parallel_probe and seg_url:
            try:
                pm = await self._pmeasure(seg_url, seg_dur)
            except Exception:  # noqa: BLE001
                pm = None
            if pm is not None:
                res["pmargin"] = pm
        return res

    async def _pmeasure(self, seg_url, seg_dur):
        """Ugyanaz a szegmens párhuzamos Range-részekben (ahogy a proxy tölti): effektív margin."""
        chunk = self.cfg.range_chunk if self.cfg.range_chunk >= 65536 else 262144
        cap = self.cfg.seg_time_cap + 4
        tmo = aiohttp.ClientTimeout(total=cap, sock_connect=self.cfg.probe_connect)
        conc = asyncio.Semaphore(max(1, min(self.cfg.range_conc, 8)))

        async def part(a, b):
            async with conc:
                async with self.A.http.get(seg_url, headers={"User-Agent": PROBE_UA, "Range": f"bytes={a}-{b}"},
                                           timeout=tmo) as r:
                    if r.status != 206:
                        return None, None
                    total = int(r.headers.get("Content-Range", "bytes 0-0/0").rsplit("/", 1)[1] or 0)
                    return len(await r.read()), total

        # egyesével, hogy a párhuzamos mérések ne zavarják egymást (a sorbanállás nem számít bele az időbe)
        async with self._pm_lock:
            t0 = time.monotonic()
            n0, total = await part(0, chunk - 1)
            if not n0 or not total or total < MIN_SEG_BYTES:
                return None
            total = min(total, SEG_SIZE_CAP)
            rest = [(a, min(a + chunk, total) - 1) for a in range(chunk, total, chunk)]
            got = n0
            if rest:
                outs = await asyncio.gather(*[part(a, b) for a, b in rest], return_exceptions=True)
                got += sum(o[0] for o in outs if isinstance(o, tuple) and o[0])
            dt = max(time.monotonic() - t0, 0.001)
        if got < min(total, MIN_SEG_BYTES):
            return None
        return round(seg_dur / dt, 2)

    # ------------------------------------------------------------ jelöltek, rangsor
    def slug_variants(self, ch):
        """Minden lehetséges slug-változat a csatornához: a katalógus-URL-ek slugjai,
        név/tvg_id alapú jelöltek és az utótag-variánsok (_hun/_atk/_hd levétele, hozzáadása).
        A provider relay-IP-nként eltérő slugot használhat, ezért minden változatot
        megpróbálunk minden relay hoston (a `slug_variants_max` korlátig)."""
        seen, out = set(), []

        def add(s):
            s = _asc_slug(s)
            if s and 2 <= len(s) <= 60 and s not in seen:
                seen.add(s)
                out.append(s)

        for u in ch.get("urls") or []:
            add(slug_and_file(u)[0])
        add(ch.get("tvg_id") or "")
        add((ch.get("tvg_id") or "").split(".")[0])
        add(ch.get("name") or "")
        for s in list(out):                      # utótag-variánsok a meglévőkre
            core = s
            for suf in SLUG_SUFFIXES:
                if core.endswith(suf):
                    core = core[:-len(suf)]
                    break
            if core and core != s:
                add(core)
                for suf in ("_hun", "_atk", "_hd"):
                    if not core.endswith(suf):
                        add(core + suf)
        return out[:max(1, self.cfg.slug_variants_max)]

    def candidates(self, ch):
        """Aktuális (nem letiltott) URL-ek + ugyanazon slug és fájlnév a többi relay hoston
        + minden slug-változat minden relay hoston (a provider IP-nként eltérő slugot használhat)."""
        meta = ch.get("url_meta") or {}
        out = []

        def add(u):
            if u and u not in out:
                out.append(u)

        files = []
        for u in ch.get("urls") or []:
            if (meta.get(u) or {}).get("disabled"):
                continue
            add(u)
            s, fn = slug_and_file(u)
            if fn and fn not in files:
                files.append(fn)
            if s and self.cfg.auto_candidates:
                for h in self.cfg.relay_list():
                    add(f"{h}/live/{s}/{fn}")
        if self.cfg.auto_candidates and self.cfg.slug_variants:
            for s in self.slug_variants(ch):
                for fn in (files or ["index.m3u8"]):
                    for h in self.cfg.relay_list():
                        add(f"{h}/live/{s}/{fn}")
        return out

    def failover_candidates(self, ch, cur_url):
        """Átváltási jelöltek prioritással: 1) ugyanazon slug (+ fájl) a többi relay hoston,
        2) slug/fájl-változatok minden hoston, 3) a csatorna további saját URL-jei.
        Legfeljebb `failover_candidates` darab (az azonos slug a másik IP-n a legvalószínűbb javítás)."""
        out = []

        def add(u):
            if u and u != cur_url and u not in out:
                out.append(u)

        slugs, files = [], []
        for u in ch.get("urls") or []:
            s, fn = slug_and_file(u)
            if s and s not in slugs:
                slugs.append(s)
            if fn and fn not in files:
                files.append(fn)
        s0 = slug_and_file(cur_url)[0] or (slugs[0] if slugs else None)
        if self.cfg.auto_candidates and s0:
            for fn in (files or ["index.m3u8"]):
                for h in self.cfg.relay_list():
                    add(f"{h}/live/{s0}/{fn}")
            if self.cfg.slug_variants:
                for s in self.slug_variants(ch):
                    for fn in (files or ["index.m3u8"]):
                        for h in self.cfg.relay_list():
                            add(f"{h}/live/{s}/{fn}")
        for u in ch.get("urls") or []:
            add(u)
        return out[:max(1, self.cfg.failover_candidates)]

    def rank(self, results, prefer=None):
        """Legjobb mért forrás. `prefer` = előnyben részesített slugok: enyhén (5%) nagyobb
        effektív margint kapnak, hogy a mérési zaj ne a spekulatív slug-változat javára döntsön."""
        ok = [r for r in results if r.get("ok") and eff(r) >= self.cfg.min_margin]
        if not ok:
            return None
        ok.sort(key=lambda r: (-eff(r) * (1.05 if slug_and_file(r["url"])[0] in (prefer or ()) else 1.0),
                               r.get("connect_ms") or 9999))
        return ok[0]

    def classify(self, results):
        alive = any(r.get("quick_ok") for r in results)
        oks = [r for r in results if r.get("ok")]
        best = max((eff(r) for r in oks), default=0.0)
        if not alive:
            return "halott", False, best
        if not oks:
            return "instabil", True, best
        if best >= 1.5:
            return "jo", True, best
        if best >= self.cfg.min_margin:
            return "hatar", True, best
        return "instabil", True, best

    # ------------------------------------------------------------ futtatás
    async def run(self, scope="all", mode="measure", ids=None, source="kézi"):
        """scope: all | ids. mode: quick (csak lejátszási lista) | measure (szegmensmérés + kiválasztás)."""
        if self.running:
            return False
        self.running = True
        t0 = time.monotonic()
        A = self.A
        try:
            chans = [c for c in A.catalog.channels if c.get("enabled") is not False]
            if scope == "ids":
                want = set(ids or [])
                chans = [c for c in A.catalog.channels if c["id"] in want]
            self.progress = {"done": 0, "total": len(chans), "mode": mode, "started": time.time(), "source": source}
            log.info("forrás-ellenőrzés indul (%s, %s): %d csatorna", mode, source, len(chans))
            sem = asyncio.Semaphore(max(1, self.cfg.check_workers))
            out = {}

            async def work(c):
                async with sem:
                    res = []
                    cache = self.results.get(c["id"]) or {}
                    reuse_s = max(0, self.cfg.probe_reuse) * 3600
                    now = time.time()
                    for u in self.candidates(c):
                        old = cache.get(u)
                        # friss, sikeres mérést nem mérünk újra (a slug-mátrix így nem nő meg napról napra);
                        # a hibákat viszont mindig újramérjük, hogy a visszatérést észrevegyük
                        if reuse_s and old and old.get("ok") and now - old.get("t", 0) < reuse_s:
                            res.append(old)
                            continue
                        res.append(await self.probe(u, measure=(mode != "quick")))
                    out[c["id"]] = res
                    self.progress["done"] += 1

            await asyncio.gather(*[work(c) for c in chans])
            changes = self._apply(chans, out, mode)
            dt = round(time.monotonic() - t0, 1)
            sm = self.summary()["counts"]
            self.last_run = {"t": time.time(), "mode": mode, "source": source, "secs": dt, "channels": len(chans),
                             "changed": len(changes), "counts": sm,
                             "scheduled_date": time.strftime("%F") if source == "ütemezett" else
                             self.last_run.get("scheduled_date", "")}
            self.save()
            A.write_legacy(measured=out if mode != "quick" else None, dt=dt, mode=mode)
            A.sync_channels()
            self.add_event("check_done", None, f"{mode}, {len(chans)} csatorna, {dt} mp, váltás: {len(changes)}, "
                           f"jó {sm['jo']} / határ {sm['hatar']} / instabil {sm['instabil']} / halott {sm['halott']}")
            return True
        finally:
            self.running = False

    def _apply(self, chans, out, mode):
        """Eredmények rögzítése, állapotváltások, forrásváltás (hiszterézissel). Visszaadja a váltásokat."""
        A, cfg = self.A, self.cfg
        changes, new_urls = [], {}
        for c in chans:
            cid = c["id"]
            res = out.get(cid, [])
            store = self.results.setdefault(cid, {})
            for r in res:
                store[r["url"]] = r
            for u in list(store):
                if u not in {r["url"] for r in res} and time.time() - store[u].get("t", 0) > 14 * 86400:
                    del store[u]
            state, alive, best_eff = self.classify(res)
            prev = self.info.get(cid) or {}
            if mode == "quick" and alive:
                state = prev.get("state") if prev.get("state") not in (None, "halott") else "ismeretlen"
                best_eff = prev.get("eff", 0)
            self.info[cid] = {"state": state, "alive": alive, "eff": round(best_eff, 2), "t": time.time(), "mode": mode,
                              "url": (c.get("urls") or [None])[0]}
            self.history.setdefault(cid, deque(maxlen=120)).append(
                {"t": time.time(), "state": state, "alive": alive, "eff": round(best_eff, 2)})
            if prev and prev.get("alive", True) and not alive:
                self.add_event("dead", c, "a csatorna egyik forrása sem elérhető")
            elif prev and not prev.get("alive", True) and alive:
                self.add_event("recovered", c, "újra elérhető")
            if mode == "quick" or c.get("pin"):
                continue
            prefer = {s for s, _ in (slug_and_file(u) for u in c.get("urls") or []) if s}
            best = self.rank(res, prefer=prefer)
            if not best:
                continue
            urls = list(c.get("urls") or [])
            cur_url = urls[0] if urls else None
            if best["url"] == cur_url:
                continue
            cur = next((r for r in res if r["url"] == cur_url), None)
            if cur and cur.get("ok") and eff(cur) >= cfg.min_margin and eff(best) < eff(cur) * cfg.switch_factor:
                continue                       # hiszterézis: nem elég jobb az új
            new_urls[cid] = [best["url"]] + [u for u in urls if u != best["url"]]
            changes.append((c, cur_url, best))
        if new_urls:
            A.catalog.set_urls_many(new_urls, "forrasvaltas")
            for c, old, best in changes:
                self.add_event("switched", c, f"{urlsplit(old).netloc if old else '–'} → {urlsplit(best['url']).netloc} "
                               f"(margin {eff(best)})")
        return changes


    # ------------------------------------------------------------ csatorna-felderítés
    def _known_slugs(self):
        """A katalógusban már ismert slugok: URL-slugok + név/tvg_id alapúak + variánsaik."""
        known = set()
        for c in self.A.catalog.channels:
            for u in c.get("urls") or []:
                sg, _ = slug_and_file(u)
                if sg:
                    known.add(sg)
            for sg in self.slug_variants(c):
                known.add(sg)
            for n in (c.get("name"), c.get("tvg_id")):
                sg = _asc_slug(n)
                if sg:
                    known.add(sg)
        return known

    def discovery_slugs(self):
        """Ismeretlen jelölt-slugok: a forrás-EPG csatornanevei + kézi lista, utótag-variánsokkal."""
        names = []
        if self.cfg.discovery_epg_names:
            for lst in (getattr(self.A, "epg_names", None) or {}).values():
                names.extend(lst)
        for line in (self.cfg.discovery_names or "").splitlines():
            if line.strip():
                names.append(line.strip())
        wl = os.path.join(self.cfg.data, "discovery-slugs.txt")
        try:
            with open(wl, encoding="utf-8") as f:
                names.extend(line.strip() for line in f if line.strip())
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("felderítés: a szólista nem olvasható (%s): %s", wl, e)

        cands, seen = [], set()

        def add(v):
            sg = _asc_slug(v)
            if not sg or not 2 <= len(sg) <= 60 or sg in seen:
                return
            seen.add(sg)
            cands.append(sg)
            core = sg
            for suf in SLUG_SUFFIXES:
                if core.endswith(suf):
                    core = core[:-len(suf)]
                    break
            if core and core != sg:
                for v2 in (core, core + "_hun", core + "_atk", core + "_hd"):
                    if v2 and v2 not in seen and 2 <= len(v2) <= 60:
                        seen.add(v2)
                        cands.append(v2)

        for n in names:
            add(n)
        known = self._known_slugs()
        return [s for s in cands if s not in known][:max(1, self.cfg.discovery_max)]

    def next_scheduled_discovery(self):
        if not self.cfg.discovery_enabled:
            return None
        try:
            hh, mm = [int(x) for x in self.cfg.discovery_time.split(":")]
        except ValueError:
            return None
        now = time.time()
        lt = time.localtime(now)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        if t <= now or self.discovery.get("last_run", {}).get("scheduled_date") == time.strftime("%F", lt):
            t += 86400
        return t

    async def discover(self, source="kézi"):
        """Új csatornák keresése a relay hostokon: ismeretlen slugokra quick mérés (csak
        playlist-érvényesség); a találatok jelöltlistába kerülnek, a katalógust nem írik."""
        if self.discovery_running or self.running:
            return False
        hosts = self.cfg.relay_list()
        if not hosts:
            log.info("felderítés: nincs relay host beállítva, kihagyva")
            return False
        self.discovery_running = True
        t0 = time.monotonic()
        try:
            slugs = self.discovery_slugs()
            log.info("csatorna-felderítés indul (%s): %d ismeretlen jelölt-slug, %d host", source, len(slugs), len(hosts))
            sem = asyncio.Semaphore(max(1, self.cfg.check_workers))
            found = []

            async def probe_slug(sg):
                async with sem:
                    for h in hosts:
                        for fn in ("index.m3u8", "playlist.m3u8"):
                            r = await self.probe(f"{h}/live/{sg}/{fn}", measure=False)
                            if r.get("quick_ok"):
                                found.append({"slug": sg, "host": h, "url": f"{h}/live/{sg}/{fn}"})
                                return

            await asyncio.gather(*[probe_slug(sg) for sg in slugs])
            dt = round(time.monotonic() - t0, 1)
            self.discovery = {"last_run": {"t": time.time(), "secs": dt, "source": source,
                                           "slugs": len(slugs), "found": len(found),
                                           "scheduled_date": time.strftime("%F") if source == "ütemezett"
                                           else self.discovery.get("last_run", {}).get("scheduled_date", "")},
                              "candidates": found[:200]}
            self.save_discovery()
            if found:
                names = ", ".join(f["slug"] for f in found[:8]) + ("…" if len(found) > 8 else "")
                self.add_event("discovered", None, f"{len(found)} új csatorna-jelölt: {names} ({source})")
            else:
                log.info("csatorna-felderítés: nincs új jelölt (%d slug, %.1fs)", len(slugs), dt)
            return True
        finally:
            self.discovery_running = False


    # ------------------------------------------------------------ élő átváltás
    async def failover(self, cid, cur_url, reason, quick=False, urgent=False, hard=False):
        """Lejátszás közbeni átváltás. A jelölteket frissen, párhuzamosan mérjük (a 12 órás
        gyorsítótár halott relay-re is átkapcsolt), a cooldown csak a SIKERES váltást
        indítja el — a sikertelen kísérlet után `failover_retry` múlva szabad az újrapróbálkozás.
        `quick=True` (első kapcsolódás vészforgalma): csak a lejátszási lista érvényességét
        nézi (nem mér margin-t), és az első működő jelöltre vált — másodpercek alatt.
        `urgent=True` a sikertelen-kísérlet várakozást is elengedi (a néző 30 mp-es ablaka
        miatt minden próbálkozásnak szabad azonnal lennie; a SIKER-cooldown érvényes marad).
        `hard=True`: a jelenlegi forrás teljesen halott (playlist-hibák / nincs új szegmens) – ekkor a
        siker-cooldown helyett csak a `failover_retry` várakozás él: a cooldown a jól működő források
        közti ide-oda váltást hivatott megakadályozni, nem azt, hogy egy halott forráson ragadjunk."""
        A, cfg = self.A, self.cfg
        ch = A.catalog.get(cid)
        if not ch or not cfg.failover_enabled:
            return None
        now = time.time()
        lf = self.last_failover.get(cid) or {}
        if not (urgent and not lf.get("ok")):
            wait = cfg.failover_cooldown if lf.get("ok") and not hard else cfg.failover_retry
            if now - lf.get("t", 0) < wait:
                if not lf.get("blocked_logged"):
                    lf["blocked_logged"] = True
                    log.info("%s: átváltás várakozik (%s, még %d mp)", ch["name"], reason,
                             wait - (now - lf.get("t", 0)))
                return None
        if ch.get("pin"):
            self.add_event("failover_blocked", ch, "rögzített forrás, nem váltok")
            return None
        cands = self.failover_candidates(ch, cur_url)
        if hard and cur_url:
            # a halott (nem válaszoló) host többi slug-változata csak időkorlátokat gyűjtene
            dead = urlsplit(cur_url).netloc
            cands = [u for u in cands if urlsplit(u).netloc != dead] or cands
        wsem = asyncio.Semaphore(max(1, cfg.failover_workers))

        async def measure(u):
            r = (self.results.get(cid) or {}).get(u)
            # csak a Friss eredmény fogadható el cache-ből: régebbi a host azóta meghalhatott
            if r and r.get("quick_ok") and time.time() - r.get("t", 0) < cfg.failover_fresh:
                return r
            async with wsem:
                r = await self.probe(u, measure=not quick)
            self.results.setdefault(cid, {})[u] = r
            return r

        s0 = slug_and_file(cur_url)[0]

        def good(r):
            # korai elfogadás: azonos slug (másik relay), és működik – a többi jelöltet nem várjuk ki
            if slug_and_file(r["url"])[0] != s0:
                return False
            return r.get("quick_ok") if quick else (r.get("ok") and eff(r) >= cfg.min_margin)

        tasks = [asyncio.create_task(measure(u)) for u in cands]
        best = None
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    r = await fut
                except Exception:  # noqa: BLE001
                    continue
                if good(r):
                    best = r
                    break
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        if best is None:
            res = [t.result() for t in tasks if t.done() and not t.cancelled() and not t.exception()]
            if quick:
                best = next((r for r in res if r.get("quick_ok")), None)
            else:
                best = self.rank(res, prefer={s0} if s0 else None) or next((r for r in res if r.get("quick_ok")), None)
        if not best:
            self.last_failover[cid] = {"t": now, "ok": False}
            self.add_event("failover_failed", ch, f"nincs használható másik forrás ({reason})")
            return None
        urls = [best["url"]] + [u for u in ch.get("urls", []) if u != best["url"]]
        A.catalog.set_urls(cid, urls, "failover")
        self.last_failover[cid] = {"t": time.time(), "ok": True}
        self.add_event("failover", ch, f"{reason}: {urlsplit(cur_url).netloc if cur_url else '–'} → "
                       f"{urlsplit(best['url']).netloc}")
        self.save()
        A.sync_channels()
        A.write_legacy()
        return best["url"]

    # ------------------------------------------------------------ ütemező
    async def scheduler(self):
        while True:
            await asyncio.sleep(30)
            try:
                if not self.cfg.check_enabled or self.running:
                    continue
                hh, mm = [int(x) for x in self.cfg.check_time.split(":")]
                lt = time.localtime()
                today = time.strftime("%F", lt)
                late = (lt.tm_hour * 60 + lt.tm_min) - (hh * 60 + mm)     # percek az időpont óta
                # az időpontban, vagy legfeljebb 2 órán belül pótolva (ha épp le volt állva a szolgáltatás)
                if 0 <= late <= 120 and self.last_run.get("scheduled_date") != today:
                    if any(c.get("urls") for c in self.A.catalog.channels):
                        await self.run("all", "measure", source="ütemezett")
                    self.last_run["scheduled_date"] = today
                    self.save()
                # csatorna-felderítés (a napi ellenőrzés után, külön időpontban)
                if self.cfg.discovery_enabled and not self.running and not self.discovery_running \
                        and self.cfg.relay_list():
                    dh, dm = [int(x) for x in self.cfg.discovery_time.split(":")]
                    dlate = (lt.tm_hour * 60 + lt.tm_min) - (dh * 60 + dm)
                    if 0 <= dlate <= 120 and self.discovery.get("last_run", {}).get("scheduled_date") != today:
                        await self.discover(source="ütemezett")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("ütemező hiba: %s", e)
