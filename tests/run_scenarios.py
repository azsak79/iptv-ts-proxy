#!/usr/bin/env python3
"""Végponttól végpontig teszt: mock relay (3 loopback IP) + proxy + TS-ellenőrző kliens.

Használat:  .venv/bin/python -I tests/run_scenarios.py --src . [--only steady,fail404,...]
A proxy a --src könyvtárból indul (így a régi és az új verzió ugyanazzal mérhető), saját ideiglenes
adatkönyvtárral; éles rendszerhez nem nyúl.
"""
import argparse
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CTL = "http://127.0.0.1:18099"
PPORT = 18095
PW = "teszt"
A, B, C = "127.0.0.2", "127.0.0.3", "127.0.0.4"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"x:{PW}".encode()).decode()}


async def ctl(s, host, mode):
    async with s.post(f"{CTL}/ctl", params={"host": host, "mode": mode}) as r:
        await r.read()


async def events(s, after=0):
    async with s.get(f"http://127.0.0.1:{PPORT}/api/events?after={after}", headers=AUTH) as r:
        return (await r.json())["events"]


async def api_status(s):
    async with s.get(f"http://127.0.0.1:{PPORT}/api/status", headers=AUTH) as r:
        return await r.json()


async def start_proxy(src, data, extra_env):
    cat = {"updated": "teszt", "channels": [
        {"id": "test", "name": "Teszt", "chno": "1", "group": "T", "tvg_id": "test.hu",
         "urls": [f"http://{A}:18080/live/testch/index.m3u8"]}]}
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "katalog.json"), "w") as f:
        json.dump(cat, f)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TSP_")}
    env.update({
        "TSP_ADMIN_PASSWORD": PW, "TSP_EPG": f"http://{A}:18080/epg.xml",
        "TSP_RELAYS": f"http://{A}:18080\nhttp://{B}:18080\nhttp://{C}:18080",
        "TSP_CHECK_ENABLED": "0", "TSP_HDHR_DISCOVERY": "0", "TSP_HDHR_PORT80": "0",
        "TSP_FAILOVER_STALL": "15", "TSP_FAILOVER_COOLDOWN": "600", "TSP_FAILOVER_RETRY": "20",
    })
    env.update(extra_env)
    log = open(os.path.join(data, "proxy.log"), "w")
    p = await asyncio.create_subprocess_exec(
        PY, "-I", os.path.join(src, "proxy.py"), "--port", str(PPORT), "--listen", "127.0.0.1", "--data", data,
        env=env, stdout=log, stderr=log)
    async with aiohttp.ClientSession() as s:
        for _ in range(50):
            try:
                async with s.get(f"http://127.0.0.1:{PPORT}/health") as r:
                    if r.status == 200:
                        return p
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("a proxy nem indult el – lásd " + log.name)


async def client(secs, stall=3.0):
    p = await asyncio.create_subprocess_exec(
        PY, "-I", os.path.join(HERE, "ts_check.py"), f"http://127.0.0.1:{PPORT}/ch/test.ts",
        "--secs", str(secs), "--stall", str(stall), stdout=asyncio.subprocess.PIPE)
    return p


async def collect(p):
    out = (await p.communicate())[0].decode()
    timeline = [x for x in out.splitlines() if not x.startswith("RESULT ")]
    res = json.loads(next(x for x in out.splitlines() if x.startswith("RESULT "))[7:])
    res["timeline"] = timeline
    return res


# ------------------------------------------------------------------ forgatókönyvek
# mind: (név, leírás, lépések [(mp, host, mód)], futásidő, elvárt-függvény)
SCEN = {
    "steady": ("Stabil forrás 40 mp", [], 40),
    "fail404": ("A host 404 (10. mp) -> átváltás B/C-re", [(10, A, "404")], 45),
    "hang": ("A host lefagy (10. mp) -> átváltás", [(10, A, "hang")], 50),
    "down": ("A host leáll, connection refused (10. mp)", [(10, A, "down")], 45),
    "segfail": ("A playlist él, a szegmensek 404 (10. mp)", [(10, A, "segfail")], 45),
    "stale": ("A playlist befagy (10. mp) -> stall-átváltás", [(10, A, "stale")], 55),
    "double": ("A 404 (10. mp), majd az új forrás is 404 (35. mp) – cooldown alatt", "double", 75),
    "startdead": ("Induláskor halott forrás (Plex 30 mp-es ablak)", [(-1, A, "404")], 35),
}


