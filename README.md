# GitMirror

GitMirror copies Git branches, tags, Git LFS objects, and repository visibility from GitHub to GitLab and Codeberg. It keeps old Git references on the local machine before changing destination history.

**Status:** alpha. Validation uses local repositories and simulated provider APIs. See the platform notes below for the tested environments and remaining checks.

## What it does

- Uses SSH for Git and APIs for repository settings.
- Supports public and private repositories, forks, and archived repositories.
- Can create missing destination projects.
- Keeps destination projects if the source repository disappears.
- Backs up old Git refs locally before force push and branch deletion.
- Mirrors the GitHub default branch to GitLab and Codeberg.
- Can temporarily change simple GitLab branch protections and restore them.
- Retries temporary network errors.
- Has dry-run mode by default.

**Do not run this next to an older GitMirror process using the same storage path. The refactor has its own lock file. Test with a new, separate data directory first.**

**Not supported yet:** GitHub Releases, release files, repository descriptions, topics, issues, pull requests, discussions, wiki, and cross-provider recovery of those items.

## Requirements

Python 3.11 or newer, Git, Git LFS, OpenSSH, and API tokens for the three providers. You must set up SSH access to all three Git hosts.

## Install

```sh
python -m pip install -e .
cp config.example.yml config.yml
```

Edit `config.yml`. Set `GITHUB_TOKEN`, `GITLAB_TOKEN` and `CODEBERG_TOKEN` in your environment, or use `token_file` paths. Do not upload real tokens.

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

For regular sync every 12 hours:

```sh
gitmirror --config config.yml run --apply
```

`run` starts a sync at once, then runs again after the configured number of hours. For Linux, an external cron job is also possible. Do not run the internal scheduler and cron for the same data directory at the same time.

## Docker

Copy `docker/config.example.yml` to `config.yml` (it uses `/data`), then prepare `.env` and an SSH directory at `docker/ssh` with verified `known_hosts` and credentials the container user can read. Change `storage.path` in Docker config to `/data`. Run:

```sh
docker compose config --quiet
docker compose build
docker compose run --rm gitmirror --config /config/config.yml doctor
docker compose up -d
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

GitMirror rechecks GitHub repository identity, visibility and default branch before
each outgoing Git or LFS transfer and before publishing a destination. If the check
disagrees with discovery or fails, it stops that repository for the current cycle.
New destinations start private and can be published after Git and LFS verification.
The provider reads and writes are separate requests, so a change after the final
check is still possible.

When the default branch changes, GitMirror archives the existing destination and
pushes the replacement branch first. It confirms the new default through the
destination API and Git's advertised `HEAD` before the push that can remove the former
branch. The former branch stays when it still exists on GitHub. If the default
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

### Restore a destination snapshot

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

The local archive is not a complete independent backup. Keep a second copy on another device. Test restore procedures before relying on it. Do not publish secrets in configuration or logs.

### Platform and production comparison

| Environment | Validation |
| --- | --- |
| Windows x64, Python 3.13 | Full local suite with Git for Windows and Git LFS. |
| Debian 12 amd64, Python 3.11 | Full local suite and an authenticated Git read against a local OpenSSH server with mocked provider APIs. |
| Docker amd64, Python 3.12 | Built the image, ran the full suite as UID 10001, and exercised Compose configuration, mounts and the offline doctor. |

The local suite covers discovery, rewrites, branch and tag deletion, visibility checks,
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
