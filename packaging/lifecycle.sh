# Sourced by the installer after release verification, or from the root-owned active release.

check_release() {
    local binary=$1
    "$binary" --version >/dev/null &&
        runuser -u "$account" -- "$binary" managed-validate --state-dir "$state"
}

build_release() {
    candidate="$app/releases/$tag-${checksum:0:16}"
    if [[ -d "$candidate" ]]; then
        [[ -f "$candidate/digest" && "$(cat "$candidate/digest")" == "$checksum" ]] ||
            fail 'An incomplete or conflicting release directory exists'
        return
    fi
    mkdir -- "$candidate"
    building=$candidate
    python3 -m venv "$candidate/venv"
    "$candidate/venv/bin/python" -m pip install --disable-pip-version-check --no-input \
        --index-url https://pypi.org/simple "$temporary/source[web]"
    install -m 0755 "$temporary/source/install.sh" "$candidate/install.sh"
    install -m 0644 "$temporary/source/packaging/"{lifecycle,legacy}.sh "$candidate/"
    printf '%s\n' "$tag" >"$candidate/version"
    printf '%s\n' "$checksum" >"$candidate/digest"
    building=
}

point() {
    local destination=$1 link=$2
    rm -f -- "$app/.$link.new" &&
        ln -s -- "$destination" "$app/.$link.new" &&
        mv -Tf -- "$app/.$link.new" "$app/$link"
}

settings() {
    [[ -f "$etc/listener" ]] || fail 'Managed listener settings are missing'
    { read -r port; read -r lan; } <"$etc/listener"
    url="http://127.0.0.1:$port"
    [[ -z "$lan" ]] || url="https://$lan:$port"
}

health() {
    local attempt data
    local -a probe=(curl -q --fail --silent --noproxy '*' --max-time 2)
    if [[ -n "$lan" ]]; then
        probe+=(--proto '=https' --cacert "$etc/tls/server.crt" --connect-to "$lan:$port:127.0.0.1:$port")
    fi
    for ((attempt=0; attempt<24; attempt++)); do
        if systemctl is-active --quiet "$web" && systemctl is-active --quiet "$scheduler" &&
           data=$("${probe[@]}" "$url/api/setup") &&
           jq -e 'type=="object" and (.required | type=="boolean")' <<<"$data" >/dev/null; then
            return 0
        fi
        sleep .4
    done
    return 1
}

