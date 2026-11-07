#!/usr/bin/env python3
"""Hamis IPTV-szolgáltató a teszthez: élő HLS (csúszó ablak, Range-támogatással), M3U lista, XMLTV.

Használat: fake_upstream.py <port>
  /list.m3u                     2 csatorna (tv1, tv2)
  /live/<slug>/index.m3u8       élő playlist, 2 mp-es szegmensek
  /seg/<slug>/<n>.ts            ~200 KB MPEG-TS szegmens (0x47 szinkronbájtokkal)
  /epg.xml                      XMLTV a két csatornához
"""
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEG_DUR = 2
PKT = 188
SEG_PKTS = 1100
T0 = time.time()


def seg_bytes(slug, n):
    body = bytearray()
    for i in range(SEG_PKTS):
        payload = f"{slug}:{n}:{i}".encode().ljust(PKT - 4, b"\xff")
        body += b"\x47\x01\x00\x10" + payload
    return bytes(body)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        host = self.headers.get("Host", "127.0.0.1")
        p = self.path.split("?")[0]
        if p == "/list.m3u":
            txt = "#EXTM3U\n"
            for i, (slug, name) in enumerate((("tv1", "Teszt Egy"), ("tv2", "Teszt Kettő")), 1):
                txt += (f'#EXTINF:-1 tvg-id="{slug}.test" tvg-chno="{i}" group-title="Teszt",{name}\n'
                        f"http://{host}/live/{slug}/index.m3u8\n")
            return self.send(200, txt.encode(), "audio/x-mpegurl")
        if p == "/epg.xml":
            now = time.strftime("%Y%m%d%H0000 +0000", time.gmtime())
            later = time.strftime("%Y%m%d%H0000 +0000", time.gmtime(time.time() + 7200))
            x = '<?xml version="1.0" encoding="UTF-8"?><tv>'
            for slug in ("tv1", "tv2", "other"):
                x += f'<channel id="{slug}.test"><display-name>{slug}</display-name></channel>'
                x += f'<programme start="{now}" stop="{later}" channel="{slug}.test"><title>Műsor {slug}</title></programme>'
            return self.send(200, (x + "</tv>").encode(), "application/xml")
        parts = p.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "live" and parts[2] == "index.m3u8":
            slug = parts[1]
            cur = int((time.time() - T0) / SEG_DUR) + 100
            seq = cur - 5
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEG_DUR}", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
            for n in range(seq, cur):
                lines += [f"#EXTINF:{SEG_DUR}.000,", f"http://{host}/seg/{slug}/{n}.ts"]
            return self.send(200, ("\n".join(lines) + "\n").encode(), "application/vnd.apple.mpegurl")
        if len(parts) == 3 and parts[0] == "seg" and parts[2].endswith(".ts"):
            body = seg_bytes(parts[1], int(parts[2][:-3]))
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                a = int(a)
                b = min(int(b) if b else len(body) - 1, len(body) - 1)
                if a >= len(body):
                    return self.send(416, b"", "text/plain", {"Content-Range": f"bytes */{len(body)}"})
                return self.send(206, body[a:b + 1], "video/MP2T",
                                 {"Content-Range": f"bytes {a}-{b}/{len(body)}", "Accept-Ranges": "bytes"})
            return self.send(200, body, "video/MP2T", {"Accept-Ranges": "bytes"})
        return self.send(404, b"not found", "text/plain")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
