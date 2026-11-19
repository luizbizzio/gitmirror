# Installing and maintaining GitMirror on Linux

The managed installer targets Debian 12 or later and Debian-based Raspberry Pi OS with systemd, on amd64 and ARM64. Windows and Docker use the existing installation methods.

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
The installer does not modify unrelated experimental cron jobs.

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

Open the printed URL to view the dashboard and read the available settings before configuration. The empty state is a preview, not live repository data. Select **Setup** in the top-right corner and enter the one-time code. Set the administrator password, provider accounts and tokens, local disks and remote destinations, repository names, schedule and retention. Register the public key shown by the dashboard in the provider's account UI. The dashboard can match a host key against the provider's published SSH fingerprint. **Settings → Verify connections** checks API authentication and can sample an SSH read. This check does not establish push permissions. Select **Start scheduled applied synchronizations** when ready.

A managed local destination gets its own directory automatically. External disks and NAS paths must already be mounted and writable by the service account. The advanced CLI is available at `/opt/gitmirror-managed/current/venv/bin/gitmirror`.

## Replacing an experimental installation

This installer does not import or detect experimental GitMirror scripts, cron jobs,
credentials or archives. It only refuses to overwrite the directories, services and
service account of an existing **managed** GitMirror installation.

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
  web/app.js web/index.html web/style.css
```

Upload it to a stable GitHub release with release immutability enabled. Verify that the release API reports the release as immutable and supplies the uploaded asset's SHA-256. Exercise the public command on a disposable host before announcing it.
