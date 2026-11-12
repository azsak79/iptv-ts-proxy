"""HDHomeRun-tuner emuláció: a proxyt Plex (és más HDHomeRun-támogatású DVR) élő tunernek látja.

A Plex natívan HDHomeRun-tunert támogat (automatikus felderítéssel), az M3U/IPTV-tunerhez
szintén Plex Pass kell, de az útvonal gyengébben integrált. Ez a modul a proxy HTTP-végpontjaira
teszi a HDHomeRun-felületet (/discover.json, /lineup.json, /lineup_status.json, /auto/v<szám>,
/hdhr/epg.xml), és UDP 65001-en válaszol a "discover" kérésre, hogy a Plex magától megtalálja.

A GuideNumber a katalógus `chno` mezője; a lineup URL-je közvetlenül a /ch/<id>.ts folyamot
adja, a guide pedig a /hdhr/epg.xml (csatorna-id = csatornaszám, ahogy a Plex párosítja).
"""
import asyncio
import hashlib
import logging
import os
import socket
import struct
import time
import zlib
from aiohttp import web

log = logging.getLogger("tsproxy.hdhr")


def device_id_int(A):
    """32 bites DeviceID a hosznévből. Az előtag (>>20) szándékosan 0x900: az
    0x100–0x122 tartományú azonosítókat a libhdhomerun „legacy" tunerként kezeli,
    ami HTTP-streamelés nélküli eszközt jelent – azt a Plex elutasítja.
    (Ha a Plex tuner-elfogadás gondot okozna, 0x10A-ra váltva a HDHomeRun FLEX
    modellprofilja használható – de az új tuner-újrabeállítást igényel.)"""
    h = int(hashlib.sha1(("iptv-ts-proxy:" + socket.gethostname()).encode()).hexdigest()[:8], 16)
    return 0x90000000 | (h & 0x0FFFFFFF)


def device_id(A):
    return f"{device_id_int(A):08X}"


def device_name(A):
    return A.cfg.hdhr_name or ("iptv-ts-proxy@" + socket.gethostname())


def _base_of(A, request, base_fn=None):
    return base_fn(request) if base_fn else A.base(request)


def setup(A, app, base_fn=None):
    """HDHomeRun-végpontok regisztrálása. `base_fn` a tükrözött (port 80) szerverhez kell:
    ott az URL-eknek a fő proxy-portra kell mutatniuk, nem a 80-ra."""
    if not A.cfg.hdhr_enabled:
        return

    async def h_discover(request):
        base = _base_of(A, request, base_fn)
        return web.json_response({
            "DeviceID": device_id(A),
            "DeviceAuth": "iptv-ts-proxy",
            "BaseURL": base,
            "LineupURL": f"{base}/lineup.json",
            "FriendlyName": device_name(A),
            "ModelNumber": "HDTC-2-US",
            "FirmwareName": "hdhomerun_ttc_atsc_firmware",
            "FirmwareVersion": A.VERSION,
            "TunerCount": A.cfg.hdhr_tuners,
        })

    async def h_lineup(request):
        base = _base_of(A, request, base_fn)
        out = []
        for c in A.catalog.channels:
            if c.get("enabled") is False:
                continue
            gn = str(c.get("chno") or "").strip()
            if not gn:
                continue
            out.append({"GuideNumber": gn, "GuideName": c.get("name", ""),
                        "URL": f"{base}/ch/{c['id']}.ts"})
        return web.json_response(out)

    async def h_lineup_status(request):
        n = sum(1 for c in A.catalog.channels
                if c.get("enabled") is not False and c.get("chno"))
        return web.json_response({"ScanInProgress": False, "ScanProgress": 0,
                                  "ScanFound": n, "Source": "Cable"})

    async def h_device_info(request):
        base = _base_of(A, request, base_fn)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<device>
  <deviceType>hdhomerun</deviceType>
  <friendlyName>{device_name(A)}</friendlyName>
  <modelNumber>HDTC-2-US</modelNumber>
  <firmwareName>hdhomerun_ttc_atsc_firmware</firmwareName>
  <firmwareVersion>{A.VERSION}</firmwareVersion>
  <deviceID>{device_id(A)}</deviceID>
  <baseURL>{base}</baseURL>
  <lineupURL>{base}/lineup.json</lineupURL>
