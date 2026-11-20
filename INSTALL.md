# Installing and maintaining GitMirror on Linux

The managed installer targets Debian 12 or later and Debian-based Raspberry Pi OS with systemd, on amd64 and ARM64. Windows and Docker use the existing installation methods.

## Pi-hole-style command-line updates

After the **first** managed installation, GitMirror places a small administrator
launcher at `/usr/local/bin/gitmirror`. Update it with the same kind of command
used by Pi-hole:

```sh
gitmirror update
```

`gitmirror -up` and `gitmirror upgrade` remain supported aliases. This command checks for a newer version, prints
the current and candidate versions, and applies the update when available. It
may request your normal `sudo` password and then ask you to confirm with
`y` or `n`. Press Enter to cancel. You do not need to type `UPGRADE`. No `git clone`, archive creation, release tag or curl command
is needed after the first installation. When the installed version is already
current, the command makes no service changes.

Other management commands: `gitmirror -v` shows the installed version;
`gitmirror status`, `gitmirror check`, `gitmirror rollback` and
`gitmirror uninstall` call the corresponding managed operations. Unrecognized
arguments go to the existing Python `gitmirror` CLI in the virtual environment.
Update, rollback and uninstall ask `Continue? [y/N]` before making changes.
Answer `y` to proceed, or `n` (or Enter) to cancel. In scripts, the existing
`--confirm UPGRADE`, `--confirm ROLLBACK`, and `--confirm UNINSTALL`
options remain supported.

On systems installed before the command launcher was introduced, run the
one-command installer **once** to upgrade and register the command. Existing
backup volumes, credentials, TLS files, listener settings and application state
are preserved. The previous release is retained for rollback. GitMirror does
not schedule unattended application updates.

## One-command installation and updates

The same command installs GitMirror on a clean system or updates an existing managed
installation:

```sh
curl -fsSL https://raw.githubusercontent.com/luizbizzio/gitmirror/main/install.sh | sudo bash
```

The installer checks the managed installation before making changes. Existing
installations use the upgrade lifecycle, retaining their saved accounts, credentials,
backup generations, systemd service configuration, TLS certificate and listener
hostname. It checks whether the chosen version is already installed, asks for
confirmation before an upgrade, waits for active operations, runs health checks,
and restores the previous executable if activation fails.

**Reinstalling after uninstall:** GitMirror's normal uninstall removes the program
and systemd units but preserves its service account, state and privileged settings.
Running this same `curl | sudo bash` command again detects that complete retained
installation, asks `Continue? [y/N]`, reinstalls the application and services,
and **retains passwords, 2FA, provider credentials, accounts, HTTPS identity and
backups**. It does not display or generate a new temporary password when a
permanent password already exists. The previously configured address is reused,
even if you supply new `--port` or `--lan` arguments. You can also explicitly
request this operation with `sudo bash install.sh reinstall`.

An **incomplete or inconsistent installation** (such as an interrupted install
with leftover application directories, missing lock metadata, missing account,
or service units still present) is not automatically overwritten. Diagnose
its paths and services instead of deleting backups blindly. This safety check
prevents a fresh installation from deleting data or adopting unknown files.
The installer warns about likely old GitMirror cron entries during install,
update and `gitmirror status`. It reports the account or cron file without
printing the job command. It leaves those jobs untouched.

By default, the installer prefers the latest **stable, immutable** GitHub release
with a matching verified SHA-256 archive. While the project has **no public stable
release**, GitHub returns HTTP 404 for this lookup. In that case, the installer
automatically uses a development snapshot from the current `main` branch and
prints a warning. No separate development branch is created or required.
This is not an immutable release: changes on `main` can be deployed by the next
installation command. A revision is identified by its Git commit and the selected
files are packed locally and checked before installation. To explicitly use the
latest development snapshot even when a release exists, append `-s -- --dev`
to `sudo bash`. Upgrades ask for a `y` or `n` confirmation.

On a fresh install, GitMirror enables LAN HTTPS and uses the device's **short
hostname** (for example `rpi5`) rather than adding `.local`. It prints the URL
and a certificate fingerprint. From another device, use
`https://rpi5:8787`, the Raspberry Pi's actual LAN IP
(`https://192.168.1.80:8787` is an example), the Tailscale IP, or the exact
Tailscale MagicDNS name, as applicable. The short hostname must resolve on the
client device. To restrict a new installation to localhost, append
`-s -- --local-only` to `sudo bash`.

