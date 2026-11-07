#!/usr/bin/env bash
# iptv-ts-proxy telepítő varázsló (Linux, systemd).
#
#   sudo ./install.sh                 interaktív telepítés (varázsló)
#   sudo ./install.sh update          frissítés a csomag verziójára (a beállítások maradnak)
#   sudo ./install.sh reconfigure     a varázsló újrafuttatása a meglévő beállításokkal
#   sudo ./install.sh status          állapot és címek
#   sudo ./install.sh uninstall       eltávolítás (a beállítások és adatok maradnak)
#   sudo ./install.sh uninstall --purge   eltávolítás mindennel együtt
#
# Kapcsolók: --unattended (kérdések nélkül, környezeti változókból), --yes, --root DIR (staging,
# root jog és systemd nélkül, csomagoláshoz/teszthez), --no-start, --no-deps, -h|--help.
# Részletek: README.md
set -Eeuo pipefail

APP=iptv-ts-proxy
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PKG_VERSION=$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo "0.0.0")
PY_MIN_MAJOR=3 PY_MIN_MINOR=9

CMD=install UNATTENDED=0 YES=0 ROOT="" NO_START=0 NO_DEPS=0 PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        install|update|reconfigure|status|uninstall) CMD=$1 ;;
        --unattended) UNATTENDED=1; YES=1 ;;
        -y|--yes) YES=1 ;;
        --root) ROOT=${2:?--root után könyvtár kell}; shift ;;
        --root=*) ROOT=${1#--root=} ;;
        --no-start) NO_START=1 ;;
        --no-deps) NO_DEPS=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ismeretlen kapcsoló: $1 (súgó: $0 --help)" >&2; exit 2 ;;
    esac
    shift
done
[ -n "$ROOT" ] && ROOT=$(mkdir -p "$ROOT" && cd "$ROOT" && pwd) && NO_START=1 && NO_DEPS=1

# ------------------------------------------------------------------ útvonalak
OPT=$ROOT/opt/$APP
ETC=$ROOT/etc/$APP
ENV_FILE=$ETC/$APP.env
DATA=$ROOT/var/lib/$APP
UNIT=$ROOT/etc/systemd/system/$APP.service
LOG=$ROOT/var/log/$APP-install.log
SVC_USER=$APP
# a unitba és az env fájlba a célrendszer útvonala kerül (staging esetén is)
T_OPT=/opt/$APP T_DATA=/var/lib/$APP T_ENV=/etc/$APP/$APP.env
[ -n "$ROOT" ] && T_OPT=$OPT T_DATA=$DATA T_ENV=$ENV_FILE

# ------------------------------------------------------------------ kimenet
if [ -t 1 ]; then B=$'\e[1m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' N=$'\e[0m'; else B="" R="" G="" Y="" N=""; fi
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log()  { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null || true; }
info() { printf '%s==>%s %s\n' "$G" "$N" "$*"; log "INFO $*"; }
warn() { printf '%s!!%s  %s\n' "$Y" "$N" "$*" >&2; log "WARN $*"; }
die()  { printf '%sHIBA:%s %s\n' "$R" "$N" "$*" >&2; log "ERROR $*"; exit 1; }
# 130 = a felhasználó megszakította (a kérdező függvények már kiírták)
trap 'rc=$?; [ "$rc" = 130 ] && exit 130; die "váratlan hiba a(z) $LINENO. sorban (napló: $LOG)"' ERR
cancel() { printf '%sMegszakítva.%s\n' "$Y" "$N" >&2; log "megszakítva"; exit 130; }

UI=plain
if [ "$UNATTENDED" = 0 ] && [ "${IPTV_UI:-}" != plain ] && [ -t 0 ] && [ -t 1 ] && command -v whiptail >/dev/null 2>&1; then UI=whiptail; fi
TITLE="iptv-ts-proxy $PKG_VERSION telepítő"

