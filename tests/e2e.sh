#!/usr/bin/env bash
# Végponttól végpontig teszt root jog és systemd nélkül: staging telepítés (--root), indítás,
# HTTP-ellenőrzések egy hamis HLS-szolgáltatóval, frissítés, eltávolítás.
# Futtatás: tests/e2e.sh   (internet kell a pip install-hoz)
set -Eeuo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
R=$(mktemp -d /tmp/iptv-ts-proxy-e2e.XXXXXX)
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; [ "${KEEP:-0}" = 1 ] || rm -rf "$R"; }
trap cleanup EXIT
pass=0 fail=0
ok()  { echo "  OK   $*"; pass=$((pass + 1)); }
bad() { echo "  HIBA $*"; fail=$((fail + 1)); }
check() { local d=$1; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
freeport() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }

UP=$(freeport) PX=$(freeport)
python3 -I "$HERE/tests/fake_upstream.py" "$UP" & PIDS+=($!)
PASS=tesztjelszo123

echo "== telepítés (staging: $R)"
TSP_PORT=$PX TSP_LISTEN=127.0.0.1 TSP_ADMIN_PASSWORD=$PASS IPTV_EXTERNAL=1 \
TSP_SOURCE=http://127.0.0.1:$UP/list.m3u TSP_EPG=http://127.0.0.1:$UP/epg.xml \
    "$HERE/install.sh" --unattended --root "$R" >"$R/install.out" 2>&1 || { cat "$R/install.out"; exit 1; }
ENV=$R/etc/iptv-ts-proxy/iptv-ts-proxy.env UNIT=$R/etc/systemd/system/iptv-ts-proxy.service
check "env fájl 600-as joggal" test "$(stat -c %a "$ENV")" = 600
check "env: jelszó" grep -qx "TSP_ADMIN_PASSWORD='$PASS'" "$ENV"
check "env: külső token generálva" grep -qE "^TSP_EXTERNAL_TOKEN='[A-Za-z0-9_-]{20,}'$" "$ENV"
check "unit: ExecStart" grep -q "^ExecStart=$R/opt/iptv-ts-proxy/venv/bin/python -I $R/opt/iptv-ts-proxy/app/proxy.py$" "$UNIT"
check "aiohttp a venv-ben" "$R/opt/iptv-ts-proxy/venv/bin/python" -c 'import aiohttp'
if command -v systemd-analyze >/dev/null 2>&1; then
    sed "s#^User=.*#User=nobody#;s#^Group=.*#Group=nogroup#" "$UNIT" >"$R/verify.service"
    check "systemd-analyze verify" bash -c "systemd-analyze verify '$R/verify.service' 2>&1 | grep -v -e 'nogroup' -e 'Special user nobody' -e 'Documentation' | { ! grep -q .; }"
fi

echo "== indítás"
( set -a; . "$ENV"; set +a; exec "$R/opt/iptv-ts-proxy/venv/bin/python" -I "$R/opt/iptv-ts-proxy/app/proxy.py" ) >"$R/proxy.log" 2>&1 & PIDS+=($!)
B=http://127.0.0.1:$PX
for _ in $(seq 1 40); do curl -fs "$B/health" >/dev/null 2>&1 && break; sleep 0.25; done
check "/health = ok" test "$(curl -fs "$B/health")" = ok
sleep 1
PL=$(curl -fs "$B/playlist.m3u?all=1")
check "playlist: 2 csatorna" test "$(grep -c '/ch/.*\.ts$' <<<"$PL")" = 2
CH=$(grep -m1 -o '/ch/[^ ]*\.ts' <<<"$PL")
check "admin auth nélkül: 401" test "$(curl -s -o /dev/null -w '%{http_code}' "$B/admin")" = 401
check "admin rossz jelszó: 401" test "$(curl -s -o /dev/null -w '%{http_code}' -u x:rossz "$B/api/status")" = 401
check "api/status jelszóval: 200" test "$(curl -s -o /dev/null -w '%{http_code}' -u "x:$PASS" "$B/api/status")" = 200
EPG=$(curl -fs "$B/epg.xml" || true)
check "EPG: szűrt (tv1 benne, other nincs)" bash -c '[[ "$1" == *tv1.test* && "$1" != *other.test* ]]' _ "$EPG"
TOK=$(sed -n "s/^TSP_EXTERNAL_TOKEN='\(.*\)'$/\1/p" "$ENV")
check "külső playlist tokennel: 200" test "$(curl -s -o /dev/null -w '%{http_code}' "$B/tv/$TOK/playlist.m3u")" = 200
check "külső playlist rossz tokennel: 404" test "$(curl -s -o /dev/null -w '%{http_code}' "$B/tv/rossz/playlist.m3u")" = 404
check "külső playlist: abszolút /tv/<token>/ch URL" bash -c "curl -fs '$B/tv/$TOK/playlist.m3u?all=1' | grep -q '^$B/tv/$TOK/ch/'"
curl -s --max-time 8 -r 0-0 "$B$CH" -o "$R/stream.ts" || true
python3 - "$R/stream.ts" <<'EOF' && ok "stream: >=200 KB, TS-szinkron 188 bájtonként" || bad "stream: TS-ellenőrzés"
import sys
d = open(sys.argv[1], "rb").read()
n = len(d) // 188
sys.exit(0 if len(d) >= 200_000 and all(d[i * 188] == 0x47 for i in range(n)) else 1)
EOF
check "katalógus a data alatt" test -s "$R/var/lib/iptv-ts-proxy/katalog.json"
kill "${PIDS[1]}"; wait "${PIDS[1]}" 2>/dev/null || true

