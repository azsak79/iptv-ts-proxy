#!/usr/bin/env python3
"""Kliensoldali TS-ellenőrző: a proxy /ch/<id>.ts folyamát olvassa, és a mock relay csomagjai alapján
méri a folytonosságot (szinkron, CC, szegmens-sorrend, lyuk, ismétlés, null-töltés, elakadás).

Kimenet: egy JSON-sor a végén; idővonal-események a futás közben (stderr nélkül, stdout-ra)."""
import argparse
import asyncio
import json
import struct
import time

import aiohttp


async def main(url, secs, stall_warn, mbps=0):
    t0 = time.time()
    buf = b""
    st = {"bytes": 0, "pkts": 0, "null": 0, "sync_err": 0, "cc_err": 0, "segs": 0, "gaps": 0, "gap_segs": 0,
          "dup": 0, "backwards": 0, "max_stall_s": 0.0, "stalls": [], "first_byte_s": None, "error": None,
          "seg_trunc": 0}
    last_seq, last_idx, last_cc, last_read = None, None, None, None
    seen_pkts = 0                     # a szegmens-sorrend csak a mock relay formátumán értelmezhető
    pair_ok, pair_tot = 0, 0
    seg_seen = set()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=secs + 30, sock_read=150)) as r:
                async for chunk in r.content.iter_any():
                    now = time.time()
                    if st["first_byte_s"] is None:
                        st["first_byte_s"] = round(now - t0, 2)
                    if last_read is not None and now - last_read > st["max_stall_s"]:
                        st["max_stall_s"] = round(now - last_read, 2)
                    if last_read is not None and now - last_read > stall_warn:
                        st["stalls"].append([round(last_read - t0, 1), round(now - last_read, 1)])
                        print(f"[{now - t0:6.1f}s] ELAKADÁS {now - last_read:.1f}s", flush=True)
                    last_read = now
                    st["bytes"] += len(chunk)
                    buf += chunk
                    n = len(buf) // 188 * 188
                    for off in range(0, n, 188):
                        p = buf[off:off + 188]
                        if p[0] != 0x47:
                            st["sync_err"] += 1
                            continue
                        st["pkts"] += 1
                        pid = ((p[1] & 0x1F) << 8) | p[2]
                        if pid == 0x1FFF:
                            st["null"] += 1
                            continue
                        if pid != 0x100:
                            continue
                        cc = p[3] & 0x0F
                        seq, idx = struct.unpack(">II", p[4:12])
                        seen_pkts += 1
                        if last_seq is not None and pair_tot < 3000:
                            pair_tot += 1
                            if (seq == last_seq and idx == last_idx + 1) or (seq == last_seq + 1 and idx == 0):
                                pair_ok += 1
                        if last_seq is not None:
                            if seq == last_seq and idx != last_idx + 1:
                                st["sync_err" if idx <= last_idx else "seg_trunc"] += 1
                            elif seq != last_seq:
                                if last_idx != 1327:
                                    st["seg_trunc"] += 1
                                if seq in seg_seen:
                                    st["dup"] += 1
                                elif seq < last_seq:
                                    st["backwards"] += 1
                                elif seq > last_seq + 1:
                                    st["gaps"] += 1
                                    st["gap_segs"] += seq - last_seq - 1
                                    print(f"[{now - t0:6.1f}s] LYUK {last_seq} -> {seq}", flush=True)
                            if last_cc is not None and cc != (last_cc + 1) & 0x0F:
                                st["cc_err"] += 1
                        if seq != last_seq:
                            st["segs"] += 1
                            seg_seen.add(seq)
                        last_seq, last_idx, last_cc = seq, idx, cc
                    buf = buf[n:]
                    if mbps:      # 1x tempójú lejátszó szimulációja: a beolvasás ne szaladjon előre
                        ahead = st["bytes"] * 8 / (mbps * 1e6) - (time.time() - t0)
                        if ahead > 0:
                            await asyncio.sleep(ahead)
                    if now - t0 >= secs:
                        break
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {e}"
    if pair_tot and pair_ok / pair_tot < 0.8:
        st["mock_format"] = False
        for k in ("segs", "gaps", "gap_segs", "dup", "backwards", "cc_err", "seg_trunc"):
            st.pop(k, None)
    st["secs"] = round(time.time() - t0, 1)
    st["mbps"] = round(st["bytes"] * 8 / 1e6 / max(st["secs"], 0.1), 2)
    print("RESULT " + json.dumps(st, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--secs", type=float, default=30)
    p.add_argument("--stall", type=float, default=3.0, help="ennyi mp adatszünet már elakadásnak számít")
    p.add_argument("--mbps", type=float, default=0, help="ennyi Mbps tempóban olvasson (0 = a végsebességgel)")
    a = p.parse_args()
    asyncio.run(main(a.url, a.secs, a.stall, a.mbps))
