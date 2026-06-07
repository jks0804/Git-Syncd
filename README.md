# git_sync

A small, dependency-free Python script that syncs one git repository to another. Useful for mirroring repos between hosts (GitHub ↔ GitLab ↔ self-hosted), keeping backup copies, or driving any workflow where commits in repo A should land in repo B.

It supports syncing all branches, a hand-picked subset, or a full mirror (every ref). It can keep a persistent local cache for fast incremental runs, or operate transiently with no local footprint. A TOML/JSON config file lets you define multiple sync jobs and run them on a new system with one command.

## Features

- **Three sync scopes**: all branches, named branches, or full mirror (every ref including tags and notes).
- **Two storage modes**: persistent local cache (fast on re-runs, ideal for cron) or transient temp directory (zero local footprint).
- **Multi-job config files** in TOML or JSON, with shared defaults and per-job overrides.
- **Optional web UI** for interactive management of the config file (single-user, password-protected; see [Web UI](#web-ui)).
- **Dry-run mode** that prints exactly what `git push` would do without making changes.
- **Tag handling**: pushed by default, opt out with `--no-tags`, always included with `--mirror`.
- **No third-party dependencies** — uses only the Python standard library and the `git` CLI you already have installed.
- **Flexible authentication** — works with whatever your local `git` already has (SSH agent, credential helper, deploy keys), or pass an HTTPS token via the `token` fields and the script hands it to git through a temporary per-host credential helper (never in the URL, command line, or logs).

## Requirements

- Python 3.9 or newer (3.11+ recommended for built-in TOML support; older versions can install `tomli` if you want TOML configs, or just use JSON).
- `git` available on `PATH`.
- Network reach and credentials for both source and destination remotes.

## Installation

The script is a single file. Drop it anywhere on your `PATH`:

```bash
curl -o ~/bin/git_sync.py https://example.invalid/path/to/git_sync.py
chmod +x ~/bin/git_sync.py
```

Or clone this repo and symlink it:

```bash
git clone <this-repo> ~/src/git-sync
ln -s ~/src/git-sync/git_sync.py ~/bin/git_sync.py
```

Verify the install:

```bash
git_sync.py --help
```

## Quick Start

### One-shot sync (no config file)

Mirror every branch and tag from one remote to another, keeping a local cache for fast subsequent runs:

```bash
git_sync.py \
  --source git@github.com:me/myrepo.git \
  --dest   git@gitlab.com:me/myrepo.git \
  --local  ~/.cache/git-sync/myrepo \
  --all-branches
```

### Config-driven sync

Generate a starter config, edit it, then run all jobs:

```bash
git_sync.py --print-example-config > sync.toml
$EDITOR sync.toml
git_sync.py --config sync.toml
```

## Usage

The script has two ways of telling it what to sync: command-line flags (one-shot mode) or a config file (multi-job mode). They are mutually exclusive — you cannot mix `--config` with target-defining flags like `--source` or `--branches`.

### One-shot CLI mode

Provide source, destination, where to do the work, and what to sync.

| Flag | Description |
| --- | --- |
| `--source URL` | Source repo URL or path. Required. |
| `--dest URL` | Destination repo URL or path. Required. |
| `--local PATH` | Use a persistent bare mirror at `PATH`. Re-runs fetch only deltas. |
| `--transient` | Clone into a temp directory that is deleted afterwards. |
| `--all-branches` | Sync every branch from source. |
| `--branches a,b,c` | Sync only the listed branches. |
| `--mirror` | Full mirror push: every ref (branches, tags, notes). **Destructive on destination** — refs that exist there but not in source are deleted. |
| `--ssh-key PATH` | Path to an SSH private key. Sets `GIT_SSH_COMMAND` for this run. |
| `--no-tags` | Skip tag push. Ignored when `--mirror` is set. |
| `--force` | Force-push to destination. |
| `--dry-run` | Show what `git push` would do without making changes. |
| `-v`, `--verbose` | Verbose logging including each `git` command. |

You must specify exactly one of `--all-branches`, `--branches`, or `--mirror`, and exactly one of `--local` or `--transient`.

#### Examples

Two specific branches, no tags, persistent cache:

```bash
git_sync.py --source <src> --dest <dst> \
            --local ./cache --branches main,release --no-tags
```

Full mirror, no local footprint:

```bash
git_sync.py --source <src> --dest <dst> --transient --mirror
```

Preview a force-push:

```bash
git_sync.py --source <src> --dest <dst> \
            --local ./cache --all-branches --force --dry-run
```

### Config file mode

Use `--config FILE` to load one or more jobs from a TOML or JSON file. Format is chosen by extension (`.toml` or `.json`).

| Flag | Description |
| --- | --- |
| `--config FILE` | Path to a `.toml` or `.json` config file. |
| `--job NAME` | Run only this job. Repeatable. Default: run all jobs in file order. |
| `--print-example-config` | Print a sample TOML config to stdout and exit. |

Behavioral flags still apply in config mode and override the corresponding setting in every job:

- `--force` — force-push for all jobs in this run.
- `--dry-run` — preview every job without pushing.
- `--no-tags` — skip tags for every job.
- `-v` / `--verbose` — verbose logging.

When `--config` is used, target-defining flags (`--source`, `--dest`, `--local`, `--transient`, `--all-branches`, `--branches`, `--mirror`) are rejected. Set those in the config file.

### Config file format

A config has an optional `[defaults]` table whose keys are merged into every job, and one `[jobs.<name>]` table per sync job.

#### TOML example

```toml
# Defaults applied to every job (each job can override).
[defaults]
tags = true

# Sync all branches and tags from GitHub to GitLab, persistent cache.
[jobs.myproject]
source       = "git@github.com:me/myrepo.git"
dest         = "git@gitlab.com:me/myrepo.git"
local        = "~/.cache/git-sync/myrepo"
all_branches = true

# Sync only main and release. Transient (no local cache). No tags.
[jobs.docs]
source    = "git@github.com:me/docs.git"
dest      = "git@gitlab.com:me/docs.git"
transient = true
branches  = ["main", "release"]
tags      = false

# Full backup mirror. Destructive on destination.
[jobs.backup]
source = "git@github.com:me/big-repo.git"
dest   = "git@backup.example.com:me/big-repo.git"
local  = "~/.cache/git-sync/big-repo"
mirror = true
force  = true
```

#### JSON equivalent

```json
{
  "defaults": { "tags": true },
  "jobs": {
    "myproject": {
      "source": "git@github.com:me/myrepo.git",
      "dest":   "git@gitlab.com:me/myrepo.git",
      "local":  "~/.cache/git-sync/myrepo",
      "all_branches": true
    },
    "docs": {
      "source": "git@github.com:me/docs.git",
      "dest":   "git@gitlab.com:me/docs.git",
      "transient": true,
      "branches": ["main", "release"],
      "tags": false
    }
  }
}
```

### Configuration keys

Valid in both `[defaults]` and inside any `[jobs.<name>]` block:

| Key | Type | Description |
| --- | --- | --- |
| `source` | string | Source repo URL or path. **Required** per job. Supports `${VAR}` env-var substitution. |
| `dest` | string | Destination repo URL or path. **Required** per job. Supports `${VAR}` env-var substitution. |
| `local` | string | Persistent local mirror path. `~` is expanded. Mutually exclusive with `transient`. Supports `${VAR}`. |
| `transient` | bool | Use a temp directory deleted after the sync. Mutually exclusive with `local`. |
| `all_branches` | bool | Sync every branch from source. |
| `branches` | list&lt;string&gt; | Sync only the listed branch names. |
| `mirror` | bool | Full mirror push (all refs). **Destructive on destination.** |
| `tags` | bool | Push tags (default `true`). Ignored when `mirror = true`. |
| `force` | bool | Force-push (default `false`). |
| `ssh_key` | string | Path to an SSH private key to use for this job. Sets `GIT_SSH_COMMAND` for the job's git invocations. Supports `${VAR}`. |
| `token` | string | HTTPS access token, applied to both source and destination. Literal or `${VAR}`. Fed to git via a temporary per-host credential helper — never enters the remote URL, command line, or logs. |
| `source_token` | string | Token for the source remote only. Overrides `token`. |
| `dest_token` | string | Token for the destination remote only. Overrides `token`. |
| `token_user` | string | Username paired with the token (default `oauth2`). GitHub accepts any non-empty value; GitLab expects `oauth2`. |

Each job must specify exactly one of `all_branches`, `branches`, or `mirror`, and exactly one of `local` or `transient`. Unknown keys are rejected with an error listing the valid options, so typos surface immediately.

#### Environment variable substitution

Any string-valued field can include `${VAR}` references that are expanded from the process environment at run time. This is the recommended way to keep secrets — tokens, deploy-key paths — out of the config file:

```toml
[jobs.ci-mirror]
source       = "https://oauth2:${GITHUB_TOKEN}@github.com/me/repo.git"
dest         = "https://oauth2:${GITLAB_TOKEN}@gitlab.com/me/repo.git"
transient    = true
all_branches = true
```

If a referenced variable is unset, the script fails with a clear error before any git command runs. Plain `$VAR` (without braces) is **not** expanded — braces are required so URLs containing literal dollar signs aren't accidentally mangled.

## How it works

Internally, every sync uses the same approach: `git clone --mirror` to a bare repo (either the persistent `--local` path or a temp directory), then `git fetch --prune origin` to refresh refs, then `git push destination` with refspecs derived from the branch selector.

This has a few useful consequences:

- **Incremental re-runs are fast.** With `--local`, the second run only fetches new objects.
- **Branch selection is uniform.** Whether you picked one branch, all branches, or full mirror, the underlying repo always has every ref available — only the push refspec differs.
- **Working tree is never checked out.** The local cache is a bare repo, so it uses minimal disk space (no duplicate working files) and there is nothing to accidentally edit.

The destination remote is always added under the name `destination`, separate from `origin` (which points at the source). You can inspect the local cache with normal git commands:

```bash
git --git-dir=~/.cache/git-sync/myrepo remote -v
git --git-dir=~/.cache/git-sync/myrepo branch -a
```

## Authentication and unattended access

The script intentionally does not store credentials. It invokes the `git` binary, which has well-tested mechanisms for credential handling — duplicating them in Python would be less secure, not more. What this means in practice: pick the auth method that matches your transport (SSH or HTTPS), set it up once, and it works for both interactive and unattended runs.

Two small features bridge the gaps where git's mechanisms don't fit unattended workflows cleanly:

- **Environment-variable substitution** in `source`, `dest`, `local`, and `ssh_key` values — write `${VAR}` and the script expands it at run time. Tokens never need to live in the config file.
- **Per-job `ssh_key`** setting that points at a private key file. The script sets `GIT_SSH_COMMAND` for that job only, so different jobs can use different keys without `~/.ssh/config` gymnastics.

Both are optional. The recommended approach is still to use git's native auth wherever possible, and to fall back to these only when needed.

### SSH (recommended for unattended)

The cleanest setup for unattended access is an SSH key with no passphrase, dedicated to this purpose, registered as a deploy key on each remote. The key sits on the machine running the sync and is never transmitted anywhere.

Generate a key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/git_sync_key -N "" -C "git_sync"
```

Add the **public** half (`~/.ssh/git_sync_key.pub`) to each repo's deploy-key list. For the source repo, read-only is enough; for the destination, you need write access. On GitHub this is *Settings → Deploy keys → Add deploy key*; GitLab and Gitea have equivalent UIs. A token-issued via a service account works similarly.

Three ways to make git use this specific key:

**Option A — `~/.ssh/config` host alias** (no script support needed):

```
Host github-sync
  HostName github.com
  User git
  IdentityFile ~/.ssh/git_sync_key
  IdentitiesOnly yes
```

Then in the config file, refer to `git@github-sync:me/repo.git` instead of `git@github.com:me/repo.git`. Cleanest if you have several jobs sharing the same key.

**Option B — per-job `ssh_key` in the config**:

```toml
[jobs.private-mirror]
source       = "git@github.com:me/repo.git"
dest         = "git@gitea.local:me/repo.git"
local        = "~/.cache/git-sync/repo"
all_branches = true
ssh_key      = "~/.ssh/git_sync_key"
```

The script sets `GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes"` for that job and restores the prior environment afterwards. `IdentitiesOnly=yes` ensures the agent's other keys aren't tried (which can trigger `MaxAuthTries` failures when many keys are loaded).

**Option C — `--ssh-key` on the CLI** (one-shot mode):

```bash
git_sync.py --source <src> --dest <dst> \
            --local ./cache --all-branches \
            --ssh-key ~/.ssh/git_sync_key
```

#### If your key has a passphrase

Unattended access typically wants a passphraseless key. If policy requires passphrases, use one of:

- A user-level systemd service that loads the key into ssh-agent at boot via `ssh-add` driven by a passphrase pulled from the OS keychain (`gnome-keyring`, `keychain` on macOS, `pass`, etc.).
- `keychain` (the tool) which manages a long-lived agent across sessions: `eval $(keychain --eval ~/.ssh/git_sync_key)`.

### HTTPS with tokens

For HTTPS remotes, the standard approach is git's credential helper, configured once per machine. For unattended use, the helpers worth knowing about:

| Helper | Storage | Best for |
| --- | --- | --- |
| `store` | Plaintext in `~/.git-credentials` (chmod 600) | Simple unattended setups on trusted machines |
| `cache` | In-memory, expires (default 15 min) | Interactive sessions; not unattended |
| `osxkeychain` / `libsecret` / `manager` | OS-level secret store | Desktops with a logged-in user |
| custom (per-URL) | A script you write | Complex multi-host scenarios |

Set up `store` for unattended access:

```bash
git config --global credential.helper store
# Run any git operation interactively once to seed the file:
git ls-remote https://github.com/me/private.git
# Username: <your-username>
# Password: <personal-access-token>     ← token, not your account password
```

After that, `~/.git-credentials` holds the token in plaintext (mode 0600). Subsequent runs are non-interactive. For per-host credentials, use `~/.netrc` instead (also chmod 600):

```
machine github.com login me password ghp_xxxxxxxxxxxxxxxx
machine gitlab.com login me password glpat-xxxxxxxxxxxx
```

Git reads `~/.netrc` automatically over HTTPS.

#### First-class token fields (recommended)

The `token`, `source_token`, and `dest_token` job fields let you authenticate an HTTPS remote without a machine-wide credential helper and without embedding the secret in the URL:

```toml
[jobs.token-mirror]
source       = "https://github.com/me/repo.git"
dest         = "https://gitlab.com/me/repo.git"
transient    = true
all_branches = true
token        = "${GITHUB_TOKEN}"   # applies to both ends; or set per-end:
# source_token = "${GITHUB_TOKEN}"
# dest_token   = "${GITLAB_TOKEN}"
# token_user   = "oauth2"          # default; GitHub accepts any non-empty user
```

The value may be a literal token or a `${VAR}` reference. At sync time the script installs a temporary, per-host git credential helper that reads the secret from an environment variable on the child git process. As a result the token:

- **never appears in the remote URL** — the configured `source`/`dest` stay credential-free, including in the stored mirror config;
- **never appears in `git`'s command line** — so it can't be read from `ps`;
- **never appears in verbose (`-v`) logs** — only the helper installation is logged, not the secret.

The helper is scoped to the remote's host, so a source token and a destination token can target different providers in the same job. It's torn down (env vars restored) when the sync finishes. This is the preferred way to use tokens; the URL-embedding approach below remains supported for compatibility.

The same fields are available on the CLI: `--token`, `--source-token`, `--dest-token`, `--token-user`.

#### Token-in-URL via env var substitution (legacy)

CI systems typically inject tokens as environment variables. Reference them with `${VAR}` and they're expanded at sync time without ever appearing in your config file:

```toml
[jobs.ci-mirror]
source       = "https://oauth2:${GITHUB_TOKEN}@github.com/me/repo.git"
dest         = "https://oauth2:${GITLAB_TOKEN}@gitlab.com/me/repo.git"
transient    = true
all_branches = true
```

Run with the variables exported:

```bash
GITHUB_TOKEN=ghp_xxx GITLAB_TOKEN=glpat-xxx git_sync.py --config sync.toml
```

If a referenced variable is unset, the script fails before invoking git, with an error naming the missing variable and the field that referenced it — no confusing git auth errors, no garbled URL hitting the wire.

A few important caveats with tokens-in-URLs:

- **Process listings**: arguments to `git` are visible in `ps`. The script uses URLs as positional args, so a brief window of token visibility exists during the clone/fetch/push. On shared machines, prefer a credential helper. On dedicated machines or CI, this is usually fine.
- **Special characters**: if a token contains `@`, `:`, `/`, or `#`, URL-encode it before exporting. Most provider tokens are URL-safe by default but it's worth checking.
- **Logs**: with `-v`, `git` commands are logged at DEBUG level. The full URL — including any token — will land in those logs. Don't ship verbose logs anywhere you don't trust.

### Choosing an approach

| Scenario | Recommended |
| --- | --- |
| Single machine, several SSH remotes | SSH key + `~/.ssh/config` host aliases |
| Multiple keys, want config-as-source-of-truth | `ssh_key` per job |
| Cron / systemd timer, HTTPS remotes | `git credential.helper store` or `~/.netrc` |
| CI runner with tokens in env | `token` / `source_token` / `dest_token` fields with `${VAR}` |
| Want a token but no machine-wide helper | `token` field (per-host helper, nothing on disk) |
| Mixed SSH + HTTPS in one config | Combine: SSH keys for some jobs, env-var URLs for others |

The script's sole job is to call git with the right arguments. Keeping credential handling out of it means you can audit credential exposure with the same tools you'd use for any other git automation, and it composes cleanly with whatever secrets-management you already have.

## Recipes

### Periodic mirror via cron

Sync every 15 minutes, log to a file:

```cron
*/15 * * * * /usr/bin/env git_sync.py --config ~/sync.toml >> ~/.cache/git-sync.log 2>&1
```

Use a persistent `local` path in the config so each run only fetches deltas. With nothing to fetch, a no-op run typically completes in well under a second.

### Systemd timer

`~/.config/systemd/user/git-sync.service`:

```ini
[Unit]
Description=Mirror repositories

[Service]
Type=oneshot
ExecStart=%h/bin/git_sync.py --config %h/sync.toml
```

`~/.config/systemd/user/git-sync.timer`:

```ini
[Unit]
Description=Run git-sync every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable: `systemctl --user enable --now git-sync.timer`.

### Bootstrapping on a new machine

Commit `sync.toml` (without secrets — the file contains only repo URLs) to a personal dotfiles repo, then on a new system:

```bash
git clone <dotfiles> ~/dotfiles
ln -s ~/dotfiles/sync.toml ~/sync.toml
git_sync.py --config ~/sync.toml --dry-run    # sanity check
git_sync.py --config ~/sync.toml              # real run
```

### Multiple destinations from one source

Define one job per destination, all pointing at the same source. The persistent cache will be reused across them if you set the same `local` path:

```toml
[defaults]
source = "git@github.com:me/myrepo.git"
local  = "~/.cache/git-sync/myrepo"
all_branches = true

[jobs.gitlab]
dest = "git@gitlab.com:me/myrepo.git"

[jobs.codeberg]
dest = "git@codeberg.org:me/myrepo.git"

[jobs.self-hosted]
dest = "git@gitea.local:me/myrepo.git"
```

### CI mirror job (GitHub Actions)

```yaml
- uses: actions/checkout@v4
- name: Setup SSH
  uses: webfactory/ssh-agent@v0.9.0
  with:
    ssh-private-key: ${{ secrets.MIRROR_DEPLOY_KEY }}
- name: Mirror to backup
  run: |
    python3 git_sync.py \
      --source ${{ github.server_url }}/${{ github.repository }}.git \
      --dest   git@backup.example.com:me/repo.git \
      --transient --mirror --force
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (or all jobs succeeded). |
| `1` | Validation error or one or more config-mode jobs failed. |
| `2` | Argument-parsing error. |
| `130` | Interrupted (Ctrl+C). |
| other | Forwarded from the underlying `git` exit code. |

In config mode, individual job failures don't abort the run — every job is attempted, errors are logged per job, and the script exits non-zero at the end if any failed.

## Web UI

For interactive management, the project ships a small Flask-based web UI (`git_sync_web.py`) that reads and writes the same TOML config file the CLI uses. It exposes a dashboard listing every job with run/dry-run/edit/delete controls, a form for creating and editing jobs, and a defaults editor. Sync output is captured and displayed inline.

The UI is intended for **internal, single-user use** — there is no account creation, just one set of credentials configured via environment variables.

### Setup

```bash
pip install -r requirements.txt    # flask + tomli-w (+ tomli on Python <3.11)
python3 git_sync_web.py            # binds to http://127.0.0.1:5000
```

The default config file is `./sync.toml` (created on first save if missing). Override with `--config PATH` or the `GIT_SYNC_CONFIG` env var.

### CLI flags

| Flag | Description |
| --- | --- |
| `--config PATH` | TOML config file to read/write (default `./sync.toml`). |
| `--host HOST` | Bind interface (default `127.0.0.1`). |
| `--port PORT` | Port (default `5000`). |
| `--debug` | Flask debug mode. **Don't use in production** — enables an interactive debugger on errors. |

### Authentication

Credentials come from environment variables, with built-in defaults so the tool works on first run:

| Env var | Default | Purpose |
| --- | --- | --- |
| `GIT_SYNC_USER` | `stroh` | Web UI username. |
| `GIT_SYNC_PASSWORD` | `24763641E@` | Web UI password. |
| `FLASK_SECRET_KEY` | random per process | Session cookie signing key. |

The defaults are first-run convenience only. For any deployment, override at least the password:

```bash
export GIT_SYNC_USER=stroh
export GIT_SYNC_PASSWORD='something-better-than-the-default'
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python3 git_sync_web.py
```

Setting `FLASK_SECRET_KEY` to a stable value makes login persist across server restarts (otherwise sessions are invalidated and you re-login each restart, which is fine for a single-user tool).

The password is compared in constant time. CSRF tokens are checked on every state-changing request. There is no account creation, signup, or password reset path — to change the password, change the environment variable and restart the server.

### Deployment notes

- **Bind to localhost** (the default) and reach it through SSH port forwarding or a reverse proxy. Do not expose `git_sync_web.py` directly to the internet.
- For LAN access, run behind nginx/Caddy/Traefik with TLS — the app speaks plain HTTP and Flask's built-in server is for development.
- Run as a systemd service for unattended uptime:

  ```ini
  # ~/.config/systemd/user/git-sync-web.service
  [Unit]
  Description=git_sync web UI

  [Service]
  Type=simple
  WorkingDirectory=%h/src/git-sync
  Environment=GIT_SYNC_USER=stroh
  Environment=GIT_SYNC_PASSWORD=...
  Environment=FLASK_SECRET_KEY=...
  Environment=GIT_SYNC_CONFIG=%h/sync.toml
  ExecStart=/usr/bin/python3 git_sync_web.py --port 5000
  Restart=on-failure

  [Install]
  WantedBy=default.target
  ```

  Then `systemctl --user daemon-reload && systemctl --user enable --now git-sync-web`.

### What it does and doesn't preserve

The UI reads the TOML file fresh on every request and writes it back atomically (temp file + rename) on every change. This means external edits to the file are picked up immediately, and concurrent web edits won't corrupt the file. **However, comments and hand-formatted whitespace are NOT preserved on write** — `tomli-w` produces a canonical form. If you maintain comments in the config, edit it by hand and use the UI only for occasional changes, or accept that comments will be lost the first time the UI saves.

### What's not in the UI

- **No live log streaming.** Sync runs are synchronous: the page hangs until the sync finishes (with a 10-minute timeout) and then shows captured output. Fine for typical repos; for very large initial mirrors, run from the CLI instead.
- **No run history.** Each run is one-shot — output is shown, then forgotten when you navigate away. If you want history, run via cron and tail the log.
- **No multi-user support.** One credential, one session at a time is the design. Add OAuth / multi-user auth in front via a reverse proxy if you need it.



- **`--mirror` is destructive on the destination.** Refs that exist there but not in source are deleted. Use `--all-branches` if you want additive behavior.
- **Diverged history.** If the destination has commits the source doesn't, the push fails unless you pass `--force`. The script never silently force-pushes.
- **No partial-clone or shallow-clone support.** Mirror clones are full clones. For very large repos, the first run will fetch everything; subsequent runs are incremental.
- **No LFS handling.** Git LFS objects are not transferred by `git push --mirror` alone. If you depend on LFS, a separate `git lfs fetch --all` + `git lfs push --all destination` step is needed.
- **Single-direction.** This is a one-way mirror, not a two-way merge. Don't push to both ends and expect them to reconcile.
- **No credential management.** Configure your environment's credential helper or SSH setup before running.

## Troubleshooting

**"Branch(es) not found in source"**
The branch name doesn't exist on the source side. List what's available with `git --git-dir=<local-path> branch`. Note that the default branch on older git installs is `master`, not `main`.

**"refusing to overwrite"**
The `--local` path you gave exists and isn't a bare mirror clone. Pick an empty directory, an existing mirror path, or remove the offending directory.

**"Updates were rejected because the tip of your current branch is behind"**
The destination has commits the source doesn't. Either `--force` (overwrite destination) or investigate why they diverged.

**Authentication failures**
Run the equivalent `git` command manually first (`git ls-remote <url>`) to confirm your credentials work outside the script. The script doesn't add or strip auth — if a manual `git` works, the script should work too.

**"TOML config requires Python 3.11+"**
Either upgrade Python, install `tomli` (`pip install tomli`), or convert your config to JSON.

## License

MIT (or substitute your preferred license here).
