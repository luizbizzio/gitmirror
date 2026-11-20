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
may request your normal `sudo` password, but does **not** ask you to type
`UPGRADE` again. No `git clone`, archive creation, release tag or curl command
is needed after the first installation. When the installed version is already
current, the command makes no service changes.

Other management commands: `gitmirror -v` shows the installed version;
`gitmirror status`, `gitmirror check`, `gitmirror rollback` and
`gitmirror uninstall` call the corresponding managed operations. Unrecognized
arguments go to the existing Python `gitmirror` CLI in the virtual environment.
Rollback and uninstall retain their explicit confirmation prompts.

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
and restores the previous executable if activation fails. A partial managed
installation or retained managed state is never overwritten automatically.
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
to `sudo bash`. Upgrades still require an explicit `UPGRADE` confirmation.

A fresh install binds to localhost by default. For a headless Raspberry Pi,
append `-s -- --lan rpi5` to `sudo bash` if `rpi5` resolves through
Tailscale, or use your desired LAN hostname/IP. The chosen listener, certificate
and authorized hostnames remain unchanged on subsequent updates. You can also set
`--port` for first installation. A hostname change requires a certificate valid
for that name; reinstalling or upgrading is not a hostname migration mechanism.

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
forwarding.

## Setup in the browser

Open the printed URL and sign in with the temporary password shown in the installer output. Set a permanent administrator password before opening **Setup**. Then choose the source, destinations, repositories and schedule. For each remote account, choose **Connect account** if its OAuth app is registered, or **Configure manually (API token)**. Add the dashboard’s SSH public key to each account and verify each server’s host key against its published fingerprint. **Settings → Verify connections** can check the API and sample an SSH read. Enable scheduled applied synchronization after checking access.

A managed local destination gets its own directory automatically. External disks and NAS paths must already be mounted and writable by the service account. The advanced CLI is available at `/opt/gitmirror-managed/current/venv/bin/gitmirror`.

## Dashboard password and optional two-factor authentication

On first installation, GitMirror prints a random temporary password in the terminal. Enter it on the dashboard's first sign-in screen. Before using the configuration wizard, replace it with a permanent password of **at least six characters**. Numeric-only passwords are allowed and there are no required character categories. A six-digit numeric password is weak; use a longer passphrase when possible, particularly if the dashboard is accessible beyond localhost.

After completing setup, open **Settings → Two-factor authentication** on the managed HTTPS dashboard. Enter your current password, add the displayed setup key to an authenticator app (TOTP, SHA1, six digits, 30-second interval), and enter a current code to enable 2FA.

GitMirror shows eight single-use recovery codes only once. Save them away from the Raspberry Pi before closing the screen. Subsequent sign-ins require the dashboard password and a six-digit authenticator code, or one unused recovery code. Disabling 2FA requires the current password and a valid second factor. A lost authenticator can be replaced using a recovery code.

2FA is supported in the managed HTTPS dashboard. The separate standalone dashboard continues to use its configured password file. Login rate limits remain enabled. GitMirror keeps 2FA credentials in a private file under the managed state directory with owner-only permissions.

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
custom scripts and schedules separately. The installer refuses to overwrite the
directories, services and service account of an existing **managed** installation.
It leaves experimental scripts, credentials and archives untouched. If an older
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

Run `sudo bash install.sh uninstall` and confirm to remove the application and its two systemd services. The service account, backup volumes, configuration and credentials are retained. Legacy schedule recovery is separate.

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
