#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

release_api=https://api.github.com/repos/luizbizzio/gitmirror/releases
release_download=https://github.com/luizbizzio/gitmirror/releases/download
asset=gitmirror-linux-source.tar.gz
app=/opt/gitmirror-managed
state=/var/lib/gitmirror-managed
etc=/etc/gitmirror-managed
account=gitmirror-managed
web=gitmirror-managed-web.service
scheduler=gitmirror-managed-scheduler.service
unit_dir=/etc/systemd/system
cli=/usr/local/bin/gitmirror
files=(INSTALL.md LICENSE NOTICE.txt README.md gitmirror.py install.sh packaging/lifecycle.sh
       pyproject.toml web/app.js web/index.html web/style.css web/fonts/LICENSE.md
       web/fonts/source-sans-pro-regular.woff2 web/fonts/source-sans-pro-semibold.woff2 web/fonts/source-sans-pro-bold.woff2 web/fonts/source-sans-pro-italic.woff2)

fail() { printf 'GitMirror: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
GitMirror installer and maintenance
  (no command)  Install if absent, otherwise update the managed installation
  install [--port PORT] [--lan HOST_OR_IPV4]
  upgrade | status | check | rollback | uninstall
  --dev           Use current development main instead of a published release
  --tag vX.Y.Z    Select an immutable stable release
  --artifact FILE --sha256 HEX --tag vX.Y.Z   Install a trusted offline archive
  --yes           Approve installing missing apt dependencies
  After installation: gitmirror update, gitmirror -v (version)
  Without a published stable release, interactive runs can choose main.
  Updates keep configuration, backups and service settings. See INSTALL.md.
EOF
}

confirm() {
    local phrase=$1 description=$2 reply
    printf '%s\n' "$description"
    if [[ -n "$confirmation" ]]; then
        # Keep explicit confirmation for existing non-interactive automation.
        [[ "$confirmation" == "$phrase" ]] || fail "Expected confirmation: $phrase"
        return
    fi
    # Require a real terminal, never assume yes when input is piped or absent.
    read -r -p "Continue? [y/N] " reply </dev/tty ||
        fail 'A terminal is required to confirm this operation'
    [[ "$reply" =~ ^([Yy]|[Yy][Ee][Ss])$ ]] || fail 'Canceled'
}

require_platform() {
    [[ $(id -u) == 0 ]] || fail 'Administrator access is required. Run the command with sudo bash'
    [[ -d /run/systemd/system && -r /etc/os-release ]] || fail 'Run on Debian or Raspberry Pi OS booted with systemd'
    local platform release architecture
    platform=$(sed -nE 's/^ID="?([^"]+)"?$/\1/p' /etc/os-release)
    release=$(sed -nE 's/^VERSION_ID="?([0-9]+)"?$/\1/p' /etc/os-release)
    architecture=$(dpkg --print-architecture)
    [[ "$platform" =~ ^(debian|raspbian)$ && "$release" =~ ^[0-9]+$ && "$release" -ge 12 ]] ||
        fail 'This installer requires Debian 12+ or Raspberry Pi OS based on Debian 12+'
    [[ "$architecture" =~ ^(amd64|arm64)$ ]] || fail "Unsupported package architecture: $architecture"
    printf 'Platform: %s %s %s\n' "$platform" "$release" "$architecture"
}

dependencies() {
    local package reply
    local -a needed=(python3 python3-venv git git-lfs openssh-client ca-certificates curl jq openssl)
    local -a missing=()
    for package in "${needed[@]}"; do
        if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
            missing+=("$package")
        fi
    done
    if (("${#missing[@]}")); then
        printf 'System packages to install: %s\n' "${missing[*]}"
        if ((approve == 0)); then
            read -r -p 'Install these packages with apt? [y/N] ' reply </dev/tty ||
                fail 'Use --yes to explicitly authorize missing dependencies in a non-interactive installation'
            [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]] || fail 'Dependency installation declined'
        fi
        apt-get update
        apt-get install -y -- "${missing[@]}"
    fi
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail 'GitMirror requires Python 3.11+'
    git lfs version >/dev/null || fail 'Git LFS is unavailable'
}

download_https() {
    curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --tlsv1.2 --max-redirs 5 --connect-timeout 10 --max-time 90 --max-filesize 8388608 \
        --output "$2" "$1"
}

verify_archive() {
    local archive=$1 digest=$2 source=$3
    printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status || fail 'Release SHA-256 mismatch'
    local members expected
    members=$(tar -tzf "$archive" --quoting-style=escape | LC_ALL=C sort)
    expected=$(printf '%s\n' "${files[@]}" | LC_ALL=C sort)
    [[ "$members" == "$expected" ]] || fail 'Release archive contains unexpected or missing paths'
    tar -tvzf "$archive" --numeric-owner --quoting-style=escape | \
        awk '$1 !~ /^-/ || $3 !~ /^[0-9]+$/ || $3 > 2097152 {bad=1} END{exit bad+0}' ||
        fail 'Release archive must contain regular files of supported sizes'
    mkdir -m 0700 -- "$source"
    tar -xzf "$archive" --no-same-owner --no-same-permissions -C "$source"
}