# ask_input <kérdés> <alapérték> -> stdout
ask_input() {
    if [ "$UI" = whiptail ]; then
        whiptail --title "$TITLE" --inputbox "$1" 12 74 "$2" 3>&1 1>&2 2>&3 || cancel
    else
        local a; read -r -p "$1 [$2]: " a </dev/tty || cancel; printf '%s' "${a:-$2}"
    fi
}
ask_password() {
    if [ "$UI" = whiptail ]; then
        whiptail --title "$TITLE" --passwordbox "$1" 12 74 3>&1 1>&2 2>&3 || cancel
    else
        local a; read -r -s -p "$1: " a </dev/tty || cancel; echo >&2; printf '%s' "$a"
    fi
}
# ask_yesno <kérdés> <alap: y|n> -> 0 = igen
ask_yesno() {
    if [ "$UI" = whiptail ]; then
        local d=(); [ "$2" = n ] && d=(--defaultno)
        whiptail --title "$TITLE" "${d[@]}" --yesno "$1" 12 74
    else
        local a h="[I/n]"; [ "$2" = n ] && h="[i/N]"
        read -r -p "$1 $h: " a </dev/tty || cancel
        a=${a:-$2}; [[ "$a" =~ ^[iIyY] ]]
    fi
}
msg() { if [ "$UI" = whiptail ]; then whiptail --title "$TITLE" --msgbox "$1" 20 76; else printf '\n%s\n\n' "$1"; fi; }

confirm() { [ "$YES" = 1 ] && return 0; ask_yesno "$1" y; }

# ------------------------------------------------------------------ segédek
need_root() { [ -n "$ROOT" ] || [ "$(id -u)" = 0 ] || die "root jog kell: sudo $0 $CMD"; }

gen_secret() { "$PY" -c "import secrets,sys;print(secrets.token_urlsafe(int(sys.argv[1])))" "$1"; }

# Az env fájl értékei egyszeres idézőjelben állnak: ez a systemd EnvironmentFile-ban és a
# bash `source`-ban is szó szerinti. Ezért ' \ " $ ` és szóköz nem lehet az értékekben.
safe_value() { [[ "$1" != *[[:space:]\'\"\\\$\`]* ]]; }
valid_url()  { [[ "$1" =~ ^https?://[^/[:space:]]+ ]] && safe_value "$1"; }
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
port_busy()  { command -v ss >/dev/null 2>&1 && ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }

# env fájl olvasása biztonságosan (nem source-oljuk: csak KULCS=érték sorok)
env_get() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | sed -e "s/^'\(.*\)'\$/\1/" -e 's/^"\(.*\)"$/\1/'
}
quote() { printf "'%s'" "$1"; }

pkg_mgr() {
    for m in apt-get dnf yum zypper pacman apk; do command -v $m >/dev/null 2>&1 && { echo $m; return; }; done
    echo none
}

pkg_install() {
    local m; m=$(pkg_mgr)
    info "csomagok telepítése ($m): $*"
    case $m in
        apt-get) DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf|yum) $m install -y -q "$@" ;;
        zypper)  zypper --non-interactive install "$@" ;;
        pacman)  pacman -Sy --noconfirm --needed "$@" ;;
        apk)     apk add --no-cache "$@" ;;
        *) die "nem ismert csomagkezelő; telepítsd kézzel: $*" ;;
    esac >>"$LOG" 2>&1 || die "a csomagtelepítés nem sikerült (napló: $LOG)"
}

# CA-tár: a disztrók szokásos bundle-fájljai, végül a Python saját (fájlból betöltött) tára
has_ca() {
    local f
    for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \
             /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/ssl/ca-bundle.pem /etc/ssl/cert.pem; do
        [ -s "$f" ] && return 0
    done
    "$PY" -c "import ssl,sys;sys.exit(0 if ssl.create_default_context().cert_store_stats()['x509_ca'] else 1)" >/dev/null 2>&1
}

