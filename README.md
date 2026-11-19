# GitMirror

GitMirror backs up Git history, branches, tags and Git LFS from GitHub, GitLab or Codeberg. A configuration can use remote destinations, a local disk destination, or both. Remote destinations also synchronize visibility and the default branch.

**Status:** alpha. Validation uses local repositories and simulated provider APIs. See the platform notes below for the tested environments and remaining checks.

## What it does

- Uses SSH for hosted Git repositories and each provider's API for repository settings.
- Discovers public, private, forked and archived repositories exposed to the configured account. It copies their Git data without recreating the provider's fork relationship or archived flag.
- Can create missing destination projects.
- Keeps destination projects if the source repository disappears.
- Backs up old Git refs locally before force push and branch deletion.
- Mirrors the source's default branch to the selected destinations.
- Can temporarily change simple GitLab branch protections and restore them.
- Retries temporary network errors.
- Has dry-run mode by default.

**Do not run this next to an older GitMirror process using the same storage path. The refactor has its own lock file. Test with a new, separate data directory first.**

**Not supported yet:** GitHub Releases, release files, repository descriptions, topics, issues, pull requests, discussions, wiki, and cross-provider recovery of those items.

## Requirements

Python 3.11 or newer, Git, Git LFS and OpenSSH. Set up an API token and SSH access for the source and for each selected remote destination. A local destination needs storage access and uses the source credentials only.

## Install

```sh
python -m pip install -e .
cp config.example.yml config.yml
```

Edit `config.yml` and set the token variables used by that configuration, or use
`token_file` paths outside the repository. Keep the actual tokens out of YAML.

### Configure sources and destinations

Version 2 takes one GitHub, GitLab or Codeberg source and a list of destinations.
Choose any other host, local disk, or a combination. A different account on the
same host can also be a destination. GitMirror rejects the source account as a
destination and checks for Git URLs that resolve to the source repository.
Each configuration allows one destination per provider, including local.

```yaml
version: 2
source:
  provider: gitlab
  owner: my-gitlab-account
  token_env: GITLAB_TOKEN
destinations:
  - provider: github
    owner: my-github-account
    token_env: GITHUB_TOKEN
  - provider: local
    path: /mnt/backups/gitmirror
storage:
  path: ~/gitmirror-work
```

For local-only backups, keep just the `local` entry. Use an absolute local path
such as `'D:\Backups\GitMirror'` on Windows, and keep the working cache separate.
Both functions are enabled for local storage. The optional `mirror`, `archive` and
`preserve_history` keys accept `true`. The destination account must match its
authenticated user. Discovery targets the configured account's own repositories.
Organization and group namespaces have not been validated.

Existing version 1 configurations still use their original working directories,
remote credentials and protection journal. To migrate, convert the destination
mapping into a list, add explicit owners and retain the schedule and retry options.
Resolve any pending GitLab protection changes with the v1 configuration first.
Version 2 uses a source-scoped working cache and a separate GitLab protection
journal. It leaves the v1 cache and its archived refs available on disk.

```sh
gitmirror --config config.yml validate
gitmirror --config config.yml doctor
gitmirror --config config.yml sync --only example-repo
gitmirror --config config.yml sync --apply --only example-repo
```

`python -m gitmirror` supports the same commands. The application source is `gitmirror.py`.

Dry-run reads the providers and Git refs without creating storage or a lock. For SSH, it
uses OpenSSH with strict host-key verification and reads identities from the SSH config
or agent. Configure trusted host keys before running it. Pass only one of `--apply` and
`--dry-run`.

`doctor` checks the Python, Git, Git LFS and OpenSSH versions, YAML, storage permissions
and free space. It reports `OK`, `WARNING` or `ERROR` and exits with status 2 for errors.
It inspects permissions without creating a file. For a missing storage path it checks the
nearest existing parent. On Windows the permission probe cannot verify effective ACL write access.

```sh
gitmirror --config config.yml doctor --network
gitmirror --config config.yml doctor --ssh --only example-repo --platform gitlab
```

