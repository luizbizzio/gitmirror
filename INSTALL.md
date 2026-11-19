# Installing and maintaining GitMirror on Linux

The managed installer targets Debian 12 or later and Debian-based Raspberry Pi OS with systemd, on amd64 and ARM64. Windows and Docker use the existing installation methods.

## Install from a release

After the bootstrap reaches the default branch and a stable immutable release is published, the intended command is:

```sh
curl -fsSL https://raw.githubusercontent.com/luizbizzio/gitmirror/main/install.sh | sudo bash
```

The command is pending publication while PR #4 is a draft. Bash, curl and administrator access are required. The installer detects Python, Git, Git LFS, OpenSSH and other system dependencies, then asks before installing missing packages. For noninteractive installation, add `-s -- --yes` after `sudo bash` to grant that authorization explicitly.

The bootstrap fetches the latest stable **immutable** GitHub release or the one selected with `--tag vX.Y.Z`. It validates the release metadata, SHA-256 digest and source archive before extracting or installing it. The initial pipe runs a script obtained from the project over HTTPS with administrator rights. Review it at a known commit if you need to pin the bootstrap itself. Artifact checks rely on GitHub HTTPS, repository and release controls, and GitHub's asset digest. Python dependencies come from PyPI.

The managed executable is under `/opt/gitmirror-managed`, configuration and backups under `/var/lib/gitmirror-managed`, and installer state under `/etc/gitmirror-managed`. Both services run as the dedicated `gitmirror-managed` user. The dashboard binds to loopback by default.

For a headless host, append `-s -- --lan rpi5.local` to `sudo bash`. Use a hostname that resolves on your LAN or the Pi's LAN IPv4 address. Use `--port` if another application needs port 8787. The installer prints the exact URL and the SHA-256 fingerprint of its self-signed certificate. Match the fingerprint from the trusted installer terminal before trusting that certificate in the browser. Self-signed certificates encrypt traffic and require manual identity verification. A hostname or IP change requires a certificate valid for the new address. Keep the service on your trusted local network and omit router port forwarding.

## Setup in the browser

Open the printed URL and enter the one-time code. Set the administrator password, provider accounts and tokens, local disks and remote destinations, repository names, schedule and retention. Register the public key shown by the dashboard in the provider's account UI. The dashboard can match a host key against the provider's published SSH fingerprint. **Settings → Verify connections** checks API authentication and can sample an SSH read. This check does not establish push permissions. Select **Start scheduled applied synchronizations** when ready.

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

Use `status` to identify the active version and `check` to verify an available release. `upgrade` requires explicit confirmation, validates the current configuration with the candidate, drains active operations and switches the executable symlink. If the candidate fails readiness, the installer switches back and restarts the previous services. `rollback` activates a previously installed release with current data and refuses an incompatible configuration. No automatic update is scheduled.

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