echo "== frissítés (idempotens, beállítások maradnak)"
"$HERE/install.sh" update --root "$R" >"$R/update.out" 2>&1 || { cat "$R/update.out"; exit 1; }
check "update: jelszó változatlan" grep -qx "TSP_ADMIN_PASSWORD='$PASS'" "$ENV"
check "update: token változatlan" grep -qx "TSP_EXTERNAL_TOKEN='$TOK'" "$ENV"
check "update: előző verzió mentve" bash -c "ls -d '$R'/opt/iptv-ts-proxy/backup-* >/dev/null"
"$HERE/install.sh" install --unattended --root "$R" >"$R/reinstall.out" 2>&1
check "újratelepítés = frissítés" grep -q "beállítások maradnak" "$R/reinstall.out"

echo "== érvénytelen bemenet"
check "rossz port elutasítva" bash -c "! TSP_PORT=99999 '$HERE/install.sh' --unattended --root '$R/x' >/dev/null 2>&1"
check "veszélyes jelszó elutasítva" bash -c "! TSP_ADMIN_PASSWORD='a\$(id)bcdefgh' '$HERE/install.sh' --unattended --root '$R/y' >/dev/null 2>&1"

echo "== elérhetetlen induló M3U: a proxy elindul, nem ment üres katalógust"
R2=$R/bad PX2=$(freeport)
TSP_PORT=$PX2 TSP_LISTEN=127.0.0.1 TSP_SOURCE=http://127.0.0.1:1/nincs.m3u \
    "$HERE/install.sh" --unattended --root "$R2" >/dev/null 2>&1
( set -a; . "$R2/etc/iptv-ts-proxy/iptv-ts-proxy.env"; set +a; exec "$R2/opt/iptv-ts-proxy/venv/bin/python" -I "$R2/opt/iptv-ts-proxy/app/proxy.py" ) >"$R/proxy2.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 40); do curl -fs "http://127.0.0.1:$PX2/health" >/dev/null 2>&1 && break; sleep 0.25; done
check "rossz M3U mellett is fut" test "$(curl -fs "http://127.0.0.1:$PX2/health")" = ok
check "nincs elmentett üres katalógus" test ! -e "$R2/var/lib/iptv-ts-proxy/katalog.json"
check "generált jelszó a kimenetben" bash -c "TSP_PORT=$(freeport) '$HERE/install.sh' --unattended --root '$R/gen' | grep -qE 'generált admin jelszó: [A-Za-z0-9_-]{16}'"
kill "${PIDS[-1]}" 2>/dev/null || true

echo "== eltávolítás"
"$HERE/install.sh" uninstall --yes --root "$R" >/dev/null 2>&1
check "uninstall: /opt törölve" test ! -e "$R/opt/iptv-ts-proxy"
check "uninstall: beállítás és adat marad" test -f "$ENV" -a -f "$R/var/lib/iptv-ts-proxy/katalog.json"
"$HERE/install.sh" uninstall --purge --yes --root "$R" >/dev/null 2>&1
check "purge: minden törölve" test ! -e "$R/etc/iptv-ts-proxy" -a ! -e "$R/var/lib/iptv-ts-proxy"

echo
echo "eredmény: $pass OK, $fail HIBA"
[ "$fail" = 0 ] || { echo "proxy napló:"; tail -n 30 "$R/proxy.log" 2>/dev/null; exit 1; }
