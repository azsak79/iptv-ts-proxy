#!/usr/bin/env python3
"""A csatorna-felderítés valós relay hostokon, valódi katalógussal és EPG-vel.

A proxy a megadott M3U-ból bootstrapol (nincs beégetett tesztkatalógus), betölti a valódi
XMLTV-t (a display-name-jei lesznek a jelölt-slugok), majd lefut a felderítés a megadott
relay hostokon. Kiírja: mennyi ismeretlen jelölt volt, mit talált a katalóguson kívül.
Használat: .venv/bin/python -I tests/relay_discovery.py [--hosts ...] [--max 150]
Az éles rendszert nem érinti (helyi példány, saját adatkönyvtár, read-only a relay felé).
"""
import argparse
import asyncio
import base64
import os
import sys
import tempfile
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_scenarios import PPORT, PW  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(f"x:{PW}".encode()).decode()}
SRC = os.environ.get("TSP_TEST_SOURCE", "")
EPG = os.environ.get("TSP_TEST_EPG", "")


async def start(data, env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("TSP_")}
    e.update(env)
    log = open(os.path.join(data, "proxy.log"), "w")
    p = await asyncio.create_subprocess_exec(
        sys.executable, "-I", os.path.join(os.path.dirname(HERE), "proxy.py"),
        "--port", str(PPORT), "--listen", "127.0.0.1", "--data", data, env=e, stdout=log, stderr=log)
    async with aiohttp.ClientSession() as s:
        for _ in range(60):
            try:
                async with s.get(f"http://127.0.0.1:{PPORT}/health") as r:
                    if r.status == 200:
                        return p
            except aiohttp.ClientError:
                await asyncio.sleep(0.25)
    raise RuntimeError("a proxy nem indult el – " + log.name)


async def main(a):
    data = tempfile.mkdtemp(prefix="relaydisc-", dir="/tmp/opencode")
    env = {"TSP_ADMIN_PASSWORD": PW, "TSP_SOURCE": SRC, "TSP_EPG": EPG,
           "TSP_RELAYS": "\n".join("http://" + h for h in a.hosts.split(",")),
           "TSP_DISCOVERY_MAX": str(a.max), "TSP_CHECK_ENABLED": "0",
           "TSP_HDHR_DISCOVERY": "0", "TSP_HDHR_PORT80": "0"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        proxy = await start(data, env)
        try:
            for _ in range(90):        # M3U-import + EPG-betöltés
                async with s.get(f"http://127.0.0.1:{PPORT}/api/status", headers=AUTH) as r:
                    d = await r.json()
                if d["channels"] > 10 and d["epg"]["channels"] > 10:
                    break
                await asyncio.sleep(1)
            print(f"katalógus: {d['channels']} csatorna, EPG: {d['epg']['channels']} cs. "
                  f"({d['epg']['programmes']} műsor)")
            async with s.post(f"http://127.0.0.1:{PPORT}/api/discovery/run", headers=AUTH, json={}) as r:
                assert r.status == 200
            for _ in range(int(a.timeout / 0.5)):
                await asyncio.sleep(0.5)
                async with s.get(f"http://127.0.0.1:{PPORT}/api/discovery", headers=AUTH) as r:
                    dd = await r.json()
                if not dd["running"] and dd["last_run"]:
                    break
            lr = dd.get("last_run", {})
            print(f"futás: {lr.get('slugs')} ismeretlen jelölt-slug, {lr.get('found')} találat, {lr.get('secs')} mp")
            for c in dd.get("candidates", []):
                print(f"  TALÁLAT: {c['slug']}  ->  {c['url']}")
            if not dd.get("candidates"):
                print("  nincs találat (minden jelölt slug már a katalógusban van vagy nem él)")
        finally:
            proxy.terminate()
            await proxy.wait()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hosts", default="88.212.15.22,88.212.15.93")
    p.add_argument("--max", type=int, default=150)
    if not SRC or not EPG:
        print("add meg TSP_TEST_SOURCE és TSP_TEST_EPG környezeti változóban (M3U és XMLTV URL)")
        raise SystemExit(2)
    p.add_argument("--timeout", type=float, default=600)
    asyncio.run(main(p.parse_args()))