find_python() {
    local c
    for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys;sys.exit(0 if sys.version_info>=($PY_MIN_MAJOR,$PY_MIN_MINOR) else 1)" 2>/dev/null; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

check_deps() {
    command -v systemctl >/dev/null 2>&1 || [ -n "$ROOT" ] || die "systemd szükséges (systemctl nem található)"
    local missing=()
    PY=$(find_python) || missing+=(python)
    if [ -n "${PY:-}" ] && ! "$PY" -c "import venv, ensurepip" >/dev/null 2>&1; then missing+=(venv); fi
    # minimál rendszeren gyakran nincs CA-tár: enélkül a HTTPS-es M3U/EPG/stream letöltés nem megy
    if [ -n "${PY:-}" ] && ! has_ca; then
        missing+=(ca-certificates)
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        [ "$NO_DEPS" = 1 ] && die "hiányzó függőség: ${missing[*]} (Python >= $PY_MIN_MAJOR.$PY_MIN_MINOR + venv modul, CA-tanúsítványok)"
        local pk=()
        case $(pkg_mgr) in
            apt-get) pk=(python3 python3-venv ca-certificates) ;;
            dnf|yum|zypper) pk=(python3 ca-certificates) ;;
            pacman) pk=(python ca-certificates) ;;
            apk) pk=(python3 py3-virtualenv ca-certificates) ;;
        esac
        confirm "Hiányzik: ${missing[*]}. Telepítsem ezeket: ${pk[*]}?" || die "függőség nélkül nem folytatható"
        pkg_install "${pk[@]}"
        PY=$(find_python) || die "Python >= $PY_MIN_MAJOR.$PY_MIN_MINOR nem található a telepítés után sem"
        "$PY" -c "import venv, ensurepip" >/dev/null 2>&1 || die "a Python venv modul nem működik ($PY -m venv)"
        has_ca || warn "a CA-tár még mindig üres: a HTTPS-es források nem fognak működni (update-ca-certificates?)"
    fi
    info "Python: $PY ($("$PY" -V 2>&1))"
}