</device>
"""
        return web.Response(text=xml, content_type="application/xml")

    async def h_device_xml(request):
        base = _base_of(A, request, base_fn)
        devid = device_id(A)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>{base}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{device_name(A)}</friendlyName>
    <manufacturer>SiliconDust (emulated)</manufacturer>
    <modelName>HDTC-2-US</modelName>
    <modelNumber>{A.VERSION}</modelNumber>
    <UDN>uuid:{devid}</UDN>
  </device>
</root>
"""
        return web.Response(text=xml, content_type="application/xml")

    async def h_epg(request):
        if getattr(A, "epg_hdhr_gz", None) is None:
            raise web.HTTPServiceUnavailable(text="HDHR-EPG még nincs kész (EPG-töltés után próbáld)")
        if request.path.endswith(".gz"):
            return web.Response(body=A.epg_hdhr_gz, content_type="application/gzip")
        return web.Response(body=A.epg_hdhr_xml, content_type="application/xml")

    async def h_auto(request):
        """HDHomeRun-stílusú stream: /auto/v<csatornaszám> (a lineup URL-je azonban
        közvetlenül a /ch/<id>.ts-re mutat, ez inkább kompatibilitási végpont)."""
        gn = request.match_info["gn"].lstrip("vV")
        rec = next((c for c in A.catalog.channels
                    if str(c.get("chno") or "").strip() == gn and c.get("enabled") is not False), None)
        if not rec:
            raise web.HTTPNotFound()
        ch = A.channels.get(rec["id"])
        if not ch:
            raise web.HTTPNotFound()
        s = A._session_for(ch)
        if s is None:
            raise web.HTTPServiceUnavailable(text="elérte az egyidejű csatornák maximumát")
        return await s.serve(request)
    app.add_routes([
        web.get("/discover.json", h_discover),
        web.get("/lineup.json", h_lineup),
        web.get("/lineup_status.json", h_lineup_status),
        web.get("/device_info.xml", h_device_info),
        web.get("/device.xml", h_device_xml),
        web.get("/tuners.html", lambda r: web.Response(text=(
            f"<html><body><h3>{device_name(A)}</h3>"
            f"<p>Tunerek: {A.cfg.hdhr_tuners} · lineup: <a href='/lineup.json'>/lineup.json</a>"
            f" · EPG: <a href='/hdhr/epg.xml'>/hdhr/epg.xml</a></p></body></html>"),
            content_type="text/html")),
        web.get("/hdhr/epg.xml", h_epg),
        web.get("/hdhr/epg.xml.gz", h_epg),
        web.get("/auto/v{gn}", h_auto),
        web.get("/auto/v{gn}.ts", h_auto),
    ])


# ------------------------------------------------------ HDHomeRun bináris protokoll
# A Plex (libhdhomerun) az UDP 65001-en TLV-csomagokat használ, nem szöveget:
#   fejléc: u16 típus, u16 hasznos hossz (big-endian), utána TLV-ek, végén CRC32 (little-endian).
#   TLV: u8 tag, varint hossz (<=127: 1 bájt, afölött: (len|0x80) + len>>7), érték.
#   DISCOVER_REQ = 0x0002, DISCOVER_RPY = 0x0003; tag-ek: DEVICE_TYPE=0x01, DEVICE_ID=0x02,
#   TUNER_COUNT=0x10, LINEUP_URL=0x27, BASE_URL=0x2A, DEVICE_AUTH_STR=0x2B, MULTI_TYPE=0x2D.
_DISCOVER_REQ = 0x0002
_DISCOVER_RPY = 0x0003


def _tlv(tag, value):
    n = len(value)
    if n <= 127:
        return bytes([tag, n]) + value
    return bytes([tag, (n & 0x7F) | 0x80, n >> 7]) + value


