#!/usr/bin/env python3
"""Valódi dekódolás egy TS-folyamon (a Plex transcodere is ffmpeg-alapú): első kép ideje,
dekódolt időtartam a falióra-időhöz képest, ffmpeg-figyelmeztetések kategóriánként.

Használat: .venv/bin/python -I tests/ffmpeg_check.py <url> [--secs 40]
Az ffmpeg a venv-be telepített imageio-ffmpeg csomagból jön (rendszerszintű telepítés nem kell)."""
import argparse
import collections
import json
import re
import subprocess
import time

import imageio_ffmpeg

CATS = [("pts/dts", r"(non monoton|DTS|PTS|timestamp)"), ("folytonosság", r"(continuity|Packet corrupt|discontinuity)"),
        ("dekódolás", r"(error while decoding|concealing|corrupt|Invalid NAL|no frame|missing picture|decode_slice)"),
        ("kapcsolat", r"(Connection|timed out|End of file|I/O error|Server returned)")]


def main(url, secs):
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-v", "warning", "-progress", "pipe:1",
           "-rw_timeout", "15000000", "-i", url, "-t", str(secs), "-map", "0:v:0?", "-map", "0:a:0?", "-f", "null", "-"]
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    first, out_us = None, 0
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                v = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if v > 0 and first is None:
                first = round(time.time() - t0, 2)
            out_us = max(out_us, v)
        if time.time() - t0 > secs + 60:
            p.kill()
            break
    err = p.stderr.read().splitlines()
    p.wait()
    cnt = collections.Counter()
    for e in err:
        for name, rx in CATS:
            if re.search(rx, e, re.I):
                cnt[name] += 1
                break
        else:
            cnt["egyéb"] += 1
    wall = time.time() - t0
    print("RESULT " + json.dumps({"url": url, "first_frame_s": first, "decoded_s": round(out_us / 1e6, 1),
                                  "wall_s": round(wall, 1), "rc": p.returncode, "warnings": dict(cnt),
                                  "sample": err[:6]}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--secs", type=float, default=40)
    a = ap.parse_args()
    main(a.url, a.secs)