issue_certificate() {
    local san="DNS:$lan"
    [[ "$lan" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && san="IP:$lan"
    install -d -m 0750 -g "$account" "$etc/tls"
    (umask 077; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -days 365 -subj "/CN=$lan" -addext "subjectAltName=$san" \
        -keyout "$etc/tls/server.key" -out "$etc/tls/server.crt")
    chown root:"$account" "$etc/tls/server.key"
    chmod 0640 "$etc/tls/server.key"
    chmod 0644 "$etc/tls/server.crt"
}

unit() {
    local name=$1 description=$2 command=$3 staged
    staged=$(mktemp "$unit_dir/.gitmirror-managed.XXXXXXXX")
    cat >"$staged" <<EOF
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
Environment=TMPDIR=$state/tmp
Environment=GITMIRROR_MIGRATION_GUARD=$etc/migration-pending
Environment=GITMIRROR_MAINTENANCE_GUARD=$etc/maintenance-pending
ExecStart=$app/current/venv/bin/gitmirror $command
Restart=on-failure
RestartSec=4s
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$staged"
    ln -T -- "$staged" "$unit_dir/$name"
    rm -- "$staged"
}

cleanup_lifecycle() {
    local result=${1:-$?}
    trap - EXIT
    [[ -z "${temporary:-}" ]] || rm -rf -- "$temporary"
    [[ -z "${building:-}" ]] || rm -rf -- "$building"
    if [[ "${fresh:-}" == 1 && "$result" != 0 ]]; then
        if ((made_web)); then systemctl disable --now "$web" || true; rm -f -- "$unit_dir/$web"; fi
        if ((made_scheduler)); then systemctl disable --now "$scheduler" || true; rm -f -- "$unit_dir/$scheduler"; fi
        systemctl daemon-reload || true
        if ((made_state)); then rm -rf -- "$state"; fi
        if ((made_etc)); then rm -rf -- "$etc"; fi
        if ((made_app)); then rm -rf -- "$app"; fi
        if ((made_account)); then userdel "$account" || true; fi
    fi
    exit "$result"
}

fresh_install() {
    fresh=1
    made_web=0 made_scheduler=0 made_state=0 made_etc=0 made_app=0 made_account=0
    mkdir -- "$app"; made_app=1
    mkdir -- "$app/releases"
    install -d -m 0755 "$etc"; made_etc=1
    ensure_managed_lock
    if [[ "$action" == migrate ]]; then
        source "$temporary/source/packaging/legacy.sh"
        prepare_legacy
    fi
    build_release
    point "$candidate" current
    useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin "$account"
    made_account=1
    install -d -m 0700 -o "$account" -g "$account" "$state"
    made_state=1
    install -d -m 0700 -o "$account" -g "$account" "$state/tmp"
    runuser -u "$account" -- "$app/current/venv/bin/gitmirror" managed-init --state-dir "$state"
    printf '%s\n%s\n' "$port" "$lan" >"$etc/listener"
    settings
    local web_args="--port $port"
    if [[ -n "$lan" ]]; then
        issue_certificate
        web_args+=" --host 0.0.0.0 --allow-lan --allowed-host $lan"
        web_args+=" --tls-cert $etc/tls/server.crt --tls-key $etc/tls/server.key"
    fi
    unit "$web" 'GitMirror web dashboard' "web --managed-dir $state $web_args"; made_web=1
    unit "$scheduler" 'GitMirror synchronization scheduler' "managed-scheduler --state-dir $state"; made_scheduler=1
    systemd-analyze verify "$unit_dir/$web" "$unit_dir/$scheduler"
    systemctl daemon-reload
    systemctl enable --now "$web" "$scheduler"
    health || fail 'Dashboard or scheduler failed its readiness check; rolling back application installation'
    fresh=0
    printf '\nOpen %s and enter the one-time setup code printed below.\n' "$url"
    if [[ -n "$lan" ]]; then
        printf 'Match this certificate fingerprint in the browser before trusting the local certificate.\n'
        openssl x509 -noout -sha256 -fingerprint -in "$etc/tls/server.crt"
    fi
    [[ "$action" != migrate ]] || printf 'Migration is staged. Applied synchronization remains blocked until cutover.\n'
    cat -- "$state/setup-code"
}

# The installer owns both the directory and the lock entry. Never recreate an
# existing entry here, including during upgrades or while a process holds it.
ensure_managed_lock() {
    local lock="$etc/sync.lock"
    [[ -d "$etc" && ! -L "$etc" && "$(stat -c '%u %a' -- "$etc")" == '0 755' ]] ||
        fail 'Managed configuration directory must be root-owned with mode 0755'
    if [[ ! -e "$lock" && ! -L "$lock" ]]; then
        [[ ! -e "$etc/sync-lock-created" ]] ||
            fail 'Persistent synchronization lock is missing; it cannot be recreated automatically'
        (umask 022; set -o noclobber; : >"$lock") || fail 'Could not create the persistent synchronization lock'
    fi
    [[ -f "$lock" && ! -L "$lock" && "$(stat -c '%u %a %h' -- "$lock")" == '0 644 1' ]] ||
        fail 'Persistent synchronization lock must be a protected root-owned regular file'
    if [[ ! -e "$etc/sync-lock-created" ]]; then
        touch "$etc/sync-lock-created"
        sync -f "$etc"
    fi
}

drain() {
    runuser -u "$account" -- mkdir -p -- "$state/work/web-dashboard"
    runuser -u "$account" -- touch "$state/work/web-dashboard/.action.lock" "$state/work/.gitmirror.lock"
    exec {web_fd}<"$state/work/web-dashboard/.action.lock"
    exec {sync_fd}<"$state/work/.gitmirror.lock"
    flock -w 40 "$web_fd" && flock -w 40 "$sync_fd" ||
        fail 'A dashboard or sync operation is active; try again when it finishes'
    ensure_managed_lock
    exec {persistent_fd}<"$etc/sync.lock"
    flock -w 40 "$persistent_fd" ||
        fail 'A dashboard or sync operation is active; try again when it finishes'
}

undrain() {
    exec {persistent_fd}<&-
    exec {sync_fd}<&-
    exec {web_fd}<&-
}

switch_release() {
    local next=$1 current
    current=$(readlink -e "$app/current")
    [[ "$next" != "$current" ]] || fail 'That release is already active'
    check_release "$next/venv/bin/gitmirror" || fail 'Candidate could not validate the current configuration'
    drain
    touch "$etc/maintenance-pending"
    if ! systemctl stop "$web" "$scheduler"; then
        if systemctl start "$web" "$scheduler" && health; then rm -- "$etc/maintenance-pending"; fi
        fail 'Could not stop both services safely'
    fi
    if ! point "$next" current; then
        if systemctl start "$web" "$scheduler" && health; then rm -- "$etc/maintenance-pending"; fi
        fail 'Could not activate candidate'
    fi
    # Keep repository and dashboard locks through the health decision. No candidate
    # synchronization starts before we commit to the new executable.
    if systemctl start "$web" "$scheduler" && health && point "$current" previous; then
        undrain
        rm -- "$etc/maintenance-pending"
        printf 'Active release: %s\n' "$(cat "$app/current/version")"
        return
    fi
    systemctl stop "$web" "$scheduler" || fail 'Candidate failed; could not stop both services to restore the previous release'
    point "$current" current || fail 'Could not restore executable pointer; the previous release is still on disk'
    if systemctl start "$web" "$scheduler" && health; then
        undrain
        rm -- "$etc/maintenance-pending"
        fail 'Could not finish activating the candidate; restored the previous release'
    fi
    fail 'Candidate failed and the previous release requires service recovery'
}

upgrade() {
    local installed newest
    installed=$(cat "$app/current/version")
    [[ "$installed" != "$tag" ]] || fail "$tag is already installed"
    newest=$(printf '%s\n%s\n' "$installed" "$tag" | sort -V | tail -n 1)
    [[ "$newest" == "$tag" ]] || fail 'Use rollback to activate a previous release'
    confirm UPGRADE "Upgrade from $installed to $tag and restart the managed services."
    build_release
    switch_release "$candidate"
}

uninstall() {
    confirm UNINSTALL 'Remove this application and its two services. Preserve configuration, credentials, backups, legacy state and the service account.'
    drain
    systemctl disable --now "$web" "$scheduler"
    rm -- "$unit_dir/$web" "$unit_dir/$scheduler"
    systemctl daemon-reload
    rm -rf -- "$app"
    printf 'Application removed. Preserved data: %s and %s\n' "$state" "$etc"
}

lifecycle() {
    trap cleanup_lifecycle EXIT
    case "$action" in
        install|migrate) fresh_install ;;
        upgrade) settings; upgrade ;;
        rollback)
            settings
            [[ -L "$app/previous" ]] || fail 'No previously active release is available'
            confirm ROLLBACK 'Revert the application while preserving current user data.'
            switch_release "$(readlink -e "$app/previous")" ;;
        cutover|rollback-migration)
            settings
            source "$app/current/legacy.sh"
            if [[ "$action" == cutover ]]; then cutover; else rollback_migration; fi ;;
        uninstall) uninstall ;;
    esac
}
