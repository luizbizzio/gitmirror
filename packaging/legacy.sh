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
        [[ "${target##*/}" == gitmirror.py ]]
    else
        [[ "$binary" == gitmirror || "$binary" == gitmirror.py ]]
    fi
}

prepare_legacy() {
    [[ -f "$old_lock" && ! -L "$old_lock" && "$(realpath -e -- "$old_lock")" == "$old_lock" &&
       "$old_lock" =~ ^/[a-zA-Z0-9_./+-]+$ ]] || fail 'Choose the canonical path of the existing flock file'
    getent passwd "$old_user" >/dev/null || fail 'Legacy cron account does not exist'
    local directory="$etc/legacy" line path sequence=0
    install -d -m 0700 -- "$directory"
    (umask 077; crontab -u "$old_user" -l >"$directory/crontab.before") ||
        fail 'Cannot read the legacy user crontab'
    line=$(sed -n "${old_line}p" "$directory/crontab.before")
    selected_legacy_command "$line" "$old_lock" ||
        fail 'Choose a cron line that only invokes GitMirror through flock -n with the given path'
    (umask 077; awk -v number="$old_line" 'NR != number' "$directory/crontab.before" >"$directory/crontab.after")
    (umask 077; printf '%s\n%s\n' "$old_user" "$old_line" >"$directory/selection")
    for path in "${preserve[@]}"; do
        [[ "$path" == /* && "$path" != *$'\n'* && -f "$path" ]] ||
            fail 'Preserve accepts absolute paths to individual configuration, script or credential files'
        ((sequence+=1))
        install -m 0600 -- "$path" "$directory/file-$sequence"
        (umask 077; printf '%s\t%s\n' "file-$sequence" "$path" >>"$directory/preserved")
    done
    { printf '%s\n' "$old_lock"; stat --format='%d %i' -- "$old_lock"; } >"$etc/legacy-lock"
    chmod 0644 "$etc/legacy-lock"
    touch "$etc/migration-pending"
}

legacy_selection() {
    legacy="$etc/legacy"
    [[ -f "$legacy/selection" && -f "$etc/legacy-lock" ]] || fail 'No staged legacy migration found'
    { read -r old_user; read -r old_line; } <"$legacy/selection"
    { read -r old_lock; read -r lock_identity; } <"$etc/legacy-lock"
}

legacy_lock() {
    local path
    path=$(realpath -e -- "$old_lock") || fail 'Legacy lock path is unavailable'
    [[ "$path" == "$old_lock" && -f "$path" ]] || fail 'Legacy lock path changed'
    exec {old_fd}<"$path"
    [[ "$(stat -L --format='%d %i' "/proc/$$/fd/$old_fd")" == "$lock_identity" ]] ||
        fail 'Legacy lock inode changed; review the old installation'
    flock -n "$old_fd" || fail 'The old job is running. Retry after it releases its flock.'
}

read_legacy_cron() {
    crontab -u "$old_user" -l >"$cron_observed"
}

cutover_cleanup() {
    local result=$?
    if ((result != 0 && changed_cron)); then
        touch "$etc/migration-pending"
        if read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.after"; then
            crontab -u "$old_user" "$legacy/crontab.before" ||
                printf 'Original cron backup needs recovery at %s\n' "$legacy" >&2
        fi
    fi
    rm -f -- "$cron_observed"
    cleanup_lifecycle "$result"
}

cutover() {
    legacy_selection
    [[ -e "$etc/migration-pending" ]] || fail 'This migration is already active'
    # Exercise actual configuration and source/destination reads from a disposable work path.
    runuser -u "$account" -- "$app/current/venv/bin/gitmirror" managed-preflight --state-dir "$state" ||
        fail 'Staging dry-run failed. Old cron was left unchanged.'
    runuser -u "$account" -- test -r "$old_lock" ||
        fail 'The managed service needs read and traverse permission to share the legacy flock'
    confirm CUTOVER "Disable only user $old_user's GitMirror cron line $old_line after validating the staged system."
    cron_observed=$(mktemp)
    changed_cron=0
    trap cutover_cleanup EXIT
    drain
    legacy_lock
    read_legacy_cron || fail 'Cannot re-read the existing user crontab'
    if cmp -s "$cron_observed" "$legacy/crontab.before"; then
        local remaining
        remaining=$(legacy_cron_reports | awk -v selected="user-cron:$old_user:$old_line" '$0 != selected')
        [[ -z "$remaining" ]] || fail "Other possible GitMirror schedules require review before cutover: $remaining"
        # Re-read immediately before the replacement so unrelated edits cannot be silently discarded.
        read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.before" ||
            fail 'The legacy crontab changed; review its changes before retrying'
        changed_cron=1
        crontab -u "$old_user" "$legacy/crontab.after"
    elif cmp -s "$cron_observed" "$legacy/crontab.after"; then
        # Recover from an interruption after the cron was parked but before unblocking the new scheduler.
        [[ -z "$(legacy_cron_reports)" ]] || fail 'Other possible GitMirror schedules require review'
        changed_cron=1
    else
        fail 'The legacy crontab changed; no lines were altered'
    fi
    read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.after" ||
        fail 'Cannot verify that the selected cron entry was disabled'
    health || fail 'The new dashboard or scheduler is unavailable'
    rm -- "$etc/migration-pending"
    changed_cron=0
    printf 'Cutover complete. Enable scheduling in the new dashboard when ready. Original files remain in place.\n'
}

rollback_migration() {
    legacy_selection
    confirm RESTORE-CRON "Restore the exact original GitMirror cron entry for $old_user. Keep the staged services idle."
    # Stop dispatch before draining existing managed writes. Readers remain available.
    touch "$etc/migration-pending"
    drain
    legacy_lock
    cron_observed=$(mktemp)
    trap 'result=$?; rm -f -- "$cron_observed"; cleanup_lifecycle "$result"' EXIT
    read_legacy_cron || fail 'Cannot read the legacy crontab; the new scheduler remains blocked'
    if cmp -s "$cron_observed" "$legacy/crontab.before"; then
        printf 'Original cron is already present. Managed scheduling is blocked.\n'
    else
        cmp -s "$cron_observed" "$legacy/crontab.after" ||
            fail "Crontab has other edits. Restore only the old line from $legacy/crontab.before."
        crontab -u "$old_user" "$legacy/crontab.before"
        read_legacy_cron && cmp -s "$cron_observed" "$legacy/crontab.before" ||
            fail 'Could not confirm restoration of the old schedule'
        printf 'Restored original cron. Managed scheduling remains blocked.\n'
    fi
}