`--network` checks the configured tokens with read-only provider identity requests.
`--ssh` also checks Git read access to one source repository and its existing destinations.
Use `--only` to choose that repository. An SSH read cannot establish push permissions.
For a local destination, `doctor` also checks its volume marker and available space.
`--platform` selects one configured target. `all` selects every configured target.

For regular sync every 12 hours:

```sh
gitmirror --config config.yml run --apply
```

`run` starts a sync at once, then runs again after the configured number of hours. For Linux, an external cron job is also possible. Do not run the internal scheduler and cron for the same data directory at the same time.

## Docker

Copy `docker/config.example.yml` for remote destinations or
`docker/config.local.example.yml` for local-only storage to `config.yml`. Prepare
`.env` and an SSH directory at `docker/ssh` with verified `known_hosts` and credentials
the container user can read. The Compose volume persists `/data`. Run:

```sh
docker compose config --quiet
docker compose build
docker compose run --rm gitmirror --config /config/config.yml doctor
docker compose up -d
```

For a new local backup volume, first create its backup directory and initialize it
as the service user:

```sh
docker compose run --rm --entrypoint mkdir gitmirror -p /data/backups
docker compose run --rm gitmirror --config /config/config.yml init-backup
```

The shipped service starts the scheduler in dry-run mode. To opt into writes, add
`--apply` to the Compose service command after testing the mounted SSH directory and
provider access. An applied one-off sync can use
`docker compose run --rm gitmirror --config /config/config.yml sync --apply`.

## Safety

Before overwriting a destination, GitMirror verifies the exact branch and tag names
and hashes, and checks the size and SHA-256 of the LFS objects needed to restore them.
Only complete snapshots move into `refs/archive/`. Interrupted or failed backups may leave
refs under `refs/gitmirror-staging/`. The next cycle builds a fresh snapshot. Use
`refs/archive/` for restoration. GitMirror keeps destination projects when their sources disappear.

GitMirror rechecks source repository identity, visibility and default branch before
each outgoing Git or LFS transfer and before publishing a destination. If the check
disagrees with discovery or fails, it stops that repository for the current cycle.
New destinations start private and can be published after Git and LFS verification.
The provider reads and writes are separate requests, so a change after the final
check is still possible.