fetch_release_metadata() {
    curl -q --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --tlsv1.2 --max-redirs 5 --connect-timeout 10 --max-time 90 --max-filesize 8388608 \
        --write-out '%{http_code}' --output "$2" "$1"
}

development_source() {
    local temp commit
    temp=$(realpath -e -- "$1") || fail 'Development package directory is unavailable'
    [[ -z "$artifact" && -z "$checksum" && -z "$tag" ]] ||
        fail 'Development snapshots cannot be combined with release tags or offline artifacts'
    command -v git >/dev/null || fail 'Development installation requires Git'
    printf 'Fetching the current GitMirror main branch (development version).\n'
    git -c protocol.version=2 clone --quiet --depth=1 --branch main \
        https://github.com/luizbizzio/gitmirror.git "$temp/checkout" ||
        fail 'Could not clone the GitMirror development branch'
    commit=$(git -C "$temp/checkout" rev-parse HEAD) ||
        fail 'Could not identify the downloaded Git commit'
    [[ "$commit" =~ ^[a-f0-9]{40}$ ]] || fail 'Invalid development commit ID'
    tag="dev-${commit:0:12}"
    (cd "$temp/checkout" && tar --sort=name --mtime='@0' --owner=0 --group=0 \
        --numeric-owner --no-recursion -czf "$temp/release.tar.gz" "${files[@]}") ||
        fail 'Could not create the development package'
    checksum=$(sha256sum "$temp/release.tar.gz" | awk '{print $1}')
    verify_archive "$temp/release.tar.gz" "$checksum" "$temp/source"
    printf 'Verified local package from main commit %s (SHA-256 %s)\n' "$commit" "$checksum"
}

