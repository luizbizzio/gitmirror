# GitMirror

GitMirror copies Git branches, tags, Git LFS objects, and repository visibility from GitHub to GitLab and Codeberg. It keeps old Git references on the local machine before changing destination history.

**Status:** early refactor of a working Linux script. Linux dry-run and local tests pass. Windows and Docker need real service tests before production use.

## What it does

- Uses SSH for Git and APIs for repository settings.
- Supports public and private repositories, forks, and archived repositories.
- Can create missing destination projects.
- Keeps destination projects if the source repository disappears.
- Backs up old Git refs locally before force push and branch deletion.
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

For regular sync every 12 hours:

```sh
gitmirror --config config.yml run --apply
```

`run` starts a sync at once, then runs again after the configured number of hours. For Linux, an external cron job is also possible. Do not run the internal scheduler and cron for the same data directory at the same time.

## Docker

Copy `docker/config.example.yml` to `config.yml` (it uses `/data`), then prepare `.env` and an SSH directory at `docker/ssh` with verified `known_hosts` and credentials the container user can read. Change `storage.path` in Docker config to `/data`. Run:

```sh
docker compose up -d --build
```

The Docker Compose example is a starting point. Do not use it before testing SSH file permissions and API access in your own environment.

## Safety

Before overwriting a destination, GitMirror verifies the archived ref names and object
hashes and preserves the Git LFS objects referenced by that history. During an applied
sync, GitMirror downloads the destination's required LFS objects into temporary storage
to verify their contents, including when Git refs already match. Plan for that transfer
and temporary disk usage on repositories with large LFS histories.

The local archive is not a complete independent backup. Keep a second copy on another device. Test restore procedures before relying on it. Do not publish secrets in configuration or logs.

## Roadmap

1. Releases, release assets, descriptions, topics, and homepage.
2. Portable archive for issues, pull requests, reviews and discussions.
3. Verified restore tools and independent backup targets.
4. Tested Windows and Docker support.

License: Apache-2.0.
