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

## Legacy migration

The installer's `status` command reports likely installations by path, process ID, cron owner and line number, or systemd unit. Reports omit cron command text and credentials. Detection covers conventional paths, user crontabs, system cron files and periodic directories. Scripts with unrelated names, remote schedulers and custom wrappers need operator review. Regular installation stops when it finds a potential legacy installation.

Automated cutover supports one explicit **user crontab** entry invoking GitMirror through `flock -n` with an existing canonical lock path. The installer reports system cron jobs, periodic jobs, systemd services and additional GitMirror schedules for manual review. It leaves them untouched.

Use a reviewed local copy of the bootstrap, or pass the same subcommand and flags after `sudo bash -s --` in the release command:

```sh
sudo bash install.sh migrate \
  --legacy-cron pi --cron-line 12 --legacy-lock /srv/gitmirror-legacy/gitmirror.lock \
  --preserve /home/pi/gitmirror/config.yml --preserve /home/pi/.config/gitmirror/token
```

Repeat `--preserve` for each configuration, standalone script or credential **file** to capture. The old scheduler continues during staging. The installer writes root-only copies of the selected files and original crontab under `/etc/gitmirror-managed/legacy`. Old Git history, LFS objects and archives remain in their original directories. New work, staging backups and credentials use separate storage. Use the browser to configure the new account with its own private credentials.

Complete browser setup with scheduling off. Migration staging can check provider access, but blocks applied sync. During staging, use managed local backup directories. Run `sudo bash install.sh cutover` when ready. Cutover runs the actual engine in dry-run mode with disposable work storage. After explicit confirmation, it checks for competing schedules and unrelated cron edits, takes the original flock, and disables only the selected line. Avoid editing the selected user crontab concurrently with cutover. It leaves the new scheduler disabled until you enable it in the dashboard. The managed account needs read and traverse permission on the original lock. Managed applied jobs continue acquiring that lock and verify its inode. For unattended startup after reboot, the original lock file needs to persist on disk with the same inode. A changed, missing or busy lock blocks writes.

To switch back, run `sudo bash install.sh rollback-migration` and confirm. It blocks managed writes, drains in-flight operations, and restores the exact original crontab if the parked version still matches. If cron changed, it keeps managed writes blocked and leaves the current crontab untouched for manual recovery. Either direction preserves the old and new repositories, LFS objects and credentials.

## Upgrade, rollback and uninstall

Use `status` to identify the active version and `check` to verify an available release. `upgrade` requires explicit confirmation, validates the current configuration with the candidate, drains active operations and switches the executable symlink. If the candidate fails readiness, the installer switches back and restarts the previous services. `rollback` activates a previously installed release with current data and refuses an incompatible configuration. No automatic update is scheduled.

For a trusted offline release, use `--tag vX.Y.Z --artifact FILE --sha256 HEX`. Obtain the digest through a trusted channel.

Run `sudo bash install.sh uninstall` and confirm to remove the application and its two systemd services. The service account, backup volumes, configuration, credentials and migration records are retained. Legacy schedule recovery is separate.

## Release preparation

Create `gitmirror-linux-source.tar.gz` from reviewed sources. The archive contains only these regular files with relative paths, without a containing directory.

```sh
tar -czf gitmirror-linux-source.tar.gz --no-recursion \
  INSTALL.md LICENSE NOTICE.txt README.md gitmirror.py install.sh \
  packaging/legacy.sh packaging/lifecycle.sh pyproject.toml \
  web/app.js web/index.html web/style.css
```

Upload it to a stable GitHub release with release immutability enabled. Verify that the release API reports the release as immutable and supplies the uploaded asset's SHA-256. Exercise the public command on a disposable host before announcing it.