# ------------------------------------------------------------------ varázsló
wizard() {
    local reconf=$1
    # alapértékek: meglévő env > környezeti változó > beépített
    C_PORT=$(env_get TSP_PORT);            C_PORT=${C_PORT:-${TSP_PORT:-8095}}
    C_LISTEN=$(env_get TSP_LISTEN);        C_LISTEN=${C_LISTEN:-${TSP_LISTEN:-0.0.0.0}}
    C_PASS=$(env_get TSP_ADMIN_PASSWORD);  C_PASS=${C_PASS:-${TSP_ADMIN_PASSWORD:-}}
    C_SOURCE=$(env_get TSP_SOURCE);        C_SOURCE=${C_SOURCE:-${TSP_SOURCE:-}}
    C_EPG=$(env_get TSP_EPG);              C_EPG=${C_EPG:-${TSP_EPG:-}}
    C_TOKEN=$(env_get TSP_EXTERNAL_TOKEN); C_TOKEN=${C_TOKEN:-${TSP_EXTERNAL_TOKEN:-}}
    C_EXTBASE=$(env_get TSP_EXTERNAL_BASE); C_EXTBASE=${C_EXTBASE:-${TSP_EXTERNAL_BASE:-}}
    C_LOGOS=$(env_get TSP_LOGOS_DIR);      C_LOGOS=${C_LOGOS:-${TSP_LOGOS_DIR:-}}
    C_PUBLIC=$(env_get TSP_PUBLIC);        C_PUBLIC=${C_PUBLIC:-${TSP_PUBLIC:-}}
    C_EXTRA=$(grep -vE '^(TSP_(PORT|LISTEN|ADMIN_PASSWORD|SOURCE|EPG|EXTERNAL_TOKEN|EXTERNAL_BASE|LOGOS_DIR|PUBLIC|DATA)=|#|$)' "$ENV_FILE" 2>/dev/null || true)
    PASS_GENERATED=0

    if [ "$UNATTENDED" = 1 ]; then
        [ -n "$C_PASS" ] || { C_PASS=$(gen_secret 12); PASS_GENERATED=1; }
        if [ "${IPTV_EXTERNAL:-0}" = 1 ] && [ -z "$C_TOKEN" ]; then C_TOKEN=$(gen_secret 18); fi
    else
        [ "$reconf" = 1 ] || msg "Üdv! A varázsló beállítja az iptv-ts-proxy szolgáltatást.

A proxy HLS (m3u8) IPTV-adásokat folyamatos MPEG-TS-ként ad tovább (NextPVR, Jellyfin, Kodi,
VLC, TVHeadend), csatornakatalógussal, forrás-ellenőrzéssel, EPG-vel és webes admin felülettel.

Minden beállítás később is módosítható: sudo $0 reconfigure
illetve az admin felület Beállítások fülén."
        while :; do
            C_PORT=$(ask_input "HTTP port, amin a proxy figyel:" "$C_PORT")
            valid_port "$C_PORT" || { msg "Érvénytelen port: $C_PORT (1-65535)"; continue; }
            if [ "$reconf" = 0 ] && port_busy "$C_PORT"; then msg "A(z) $C_PORT port foglalt, válassz másikat."; continue; fi
            break
        done
        if ask_yesno "Elérhető legyen a proxy a helyi hálózatról is?

Igen: 0.0.0.0 (a hálózat összes eszköze eléri; lejátszókhoz ez kell)
Nem: 127.0.0.1 (csak erről a gépről, pl. reverse proxy mögött)" "$([ "$C_LISTEN" = 127.0.0.1 ] && echo n || echo y)"; then
            C_LISTEN=0.0.0.0; else C_LISTEN=127.0.0.1; fi

        local p1 p2
        while :; do
            p1=$(ask_password "Admin jelszó a webes felülethez (üresen hagyva $( [ -n "$C_PASS" ] && echo "marad a jelenlegi" || echo "erős jelszót generálok"))")
            if [ -z "$p1" ]; then
                [ -n "$C_PASS" ] || { C_PASS=$(gen_secret 12); PASS_GENERATED=1; }
                break
            fi
            [ ${#p1} -ge 8 ] || { msg "A jelszó legalább 8 karakter legyen."; continue; }
            safe_value "$p1" || { msg "A jelszóban nem lehet szóköz és ' \" \\ \$ \` karakter."; continue; }
            p2=$(ask_password "Jelszó még egyszer")
            [ "$p1" = "$p2" ] || { msg "A két jelszó nem egyezik."; continue; }
            C_PASS=$p1; break
        done

        if [ ! -f "$DATA/katalog.json" ]; then
            while :; do
                C_SOURCE=$(ask_input "Induló csatornalista (M3U URL), ebből jön létre a katalógus.
Üresen hagyható: a csatornák az admin felületen is felvehetők / importálhatók." "$C_SOURCE")
                [ -z "$C_SOURCE" ] || valid_url "$C_SOURCE" || { msg "http(s):// kezdetű URL kell."; continue; }
                break
            done
        fi
        while :; do
            C_EPG=$(ask_input "EPG (XMLTV) forrás URL, .xml vagy .xml.gz (üresen: nincs EPG):" "$C_EPG")
            [ -z "$C_EPG" ] || valid_url "$C_EPG" || { msg "http(s):// kezdetű URL kell."; continue; }
            break
        done
        C_LOGOS=$(ask_input "Csatornalogók könyvtára (<csatornanév>.png fájlok; üresen: nincs).
A /home alatti könyvtár nem olvasható a szolgáltatás számára." "$C_LOGOS")
        if [ -n "$C_LOGOS" ] && [ -z "$ROOT" ] && [ ! -d "$C_LOGOS" ]; then warn "a logókönyvtár nem létezik: $C_LOGOS"; fi

        if ask_yesno "Külső (interneten át, tokenes) elérés bekapcsolása?

Ekkor a /tv/<titkos-token>/playlist.m3u címen is kiszolgál; reverse proxy vagy
tunnel (pl. Cloudflare Tunnel, nginx) mögé való. A token nélküli kérések 404-et kapnak." "$([ -n "$C_TOKEN" ] && echo y || echo n)"; then
            [ -n "$C_TOKEN" ] || C_TOKEN=$(gen_secret 18)
            while :; do
                C_EXTBASE=$(ask_input "Külső URL-előtag (pl. https://tv.example.com/tv; üresen: a kérés címéből)" "$C_EXTBASE")
                [ -z "$C_EXTBASE" ] || valid_url "$C_EXTBASE" || { msg "http(s):// kezdetű URL kell."; continue; }
                break
            done
        else
            C_TOKEN="" C_EXTBASE=""
        fi
    fi

    valid_port "$C_PORT" || die "érvénytelen port: $C_PORT"
    [[ "$C_LISTEN" =~ ^[0-9a-fA-F.:]+$ ]] || die "érvénytelen listen cím: $C_LISTEN"
    for u in "$C_SOURCE" "$C_EPG" "$C_EXTBASE" "$C_PUBLIC"; do [ -z "$u" ] || valid_url "$u" || die "érvénytelen URL: $u"; done
    safe_value "$C_PASS" || die "az admin jelszóban nem lehet szóköz és ' \" \\ \$ \` karakter"
    [ -z "$C_LOGOS" ] || { [[ "$C_LOGOS" = /* ]] && safe_value "$C_LOGOS"; } || die "a logókönyvtár abszolút, szóköz nélküli útvonal legyen: $C_LOGOS"
}

write_env() {
    mkdir -p "$ETC"
    local tmp; tmp=$(mktemp "$ETC/.env.XXXXXX")
    {
        echo "# iptv-ts-proxy beállítások ($(date '+%F %T')). Módosítás után: systemctl restart $APP"
        echo "# Az admin felületen mentett beállítások ($T_DATA/settings.json) ezeket felülírják."
        echo "TSP_PORT=$C_PORT"
        echo "TSP_LISTEN=$C_LISTEN"
        echo "TSP_DATA=$T_DATA"
        echo "TSP_ADMIN_PASSWORD=$(quote "$C_PASS")"
        [ -n "$C_SOURCE" ]  && echo "TSP_SOURCE=$(quote "$C_SOURCE")"
        [ -n "$C_EPG" ]     && echo "TSP_EPG=$(quote "$C_EPG")"
        [ -n "$C_LOGOS" ]   && echo "TSP_LOGOS_DIR=$(quote "$C_LOGOS")"
        [ -n "$C_PUBLIC" ]  && echo "TSP_PUBLIC=$(quote "$C_PUBLIC")"
        [ -n "$C_TOKEN" ]   && echo "TSP_EXTERNAL_TOKEN=$(quote "$C_TOKEN")"
        [ -n "$C_EXTBASE" ] && echo "TSP_EXTERNAL_BASE=$(quote "$C_EXTBASE")"
        [ -n "$C_EXTRA" ]   && printf '%s\n' "$C_EXTRA"
        true
    } >"$tmp"
    chmod 600 "$tmp"
    [ -z "$ROOT" ] && chown root:root "$tmp"
    [ -f "$ENV_FILE" ] && cp -p "$ENV_FILE" "$ENV_FILE.bak"
    mv -f "$tmp" "$ENV_FILE"
    info "beállítások: $ENV_FILE (600)"
}

write_unit() {
    mkdir -p "$(dirname "$UNIT")"
    local rw="$T_DATA" logos_ro=""
    [ -n "$C_LOGOS" ] && logos_ro="ReadOnlyPaths=-$C_LOGOS"
    cat >"$UNIT" <<EOF
# iptv-ts-proxy $PKG_VERSION – a telepítő generálta ($APP install.sh); kézi módosítás helyett: install.sh reconfigure
# vagy override: systemctl edit $APP
[Unit]
Description=iptv-ts-proxy – HLS -> MPEG-TS IPTV proxy
Documentation=file://$T_OPT/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=$T_ENV
WorkingDirectory=$T_OPT/app
ExecStart=$T_OPT/venv/bin/python -I $T_OPT/app/proxy.py
Restart=always
RestartSec=3
TimeoutStopSec=15
StateDirectory=$APP
StateDirectoryMode=0750
LimitNOFILE=65536
MemoryMax=512M
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=$rw
$logos_ro

[Install]
WantedBy=multi-user.target
EOF
    info "systemd unit: $UNIT"
}

install_files() {
    mkdir -p "$OPT"
    local new=$OPT/app.new
    rm -rf "$new"
    mkdir -p "$new"
    install -m 644 "$SRC_DIR"/app/*.py "$SRC_DIR"/app/admin.html "$SRC_DIR"/app/requirements.txt "$new/"
    if ! [ "$SRC_DIR" -ef "$OPT" ]; then
        install -m 644 "$SRC_DIR/VERSION" "$SRC_DIR/README.md" "$OPT/"
        install -m 755 "$SRC_DIR/install.sh" "$OPT/install.sh"
    fi
    if [ -d "$OPT/app" ]; then
        BACKUP=$OPT/backup-$(date +%Y%m%d-%H%M%S)
        mv "$OPT/app" "$BACKUP"
        info "előző verzió mentve: $BACKUP"
    fi
    mv "$new" "$OPT/app"
    # a legutóbbi 3 mentés marad
    find "$OPT" -maxdepth 1 -name 'backup-*' -type d -printf '%T@ %p\n' 2>/dev/null | sort -rn | tail -n +4 | cut -d' ' -f2- | xargs -r rm -rf
}

setup_venv() {
    if [ ! -x "$OPT/venv/bin/python" ] || ! "$OPT/venv/bin/python" -c 'import sys' 2>/dev/null; then
        info "Python virtuális környezet: $OPT/venv"
        rm -rf "$OPT/venv"
        "$PY" -m venv "$OPT/venv" >>"$LOG" 2>&1 || die "venv létrehozása nem sikerült (napló: $LOG)"
    fi
    info "függőségek telepítése (aiohttp) – internetkapcsolat kell"
    "$OPT/venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade -r "$OPT/app/requirements.txt" >>"$LOG" 2>&1 \
        || die "pip install nem sikerült (napló: $LOG). Proxy mögött: export HTTPS_PROXY=..."
    "$OPT/venv/bin/python" -c 'import aiohttp' || die "az aiohttp nem importálható"
}

setup_user() {
    [ -n "$ROOT" ] && return 0
    if ! id "$SVC_USER" >/dev/null 2>&1; then
        local nl; nl=$(command -v nologin || echo /usr/sbin/nologin)
        useradd --system --no-create-home --home-dir "$T_DATA" --shell "$nl" --user-group "$SVC_USER" 2>/dev/null \
            || adduser -S -D -H -h "$T_DATA" -s "$nl" "$SVC_USER" \
            || die "a(z) $SVC_USER felhasználó létrehozása nem sikerült"
        info "szolgáltatásfelhasználó: $SVC_USER"
    fi
    mkdir -p "$DATA"; chown "$SVC_USER:$SVC_USER" "$DATA"; chmod 750 "$DATA"
}

wait_health() {
    local port=$1 host=127.0.0.1
    [ "$C_LISTEN" != 0.0.0.0 ] && [ "$C_LISTEN" != "::" ] && host=$C_LISTEN
    for _ in $(seq 1 30); do
        if "$PY" - "$host" "$port" <<'PYEOF' 2>/dev/null; then return 0; fi
import sys, urllib.request
h, p = sys.argv[1], sys.argv[2]
h = f"[{h}]" if ":" in h else h
with urllib.request.urlopen(f"http://{h}:{p}/health", timeout=2) as r:
    sys.exit(0 if r.read().strip() == b"ok" else 1)
PYEOF
        sleep 1
    done
    return 1
}

start_service() {
    if [ "$NO_START" = 1 ]; then info "indítás kihagyva (--no-start / --root)"; return 0; fi
    systemctl daemon-reload
    systemctl enable "$APP" >>"$LOG" 2>&1
    systemctl restart "$APP"
    info "indítás és állapotellenőrzés..."
    if wait_health "$C_PORT"; then
        info "a szolgáltatás fut és válaszol (/health = ok)"
        return 0
    fi
    warn "a szolgáltatás 30 mp alatt nem válaszolt; utolsó naplósorok:"
    journalctl -u "$APP" -n 20 --no-pager >&2 || true
    return 1
}

rollback() {
    [ -n "${BACKUP:-}" ] && [ -d "$BACKUP" ] || return 1
    warn "visszaállítás az előző verzióra: $BACKUP"
    rm -rf "$OPT/app"; mv "$BACKUP" "$OPT/app"
    [ -f "$ENV_FILE.bak" ] && cp -p "$ENV_FILE.bak" "$ENV_FILE"
    systemctl restart "$APP" && wait_health "$C_PORT" && warn "visszaállítva, az előző verzió fut"
}

lan_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

firewall_hint() {
    [ "$C_LISTEN" = 127.0.0.1 ] && return 0
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        echo "  Tűzfal (ufw aktív), ha kell:   sudo ufw allow from 192.168.0.0/16 to any port $C_PORT proto tcp"
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        echo "  Tűzfal (firewalld aktív), ha kell:   sudo firewall-cmd --permanent --add-port=$C_PORT/tcp && sudo firewall-cmd --reload"
    fi
}

summary() {
    local ip; ip=$(lan_ip); ip=${ip:-<gép-IP>}
    [ "$C_LISTEN" = 127.0.0.1 ] && ip=127.0.0.1
    local base="http://$ip:$C_PORT"
    echo
    echo "${B}iptv-ts-proxy $PKG_VERSION telepítve${N}"
    echo "  Admin felület:  $base/admin   (felhasználónév: bármi)"
    if [ "$PASS_GENERATED" = 1 ]; then
        echo "  Admin jelszó:   ${B}$C_PASS${N}   (generált; később: sudo grep PASSWORD $T_ENV)"
    else
        echo "  Admin jelszó:   a megadott (sudo grep PASSWORD $T_ENV)"
    fi
    echo "  Playlist (M3U): $base/playlist.m3u"
    echo "  EPG (XMLTV):    $base/epg.xml.gz"
    echo "  Egy csatorna:   $base/ch/<id>.ts"
    [ -n "$C_TOKEN" ] && echo "  Külső playlist: ${C_EXTBASE:-$base/tv}/<token>/playlist.m3u   (token: sudo grep TOKEN $T_ENV)"
    echo "  Napló:          journalctl -u $APP -f"
    echo "  Kezelés:        sudo $T_OPT/install.sh {status|update|reconfigure|uninstall}"
    firewall_hint
    echo
}

# ------------------------------------------------------------------ parancsok
do_install() {
    local reconf=$1
    need_root
    [ -f "$SRC_DIR/app/proxy.py" ] || die "a csomag hiányos: $SRC_DIR/app/proxy.py nincs meg"
    log "=== $CMD $PKG_VERSION (root=${ROOT:-/})"
    if [ "$reconf" = 0 ] && [ -f "$ENV_FILE" ] && [ "$CMD" = install ]; then
        info "már telepítve van ($ENV_FILE megvan): a beállítások maradnak, a fájlok frissülnek"
        CMD=update
    fi
    check_deps
    if [ "$CMD" = update ]; then
        [ -f "$ENV_FILE" ] || die "nincs telepítve (nincs $ENV_FILE); előbb: $0 install"
        UNATTENDED=1 wizard 0
    else
        wizard "$reconf"
        [ "$UNATTENDED" = 1 ] || confirm "Mehet a telepítés? (port $C_PORT, cím $C_LISTEN, $OPT)" || cancel
    fi
    BACKUP=""
    [ "$reconf" = 1 ] || { install_files; setup_venv; }
    [ -n "$ROOT" ] && mkdir -p "$DATA"
    setup_user
    write_env
    write_unit
    if ! start_service; then
        if [ "$CMD" = update ]; then rollback || true; fi
        die "a szolgáltatás nem indult el (journalctl -u $APP)"
    fi
    if [ "$NO_START" = 1 ]; then
        info "kész$([ -n "$ROOT" ] && echo " (staging: $ROOT)")"
        [ "$PASS_GENERATED" = 1 ] && echo "  generált admin jelszó: $C_PASS"
        if [ -n "$ROOT" ]; then
            echo "  futtatás kézzel: set -a; . $ENV_FILE; set +a; $OPT/venv/bin/python -I $OPT/app/proxy.py"
        else
            echo "  indítás: systemctl daemon-reload && systemctl enable --now $APP"
        fi
    else
        summary
    fi
    log "=== kész"
}

do_status() {
    local port; port=$(env_get TSP_PORT)
    [ -f "$ENV_FILE" ] || { echo "nincs telepítve"; exit 1; }
    echo "verzió:   $(cat "$OPT/VERSION" 2>/dev/null || echo '?')"
    echo "port:     ${port:-?}   cím: $(env_get TSP_LISTEN)"
    [ -z "$ROOT" ] && systemctl --no-pager --lines=5 status "$APP" || true
}

do_uninstall() {
    need_root
    confirm "Eltávolítsam az iptv-ts-proxy-t?$([ "$PURGE" = 1 ] && echo " (--purge: a katalógus, a beállítások és a felhasználó is törlődik!)")" || cancel
    if [ -z "$ROOT" ] && command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "$APP" >>"$LOG" 2>&1 || true
    fi
    rm -f "$UNIT"
    [ -z "$ROOT" ] && systemctl daemon-reload 2>/dev/null || true
    rm -rf "$OPT"
    if [ "$PURGE" = 1 ]; then
        rm -rf "$ETC" "$DATA"
        [ -z "$ROOT" ] && id "$SVC_USER" >/dev/null 2>&1 && { userdel "$SVC_USER" 2>/dev/null || deluser "$SVC_USER" 2>/dev/null || true; }
        info "eltávolítva (beállítások, adatok és felhasználó is)"
    else
        info "eltávolítva; megmaradt: $ETC (beállítások), $DATA (katalógus, statisztika). Teljes törlés: uninstall --purge"
    fi
}

case $CMD in
    install)     do_install 0 ;;
    update)      do_install 0 ;;
    reconfigure) [ -f "$ENV_FILE" ] || die "nincs telepítve"; do_install 1 ;;
    status)      do_status ;;
    uninstall)   do_uninstall ;;
esac
