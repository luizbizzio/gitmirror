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
files=(INSTALL.md LICENSE NOTICE.txt README.md gitmirror.py install.sh packaging/legacy.sh packaging/lifecycle.sh
       pyproject.toml web/app.js web/index.html web/style.css)

fail() { printf 'GitMirror: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
GitMirror installer and maintenance
  install  [--port PORT] [--lan HOST_OR_IPV4]
  migrate  --legacy-cron USER --cron-line N --legacy-lock PATH --preserve FILE [--preserve FILE ...]
  cutover | rollback-migration | status | check | upgrade | rollback | uninstall
  install, migrate and upgrade accept --tag vX.Y.Z and --yes (approve missing apt dependencies).
  A trusted offline release needs --artifact FILE --sha256 HEX --tag vX.Y.Z.
  Confirmation phrases are separate from --yes. See the installation guide.
EOF
}

confirm() {
    local phrase=$1 description=$2 reply
    printf '%s\n' "$description"
    if [[ -n "$confirmation" ]]; then
        [[ "$confirmation" == "$phrase" ]] || fail "Expected confirmation: $phrase"
    else
        read -r -p "Type $phrase to continue: " reply </dev/tty || fail 'An explicit confirmation is required'
        [[ "$reply" == "$phrase" ]] || fail 'Canceled'
    fi
}

