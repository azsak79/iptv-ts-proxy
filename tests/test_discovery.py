#!/usr/bin/env python3
"""A csatorna-felderítés végponttól végpontig: mock relay + proxy + felderítés futtatása.

Ellenőrzés: az ismeretlen jelölt-slugok megtalálása, a katalógusból ismertek kihagyása,
importálás letiltott csatornaként."""
import asyncio
import base64
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_scenarios import A, B, C, CTL, PPORT, PW, AUTH, ctl, start_proxy  # noqa: E402

import aiohttp


async def main():
    relay = await asyncio.create_subprocess_exec(
        sys.executable, "-I", os.path.join(HERE, "mock_relay.py"), stdout=asyncio.subprocess.DEVNULL)
    async with aiohttp.ClientSession() as s:
        # megvárjuk, míg a relay vezérlője él
        for _ in range(50):
            try:
                async with s.get(f"{CTL}/stat") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.2)
        for h in (A, B, C):
            await ctl(s, h, "ok")
        data = os.path.join("/tmp/opencode", "discovery-test")
        os.system(f"rm -rf {data}")
        env = {"TSP_DISCOVERY_NAMES": "newch\nMore TV"}
        proxy = await start_proxy(os.path.join(os.path.dirname(HERE), "app"), data, env)
        ok, fail = 0, 0

        def check(what, cond):
            nonlocal ok, fail
            print(("  OK   " if cond else "  HIBA ") + what)
            ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)
        try:
            # a felderítés futtatása
            async with s.post(f"http://127.0.0.1:{PPORT}/api/discovery/run", headers=AUTH, json={}) as r:
                check("felderítés indítása", r.status == 200)
            cands = []
            for _ in range(40):
                await asyncio.sleep(0.5)
                async with s.get(f"http://127.0.0.1:{PPORT}/api/discovery", headers=AUTH) as r:
                    d = await r.json()
                if not d["running"] and d["last_run"]:
                    cands = d["candidates"]
                    break
            slugs = {c["slug"] for c in cands}
            check(f"találatok: {sorted(slugs)}", "newch" in slugs and "more_tv" in slugs)
            check("a katalógus saját slugja (testch) Nincs a találatok közt", "testch" not in slugs)
            check("a jelöltnek van host-URL-je", all(c.get("url") for c in cands))
            # importálás letiltva
            async with s.post(f"http://127.0.0.1:{PPORT}/api/discovery/import", headers=AUTH,
                              json={"slugs": ["newch"]}) as r:
                imp = await r.json()
            check(f"importálás: {imp.get('added')}", len(imp.get("added", [])) == 1)
            async with s.get(f"http://127.0.0.1:{PPORT}/api/catalog", headers=AUTH) as r:
                cat = await r.json()
            ch = next(c for c in cat["channels"] if c["id"] in imp["added"])
            check("importált csatorna letiltva", ch["enabled"] is False)
            check(f"importált csatorna URL-je: {ch['urls'][0]}", ch["urls"] and ch["urls"][0].startswith("http://127.0.0."))
            check("importált név: Newch", ch["name"] == "Newch")
            # esemény
            async with s.get(f"http://127.0.0.1:{PPORT}/api/events", headers=AUTH) as r:
                ev = await r.json()
            check("discovered esemény keletkezett", any(e["type"] == "discovered" for e in ev["events"]))
            # szólista-fájl (discovery-slugs.txt): a szavak is jelöltek
            with open(os.path.join(data, "discovery-slugs.txt"), "w") as f:
                f.write("wlslug\n")
            async with s.post(f"http://127.0.0.1:{PPORT}/api/discovery/run", headers=AUTH, json={}) as r:
                check("szólistás felderítés indítása", r.status == 200)
            for _ in range(40):
                await asyncio.sleep(0.5)
                async with s.get(f"http://127.0.0.1:{PPORT}/api/discovery", headers=AUTH) as r:
                    d = await r.json()
                if not d["running"] and d["last_run"].get("found"):
                    break
            slugs2 = {c["slug"] for c in d["candidates"]}
            check(f"szólista-fájl találata: {sorted(slugs2)}", "wlslug" in slugs2)
        finally:
            proxy.terminate()
            await proxy.wait()
            for h in (A, B, C):
                await ctl(s, h, "ok")
        relay.terminate()
        await relay.wait()
        print(f"eredmény: {ok} OK, {fail} HIBA")
        return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