def _frame(ftype, payload):
    head = struct.pack(">HH", ftype, len(payload))
    return head + payload + struct.pack("<I", zlib.crc32(head + payload) & 0xFFFFFFFF)


def _parse_discover(data):
    """DISCOVER_REQ értelmezése; (device_types, device_id) vagy None, ha nem érvényes kérés."""
    if len(data) < 8:
        return None
    ftype, plen = struct.unpack(">HH", data[:4])
    if ftype != _DISCOVER_REQ or plen != len(data) - 8:
        return None
    payload = data[4:4 + plen]
    crc, = struct.unpack("<I", data[-4:])
    if zlib.crc32(data[:-4]) & 0xFFFFFFFF != crc:
        return None
    types, devid = None, None
    i = 0
    while i + 2 <= len(payload):
        tag = payload[i]
        i += 1
        n = payload[i]
        i += 1
        if n & 0x80:
            n = (n & 0x7F) | (payload[i] << 7)
            i += 1
        val = payload[i:i + n]
        i += n
        if tag == 0x01 and n == 4:
            types = [struct.unpack(">I", val)[0]]
        elif tag == 0x2D and n % 4 == 0:
            types = [struct.unpack(">I", val[j:j + 4])[0] for j in range(0, n, 4)]
        elif tag == 0x02 and n == 4:
            devid = struct.unpack(">I", val)[0]
    return types, devid


def _build_discover_reply(A, base):
    devid = device_id_int(A)
    payload = b""
    payload += _tlv(0x01, struct.pack(">I", 1))                       # TUNER
    payload += _tlv(0x02, struct.pack(">I", devid))
    payload += _tlv(0x10, bytes([max(1, A.cfg.hdhr_tuners)]))
    payload += _tlv(0x2B, b"iptv-ts-proxy")                           # DEVICE_AUTH_STR
    payload += _tlv(0x2A, base.encode())                              # BASE_URL
    payload += _tlv(0x27, f"{base}/lineup.json".encode())             # LINEUP_URL
    return _frame(_DISCOVER_RPY, payload)


class Discovery(asyncio.DatagramProtocol):
    """UDP 65001: bináris TLV-discovery (a Plex/libhdhomerun natív útja) és
    az egyszerű szöveges "discover" kérésre is válaszol a proxy URL-jével."""

    def __init__(self, A):
        self.A = A
        self.transport = None
        self._last_log = 0.0

    def connection_made(self, transport):
        self.transport = transport

    def _reply_url(self, peer):
        # a saját IP-t úgy kapjuk meg, hogy a kérdező felé "kihúzunk" egy UDP socketet
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((peer if isinstance(peer, str) else peer[0], 9))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            ip = "127.0.0.1"
        return f"http://{ip}:{self.A.cfg.port}"

    def _log(self, fmt, *a):
        now = time.time()
        if now - self._last_log > 30:            # a Plex gyorsan egymás után kérdez: ne spammeljük a naplót
            self._last_log = now
            log.info("hdhr discover: " + fmt, *a)

    def datagram_received(self, data, addr):
        try:
            req = _parse_discover(data)
            if req is not None:
                types, devid = req
                if types is not None and 1 not in types:
                    return                        # nem tunert kérdeznek
                if devid not in (None, 0xFFFFFFFF) and devid != device_id_int(self.A):
                    return                        # más eszközt keresnek
                url = self._reply_url(addr)
                self.transport.sendto(_build_discover_reply(self.A, url), addr)
                self._log("bináris %s -> %s", addr[0], url)
                return
        except Exception as e:  # noqa: BLE001
            log.warning("hdhr bináris discover hiba: %s", e)
            return
        # szöveges "discover" (régebbi kliensek, ffmpeg)
        try:
            txt = (data or b"").decode("ascii", "replace")
        except Exception:  # noqa: BLE001
            return
        if "discover" not in txt.lower() and "<" not in txt:
            return
        url = self._reply_url(addr)
        try:
            self.transport.sendto(url.encode(), addr)
            self._log("szöveges %s -> %s", addr[0], url)
        except Exception:  # noqa: BLE001
            pass


