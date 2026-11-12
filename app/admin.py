"""Admin felület és JSON API az iptv-ts-proxyhoz (/admin, /api/*)."""
import asyncio
import base64
import collections
import hmac
import logging
import os
import sys
import time
import urllib.parse
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

# a már futó proxy-modul (szkriptként __main__), hogy ne töltődjön be kétszer (RING, log, SPEC stb.)
# a __main__ élvez elsőbbséget: az ő RING-jét köti a logging be – a másolatok RING-je üres marad
_m = sys.modules.get("__main__")
P = _m if hasattr(_m, "RING") else sys.modules.get("proxy")
import hdhr  # noqa: E402
from catalog import CatalogError  # noqa: E402

alog = logging.getLogger("tsproxy.admin")
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")


def make_middleware(A):
    @web.middleware
    async def mw(request, handler):
        p = request.path
        protected = p == "/" or p.startswith("/admin") or p.startswith("/api")
        if protected and A.cfg.admin_password:
            ok = False
            h = request.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    ok = hmac.compare_digest(pw.encode(), A.cfg.admin_password.encode())
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                await asyncio.sleep(0.5)          # brute force lassítása
                return web.Response(status=401, text="Hitelesítés szükséges",
                                    headers={"WWW-Authenticate": 'Basic realm="iptv-ts-proxy"'})
        if request.method == "POST" and p.startswith("/api") and request.content_type != "application/json":
            return web.json_response({"error": "Content-Type: application/json szükséges"}, status=400)
        try:
            return await handler(request)
        except CatalogError as e:
            return web.json_response({"error": str(e)}, status=400)
    return mw