For a specific first-install hostname/IP, append `-s -- --lan rpi5` or
`-s -- --lan 192.168.1.80`. The installer-generated certificate includes the
configured hostname, short hostname, currently assigned device IPs, and the
Tailscale MagicDNS name when Tailscale is available during installation.
Certificate trust is separate from hostname approval: a browser may still
warn about a self-signed certificate. Existing installations keep their
original certificate and may require its renewal before newly accepted
hostnames or IPs appear in its certificate's Subject Alternative Names.

The listener, its existing certificate and saved settings are preserved during
updates and retained-state reinstalls. Use `--port` only on first installation.
Changing the listener hostname requires separately updating its certificate
and service configuration; an ordinary upgrade is not a hostname migration.

Bash, curl and administrator access are required. Missing Python, Git, Git LFS,
OpenSSH and other system packages require explicit approval. Use `--yes` only
if you approve installation of missing apt packages. Release downloads are
validated against GitHub's immutable metadata, pinned artifact digest, archive
member list and file types. Executing the bootstrap with sudo trusts the source
served at the specified URL. Development builds further trust the mutable
GitHub main branch and HTTPS Git transport; they should not be treated as
verified stable releases. Dependencies are installed from PyPI.

Managed code lives under `/opt/gitmirror-managed`, app state under
`/var/lib/gitmirror-managed` and privileged installer state under
`/etc/gitmirror-managed`. Services use the dedicated `gitmirror-managed`
account. For LAN HTTPS, verify the certificate fingerprint printed during
installation before trusting the browser warning, and avoid public port
forwarding. The managed dashboard accepts Host headers for the configured
hostname, the device's short/system hostname, IP addresses actually assigned
to local interfaces (including Tailscale), and its exact Tailscale MagicDNS
name. It does not accept unrestricted domain suffixes or arbitrary
private-network IPs. The standalone dashboard keeps its explicit allowed-host
configuration. Origin/CSRF checks remain active for all hostnames.

## Setup in the browser

Open the printed URL and sign in with the temporary password shown in the installer output. Set a permanent administrator password before opening **Setup**. Then choose the source, destinations, repositories and schedule. For each remote account, choose **Connect account** if its OAuth app is registered, or **Configure manually (API token)**. Add the dashboard’s SSH public key to each account and verify each server’s host key against its published fingerprint. **Settings → Verify connections** can check the API and sample an SSH read. Enable scheduled applied synchronization after checking access.

A managed local destination gets its own directory automatically. External disks and NAS paths must already be mounted and writable by the service account. The advanced CLI is available at `/opt/gitmirror-managed/current/venv/bin/gitmirror`.

## Dashboard password and optional two-factor authentication

On first installation, GitMirror prints a random temporary password in the terminal. Enter it on the dashboard's first sign-in screen. Before using the configuration wizard, replace it with a permanent password of **at least six characters**. Numeric-only passwords are allowed and there are no required character categories. A six-digit numeric password is weak; use a longer passphrase when possible, particularly if the dashboard is accessible beyond localhost.

After completing setup, open **Settings → Two-factor authentication** on the managed HTTPS dashboard. Enter your current password, add the displayed setup key to an authenticator app (TOTP, SHA1, six digits, 30-second interval), and enter a current code to enable 2FA.

GitMirror shows eight single-use recovery codes only once. Save them away from the Raspberry Pi before closing the screen. Subsequent sign-ins require the dashboard password and a six-digit authenticator code, or one unused recovery code. Disabling 2FA requires the current password and a valid second factor. A lost authenticator can be replaced using a recovery code.

2FA is supported in the managed HTTPS dashboard. The separate standalone dashboard continues to use its configured password file. Login rate limits remain enabled. GitMirror keeps 2FA credentials in a private file under the managed state directory with owner-only permissions.

## Dashboard users and roles

The managed installation can create up to 32 local dashboard accounts. Existing installations are upgraded automatically: the previous administrator is preserved as username `admin`, with its password and any configured TOTP authenticator. No reinstallation or new initial setup is required. Sign in using username `admin` and your existing password.

Open **Users** as an administrator to create a user, select their role, change a role, reset a password or 2FA, or delete a user. Creating or changing an account requires the administrator's current password. New users receive an initial password chosen by the administrator and should change it in **Settings** after signing in. All users can configure their own TOTP app and single-use recovery codes.