release_source() {
    local temp=$1 release pin checked_digest expected_url metadata http_code
    if ((${development:-0})); then
        development_source "$temp"
        return
    fi
    if [[ -n "$artifact" ]]; then
        [[ -f "$artifact" && "$checksum" =~ ^[a-f0-9]{64}$ && -n "$tag" ]] ||
            fail 'Offline artifacts require an existing file, a SHA-256 pin and a release tag'
        install -m 0600 -- "$artifact" "$temp/release.tar.gz"
    else
        [[ -z "$checksum" ]] || fail 'Use --sha256 with --artifact'
        release="$release_api/latest"
        [[ -z "$tag" ]] || release="$release_api/tags/$tag"
        http_code=$(fetch_release_metadata "$release" "$temp/release.json") ||
            fail 'Unable to check GitHub releases'
        if [[ "$http_code" == 404 && -z "$tag" ]]; then
            printf 'No published stable release exists. Using the current main branch (development code).\n'
            development_source "$temp"
            return
        fi
        [[ "$http_code" == 200 ]] ||
            fail "GitHub releases returned HTTP $http_code (not installing unverified code)"
        metadata=$(jq -er --arg asset "$asset" '
            select(.immutable == true and .draft == false and .prerelease == false)
            | .tag_name as $tag
            | [.assets[] | select(.name == $asset and .state == "uploaded")] as $files
            | select(($files | length) == 1)
            | [$tag, $files[0].digest, $files[0].browser_download_url] | @tsv
        ' "$temp/release.json") || fail 'No immutable stable release with the required uploaded artifact'
        IFS=$'\t' read -r pin checked_digest expected_url <<<"$metadata"
        [[ "$pin" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && ( -z "$tag" || "$pin" == "$tag" ) &&
           "$checked_digest" =~ ^sha256:[a-f0-9]{64}$ &&
           "$expected_url" == "$release_download/$pin/$asset" ]] || fail 'Invalid release metadata or digest'
        tag=$pin
        checksum=${checked_digest#sha256:}
        download_https "$expected_url" "$temp/release.tar.gz" || fail 'Unable to download pinned release'
    fi
    verify_archive "$temp/release.tar.gz" "$checksum" "$temp/source"
    printf 'Verified %s (%s)\n' "$tag" "$checksum"
}

select_action() {
    if [[ "$action" != auto ]]; then
        return
    fi
    if [[ -f "$app/current/version" ]]; then
        action=upgrade
    elif [[ -e "$app" || -L "$app" || -e "$state" || -L "$state" ||
            -e "$etc" || -L "$etc" || -e "$unit_dir/$web" || -L "$unit_dir/$web" ||
            -e "$unit_dir/$scheduler" || -L "$unit_dir/$scheduler" ]]; then
        fail 'A partial or retained managed installation exists; refusing to overwrite it'
    else
        action=install
    fi
}

warn_legacy_cron() {
    local user table file
    local pattern='^[[:space:]]*[^#[:space:]].*(/tmp/gitmirror\.lock|gitmirror\.py|(^|[ /])gitmirror[[:space:]]+(sync|run))'
    if command -v crontab >/dev/null; then
        while IFS=: read -r user _; do
            table=$(crontab -u "$user" -l 2>/dev/null) || continue
            if grep -Eqi "$pattern" <<<"$table"; then
                printf 'WARNING: Possible old GitMirror schedule in the crontab for %s. Review it before enabling managed sync.\n' "$user" >&2
            fi
        done </etc/passwd
    fi
    for file in /etc/crontab /etc/cron.d/* /etc/cron.{hourly,daily,weekly,monthly}/*; do
        [[ -f "$file" ]] || continue
        if grep -Eqi "$pattern" "$file"; then
            printf 'WARNING: Possible old GitMirror schedule in %s. Review it before enabling managed sync.\n' "$file" >&2
        fi
    done
}

main() {
    action=auto
    tag= checksum= artifact= confirmation= lan=
    approve=0 development=0 port=8787
    case "${1:-}" in install|upgrade|rollback|uninstall|status|check) action=$1; shift;; esac
    while (($#)); do
        case "$1" in
            --yes) approve=1; shift; continue ;;
            --dev) development=1; shift; continue ;;
            --help|-h) usage; exit 0 ;;
        esac
        (($# >= 2)) || fail "Missing value for $1"
        case "$1" in
            --tag) tag=$2;; --artifact) artifact=$2;; --sha256) checksum=$2;;
            --confirm) confirmation=$2;; --port) port=$2;; --lan) lan=$2;;
            *) fail "Unknown parameter: $1";;
        esac
        shift 2
    done
    [[ -z "$tag" || "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Use a stable release tag such as v1.2.3'
    if ((development)) && [[ -n "$tag" || -n "$artifact" || -n "$checksum" ]]; then
        fail 'Use either --dev or a specific stable/offline release, not both'
    fi
    [[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1024 && 10#$port <= 65535)) || fail 'Use a port from 1024 to 65535'
    port=$((10#$port))
    [[ -z "$lan" || "$lan" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || fail 'Use a LAN hostname or IPv4 address'
    require_platform
    exec 9>/run/lock/gitmirror-managed-install.lock
    flock -n 9 || fail 'Another GitMirror installation or maintenance operation is active'
    select_action
    case "$action" in
        status)
            if [[ -f "$app/current/version" ]]; then
                printf 'Installed release: %s\n' "$(cat "$app/current/version")"
            else
                printf 'No managed installation found.\n'
            fi
            [[ ! -e "$etc/maintenance-pending" ]] || printf 'Maintenance pending. Applied runs are blocked.\n'
            [[ ! -e "$etc/migration-pending" ]] || printf 'A previous migration pause is still active. Applied runs are blocked.\n'
            warn_legacy_cron
            return 0 ;;
        rollback|uninstall)
            [[ -f "$app/current/lifecycle.sh" ]] || fail 'No managed installation found'
            source "$app/current/lifecycle.sh"
            lifecycle
            return ;;
    esac
    if [[ "$action" != check ]]; then warn_legacy_cron; fi
    if [[ "$action" == install ]]; then
        [[ ! -e "$app" && ! -L "$app" && ! -e "$state" && ! -L "$state" &&
           ! -e "$etc" && ! -L "$etc" && ! -e "$unit_dir/$web" && ! -L "$unit_dir/$web" &&
           ! -e "$unit_dir/$scheduler" && ! -L "$unit_dir/$scheduler" ]] ||
            fail 'Managed installation or retained state already exists. Use status or upgrade'
        ! getent passwd "$account" >/dev/null && ! getent group "$account" >/dev/null ||
            fail 'The managed service account already exists'
    else
        [[ -f "$app/current/version" ]] || fail 'No managed installation to upgrade'
        printf 'Installed release: %s\n' "$(cat "$app/current/version")"
    fi
    if [[ "$action" == check ]]; then
        command -v curl >/dev/null && command -v jq >/dev/null || fail 'Update checks require curl and jq'
    else
        dependencies
    fi
    temporary=$(mktemp -d -t gitmirror-install.XXXXXXXX)
    trap 'rm -rf -- "$temporary"' EXIT
    release_source "$temporary"
    if [[ "$action" == check ]]; then
        printf 'Available verified package: %s\n' "$tag"
        return 0
    fi
    source "$temporary/source/packaging/lifecycle.sh"
    lifecycle
}

# The installed lifecycle reuses these functions through its trusted local bootstrap.
if [[ "${GITMIRROR_INSTALL_LIBRARY:-}" != 1 ]]; then
    main "$@"
fi