def setup(A, app):
    cat, H = A.catalog, A.health

    def jerr(msg, status=400):
        return web.json_response({"error": msg}, status=status)

    def changed(epg=False):
        """Katalógus-módosítás után: futó csatornák, régi fájlok, (EPG) frissítése."""
        A.sync_channels()
        A.write_legacy()
        if epg:
            asyncio.create_task(_reload_epg())

    async def _reload_epg():
        try:
            await A.load_epg()
        except Exception as e:  # noqa: BLE001
            alog.warning("EPG újraolvasás hiba: %s", e)

    async def body(request):
        try:
            return await request.json()
        except Exception:  # noqa: BLE001
            raise CatalogError("érvénytelen JSON") from None

    # ------------------------------------------------------------ oldalak
    async def h_index(request):
        raise web.HTTPFound("/admin")

    async def h_admin(request):
        return web.FileResponse(HTML, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ állapot
    async def h_status(request):
        now = time.time()
        sessions, viewers, total_kbps = [], 0, 0
        for c in A.channels.values():
            s = c.session
            if not s or s.stopped:
                continue
            kb = s.kbps()
            viewers += s.clients
            total_kbps += kb
            cur, _, _ = A.now_next(c.tvg_id)
            sessions.append({
                "id": c.id, "name": c.name, "num": c.num, "upstream": s.cur_url,
                "uptime_s": int(now - s.started), "mb_in": round(s.bytes_in / 1e6, 1),
                "errors": s.errors, "segments": len(s.segs), "kbps": kb, "series": s.kbps_series()[-60:],
                "now": cur[2] if cur else "", "fail_streak": s.fail_streak,
                "clients": [{"id": r["id"], "ip": r["ip"], "ua": r["ua"], "age_s": int(now - r["since"]),
                             "mb": round(r["bytes"] / 1e6, 1)} for r in s.client_list.values()],
            })
        sessions.sort(key=lambda x: x["name"].lower())
        hs = H.summary()
        return web.json_response({
            "version": P.VERSION, "now": now, "uptime_s": int(now - A.started),
            "channels": len(cat.channels), "streamable": len(A.channels), "viewers": viewers, "kbps": total_kbps,
            "sessions": sessions,
            "catalog": {"path": cat.path, "updated": cat.updated},
            "health": {"counts": hs["counts"], "running": hs["running"], "progress": hs["progress"],
                       "last_run": hs["last_run"], "next": hs["next"]},
            "epg": {"age_s": int(now - A.epg_time) if A.epg_time else None, "channels": A.epg_stats[0],
                    "programmes": A.epg_stats[1], "source": A.epg_used},
            "auth": bool(A.cfg.admin_password),
        })

    # ------------------------------------------------------------ katalógus
    def view(c):
        cid = c["id"]
        s = (A.channels.get(cid).session if cid in A.channels else None)
        active = bool(s and not s.stopped)
        cur, nxt, n = A.now_next(c.get("tvg_id", ""))
        info = H.info.get(cid) or {}
        res = H.results.get(cid) or {}
        ch = A.channels.get(cid)
        return {
            "id": cid, "name": c["name"], "chno": c.get("chno", ""), "group": c.get("group", ""),
            "logo": c.get("logo", ""), "tvg_id": c.get("tvg_id", ""), "urls": c.get("urls", []),
            "url_meta": c.get("url_meta") or {}, "enabled": c.get("enabled") is not False, "pin": c.get("pin", ""),
            "megjegyzes": c.get("megjegyzes", ""),
            "active": active, "clients": s.clients if active else 0, "stream_url": f"/ch/{cid}.ts",
            "streamable": cid in A.channels, "has_logo": bool(c.get("logo") or (ch and ch.logo_path(A.cfg.logos_dir))),
            "logo_file": bool(ch and ch.logo_path(A.cfg.logos_dir)),
            "state": info.get("state", "ismeretlen"), "alive": info.get("alive"), "eff": info.get("eff"),
            "checked": info.get("t"), "uptime": H.uptime(cid),
            "results": {u: {k: r.get(k) for k in ("ok", "quick_ok", "margin", "pmargin", "error", "t", "connect_ms",
                                                   "speed_kbps", "bitrate", "placeholder")} for u, r in res.items()},
            "epg_count": n, "now": {"title": cur[2], "start": cur[0], "stop": cur[1]} if cur else None,
            "next": {"title": nxt[2], "start": nxt[0]} if nxt else None,
        }

    async def h_catalog(request):
        return web.json_response({"channels": [view(c) for c in cat.channels], "groups": cat.groups(),
                                  "updated": cat.updated, "history": len(cat.history())})

    async def h_cat_add(request):
        d = await body(request)
        c = cat.add(d.get("fields", d), d.get("position"))
        changed(epg=bool(c.get("tvg_id")))
        return web.json_response({"ok": True, "id": c["id"]})

    async def h_cat_update(request):
        cid = request.match_info["cid"]
        d = await body(request)
        f = d.get("fields", d)
        cat.update(cid, f)
        changed(epg="tvg_id" in f or "enabled" in f)
        return web.json_response({"ok": True})

    async def h_cat_delete(request):
        c = cat.delete(request.match_info["cid"])
        changed(epg=True)
        alog.info("csatorna törölve (admin): %s", c["name"])
        return web.json_response({"ok": True})

    async def h_cat_reorder(request):
        cat.reorder((await body(request)).get("ids", []))
        changed()
        return web.json_response({"ok": True})

    async def h_cat_renumber(request):
        cat.renumber()
        changed()
        return web.json_response({"ok": True})

    async def h_cat_bulk(request):
        d = await body(request)
        n = cat.bulk(d.get("ids", []), d.get("action", ""), d.get("value"))
        changed(epg=True)
        alog.info("tömeges művelet (admin): %s, %d csatorna", d.get("action"), n)
        return web.json_response({"ok": True, "count": n})

    async def h_cat_import(request):
        d = await body(request)
        text = d.get("text", "")
        if d.get("url"):
            try:
                async with A.http.get(d["url"], timeout=aiohttp.ClientTimeout(total=30)) as r:
                    r.raise_for_status()
                    text = await r.text()
            except Exception as e:  # noqa: BLE001
                raise CatalogError(f"az M3U nem tölthető le: {e}") from None
        rep = cat.import_m3u(text, d.get("mode", "add_new"))
        changed(epg=True)
        alog.info("M3U import (admin): +%d új, %d összefésült", len(rep["added"]), len(rep["merged"]))
        return web.json_response({"ok": True, **rep})

    async def h_cat_export(request):
        fmt = request.query.get("format", "json")
        if fmt == "m3u":
            alive_only = request.query.get("scope") == "alive"
            chans = [c for c in cat.channels if c.get("enabled") is not False and cat.active_url(c, ip_only=False)
                     and (not alive_only or H.alive(c["id"]))]
            txt = cat.export_m3u(chans, f"Katalógus ({len(chans)})", A.cfg.legacy_epg_url,
                                 url_for=lambda c: cat.active_url(c, ip_only=False))
            return web.Response(text=txt, content_type="audio/x-mpegurl", charset="utf-8",
                                headers={"Content-Disposition": 'attachment; filename="katalogus.m3u"'})
        return web.Response(text=cat.export_json(), content_type="application/json", charset="utf-8",
                            headers={"Content-Disposition": 'attachment; filename="katalog.json"'})

    async def h_cat_history(request):
        return web.json_response({"history": cat.history()})

    async def h_cat_restore(request):
        cat.restore((await body(request)).get("name", ""))
        changed(epg=True)
        alog.info("katalógus visszaállítva (admin)")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ forrás-ellenőrzés
    async def h_health(request):
        rows = []
        for c in cat.channels:
            i = H.info.get(c["id"]) or {}
            urls = c.get("urls") or []
            rows.append({"id": c["id"], "name": c["name"], "enabled": c.get("enabled") is not False,
                         "state": i.get("state", "ismeretlen"), "alive": i.get("alive"), "eff": i.get("eff"),
                         "checked": i.get("t"), "host": urlsplit(urls[0]).netloc if urls else "",
                         "sources": len(urls), "pin": bool(c.get("pin")), "uptime": H.uptime(c["id"])})
        return web.json_response({**H.summary(), "channels": rows})

    async def h_health_run(request):
        d = await body(request)
        if H.running:
            return jerr("már fut egy ellenőrzés", 409)
        mode = d.get("mode", "measure")
        if mode not in ("quick", "measure"):
            return jerr("mode: quick|measure")
        scope = "ids" if d.get("ids") else "all"
        asyncio.create_task(H.run(scope, mode, d.get("ids"), source="kézi"))
        return web.json_response({"ok": True})

    async def h_health_check(request):
        """Egy csatorna szinkron ellenőrzése (a szerkesztő ablakból)."""
        cid = request.match_info["cid"]
        if not cat.get(cid):
            return jerr("nincs ilyen csatorna", 404)
        if H.running:
            return jerr("már fut egy ellenőrzés", 409)
        mode = (await body(request)).get("mode", "measure")
        await H.run("ids", mode, [cid], source="kézi (egy csatorna)")
        return web.json_response({"ok": True, "channel": view(cat.get(cid))})

    async def h_health_channel(request):
        cid = request.match_info["cid"]
        c = cat.get(cid)
        if not c:
            return jerr("nincs ilyen csatorna", 404)
        return web.json_response({"channel": view(c), "history": list(H.history.get(cid, []))})

    def qint(request, key, default):
        try:
            return int(request.query.get(key, default))
        except ValueError:
            raise web.HTTPBadRequest(text=f"{key}: egész szám kell") from None

    async def h_events(request):
        after = qint(request, "after", 0)
        typ = request.query.get("type", "")
        ev = [e for e in H.events if e["i"] > after and (not typ or e["type"] == typ)]
        return web.json_response({"events": ev[-300:], "last": H._ev_n})

    # ------------------------------------------------------------ statisztika
    async def h_stats(request):
        days = max(1, min(qint(request, "days", 7), 3650))
        rows = A.usage(days)
        ch, ip, day = collections.defaultdict(lambda: [0, 0, 0.0, set()]), collections.defaultdict(lambda: [0, 0, 0.0]), \
            collections.defaultdict(lambda: [0, 0])
        for r in rows:
            a = ch[r["name"]]
            a[0] += 1
            a[1] += r["s"]
            a[2] += r["mb"]
            a[3].add(r["ip"])
            b = ip[r["ip"]]
            b[0] += 1
            b[1] += r["s"]
            b[2] += r["mb"]
            d = day[time.strftime("%Y-%m-%d", time.localtime(r["t"]))]
            d[0] += 1
            d[1] += r["s"]
        return web.json_response({
            "days": days, "sessions": len(rows), "seconds": sum(r["s"] for r in rows),
            "mb": round(sum(r["mb"] for r in rows), 1),
            "channels": sorted([{"name": k, "sessions": v[0], "seconds": v[1], "mb": round(v[2], 1), "ips": len(v[3])}
                                for k, v in ch.items()], key=lambda x: -x["seconds"])[:100],
            "ips": sorted([{"ip": k, "sessions": v[0], "seconds": v[1], "mb": round(v[2], 1)} for k, v in ip.items()],
                          key=lambda x: -x["seconds"])[:50],
            "days_list": [{"day": k, "sessions": v[0], "seconds": v[1]} for k, v in sorted(day.items())],
        })

    # ------------------------------------------------------------ beállítások
    def _links(request):
        base = A.base(request)
        ext = (A.cfg.external_base or A.base(request) + "/tv").rstrip("/")
        tok = A.cfg.external_token
        ext_pl = f"{ext}/{tok}/playlist.m3u" if tok else f"{ext}/<token>/playlist.m3u"
        ext_epg = f"{ext}/{tok}/epg.xml.gz" if tok else f"{ext}/<token>/epg.xml.gz"
        return {
            "internal": {"playlist": f"{base}/playlist.m3u", "epg": f"{base}/epg.xml.gz",
                         "status": f"{base}/status"},
            "external": {"playlist": ext_pl, "epg": ext_epg,
                         "epg_legacy": A.cfg.legacy_epg_url},
            "hdhr": {"tuner": f"{base}/discover.json", "lineup": f"{base}/lineup.json",
                     "epg": f"{base}/hdhr/epg.xml",
                     "udp": bool(A.cfg.hdhr_enabled and A.cfg.hdhr_discovery),
                     "mirror80": bool(A.cfg.hdhr_enabled and A.cfg.hdhr_port80),
                     "tuners": A.cfg.hdhr_tuners,
                     "device_id": hdhr.device_id(A)},
            "hdhr_enabled": bool(A.cfg.hdhr_enabled),
        }

    async def h_config_get(request):
        spec = {k: {kk: v.get(kk) for kk in ("type", "min", "max", "help", "group", "choices")} for k, v in P.SPEC.items()}
        return web.json_response({"values": {k: getattr(A.cfg, k) for k in P.SPEC}, "defaults": A.cfg.defaults,
                                  "overrides": A.cfg.overrides, "spec": spec,
                                  "links": _links(request)})

    async def h_config_post(request):
        try:
            d = await body(request)
            changes = dict(d.get("set", {}))
            for k in d.get("reset", []):
                if k in A.cfg.defaults:
                    changes[k] = A.cfg.defaults[k]
            if not changes:
                return jerr("nincs módosítás")
            before = (A.cfg.epg_source, A.cfg.legacy_epg_url, A.cfg.ip_only)
            A.cfg.apply(changes)
            A.cfg.save_settings()
        except ValueError as e:
            return jerr(str(e))
        cat.keep = A.cfg.history_keep
        if any(k.startswith("log_") for k in changes):
            P.apply_logging(A.cfg)
        alog.info("beállítások módosítva (admin): %s", ", ".join(changes))
        if before != (A.cfg.epg_source, A.cfg.legacy_epg_url, A.cfg.ip_only):
            changed(epg=True)
        elif set(changes) & {"playlist_only_alive", "write_legacy_files"}:
            changed()
        return web.json_response({"ok": True, "applied": list(changes)})

    async def h_reload(request):
        what = (await body(request)).get("what")
        if what not in ("epg", "catalog"):
            return jerr("what: epg|catalog")
        try:
            if what == "epg":
                await A.load_epg()
            else:
                cat.load()
                A.sync_channels()
        except Exception as e:  # noqa: BLE001
            return jerr(f"sikertelen: {e}", 502)
        alog.info("újraolvasás (admin): %s", what)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ élő adások
    async def h_stop(request):
        c = A.channels.get(request.match_info["cid"])
        if not c or not c.session or c.session.stopped:
            return jerr("nincs aktív adás", 404)
        c.session.stopped = True
        async with c.session.cond:
            c.session.cond.notify_all()
        alog.info("%s: leállítva (admin)", c.name)
        return web.json_response({"ok": True})

    async def h_kick(request):
        c = A.channels.get(request.match_info["cid"])
        try:
            client = int(request.match_info["client"])
        except ValueError:
            return jerr("érvénytelen kliens-azonosító")
        rec = c.session.client_list.get(client) if c and c.session else None
        if not rec:
            return jerr("nincs ilyen kliens", 404)
        rec["transport"].close()
        alog.info("%s: kliens kirúgva (admin): %s", c.name, rec["ip"])
        return web.json_response({"ok": True})

    async def h_probe(request):
        c = A.channels.get(request.match_info["cid"])
        if not c:
            return jerr("nincs ilyen (kiszolgálható) csatorna", 404)
        return web.json_response(await A.deep_probe(c, 8))

    async def h_logs(request):
        after = qint(request, "after", 0)
        lines = [x for x in P.RING.buf if x["i"] > after]
        return web.json_response({"lines": lines[-400:], "last": P.RING.n})

    async def h_m3u_single(request):
        c = A.channels.get(request.match_info["cid"])
        if not c:
            raise web.HTTPNotFound()
        base = A.base(request)
        txt = f"#EXTM3U\n{P.cat_extinf(c.rec)}\n{base}/ch/{c.id}.ts\n"
        return web.Response(text=txt, content_type="audio/x-mpegurl", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(c.id)}.m3u"'})

    app.add_routes([
        web.get("/", h_index),
        web.get("/admin", h_admin),
        web.get("/api/status", h_status),
        web.get("/api/catalog", h_catalog),
        web.post("/api/catalog/channel", h_cat_add),
        web.post("/api/catalog/channel/{cid}/update", h_cat_update),
        web.post("/api/catalog/channel/{cid}/delete", h_cat_delete),
        web.post("/api/catalog/reorder", h_cat_reorder),
        web.post("/api/catalog/renumber", h_cat_renumber),
        web.post("/api/catalog/bulk", h_cat_bulk),
        web.post("/api/catalog/import", h_cat_import),
        web.get("/api/catalog/export", h_cat_export),
        web.get("/api/catalog/history", h_cat_history),
        web.post("/api/catalog/restore", h_cat_restore),
        web.get("/api/health", h_health),
        web.post("/api/health/run", h_health_run),
        web.post("/api/health/check/{cid}", h_health_check),
        web.get("/api/health/channel/{cid}", h_health_channel),
        web.get("/api/events", h_events),
        web.get("/api/stats", h_stats),
        web.get("/api/config", h_config_get),
        web.post("/api/config", h_config_post),
        web.post("/api/reload", h_reload),
        web.post("/api/channel/{cid}/stop", h_stop),
        web.post("/api/channel/{cid}/kick/{client}", h_kick),
        web.post("/api/channel/{cid}/probe", h_probe),
        web.get("/api/logs", h_logs),
        web.get("/api/channel/{cid}.m3u", h_m3u_single),
    ])