cron_matches() {
    awk 'tolower($0) ~ /git[-_]?mirror([[:space:]./[:punct:]]|$)/ &&
        $0 !~ /^[[:space:]]*(#|[A-Za-z_][A-Za-z_0-9]*[[:space:]]*=)/ {print NR ":" $0}'
}

legacy_cron_reports() {
    local file user contents match
    if command -v crontab >/dev/null; then
        while IFS=: read -r user _; do
            contents=$(crontab -u "$user" -l 2>/dev/null) || continue
            while IFS= read -r match; do
                [[ -n "$match" ]] && printf 'user-cron:%s:%s\n' "$user" "${match%%:*}"
            done < <(printf '%s\n' "$contents" | cron_matches)
        done </etc/passwd
    fi
    for file in /etc/crontab /etc/cron.d/*; do
        [[ -f "$file" ]] || continue
        while IFS= read -r match; do
            [[ -n "$match" ]] && printf 'system-cron:%s:%s\n' "$file" "${match%%:*}"
        done < <(cron_matches <"$file")
    done
    for file in /etc/cron.{hourly,daily,weekly,monthly}/*; do
        [[ -f "$file" ]] || continue
        if [[ "$file" == *[Gg]it[Mm]irror* ]] || grep -qEi 'git[-_]?mirror' "$file"; then
            printf 'periodic-cron:%s\n' "$file"
        fi
    done
    systemctl list-unit-files --no-legend --no-pager |
        awk 'tolower($1) ~ /git[-_]?mirror/ && $1 !~ /^gitmirror-managed-(web|scheduler)\.service$/ {print "unit:" $1}'
}

detect_legacy() {
    local path home
    for path in /opt/gitmirror /var/lib/gitmirror /etc/gitmirror /usr/local/bin/gitmirror /usr/bin/gitmirror; do
        [[ ! -e "$path" && ! -L "$path" ]] || printf 'path:%s\n' "$path"
    done
    while IFS=: read -r _ _ _ _ _ home _; do
        [[ -n "$home" && "$home" != "/" && -d "$home" ]] || continue
        for path in "$home"/{gitmirror,gitmirror.py,gitmirror-data,.config/gitmirror,scripts/gitmirror.py,bin/gitmirror,bin/gitmirror.py}; do
            [[ ! -e "$path" && ! -L "$path" ]] || printf 'path:%s\n' "$path"
        done
    done </etc/passwd
    legacy_cron_reports
    # Report PIDs only. Legacy command arguments can contain tokens.
    ps -eo pid=,comm=,args= | awk '$2 ~ /^(python[0-9.]*|gitmirror|bash|sh|flock)$/ && $0 ~ /[ /]gitmirror(\.py)?( |$)/ && $0 !~ /\/(opt|var\/lib)\/gitmirror-managed\// {print "process:" $1}'
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

release_source() {
    local temp=$1 release pin checked_digest expected_url metadata
    if [[ -n "$artifact" ]]; then
        [[ -f "$artifact" && "$checksum" =~ ^[a-f0-9]{64}$ && -n "$tag" ]] ||
            fail 'Offline artifacts require an existing file, a SHA-256 pin and a release tag'
        install -m 0600 -- "$artifact" "$temp/release.tar.gz"
    else
        [[ -z "$checksum" ]] || fail 'Use --sha256 with --artifact'
        release="$release_api/latest"
        [[ -z "$tag" ]] || release="$release_api/tags/$tag"
        download_https "$release" "$temp/release.json" || fail 'Unable to fetch a release from GitHub'
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

main() {
    action=install
    tag= checksum= artifact= confirmation= lan= old_user= old_line= old_lock=
    approve=0 port=8787
    preserve=()
    case "${1:-}" in install|migrate|upgrade|rollback|cutover|rollback-migration|uninstall|status|check) action=$1; shift;; esac
    while (($#)); do
        case "$1" in
            --yes) approve=1; shift; continue ;;
            --help|-h) usage; exit 0 ;;
        esac
        (($# >= 2)) || fail "Missing value for $1"
        case "$1" in
            --tag) tag=$2;; --artifact) artifact=$2;; --sha256) checksum=$2;;
            --confirm) confirmation=$2;; --port) port=$2;; --lan) lan=$2;;
            --legacy-cron) old_user=$2;; --cron-line) old_line=$2;; --legacy-lock) old_lock=$2;;
            --preserve) preserve+=("$2");;
            *) fail "Unknown parameter: $1";;
        esac
        shift 2
    done
    [[ -z "$tag" || "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Use a stable release tag such as v1.2.3'
    [[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1024 && 10#$port <= 65535)) || fail 'Use a port from 1024 to 65535'
    port=$((10#$port))
    [[ -z "$lan" || "$lan" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || fail 'Use a LAN hostname or IPv4 address'
    require_platform
    exec 9>/run/lock/gitmirror-managed-install.lock
    flock -n 9 || fail 'Another GitMirror installation or maintenance operation is active'
    case "$action" in
        status)
            [[ ! -f "$app/current/version" ]] || printf 'Installed release: %s\n' "$(cat "$app/current/version")"
            [[ ! -e "$etc/migration-pending" ]] || printf 'Migration pending. Applied runs are blocked.\n'
            [[ ! -e "$etc/maintenance-pending" ]] || printf 'Maintenance pending. Applied runs are blocked.\n'
            detect_legacy
            return 0 ;;
        cutover|rollback-migration|rollback|uninstall)
            [[ -f "$app/current/lifecycle.sh" ]] || fail 'No managed installation found'
            source "$app/current/lifecycle.sh"
            lifecycle
            return ;;
    esac
    if [[ "$action" == install || "$action" == migrate ]]; then
        [[ ! -e "$app" && ! -L "$app" && ! -e "$state" && ! -L "$state" &&
           ! -e "$etc" && ! -L "$etc" && ! -e "$unit_dir/$web" && ! -L "$unit_dir/$web" &&
           ! -e "$unit_dir/$scheduler" && ! -L "$unit_dir/$scheduler" ]] ||
            fail 'Managed installation or retained state already exists. Use status or upgrade'
        ! getent passwd "$account" >/dev/null && ! getent group "$account" >/dev/null ||
            fail 'The managed service account already exists'
        detections=$(detect_legacy)
        if [[ -n "$detections" ]]; then
            printf 'Possible older GitMirror installation:\n%s\n' "$detections"
            [[ "$action" == migrate ]] || fail 'Nothing was changed. Use the staged migration procedure'
        fi
        if [[ "$action" == migrate ]]; then
            [[ "$old_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && "$old_line" =~ ^[1-9][0-9]{0,5}$ &&
               "$old_lock" == /* && "${#preserve[@]}" -gt 0 ]] ||
                fail 'Choose an exact legacy user cron line, an existing flock path and the configuration files to preserve'
            confirm STAGE 'Stage an isolated dashboard. The original installation and schedule stay active.'
        fi
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
        printf 'Available verified release: %s\n' "$tag"
        return 0
    fi
    source "$temporary/source/packaging/lifecycle.sh"
    lifecycle
}

# The installed lifecycle reuses these functions through its trusted local bootstrap.
if [[ "${GITMIRROR_INSTALL_LIBRARY:-}" != 1 ]]; then
    main "$@"
fi
