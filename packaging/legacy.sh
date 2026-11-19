# Conservative handoff for one explicitly selected user-cron entry using flock -n.

selected_legacy_command() {
    local line=$1 lock=$2 command binary target
    local -a words
    if [[ "$line" =~ ^[[:space:]]*@[a-zA-Z]+[[:space:]]+ ]]; then
        command=${line#"${BASH_REMATCH[0]}"}
    elif [[ "$line" =~ ^([[:space:]]*[^[:space:]]+[[:space:]]+){5} ]]; then
        command=${line#"${BASH_REMATCH[0]}"}
    else
        return 1
    fi
    command=${command% 2>&1}
    if [[ "$command" =~ ^(.*)[[:space:]]+\>\>?[[:space:]]*/[A-Za-z0-9_./+-]+[[:space:]]*$ ]]; then
        command=${BASH_REMATCH[1]}
    fi
    case "$command" in
        *';'*|*'&'*|*'|'*|*'<'*|*'>'*|*'$('*|*'`'*|*'%'*) return 1 ;;
    esac
    read -r -a words <<<"$command"
    ((${#words[@]} >= 4)) || return 1
    [[ "${words[0]##*/}" == flock && "${words[1]}" == -n &&
       ( "${words[2]}" == "$lock" || "${words[2]}" == "\"$lock\"" ) ]] || return 1
    binary=${words[3]##*/}
    if [[ "$binary" =~ ^python[0-9.]*$ ]]; then
        target=${words[4]:-}
        [[ "$target" != -u ]] || target=${words[5]:-}
        [[ "${target##*/}" == gitmirror.py ]] || return 1
    else
        [[ "$binary" == gitmirror || "$binary" == gitmirror.py ]] || return 1
        target=${words[3]}
    fi
    [[ "$target" =~ ^/[A-Za-z0-9_./+-]+$ ]] || return 1
    legacy_target=$target
}

migration_state() {
    local output
    output=$(mktemp "$etc/.migration-state.XXXXXXXX")
    printf '%s\n%s\n' "$1" "$2" >"$output"
    chmod 0644 "$output"
    mv -Tf -- "$output" "$etc/migration-state"
    sync -f "$etc"
}

block_migration() {
    touch "$etc/migration-pending"
    migration_state blocked "$1"
    fail "$1"
}

valid_legacy_lock_path() {
    [[ "$old_lock" =~ ^/[a-zA-Z0-9_./+-]+$ && ! -L "$old_lock" &&
       "$(realpath -m -- "$old_lock")" == "$old_lock" &&
       ( ! -e "$old_lock" || -f "$old_lock" ) ]]
}

record_legacy_target() {
    local target expected
    target=$(realpath -e -- "$legacy_target") || fail 'Legacy GitMirror executable is unavailable'
    [[ -f "$target" && "$target" =~ ^/[A-Za-z0-9_./+-]+$ ]] || fail 'Cannot identify the legacy GitMirror executable'
    expected=$({
        printf '%s\n' "$legacy_target"
        if [[ "$target" != "$legacy_target" ]]; then printf '%s\n' "$target"; fi
    })
    if [[ -f "$etc/legacy-target" ]]; then
        [[ "$(cat "$etc/legacy-target")" == "$expected" ]] ||
            block_migration 'The legacy GitMirror executable path changed since staging'
    else
        printf '%s\n' "$expected" >"$etc/legacy-target"
        chmod 0644 "$etc/legacy-target"
    fi
}

prepare_legacy() {
    valid_legacy_lock_path || fail 'Choose the canonical path of the legacy flock file'
    getent passwd "$old_user" >/dev/null || fail 'Legacy cron account does not exist'
    local directory="$etc/legacy" line path sequence=0
    install -d -m 0700 -- "$directory"
    (umask 077; crontab -u "$old_user" -l >"$directory/crontab.before") ||
        fail 'Cannot read the legacy user crontab'
    line=$(sed -n "${old_line}p" "$directory/crontab.before")
    selected_legacy_command "$line" "$old_lock" ||
        fail 'Choose a cron line that only invokes an absolute GitMirror executable through the given flock -n path'
    record_legacy_target
    (umask 077; awk -v number="$old_line" 'NR != number' "$directory/crontab.before" >"$directory/crontab.after")
    (umask 077; printf '%s\n%s\n' "$old_user" "$old_line" >"$directory/selection")
    for path in "${preserve[@]}"; do
        [[ "$path" == /* && "$path" != *$'\n'* && -f "$path" ]] ||
            fail 'Preserve accepts absolute paths to individual configuration, script or credential files'
        ((sequence+=1))
        install -m 0600 -- "$path" "$directory/file-$sequence"
        (umask 077; printf '%s\t%s\n' "file-$sequence" "$path" >>"$directory/preserved")
    done
    (umask 077; printf '%s\n' "$old_lock" >"$directory/flock")
    touch "$etc/migration-pending"
    migration_state staged 'Original cron remains enabled; managed writes are paused.'
}

legacy_selection() {
    legacy="$etc/legacy"
    [[ -f "$legacy/selection" ]] || fail 'No staged legacy migration found'
    { read -r old_user; read -r old_line; } <"$legacy/selection"
    if [[ -f "$legacy/flock" ]]; then
        read -r old_lock <"$legacy/flock"
    elif [[ -f "$etc/legacy-lock" ]]; then
        # Read the path from an earlier draft's state. Its inode is no longer authoritative.
        read -r old_lock <"$etc/legacy-lock"
    else
        fail 'Legacy flock path record is missing'
    fi
}

legacy_lock_unchanged() {
    kill -0 "$legacy_holder" 2>/dev/null && valid_legacy_lock_path && [[ -f "$old_lock" &&
        "$(stat -c '%d %i' -- "$old_lock")" == "$lock_identity" ]]
}

legacy_lock() {
    valid_legacy_lock_path || block_migration 'The legacy flock path is unsafe'
    if [[ ! -e "$old_lock" ]]; then
        # Let the original account create the temporary entry; it must still be
        # usable by the original cron if cutover is interrupted or rolled back.
        runuser -u "$old_user" -- sh -c 'umask 077; set -C; : >"$1"' sh "$old_lock" ||
            [[ -f "$old_lock" ]] || block_migration 'The original account could not recreate the missing legacy flock'
    fi
    [[ -f "$old_lock" && ! -L "$old_lock" ]] || block_migration 'The legacy flock path changed while opening it'
    local reader
    coproc LEGACY_FLOCK {
        exec "$app/current/venv/bin/gitmirror" managed-legacy-lock --path "$old_lock"
    }
    legacy_holder=$!
    reader=${LEGACY_FLOCK[0]:-}
    [[ -n "$reader" ]] && read -t 6 -r lock_identity <&"$reader" &&
        [[ "$lock_identity" =~ ^[0-9]+\ [0-9]+$ ]] ||
        block_migration 'Could not hold the legacy flock; resolve the preceding error and retry'
    legacy_lock_unchanged || block_migration 'The legacy flock was replaced while opening it'
}

legacy_jobs_idle() {
    local detail
    if ! detail=$(runuser -u "$account" -- "$app/current/venv/bin/gitmirror" \
                   managed-legacy-scan --record "$etc/legacy-target" "$@" 2>&1); then
        detail=${detail#GitMirror: }
        block_migration "${detail:-Cannot verify that the old job is inactive}"
    fi
    if [[ -n "$detail" ]]; then printf '%s\n' "$detail"; fi
}

cron_instance() {
    local pid info binary others
    pid=$(systemctl show cron.service -p MainPID --value) || return 1
    others=$(pgrep -x 'cron|crond' || true)
    if [[ "$pid" == 0 && -z "$others" ]] && ! pgrep -x CRON >/dev/null; then
        printf '0\n'
        return
    fi
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$others" == "$pid" ]] || return 1
    binary=$(readlink -e "/proc/$pid/exe") || return 1
    [[ "$binary" == /usr/sbin/cron ]] || return 1
    info=$(cat "/proc/$pid/stat") || return 1
    printf '%s %s\n' "$pid" "$(awk '{print $20}' <<<"${info##*) }")"
}

cron_reloaded_after() {
    jq -ne --argjson after "$1" --arg message "($old_user) RELOAD (crontabs/$old_user)" \
        'any(inputs; .MESSAGE == $message and (.__MONOTONIC_TIMESTAMP | tonumber) > $after)' >/dev/null
}

wait_for_cron_reload() {
    local daemon=$1 parked_at=$2 pid="${1%% *}" deadline=$((SECONDS + 85)) messages
    [[ "$daemon" != 0 ]] || {
        [[ "$(cron_instance)" == 0 ]] || block_migration 'Cron started while parking the old schedule'
        return
    }
    printf 'Waiting for Debian cron to reload the selected crontab and drain queued jobs.\n'
    while ((SECONDS < deadline)); do
        legacy_lock_unchanged || block_migration 'The legacy flock was replaced during cutover'
        [[ "$(cron_instance)" == "$daemon" ]] || block_migration 'The cron daemon changed during cutover'
        messages=$(journalctl -b "_PID=$pid" -n 128 --no-pager -o json --output-fields=MESSAGE) ||
            block_migration 'Cannot read cron reload events from the system journal'
        if cron_reloaded_after "$parked_at" <<<"$messages" &&
           ! pgrep -P "$pid" >/dev/null; then
            return
        fi
        sleep 1
    done
    block_migration 'Cron did not confirm reload with its job queue drained; retry or roll back'
}

read_legacy_cron() {
    crontab -u "$old_user" -l >"$cron_observed"
}

cutover_cleanup() {
    local result=$?
    if ((result != 0 && changed_cron)); then
        # Applied writes are still blocked. A regular failure restores the old
        # entry only when the current table still matches the one we parked.
        if read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.after"; then
            if crontab -u "$old_user" "$legacy/crontab.before"; then
                sync -f /var/spool/cron/crontabs
                printf 'Restored the original crontab after the failed cutover.\n' >&2
            else
                printf 'Original cron backup needs recovery at %s\n' "$legacy" >&2
            fi
        fi
    fi
    rm -f -- "$cron_observed"
    cleanup_lifecycle "$result"
}

cutover() {
    legacy_selection
    if [[ ! -e "$etc/migration-pending" && -f "$etc/migration-state" &&
          "$(head -n 1 "$etc/migration-state")" == complete ]]; then
        fail 'This migration is already complete'
    fi
    confirm CUTOVER "Disable only user $old_user's GitMirror cron line $old_line after validating the staged system."
    touch "$etc/migration-pending"
    migration_state blocked 'Cutover in progress or interrupted. Run cutover again or rollback-migration.'
    cron_observed=$(mktemp)
    changed_cron=0
    trap cutover_cleanup EXIT
    drain
    runuser -u "$account" -- "$app/current/venv/bin/gitmirror" managed-preflight --state-dir "$state" ||
        block_migration 'Staging dry-run failed. Existing cron was left unchanged.'
    local line remaining daemon parked_at=0
    line=$(sed -n "${old_line}p" "$legacy/crontab.before")
    selected_legacy_command "$line" "$old_lock" || block_migration 'Saved cron command is unsupported'
    record_legacy_target
    legacy_lock
    legacy_jobs_idle
    read_legacy_cron || block_migration 'Cannot read the existing user crontab'
    if cmp -s "$cron_observed" "$legacy/crontab.before"; then
        remaining=$(legacy_cron_reports | awk -v selected="user-cron:$old_user:$old_line" '$0 != selected')
    elif cmp -s "$cron_observed" "$legacy/crontab.after"; then
        # An earlier cutover may have died after parking cron. Republish the known
        # table and obtain a fresh reload proof while still holding both locks.
        remaining=$(legacy_cron_reports)
        changed_cron=1
    else
        block_migration 'The legacy crontab changed; no lines were altered'
    fi
    [[ -z "$remaining" ]] || block_migration "Other possible GitMirror schedules require review: $remaining"
    daemon=$(cron_instance) || block_migration 'Cannot verify the Debian cron daemon instance'
    read_legacy_cron &&
        { cmp -s "$cron_observed" "$legacy/crontab.before" || cmp -s "$cron_observed" "$legacy/crontab.after"; } ||
        block_migration 'The legacy crontab changed; no lines were altered'
    changed_cron=1
    crontab -u "$old_user" "$legacy/crontab.after" || block_migration 'Could not park the selected cron entry'
    if [[ "$daemon" != 0 ]]; then
        # Journal monotonic time is per boot and unaffected by wall-clock changes.
        parked_at=$(legacy_jobs_idle --clock) ||
            block_migration 'Cannot verify a safe monotonic boundary after parking the old cron entry'
        [[ "$parked_at" =~ ^[0-9]+$ ]] || block_migration 'Cannot measure the cron parking time'
    fi
    sync -f /var/spool/cron/crontabs
    wait_for_cron_reload "$daemon" "$parked_at"
    read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.after" ||
        block_migration 'The parked crontab changed during cutover'
    [[ -z "$(legacy_cron_reports)" ]] || block_migration 'Another GitMirror schedule appeared during cutover'
    health || block_migration 'The new dashboard or scheduler is unavailable'
    legacy_jobs_idle
    legacy_lock_unchanged || block_migration 'The legacy flock was replaced before cutover completed'
    migration_state complete 'Original cron is parked. Applied runs use the persistent managed lock.'
    changed_cron=0
    rm -- "$etc/migration-pending"
    sync -f "$etc"
    printf 'Cutover complete. Enable scheduling in the new dashboard when ready. Original files remain in place.\n'
}

rollback_migration() {
    legacy_selection
    confirm RESTORE-CRON "Restore the exact original GitMirror cron entry for $old_user. Keep the staged services idle."
    touch "$etc/migration-pending"
    migration_state blocked 'Rollback in progress or interrupted. Managed writes are paused.'
    drain
    cron_observed=$(mktemp)
    trap 'result=$?; rm -f -- "$cron_observed"; cleanup_lifecycle "$result"' EXIT
    read_legacy_cron || block_migration 'Cannot read the legacy crontab; managed writes remain blocked'
    if ! cmp -s "$cron_observed" "$legacy/crontab.before"; then
        cmp -s "$cron_observed" "$legacy/crontab.after" ||
            block_migration "Crontab has other edits. Restore only the old line from $legacy/crontab.before."
        crontab -u "$old_user" "$legacy/crontab.before" ||
            block_migration 'Could not restore the original cron entry'
        sync -f /var/spool/cron/crontabs
        read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.before" ||
            block_migration 'Could not confirm restoration of the old schedule'
    fi
    migration_state rolled-back 'Original cron restored. Managed writes are paused.'
    printf 'Restored original cron. Managed scheduling remains blocked.\n'
}