| Role | Access |
| --- | --- |
| Administrator | Full configuration, OAuth connections, SSH settings, repository sync, backup verification, restore, account management and audit history |
| Operator | Read the dashboard and logs, refresh repository inventory, run dry-run or applied syncs, check connections and verify backups |
| Viewer | Read dashboard status, repositories, backups and redacted activity logs only |

Roles are enforced on the server for every privileged action. Only administrators can access account management, configuration and restore endpoints. Users cannot delete their own account or demote themselves from the administrator role, and GitMirror prevents removing the last administrator. Role changes, password changes and 2FA resets revoke affected sessions; a current self-service session stays signed in after password or 2FA changes. User credentials are hashed with scrypt and stored separately from GitMirror's versioned repository configurations.

**Audit history:** the Users page records who created or changed accounts, changed 2FA and passwords, updated configurations, or started sync, verification and restore operations. It stores no submitted passwords, OAuth tokens or TOTP codes. Scheduled CLI operations remain identified as scheduled in the normal activity log rather than attributed to a dashboard account.

The managed Linux dashboard supports user accounts; standalone or Docker dashboards keep their existing single-password behavior.

## Register OAuth apps for browser authorization

Open the managed dashboard at the HTTPS address your browser will use every time,
for example `https://rpi5:8787`. The provider redirects the browser to that address
after approval. The Raspberry Pi can stay on your LAN. Use a certificate trusted
by your browser. When TLS terminates at a reverse proxy, preserve the configured
Host and omit callback query strings from reverse-proxy access logs.

| Provider | Register the app and grant access |
| --- | --- |
| GitHub | Create a **GitHub App** with expiring user tokens and **Device Flow** enabled. Leave the webhook inactive. Copy the **Client ID** and install the app on the account being connected. For a source, grant Metadata read and select the repositories GitMirror should see. For a destination, grant Administration write and install for all repositories so GitMirror can create new ones. GitHub App installation permissions and the signed-in user’s access both apply. |
| GitLab | In **Applications**, register an app with the exact GitLab callback below. Enable `read_api` for a source and `api` for a destination. For device approval, use a public application on GitLab 17.9 or later and allow the `device_code` grant. If the instance lacks device approval or refreshable device credentials, use browser approval. |
| Codeberg | In **Settings → Applications**, register a public OAuth2 application for the exact Codeberg callback below. Forgejo OAuth grants broad account access. Confirm that grant in GitMirror, or choose a scoped manual API token. |
| Gitea | On each Gitea 1.x instance starting with 1.23, register a public OAuth2 application under **Settings → Applications** for the exact Gitea callback below. GitMirror requests User read plus Repository read for sources, and User read plus Repository write for destinations. |

