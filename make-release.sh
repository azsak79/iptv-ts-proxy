#!/usr/bin/env bash
# Kiadható csomag: dist/iptv-ts-proxy-<verzió>.tar.gz (+ .sha256). Csak a git által követett fájlokból.
set -euo pipefail
cd "$(dirname "$0")"
V=$(cat VERSION)
[ -z "$(git status --porcelain)" ] || { echo "a munkakönyvtár nem tiszta, előbb commitolj" >&2; exit 1; }
mkdir -p dist
OUT=dist/iptv-ts-proxy-$V.tar.gz
git archive --format=tar --prefix="iptv-ts-proxy-$V/" HEAD -- install.sh VERSION README.md CHANGELOG.md docs app tests | gzip -9n >"$OUT"
( cd dist && sha256sum "$(basename "$OUT")" >"$(basename "$OUT").sha256" )
echo "$OUT"; cat "$OUT.sha256"