When the default branch changes, GitMirror archives the existing destination and
pushes the replacement branch first. It confirms the new default through the
destination API and Git's advertised `HEAD` before the push that can remove the former
branch. The former branch stays when it still exists at the source. If the default
cannot be confirmed or deletion fails, the next applied run inspects the destination
again and resumes from its observed state. [GitLab Projects API](https://docs.gitlab.com/api/projects/#edit-a-project) · [Codeberg API schema](https://codeberg.org/swagger.v1.json)

Pushes use atomic updates and explicit leases. A server that declines atomic push
can receive a single leased change after a fresh ref check. Multi-ref updates on such
servers fail before updating refs. GitMirror checks the final ref map and reports a
failed or incomplete push as an error. After an interrupted run, the next applied
cycle restores pending GitLab protections and verifies the destination before retrying.
[Git push documentation](https://git-scm.com/docs/git-push)

GitMirror hashes required objects in the local backup. For destination read-back it
checks all new or expired objects, then keeps verification timestamps by object ID
and size in the local bare repository. Evidence is scoped to the destination and its
configured LFS endpoint and expires after seven days. Objects with fresh evidence are
reused, including when a run introduces new objects. A remote object removed or
damaged after a successful read-back may go undetected until its evidence expires.

Use `--verify-lfs` with `--apply` to bypass remote evidence and check every object reachable
from the current mirrored refs. This also checks the local historical archive.
Full read-back can take significant bandwidth and temporary disk space.

```sh
gitmirror --config config.yml sync --apply --verify-lfs --only example-repo
```

### Local mirror, archive and offline restoration

Create the backup directory on the intended disk and run `init-backup` once.
GitMirror requires a volume marker for sync and never creates the configured
backup root. If a drive is disconnected, an unmarked fallback directory is rejected.

```sh
gitmirror --config config.yml init-backup
gitmirror --config config.yml sync --apply --platform local
gitmirror --config config.yml verify-backup
```

For each repository the backup root contains
`<source-scope>/<repository-id>/generations/<run-id>.git` and a `CURRENT` file.
Each generation is an independent bare repository. Its heads and tags mirror the
source. Refs under `refs/archive/<prior-run-id>/` retain earlier names and objects,
including deleted branches and replaced tags. Regular Git garbage collection
keeps the objects reachable through those archive refs. Required LFS binaries are
copied into the generation, including binaries referenced only by its archives.

GitMirror checks the complete ref map, default branch, Git object database and
every required LFS object's size and SHA-256 before advancing `CURRENT`.
The previous generation remains available after an interruption or write failure.
Historical generations are retained automatically, and changed snapshots can
consume substantial space because each generation is independent. An interrupted
write may leave a staging directory or an unreferenced generation.

`verify-backup` checks the current generations offline. Use `--only NAME` to
select a repository. The output gives the verified bare repository path. Choose
that path, or a known-good older generation if the current copy is damaged.
Replace the example file URL and `trunk` with the path and default branch in
`gitmirror-backup.json`. A Windows file URL starts like `file:///D:/Backups/`.

```sh
git init --bare restored.git
git -C restored.git remote add backup file:///absolute/path/to/generation.git
git -C restored.git fetch --no-tags backup 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' 'refs/archive/*:refs/archive/*'
git -C restored.git symbolic-ref HEAD refs/heads/trunk
git -C restored.git lfs fetch --all backup
git -C restored.git fsck --full
```

No hosting token, API or working cache is required for this restore. Store backups
outside the source checkout and the Docker build context. Restrict write access
to the backup volume since it holds private Git and LFS data. Integrity checks
detect corruption against the stored manifest, which is local and unsigned.
Network filesystems need reliable locking and atomic rename. Physical removable
drives, network filesystems and power loss have not been exercised here.

### Restore a remote destination snapshot from the working archive

Choose the exact `refs/archive/<run-id>/<destination>` shown in a successful backup.
Restore into an empty local repository first. Replace the example run ID and the
absolute file URL with your own. On Windows, a local file URL looks like
`file:///C:/GitMirror-data/example-repo.git`.

```sh
git init --bare restored.git
git -C restored.git remote add backup file:///absolute/backup/example-repo.git
git -C restored.git fetch --no-tags backup \
  'refs/archive/RUN-ID/gitlab/heads/*:refs/heads/*' \
  'refs/archive/RUN-ID/gitlab/tags/*:refs/tags/*'
git -C restored.git lfs fetch --all backup
git -C restored.git fsck --full
git -C restored.git for-each-ref --format='%(refname) %(objectname)' refs/heads refs/tags
```

Compare the restored names and hashes against the chosen archive and open or verify
the LFS content before relying on the restore.

The working archive relies on the configured working storage. Use an independent
local destination and a second copy on another device for disk-loss recovery.

### Platform and production comparison

| Environment | Validation |
| --- | --- |
| Windows x64, Python 3.13 | Full local suite with Git for Windows and Git LFS. |
| Debian 12 amd64, Python 3.11 | Full local suite and an authenticated Git read against a local OpenSSH server with mocked provider APIs. |
| Docker amd64, Python 3.12 | Built the image, ran the full suite as UID 10001, and exercised Compose configuration, mounts and the offline doctor. |

The local suite covers the six directions between distinct hosting providers,
local-only backups from each, hybrid destinations, rewrites, branch and tag deletion, visibility checks,
backup restoration and GitLab protection recovery with temporary repositories and
mocked APIs. Authentication and writable-service integration with GitHub, GitLab and
Codeberg and ARM64 deployment require separate validation. The original production script is unavailable
for direct comparison. The current implementation preserves the documented 12-hour
default and the Git, LFS, visibility and protection-management flows.

## Roadmap

1. Releases, release assets, descriptions, topics, and homepage.
2. Portable archive for issues, pull requests, reviews and discussions.
3. Verified restore tools and independent backup targets.
4. Validate provider integration and ARM64 deployments.

License: Apache-2.0.