async def run_one(name, src, keep):
    desc, steps, secs = SCEN[name]
    data = tempfile.mkdtemp(prefix=f"tsp-{name}-", dir="/tmp/opencode")
    async with aiohttp.ClientSession() as s:
        for h in (A, B, C):
            await ctl(s, h, "ok")
        if steps != "double":
            for t, h, m in steps:
                if t < 0:
                    await ctl(s, h, m)
        proxy = await start_proxy(src, data, {})
        t0 = time.time()
        try:
            cp = await client(secs)
            if steps == "double":
                await asyncio.sleep(10)
                await ctl(s, A, "404")
                # megvárjuk az első átváltást, és a célhostot is lelőjük
                tgt = None
                for _ in range(40):
                    await asyncio.sleep(1)
                    st = await api_status(s)
                    up = (st["sessions"] or [{}])[0].get("upstream", "")
                    if A not in up and up:
                        tgt = up.split("/")[2].split(":")[0]
                        break
                await asyncio.sleep(max(0, 35 - (time.time() - t0)))
                if tgt:
                    await ctl(s, tgt, "404")
            else:
                for t, h, m in sorted(x for x in steps if x[0] >= 0):
                    await asyncio.sleep(max(0, t - (time.time() - t0)))
                    await ctl(s, h, m)
            res = await collect(cp)
            ev = await events(s)
            st = await api_status(s)
        finally:
            proxy.terminate()
            await proxy.wait()
            for h in (A, B, C):
                await ctl(s, h, "ok")
    res["events"] = [f"{e['t'] - t0:5.1f}s {e['type']}: {e['detail']}" for e in ev if e["type"] != "check_done"]
    res["upstream_end"] = (st["sessions"] or [{}])[0].get("upstream")
    with open(os.path.join(data, "proxy.log")) as f:
        lines = f.read().splitlines()
    res["log_lines"] = len(lines)
    res["log_warn"] = sum(1 for x in lines if " WARNING " in x)
    if not keep:
        shutil.rmtree(data, ignore_errors=True)
    else:
        res["data"] = data
    return desc, res


async def main(a):
    os.makedirs("/tmp/opencode", exist_ok=True)
    rlog = open("/tmp/opencode/mock_relay.log", "w")
    relay = await asyncio.create_subprocess_exec(PY, "-I", os.path.join(HERE, "mock_relay.py"),
                                                 stdout=rlog, stderr=rlog)
    async with aiohttp.ClientSession() as s:       # megvárjuk, amíg a relay vezérlője válaszol
        for _ in range(50):
            try:
                async with s.get(f"{CTL}/stat") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.2)
    try:
        names = a.only.split(",") if a.only else list(SCEN)
        for n in names:
            desc, r = await run_one(n, os.path.abspath(a.src), a.keep)
            ok = (not r["error"] and r["sync_err"] == 0 and r["cc_err"] == 0 and r["seg_trunc"] == 0
                  and r["dup"] == 0 and r["backwards"] == 0)
            print(f"\n=== {n}: {desc}  ->  {'TS ÉP' if ok else 'TS HIBA'}")
            print(f"  első bájt {r['first_byte_s']}s, {r['mbps']} Mbps, szegmens {r['segs']}, lyuk {r['gaps']} "
                  f"({r['gap_segs']} szegm.), csonka {r['seg_trunc']}, dup {r['dup']}, vissza {r['backwards']}, "
                  f"cc-hiba {r['cc_err']}, sync-hiba {r['sync_err']}, null {r['null']}, max szünet {r['max_stall_s']}s, "
                  f"hiba {r['error']}")
            print(f"  elakadások: {r['stalls']}")
            print(f"  végső forrás: {r['upstream_end']}; napló: {r['log_lines']} sor, {r['log_warn']} warning"
                  + (f"; adat: {r['data']}" if a.keep else ""))
            for e in r["events"]:
                print("   ", e)
            for e in r["timeline"][:12]:
                print("    kliens", e)
    finally:
        if relay.returncode is None:
            relay.terminate()
        await relay.wait()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=os.path.join(os.path.dirname(HERE), "app"))
    p.add_argument("--only", default="")
    p.add_argument("--keep", action="store_true")
    asyncio.run(main(p.parse_args()))
