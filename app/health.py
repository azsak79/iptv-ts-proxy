"""Forrás-ellenőrzés, legjobb forrás kiválasztása, élő átváltás, események.

A régi pipeline `best.py` + `refresh.py check_quick` logikájának átvétele (aszinkron), kiegészítve:
  - párhuzamos (Range) méréssel: a proxy párhuzamosan tölt, ezért egy egykapcsolatos mérésnél
    „instabilnak” látszó forrás is használható lehet;
  - élő átváltással lejátszás közben;
  - eseményekkel (kiesés, visszatérés, váltás) és webhook-értesítéssel.
"""
import asyncio
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import aiohttp

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


def slug_of(url):
    m = re.match(r"^/live/([^/]+)/(?:index\.m3u8)?$", urlsplit(url).path)
    return m.group(1) if m else None


class Health:
    def __init__(self, app):
        self.A = app
        self.results = {}        # cid -> {url: eredmény}
        self.info = {}           # cid -> {state, alive, url, eff, t, mode}
        self.history = {}        # cid -> deque([{t, state, alive, eff, host}])
        self.events = deque(maxlen=1000)
        self.running = False
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
            self.A.log.warning("health.json hibás, kihagyva: %s", e)
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
        self.A.log.info("esemény: %s %s %s", typ, ev["name"], detail)
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
                                       timeout=aiohttp.ClientTimeout(total=to)) as r:
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
                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.cfg.probe_timeout,
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
        tmo = aiohttp.ClientTimeout(total=cap, sock_connect=self.cfg.probe_timeout)
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
    def candidates(self, ch):
        """Aktuális (nem letiltott) URL-ek + ugyanazon slug a többi relay hoston."""
        meta = ch.get("url_meta") or {}
        out = []
        for u in ch.get("urls") or []:
            if (meta.get(u) or {}).get("disabled"):
                continue
            if u not in out:
                out.append(u)
            s = slug_of(u)
            if s and self.cfg.auto_candidates:
                for h in self.cfg.relay_list():
                    cand = f"{h}/live/{s}/index.m3u8"
                    if cand not in out:
                        out.append(cand)
        return out

    def rank(self, results):
        ok = [r for r in results if r.get("ok") and eff(r) >= self.cfg.min_margin]
        if not ok:
            return None
        ok.sort(key=lambda r: (-eff(r), r.get("connect_ms") or 9999))
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
            A.log.info("forrás-ellenőrzés indul (%s, %s): %d csatorna", mode, source, len(chans))
            sem = asyncio.Semaphore(max(1, self.cfg.check_workers))
            out = {}

            async def work(c):
                async with sem:
                    res = []
                    for u in self.candidates(c):
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
            best = self.rank(res)
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

    # ------------------------------------------------------------ élő átváltás
    async def failover(self, cid, cur_url, reason):
        A, cfg = self.A, self.cfg
        ch = A.catalog.get(cid)
        if not ch or not cfg.failover_enabled:
            return None
        now = time.time()
        if now - self.last_failover.get(cid, 0) < cfg.failover_cooldown:
            return None
        self.last_failover[cid] = now
        if ch.get("pin"):
            self.add_event("failover_blocked", ch, "rögzített forrás, nem váltok")
            return None
        res = []
        for u in [x for x in self.candidates(ch) if x != cur_url][:6]:
            r = (self.results.get(cid) or {}).get(u)
            if not r or now - r.get("t", 0) > 12 * 3600 or not r.get("quick_ok"):
                r = await self.probe(u, measure=True)
                self.results.setdefault(cid, {})[u] = r
            res.append(r)
        best = self.rank(res) or next((r for r in res if r.get("quick_ok")), None)
        if not best:
            self.add_event("failover_failed", ch, f"nincs használható másik forrás ({reason})")
            return None
        urls = [best["url"]] + [u for u in ch.get("urls", []) if u != best["url"]]
        A.catalog.set_urls(cid, urls, "failover")
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
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.A.log.warning("ütemező hiba: %s", e)
