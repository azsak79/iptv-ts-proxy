#!/usr/bin/env python3
"""Mock HLS relay a proxy teszteléséhez: több loopback IP-n (127.0.0.2..) ugyanazt az élő csatornát adja.

A szegmensek valódi 188 bájtos TS-csomagokból állnak (PID 0x100), a hasznos adatban a forrás-szegmens
sorszáma és a csomagindex: a kliensoldali ellenőrző (`ts_check.py`) ebből látja a sorrendet, a lyukakat
és az ismétlést. Host-onként vezérelhető hibamód (POST http://127.0.0.1:<ctl>/ctl?host=IP&mode=...):
  ok      – normál
  404     – playlist és szegmens 404
  hang    – a kérés nem kap választ (a kapcsolat él)
  stale   – a playlist befagy (nem jön új szegmens)
  segfail – a playlist jó, a szegmensek 404-et adnak
  down    – a host nem figyel (connection refused)
"""
import argparse
import asyncio
import struct
import time

from aiohttp import web

SEG_PKTS = 1328            # 16 többszöröse: a CC szegmenshatáron is folytonos (~250 KB/szegmens)


def ts_segment(seq):
    out = bytearray()
    for i in range(SEG_PKTS):
        hdr = bytes([0x47, 0x01 | (0x40 if i == 0 else 0), 0x00, 0x10 | (i & 0x0F)])
        payload = struct.pack(">II", seq, i) + bytes(176)
        out += hdr + payload
    return bytes(out)


class Relay:
    def __init__(self, ips, port, segdur, window):
        self.ips, self.port, self.segdur, self.window = ips, port, segdur, window
        self.mode = {ip: "ok" for ip in ips}
        self.frozen = {}
        self.sites = {}
        self.hits = {ip: 0 for ip in ips}
        self.cache = {}

    def cur_seq(self):
        return int(time.time() / self.segdur)

    def ip_of(self, request):
        return request.transport.get_extra_info("sockname")[0]

    async def guard(self, request):
        ip = self.ip_of(request)
        self.hits[ip] = self.hits.get(ip, 0) + 1
        m = self.mode.get(ip, "ok")
        if m == "down":                          # a meglévő keep-alive kapcsolat is megszakad
            request.transport.close()
            raise ConnectionResetError()
        if m == "404":
            raise web.HTTPNotFound()
        while self.mode.get(ip) == "hang":       # a kapcsolat él, válasz nincs (amíg a mód tart)
            await asyncio.sleep(0.5)
        return ip, m

    async def h_playlist(self, request):
        ip, m = await self.guard(request)
        if m == "stale":
            last = self.frozen.setdefault(ip, self.cur_seq())
        else:
            self.frozen.pop(ip, None)
            last = self.cur_seq()
        first = last - self.window + 1
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(self.segdur)}",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for s in range(first, last + 1):
            lines += [f"#EXTINF:{self.segdur:.3f},", f"seg-{s}.ts"]
        return web.Response(text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl")

    async def h_seg(self, request):
        ip, m = await self.guard(request)
        if m == "segfail":
            raise web.HTTPNotFound()
        seq = int(request.match_info["seq"])
        if seq > self.cur_seq() or seq < self.cur_seq() - self.window - 2:
            raise web.HTTPNotFound()
        body = self.cache.get(seq)
        if body is None:
            body = self.cache[seq] = ts_segment(seq)
            for k in [k for k in self.cache if k < seq - 20]:
                del self.cache[k]
        rng = request.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            a = int(a)
            b = min(int(b) if b else len(body) - 1, len(body) - 1)
            if a >= len(body):
                raise web.HTTPRequestRangeNotSatisfiable()
            return web.Response(status=206, body=body[a:b + 1], content_type="video/MP2T",
                                headers={"Content-Range": f"bytes {a}-{b}/{len(body)}"})
        return web.Response(body=body, content_type="video/MP2T")

    async def h_epg(self, request):
        xml = ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="test.hu"><display-name>Teszt</display-name>'
               '</channel></tv>')
        return web.Response(text=xml, content_type="application/xml")

    async def h_ctl(self, request):
        ip, mode = request.query["host"], request.query["mode"]
        prev = self.mode.get(ip)
        self.mode[ip] = mode
        if mode == "down" and ip in self.sites:
            await self.sites.pop(ip).stop()
        elif prev == "down" and mode != "down":
            await self.start_site(ip)
        print(f"ctl {ip} -> {mode}", flush=True)
        return web.json_response({"ok": True, "mode": self.mode})

    async def h_stat(self, request):
        return web.json_response({"mode": self.mode, "hits": self.hits, "seq": self.cur_seq()})

    async def start_site(self, ip):
        site = web.TCPSite(self.runner, ip, self.port, reuse_address=True)
        await site.start()
        self.sites[ip] = site

    async def main(self, ctl_port):
        app = web.Application()
        app.add_routes([web.get("/live/{slug}/{f:(index|playlist)\\.m3u8}", self.h_playlist),
                        web.get("/live/{slug}/seg-{seq:\\d+}.ts", self.h_seg),
                        web.get("/epg.xml", self.h_epg)])
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        for ip in self.ips:
            await self.start_site(ip)
        ctl = web.Application()
        ctl.add_routes([web.post("/ctl", self.h_ctl), web.get("/stat", self.h_stat)])
        cr = web.AppRunner(ctl, access_log=None)
        await cr.setup()
        await web.TCPSite(cr, "127.0.0.1", ctl_port).start()
        print(f"mock relay: {', '.join(self.ips)} :{self.port}, ctl :{ctl_port}", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ips", default="127.0.0.2,127.0.0.3,127.0.0.4")
    p.add_argument("--port", type=int, default=18080)
    p.add_argument("--ctl", type=int, default=18099)
    p.add_argument("--segdur", type=float, default=2.0)
    p.add_argument("--window", type=int, default=5)
    a = p.parse_args()
    asyncio.run(Relay(a.ips.split(","), a.port, a.segdur, a.window).main(a.ctl))