class Ssdp(asyncio.DatagramProtocol):
    """UDP 1900 SSDP: M-SEARCH válasz LOCATION -> /device.xml (a Plex-felderítés másik útja)."""

    def __init__(self, A):
        self.A = A
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            txt = (data or b"").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        if not txt.upper().startswith("M-SEARCH"):
            return
        st = ""
        for line in txt.splitlines():
            if line.lower().startswith("st:"):
                st = line[3:].strip()
                break
        udn = device_id(self.A)
        advert = [f"uuid:{udn}", "upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1"]
        if st == "ssdp:all":
            targets = advert
        elif st in advert:
            targets = [st]
        else:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((addr[0], 9))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            ip = "127.0.0.1"
        loc = f"http://{ip}:{self.A.cfg.port}/device.xml"
        for stx in targets:
            usn = f"uuid:{udn}" if stx == f"uuid:{udn}" else f"uuid:{udn}::{stx}"
            msg = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\n"
                   f"SERVER: Linux UPnP/1.0 iptv-ts-proxy/{self.A.VERSION}\r\n"
                   f"ST: {stx}\r\nUSN: {usn}\r\nLOCATION: {loc}\r\n\r\n")
            try:
                self.transport.sendto(msg.encode(), addr)
            except Exception:  # noqa: BLE001
                pass
        log.info("ssdp M-SEARCH %s (%s) -> %s", addr[0], st, loc)


async def start_discovery(A, app):
    """A UDP-figyelő indítása; hiba (pl. foglalt port) csak napló, nem állít le semmit."""
    if not (A.cfg.hdhr_enabled and A.cfg.hdhr_discovery):
        return
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(
            lambda: Discovery(A), local_addr=("0.0.0.0", 65001))
        app["hdhr_udp"] = transport
        log.info("hdhr felderítés: UDP 65001 figyelve (bináris TLV + szöveges, Plex automatikusan megtalálja)")
    except OSError as e:
        log.warning("hdhr felderítés nem indítható (UDP 65001): %s – a tuner kézzel is hozzáadható "
                    "a Plexben a proxy IP:port címével", e)
    try:
        transport, proto = await loop.create_datagram_endpoint(
            lambda: Ssdp(A), local_addr=("0.0.0.0", 1900))
        app["ssdp_udp"] = transport
        log.info("hdhr felderítés: SSDP UDP 1900 figyelve")
    except OSError as e:
        log.warning("SSDP (UDP 1900) nem indítható: %s – ez csak az automatikus felderítést érinti", e)


async def stop_discovery(A, app):
    for k in ("hdhr_udp", "ssdp_udp"):
        t = app.get(k)
        if t:
            t.close()


async def start_mirror(A, app):
    """Port-80-as tükör: a Plex egyes útvonalai a hirdetett port helyett a 80-at kérdezik.
    A tükörön az URL-ek a fő proxy-portra mutatnak (`base_fn`)."""
    if not (A.cfg.hdhr_enabled and A.cfg.hdhr_port80):
        return
    mirror = web.Application()
    mirror_port = int(os.environ.get("TSP_HDHR_MIRROR_PORT", "80"))
    setup(A, mirror, base_fn=lambda r: f"http://{r.host.split(':')[0]}:{A.cfg.port}")
    runner = web.AppRunner(mirror)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", mirror_port)
        await site.start()
        app["hdhr_mirror"] = runner
        log.info("hdhr port-%d tükör elindítva (a Plex IP nélküli portmegadását is elviszi)", mirror_port)
    except OSError as e:
        await runner.cleanup()
        log.warning("hdhr port-%d tükör nem indítható: %s (szabad port kell, "
                    "root nélkül AmbientCapabilities=CAP_NET_BIND_SERVICE)", mirror_port, e)


async def stop_mirror(A, app):
    r = app.get("hdhr_mirror")
    if r:
        await r.cleanup()