See the provider documentation for [GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app), [GitLab OAuth](https://docs.gitlab.com/api/oauth2/), [Forgejo OAuth](https://forgejo.org/docs/latest/user/authentication/oauth2-provider/) and [Gitea OAuth](https://docs.gitea.com/development/oauth2-provider/).

Create `/var/lib/gitmirror-managed/oauth-apps.json` outside the repository.
Restrict the file to mode `0600` and make it readable by the GitMirror service
user. Replace each example Client ID with the provider-issued value and omit
registrations you will not use.

```json
[
  {
    "provider": "github",
    "client_id": "YOUR_GITHUB_APP_CLIENT_ID"
  },
  {
    "provider": "gitlab",
    "client_id": "YOUR_GITLAB_APPLICATION_ID",
    "redirect_uri": "https://rpi5:8787/api/managed/oauth/gitlab/callback",
    "device": true
  },
  {
    "provider": "codeberg",
    "client_id": "YOUR_CODEBERG_APPLICATION_ID",
    "redirect_uri": "https://rpi5:8787/api/managed/oauth/codeberg/callback"
  },
  {
    "provider": "gitea",
    "api_url": "https://gitea.example.net/api/v1",
    "client_id": "YOUR_GITEA_APPLICATION_ID",
    "redirect_uri": "https://rpi5:8787/api/managed/oauth/gitea/callback"
  }
]
```

For a confidential browser client, add `client_secret` to its private
registration. GitHub Device Flow does not require one. A confidential GitLab
client uses browser approval. For self-managed GitLab, set its exact HTTPS
`api_url` ending in `/api/v4`. For private Gitea or GitLab servers, specify
approved RFC 1918 or IPv6 ULA addresses explicitly, for example
`"private_ips": ["192.168.10.20"]`. Use exact addresses and certificates trusted by
the service’s TLS trust store. GitMirror pins connections to approved DNS
answers and rejects redirects. The repository SSH hostname must match the
registered server. Set `ssh_port` if the server advertises a port other than 22.
Verify its host key separately. Each self-hosted instance needs its own registration.

In the dashboard, choose the provider and API URL, then **Connect account**.
For device approval, leave the configuration tab open and use the short code
on the provider’s verification page. For browser approval, the registered
callback must match the URI displayed in the account card exactly. A connected
card means that the provider confirmed the account and repository API access.
Write permissions and SSH Git access still depend on the account, app and keys.
If you abandon setup after granting access, disconnect the draft connection
before closing the editor or revoke the grant with the provider.

Managed profiles store OAuth connection references. The service keeps access
and refresh tokens in a private state directory and renews them for applied CLI
and scheduled runs. A dry-run or `doctor` uses an existing valid access token.
When it needs renewal, use **Check access** in Settings or run an applied sync.
**Disconnect** erases GitMirror’s credentials and requests revocation on
GitHub or GitLab. On Codeberg and Gitea, or if the revocation request fails,
revoke the grant under **Manage permissions**. Reconnect or switch explicitly
to a manual token to resume sync.

Existing manual profiles need no migration. Older GitMirror versions cannot
validate an OAuth profile. Switch back to manual tokens in Settings before
rolling back to a version without OAuth.

## Replacing an experimental installation

The cron warning covers user crontabs and common system cron paths. Check
custom scripts and schedules separately. The installer upgrades a complete running **managed** installation and can
reinstall over a complete, inactive retained state after a normal uninstall.
It refuses to overwrite incomplete or unrecognized installations. Experimental
scripts, credentials and archives outside the managed directories are untouched. If an older
managed install paused writes for migration, updates keep that pause. Check
`gitmirror status` before resuming.

Before a clean installation on a machine that already runs a separate GitMirror
script, back up that script, its configuration, credentials, Git and LFS archives.
Check that its sync is not running and disable its cron or other scheduler.
The clean installation uses different directories and leaves old files untouched.
Configure the new accounts and destinations in the browser, verify access and
complete a dry-run before enabling any applied synchronization. Do not run the
old and managed schedulers against the same repositories at the same time.

Managed applied runs use `/etc/gitmirror-managed/sync.lock`, a root-owned lock
in a protected directory. It persists across reboot and protects managed jobs
and maintenance operations, but does not coordinate with unrelated old scripts.

## Upgrade, rollback and uninstall

Run the same online installer for future upgrades. Use `status` to identify the active version and `check` to inspect an available package. Explicit `upgrade` also requires confirmation, validates the current configuration with the candidate, drains active operations and switches the executable symlink. If the candidate fails readiness, the installer switches back and restarts the previous services. `rollback` activates a previously installed release with current data and refuses an incompatible configuration. No automatic update is scheduled.

For a trusted offline release, use `--tag vX.Y.Z --artifact FILE --sha256 HEX`. Obtain the digest through a trusted channel.

Run `gitmirror uninstall` and answer `y` to remove the application and its two systemd services. The service account, backup volumes, configuration and credentials are retained. Reinstall with the original curl command (confirm `y`) to restore the program and reuse them. An incomplete installation still requires diagnosis rather than an unsafe overwrite. Legacy schedule recovery is separate.

## Release preparation

Create `gitmirror-linux-source.tar.gz` from reviewed sources. The archive contains only these regular files with relative paths, without a containing directory.

```sh
tar -czf gitmirror-linux-source.tar.gz --no-recursion \
  INSTALL.md LICENSE NOTICE.txt README.md gitmirror.py install.sh \
  packaging/lifecycle.sh pyproject.toml \
  web/app.js web/index.html web/style.css web/fonts/LICENSE.md \
  web/fonts/source-sans-pro-regular.woff2 \
  web/fonts/source-sans-pro-semibold.woff2 \
  web/fonts/source-sans-pro-bold.woff2 \
  web/fonts/source-sans-pro-italic.woff2
```

Upload it to a stable GitHub release with release immutability enabled. Verify that the release API reports the release as immutable and supplies the uploaded asset's SHA-256. Exercise the public command on a disposable host before announcing it.
