#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

usage() {
    cat <<'EOF'
Usage: bash install.sh [--yes] [--port PORT] [--lan HOSTNAME_OR_IPV4]

Install GitMirror from this checkout on Debian 12+ or Raspberry Pi OS with
systemd. The default dashboard listens on 127.0.0.1:8787. --lan explicitly
enables the LAN listener with a locally generated TLS certificate.
--yes authorizes installation of any missing system dependencies.
EOF
}

script=$(realpath -- "${BASH_SOURCE[0]}")
source_dir=$(dirname -- "$script")
approve=0
port=8787
lan=
original=("$@")
while (($#)); do
    case "$1" in
        --yes) approve=1; shift ;;
        --port) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; port=$2; shift 2 ;;
        --lan) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; lan=$2; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1024 && 10#$port <= 65535)) || {
    printf 'Choose a dashboard port from 1024 to 65535.\n' >&2; exit 2;
}
port=$((10#$port))
if [[ -n "$lan" && ! "$lan" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; then
    printf 'Use a LAN hostname or IPv4 address.\n' >&2
    exit 2
fi
if [[ $(id -u) != 0 ]]; then
    printf 'Administrator access is required to install dependencies and create systemd services.\n'
    exec sudo -- bash "$script" "${original[@]}"
fi

if [[ ! -d /run/systemd/system ]] || ! command -v apt-get >/dev/null || ! command -v dpkg-query >/dev/null; then
    printf 'Run this installer on a Debian-based system booted with systemd.\n' >&2
    exit 2
fi
exec 9>/run/lock/gitmirror-install.lock
flock -n 9 || { printf 'Another GitMirror installation is active.\n' >&2; exit 2; }
if [[ ! -f "$source_dir/pyproject.toml" || ! -f "$source_dir/gitmirror.py" ]]; then
    printf 'Run install.sh from the GitMirror source checkout.\n' >&2
    exit 2
fi

application=/opt/gitmirror
state=/var/lib/gitmirror
account=gitmirror-svc
web_unit=/etc/systemd/system/gitmirror-web.service
scheduler_unit=/etc/systemd/system/gitmirror-scheduler.service
if [[ -e "$application" || -L "$application" || -e "$state" || -L "$state" ||
      -e "$web_unit" || -L "$web_unit" || -e "$scheduler_unit" || -L "$scheduler_unit" ]] ||
      getent passwd "$account" >/dev/null || getent group "$account" >/dev/null; then
    printf 'An existing GitMirror installation or service account requires a manual migration.\n' >&2
    exit 2
fi

required=(python3 python3-venv git git-lfs openssh-client ca-certificates)
[[ -n "$lan" ]] && required+=(openssl)
missing=()
for package in "${required[@]}"; do
    if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
        missing+=("$package")
    fi
done
if (("${#missing[@]}")); then
    printf 'System packages to install: %s\n' "${missing[*]}"
    if ((approve == 0)); then
        read -r -p 'Install these packages with apt? [y/N] ' reply </dev/tty || exit 1
        [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]] || exit 1
    fi
    apt-get update
    apt-get install -y -- "${missing[@]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    printf 'GitMirror requires Python 3.11 or newer.\n' >&2; exit 2;
}

mkdir -- "$application"
temporary=$(mktemp -d -p "$application" source.XXXXXXXX)
account_created=0
state_created=0
web_written=0
scheduler_written=0
complete=0
cleanup() {
    if ((complete == 0)); then
        if ((web_written)); then
            systemctl disable --now gitmirror-web.service || true
            rm -f -- "$web_unit"
        fi
        if ((scheduler_written)); then
            systemctl disable --now gitmirror-scheduler.service || true
            rm -f -- "$scheduler_unit"
        fi
        if ((web_written || scheduler_written)); then systemctl daemon-reload || true; fi
        if ((state_created)); then
            # This path was absent before this invocation and has never exposed a setup code.
            rm -rf -- "$state"
        fi
        if ((account_created)); then
            userdel "$account" || true
            groupdel "$account" 2>/dev/null || true
        fi
        rm -rf -- "$application"
    fi
}
trap cleanup EXIT

install -m 0644 "$source_dir"/{gitmirror.py,pyproject.toml,README.md,LICENSE,NOTICE.txt} "$temporary"
install -d "$temporary/web"
install -m 0644 "$source_dir"/web/{app.js,index.html,style.css} "$temporary/web"
python3 -m venv "$application/venv"
"$application/venv/bin/python" -m pip install --disable-pip-version-check "$temporary[web]"
rm -rf -- "$temporary"

useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin "$account"
account_created=1
install -d -m 0700 -o "$account" -g "$account" "$state"
state_created=1
runuser -u "$account" -- "$application/venv/bin/gitmirror" managed-init --state-dir "$state"

web_args="--port $port"
url="http://127.0.0.1:$port"
if [[ -n "$lan" ]]; then
    install -d -m 0750 -o root -g "$account" "$application/tls"
    san="DNS:$lan"
    [[ "$lan" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && san="IP:$lan"
    umask 077
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 365 \
        -subj "/CN=$lan" -addext "subjectAltName=$san" \
        -keyout "$application/tls/server.key" -out "$application/tls/server.crt"
    chown root:"$account" "$application/tls/server.key"
    chmod 0640 "$application/tls/server.key"
    chmod 0644 "$application/tls/server.crt"
    web_args+=" --host 0.0.0.0 --allow-lan --allowed-host $lan"
    web_args+=" --tls-cert $application/tls/server.crt --tls-key $application/tls/server.key"
    url="https://$lan:$port"
fi

unit() {
    local file=$1 description=$2 command=$3
    local scratch
    scratch=$(mktemp /etc/systemd/system/.gitmirror.XXXXXXXX)
    cat >"$scratch" <<EOF
[Unit]
Description=$description
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$account
Group=$account
WorkingDirectory=$state
Environment=PYTHONUNBUFFERED=1
ExecStart=$application/venv/bin/gitmirror $command
Restart=on-failure
RestartSec=4s
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$scratch"
    ln -T -- "$scratch" "$file"
    rm -- "$scratch"
}
unit "$web_unit" 'GitMirror web console' "web --managed-dir $state $web_args"
web_written=1
unit "$scheduler_unit" 'GitMirror synchronization scheduler' "managed-scheduler --state-dir $state"
scheduler_written=1
systemd-analyze verify "$web_unit" "$scheduler_unit"
systemctl daemon-reload
systemctl enable --now gitmirror-web.service gitmirror-scheduler.service
python3 - "$port" "$lan" "$application" <<'PY'
import json
import ssl
import sys
import time
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

port, lan, application = sys.argv[1:]
scheme = 'https' if lan else 'http'
context = ssl.create_default_context(cafile=f'{application}/tls/server.crt' if lan else None)
context.check_hostname = False  # The local readiness check uses loopback; LAN browsers verify the hostname.
opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
for _ in range(40):
    try:
        with opener.open(f'{scheme}://127.0.0.1:{port}/api/setup', timeout=1) as response:
            if json.load(response)['required'] is True:
                break
    except (OSError, ValueError):
        time.sleep(0.25)
else:
    sys.exit('The GitMirror web service did not become ready.')
PY
systemctl is-active --quiet gitmirror-web.service
systemctl is-active --quiet gitmirror-scheduler.service

complete=1
printf '\nOpen %s and enter the one-time setup code below.\n' "$url"
if [[ -n "$lan" ]]; then
    printf 'Verify this fingerprint before trusting the local TLS certificate in your browser.\n'
    openssl x509 -noout -sha256 -fingerprint -in "$application/tls/server.crt"
fi
cat -- "$state/setup-code"
