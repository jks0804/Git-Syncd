#!/usr/bin/env python3
"""
git_sync.py — Sync one git repository to another.

Modes:
  --local PATH    Keep a persistent bare mirror at PATH (faster on re-runs).
  --transient     Use a temp directory that is deleted when finished.

Branch selection (pick one):
  --all-branches          Sync every branch from source.
  --branches main,dev     Sync only the listed branches.
  --mirror                Full mirror push: every ref (branches, tags, notes,
                          remote-tracking) — destructive on destination.

Other useful flags:
  --no-tags     Skip pushing tags (ignored with --mirror, which always pushes everything).
  --force       Force-push to destination.
  --dry-run     Print what git would do without actually pushing.
  -v            Verbose logging.

Examples:
  # Persistent mirror, all branches + tags
  ./git_sync.py --source git@github.com:me/a.git \\
                --dest   git@github.com:me/b.git \\
                --local  ~/.cache/repo-sync/a --all-branches

  # Two specific branches
  ./git_sync.py --source <src> --dest <dst> --local ./cache --branches main,release

  # Transient one-shot full mirror
  ./git_sync.py --source <src> --dest <dst> --transient --mirror

  # Run all jobs defined in a config file
  ./git_sync.py --config sync.toml

  # Run only specific jobs from the config
  ./git_sync.py --config sync.toml --job myproject --job docs

  # Print a sample config to get started
  ./git_sync.py --print-example-config > sync.toml

Authentication:
  SSH remotes use your ssh-agent / ~/.ssh/config, or pass --ssh-key / set
  `ssh_key` in a job.
  HTTPS remotes can use your git credential helper, or pass an access token
  via --token / --source-token / --dest-token (or the matching job keys). The
  token is handed to git through a temporary per-host credential helper, so it
  never appears in the remote URL, the command line, or trace logs.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path


# ---------------------------------------------------------------------------
# Auth helpers — env var expansion + per-job SSH key
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str | None, *, field: str = "value") -> str | None:
    """Expand ${VAR} references from the process environment.

    Raises RuntimeError if any referenced variable is unset, so users
    get a clear error instead of a confusing git auth failure.
    Plain '$VAR' (no braces) is *not* expanded — braces are required so
    we don't accidentally mangle URLs containing literal dollar signs.
    """
    if not value:
        return value
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            missing.append(name)
            return match.group(0)
        return os.environ[name]

    result = _ENV_VAR_PATTERN.sub(replace, value)
    if missing:
        raise RuntimeError(
            f"Environment variable(s) referenced in {field} not set: "
            f"{', '.join(sorted(set(missing)))}"
        )
    return result


def _host_scope(url: str, *, field: str) -> tuple[str, str]:
    """Validate an http(s) URL for token auth; return (scheme, host)."""
    parsed = urllib.parse.urlsplit(url or "")
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(
            f"A token ({field}) is only usable with an http(s):// URL; "
            f"got scheme {parsed.scheme!r} for {url!r}. "
            "Use ssh_key for SSH remotes instead."
        )
    if not parsed.hostname:
        raise RuntimeError(f"Could not determine host from URL {url!r} ({field}).")
    return parsed.scheme, parsed.hostname


@contextlib.contextmanager
def token_credentials(creds: list[tuple[str, str, str]]):
    """Temporarily configure git to authenticate to HTTPS remotes with tokens.

    `creds` is a list of (url, token, token_user) tuples — typically one for the
    source and/or one for the destination. Each non-empty token installs a
    per-host credential helper for the duration of the block.

    Rather than embedding the token in the remote URL (which would leak it into
    `git` argv visible to `ps`, into GIT_TRACE logs, and into the stored remote
    config), the helper reads each secret from an env var we set on the child
    git process, so the token never appears in any command line.

    Helpers are scoped per host (credential.<scheme>://<host>.helper) so each
    only applies to its matching remote.
    """
    active = [(u, t, tu) for (u, t, tu) in creds if t]
    if not active:
        yield
        return

    # Track every env var we touch so we can restore exactly on exit.
    touched = {"GIT_CONFIG_COUNT", "GIT_TERMINAL_PROMPT"}
    config_entries: list[tuple[str, str]] = []  # (scope_key, helper_value)

    for i, (url, token, token_user) in enumerate(active):
        scheme, host = _host_scope(url, field=f"credential #{i}")
        user = token_user or "oauth2"
        user_var = f"GIT_SYNC_CRED_USER_{i}"
        tok_var = f"GIT_SYNC_CRED_TOKEN_{i}"
        touched.update({user_var, tok_var})
        # Leading '!' marks the value as a shell command. It emits git's
        # credential protocol fields, reading the secret from the child's env.
        helper = (
            "!f() { "
            f'echo "username=${user_var}"; '
            f'echo "password=${tok_var}"; '
            "}; f"
        )
        config_entries.append((f"credential.{scheme}://{host}.helper", helper))
        # Stash the secret values to set below (kept separate so we can restore).
        active[i] = (user_var, user, tok_var, token)  # type: ignore[assignment]

    saved = {k: os.environ.get(k) for k in touched}
    # Also save any GIT_CONFIG_KEY_n / VALUE_n we'll write.
    for n in range(len(config_entries)):
        for k in (f"GIT_CONFIG_KEY_{n}", f"GIT_CONFIG_VALUE_{n}"):
            saved[k] = os.environ.get(k)

    try:
        for entry in active:
            user_var, user, tok_var, token = entry  # type: ignore[misc]
            os.environ[user_var] = user
            os.environ[tok_var] = token
        os.environ["GIT_CONFIG_COUNT"] = str(len(config_entries))
        for n, (scope, helper) in enumerate(config_entries):
            os.environ[f"GIT_CONFIG_KEY_{n}"] = scope
            os.environ[f"GIT_CONFIG_VALUE_{n}"] = helper
        # Never fall back to an interactive prompt; fail fast instead of hanging.
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
        logging.debug("Token credential helper(s) installed for %d host(s).",
                      len(config_entries))
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def ssh_key_env(ssh_key: str | None):
    """Temporarily set GIT_SSH_COMMAND so git uses the given private key.

    Sets `IdentitiesOnly=yes` so the agent's other keys aren't tried
    (which can cause MaxAuthTries failures when many keys are loaded).
    Restores the previous value on exit.
    """
    if not ssh_key:
        yield
        return

    key_path = Path(ssh_key).expanduser()
    if not key_path.is_file():
        raise RuntimeError(f"SSH key file not found: {key_path}")

    cmd = f"ssh -i {shlex.quote(str(key_path))} -o IdentitiesOnly=yes"
    saved = os.environ.get("GIT_SSH_COMMAND")
    os.environ["GIT_SSH_COMMAND"] = cmd
    logging.debug("GIT_SSH_COMMAND set for this job: %s", cmd)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("GIT_SSH_COMMAND", None)
        else:
            os.environ["GIT_SSH_COMMAND"] = saved


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a git command, log it, raise on non-zero exit."""
    logging.debug("$ %s%s", " ".join(cmd), f"  (cwd={cwd})" if cwd else "")
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=capture,
    )


# ---------------------------------------------------------------------------
# Repo management — always a bare mirror clone under the hood
# ---------------------------------------------------------------------------

def is_bare_mirror(path: Path) -> bool:
    """Heuristic: bare repo has HEAD and config at top level (no .git/)."""
    return path.is_dir() and (path / "HEAD").is_file() and (path / "config").is_file()


def ensure_mirror(local_path: Path, source_url: str) -> None:
    """Create or refresh a bare mirror clone of source_url at local_path."""
    if is_bare_mirror(local_path):
        logging.info("Reusing existing mirror at %s", local_path)
        run(["git", "remote", "set-url", "origin", source_url], cwd=local_path)
        return

    if local_path.exists() and any(local_path.iterdir()):
        raise RuntimeError(
            f"{local_path} exists and is not an empty directory or mirror repo. "
            "Refusing to overwrite. Pick a different --local path."
        )

    local_path.mkdir(parents=True, exist_ok=True)
    logging.info("Cloning mirror from %s into %s", source_url, local_path)
    run(["git", "clone", "--mirror", source_url, str(local_path)])


def set_destination(local_path: Path, dest_url: str) -> None:
    """Make sure remote 'destination' points at dest_url."""
    existing = subprocess.run(
        ["git", "remote"], cwd=str(local_path),
        text=True, capture_output=True, check=True,
    ).stdout.split()

    if "destination" in existing:
        run(["git", "remote", "set-url", "destination", dest_url], cwd=local_path)
    else:
        run(["git", "remote", "add", "destination", dest_url], cwd=local_path)


def fetch_source(local_path: Path) -> None:
    """Fetch latest from origin (mirror fetch updates all refs and prunes)."""
    logging.info("Fetching from source...")
    run(["git", "fetch", "--prune", "origin"], cwd=local_path)


# ---------------------------------------------------------------------------
# Push strategies
# ---------------------------------------------------------------------------

def list_local_branches(local_path: Path) -> list[str]:
    """List branch names present in the bare mirror (refs/heads/*)."""
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=str(local_path), text=True, capture_output=True, check=True,
    )
    return [b.strip() for b in result.stdout.splitlines() if b.strip()]


def push(local_path: Path, *, branches: list[str] | None, mirror: bool,
         all_branches: bool, tags: bool, force: bool, dry_run: bool) -> None:
    """Push selected refs to the destination remote."""
    base = ["git", "push", "destination"]
    if dry_run:
        base.append("--dry-run")
    if force:
        base.append("--force")

    if mirror:
        logging.info("Pushing full mirror to destination...")
        run(base + ["--mirror"], cwd=local_path)
        return

    if all_branches:
        available = list_local_branches(local_path)
        if not available:
            logging.warning("No branches found in source; nothing to push.")
            return
        logging.info("Pushing %d branch(es): %s", len(available), ", ".join(available))
        refspecs = [f"refs/heads/{b}:refs/heads/{b}" for b in available]
        run(base + refspecs, cwd=local_path)
    elif branches:
        available = set(list_local_branches(local_path))
        missing = [b for b in branches if b not in available]
        if missing:
            raise RuntimeError(
                f"Branch(es) not found in source: {', '.join(missing)}. "
                f"Available: {', '.join(sorted(available))}"
            )
        logging.info("Pushing branch(es): %s", ", ".join(branches))
        refspecs = [f"refs/heads/{b}:refs/heads/{b}" for b in branches]
        run(base + refspecs, cwd=local_path)

    if tags:
        logging.info("Pushing tags...")
        tag_cmd = ["git", "push", "destination", "--tags"]
        if dry_run:
            tag_cmd.append("--dry-run")
        if force:
            tag_cmd.append("--force")
        run(tag_cmd, cwd=local_path)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def sync(
    source: str,
    dest: str,
    *,
    local: str | None = None,
    transient: bool = False,
    branches: list[str] | None = None,
    all_branches: bool = False,
    mirror: bool = False,
    tags: bool = True,
    force: bool = False,
    dry_run: bool = False,
    ssh_key: str | None = None,
    source_token: str | None = None,
    dest_token: str | None = None,
    token_user: str | None = None,
) -> None:
    cleanup: Path | None = None
    try:
        if transient:
            cleanup = Path(tempfile.mkdtemp(prefix="git-sync-"))
            local_path = cleanup
            logging.info("Using transient working dir: %s", local_path)
        else:
            assert local, "local path required when not transient"
            local_path = Path(local).expanduser().resolve()

        token_creds = [
            (source, source_token, token_user),
            (dest, dest_token, token_user),
        ]
        with ssh_key_env(ssh_key), token_credentials(token_creds):
            ensure_mirror(local_path, source)
            set_destination(local_path, dest)
            fetch_source(local_path)
            push(
                local_path,
                branches=branches,
                mirror=mirror,
                all_branches=all_branches,
                tags=tags and not mirror,  # --mirror already covers tags
                force=force,
                dry_run=dry_run,
            )
        logging.info("Sync complete.%s", " (dry-run)" if dry_run else "")
    finally:
        if cleanup is not None and cleanup.exists():
            logging.info("Removing transient dir %s", cleanup)
            shutil.rmtree(cleanup, ignore_errors=True)


# ---------------------------------------------------------------------------
# Config file support (TOML or JSON)
# ---------------------------------------------------------------------------

EXAMPLE_CONFIG = '''\
# git_sync config file (TOML)
#
# [defaults] applies to every job; each [jobs.<name>] block can override.
# Run all jobs:    git_sync.py --config this-file.toml
# Run one job:     git_sync.py --config this-file.toml --job myproject
#
# Authentication:
#   SSH URLs use your ssh-agent / ~/.ssh/config / the per-job `ssh_key` setting.
#   HTTPS URLs use git's credential helper (or embed a token via ${ENV_VAR}
#   substitution — see the 'mirror-via-token' job below for the pattern).
#   Tokens never need to live in this file; reference them as ${VAR} and
#   export them in the environment that runs git_sync.

[defaults]
# tags = true        # push tags (ignored when mirror = true)
# force = false
# Optional: any key valid in a job block can be set here as a default.

[jobs.myproject]
source       = "git@github.com:me/a.git"
dest         = "git@github.com:me/b.git"
local        = "~/.cache/git-sync/myproject"
all_branches = true

[jobs.docs]
source    = "git@github.com:me/docs.git"
dest      = "git@gitlab.com:me/docs.git"
transient = true
branches  = ["main", "release"]
tags      = false

# Use a specific SSH key for this job (does not affect other jobs).
[jobs.private-mirror]
source       = "git@github.com:me/private.git"
dest         = "git@gitea.local:me/private.git"
local        = "~/.cache/git-sync/private"
all_branches = true
ssh_key      = "~/.ssh/git_sync_key"

# HTTPS with access tokens, supplied as first-class fields. The token is fed
# to git through a per-host credential helper, so it never lands in the remote
# URL, the git command line (visible via `ps`), or trace logs.
#
#   token        applies to BOTH source and dest
#   source_token / dest_token  override per-end
#   token_user   the username paired with the token (default: oauth2; GitHub
#                accepts any non-empty username, GitLab wants oauth2)
#
# Tokens may be written literally (as below) or referenced as ${ENV_VAR} to
# keep them out of this file. Plain HTTPS URLs — no embedded credentials.
[jobs.mirror-via-token]
source       = "https://github.com/me/repo.git"
dest         = "https://gitlab.com/me/repo.git"
transient    = true
all_branches = true
source_token = "ghp_xxxxxxxxxxxxxxxxxxxx"
dest_token   = "glpat-xxxxxxxxxxxxxxxxxxxx"
token_user   = "oauth2"

[jobs.full-backup]
source = "git@github.com:me/big-repo.git"
dest   = "git@backup.example.com:me/big-repo.git"
local  = "~/.cache/git-sync/big-repo"
mirror = true        # WARNING: destructive on destination
force  = true
'''


# Keys recognised inside a [jobs.<name>] block (and in [defaults]).
_VALID_JOB_KEYS = {
    "source", "dest", "local", "transient",
    "all_branches", "branches", "mirror",
    "tags", "force", "ssh_key",
    "token", "source_token", "dest_token", "token_user",
}


def load_config(path: Path) -> dict:
    """Load a config file. Format chosen by extension: .toml or .json."""
    if not path.is_file():
        raise RuntimeError(f"Config file not found: {path}")
    text = path.read_text()
    suffix = path.suffix.lower()

    if suffix == ".toml":
        try:
            import tomllib  # Python 3.11+
        except ImportError:  # pragma: no cover
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError as exc:
                raise RuntimeError(
                    "TOML config requires Python 3.11+ or `pip install tomli`. "
                    "Alternatively, use a .json config file."
                ) from exc
        return tomllib.loads(text)

    if suffix == ".json":
        import json
        return json.loads(text)

    raise RuntimeError(
        f"Unsupported config extension '{suffix}'. Use .toml or .json."
    )


def expand_jobs(config: dict, only: list[str] | None) -> list[tuple[str, dict]]:
    """Merge defaults into each selected job. Returns [(name, params), ...]."""
    defaults = config.get("defaults", {}) or {}
    jobs = config.get("jobs", {}) or {}

    if not isinstance(jobs, dict) or not jobs:
        raise RuntimeError("Config has no [jobs.<name>] entries.")

    if only:
        missing = [n for n in only if n not in jobs]
        if missing:
            raise RuntimeError(
                f"Job(s) not in config: {', '.join(missing)}. "
                f"Available: {', '.join(sorted(jobs))}"
            )
        names = list(only)
    else:
        names = list(jobs.keys())

    expanded: list[tuple[str, dict]] = []
    for name in names:
        block = jobs[name] or {}
        if not isinstance(block, dict):
            raise RuntimeError(f"Job '{name}' must be a table/object, got {type(block).__name__}.")
        merged = {**defaults, **block}
        # Validate keys early so typos surface fast.
        unknown = set(merged) - _VALID_JOB_KEYS
        if unknown:
            raise RuntimeError(
                f"Job '{name}' has unknown key(s): {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(_VALID_JOB_KEYS))}"
            )
        expanded.append((name, merged))
    return expanded


def run_job(name: str, params: dict, *, force_override: bool, dry_run: bool, no_tags_override: bool) -> None:
    """Validate and run a single job from a config file."""
    # Required fields
    for required in ("source", "dest"):
        if not params.get(required):
            raise RuntimeError(f"Job '{name}' is missing required field '{required}'.")

    # Expand ${VAR} env-var references in string fields. Tokens never need to
    # appear in the config file — keep them in env vars and reference here.
    source = expand_env(params["source"], field=f"jobs.{name}.source")
    dest = expand_env(params["dest"], field=f"jobs.{name}.dest")
    local = expand_env(params.get("local"), field=f"jobs.{name}.local")
    ssh_key = expand_env(params.get("ssh_key"), field=f"jobs.{name}.ssh_key")

    # `token` is shorthand applying to both ends; source_token/dest_token
    # override it per-end. Tokens may be literal or ${ENV_VAR} references.
    shared_token = expand_env(params.get("token"), field=f"jobs.{name}.token")
    source_token = expand_env(params.get("source_token"), field=f"jobs.{name}.source_token") or shared_token
    dest_token = expand_env(params.get("dest_token"), field=f"jobs.{name}.dest_token") or shared_token
    token_user = expand_env(params.get("token_user"), field=f"jobs.{name}.token_user")

    transient = bool(params.get("transient", False))
    if transient and local:
        raise RuntimeError(f"Job '{name}': 'transient' and 'local' are mutually exclusive.")
    if not transient and not local:
        raise RuntimeError(f"Job '{name}': must set either 'local' or 'transient = true'.")

    selectors = [bool(params.get("all_branches")), bool(params.get("branches")), bool(params.get("mirror"))]
    if sum(selectors) != 1:
        raise RuntimeError(
            f"Job '{name}': must specify exactly one of 'all_branches', 'branches', or 'mirror'."
        )

    branches = params.get("branches")
    if branches is not None and (
        not isinstance(branches, list) or not all(isinstance(b, str) for b in branches)
    ):
        raise RuntimeError(f"Job '{name}': 'branches' must be a list of strings.")

    sync(
        source=source,
        dest=dest,
        local=local,
        transient=transient,
        branches=branches,
        all_branches=bool(params.get("all_branches")),
        mirror=bool(params.get("mirror")),
        tags=False if no_tags_override else bool(params.get("tags", True)),
        force=force_override or bool(params.get("force", False)),
        dry_run=dry_run,
        ssh_key=ssh_key,
        source_token=source_token,
        dest_token=dest_token,
        token_user=token_user,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync a git repo from one remote to another.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if "Examples:" in __doc__ else "",
    )

    # Config-file mode
    p.add_argument("--config", help="Path to a .toml or .json config file.")
    p.add_argument("--job", action="append", default=[],
                   help="Run only this job from the config (repeatable). Default: run all jobs.")
    p.add_argument("--print-example-config", action="store_true",
                   help="Print a sample TOML config to stdout and exit.")

    # CLI (one-shot) mode
    p.add_argument("--source", help="Source repo URL or path.")
    p.add_argument("--dest", help="Destination repo URL or path.")
    p.add_argument("--local", help="Persistent local mirror path.")
    p.add_argument("--transient", action="store_true",
                   help="Use a temp dir that is deleted after the sync.")
    p.add_argument("--all-branches", action="store_true",
                   help="Sync every branch from source.")
    p.add_argument("--branches", help="Comma-separated branch list to sync.")
    p.add_argument("--mirror", action="store_true",
                   help="Full mirror push (all refs). Destructive on destination.")
    p.add_argument("--ssh-key", help="Path to SSH private key to use for this sync.")
    p.add_argument("--token", help="Access token for HTTPS auth on both source and dest. "
                                   "Accepts ${ENV_VAR}. Use --token-user to set the username.")
    p.add_argument("--source-token", help="Token for the source remote only (overrides --token).")
    p.add_argument("--dest-token", help="Token for the destination remote only (overrides --token).")
    p.add_argument("--token-user", help="Username paired with the token (default: oauth2).")

    # Behavioural flags (apply in both modes; in config mode they override jobs)
    p.add_argument("--no-tags", action="store_true",
                   help="Don't push tags (ignored with --mirror). Overrides config.")
    p.add_argument("--force", action="store_true",
                   help="Force-push. In config mode, applies to all jobs.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be pushed; no changes made.")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)

    if args.print_example_config:
        return args  # Handled in main, no further validation needed.

    using_config = bool(args.config)
    cli_target_args = [args.source, args.dest, args.local, args.transient,
                       args.all_branches, args.branches, args.mirror]

    if using_config:
        # In config mode, target-defining CLI args shouldn't be mixed in.
        offending = []
        if args.source: offending.append("--source")
        if args.dest: offending.append("--dest")
        if args.local: offending.append("--local")
        if args.transient: offending.append("--transient")
        if args.all_branches: offending.append("--all-branches")
        if args.branches: offending.append("--branches")
        if args.mirror: offending.append("--mirror")
        if args.ssh_key: offending.append("--ssh-key")
        if args.token: offending.append("--token")
        if args.source_token: offending.append("--source-token")
        if args.dest_token: offending.append("--dest-token")
        if args.token_user: offending.append("--token-user")
        if offending:
            p.error(
                "When using --config, do not also pass " + ", ".join(offending)
                + ". Set those in the config file instead. "
                  "(--force, --dry-run, --no-tags, --job, -v are still allowed.)"
            )
    else:
        if args.job:
            p.error("--job only makes sense with --config.")
        # CLI mode: enforce the same requirements as before.
        if not args.source or not args.dest:
            p.error("--source and --dest are required (or use --config).")
        if not (args.local or args.transient):
            p.error("Either --local PATH or --transient is required.")
        if args.local and args.transient:
            p.error("--local and --transient are mutually exclusive.")
        n_selectors = sum([args.all_branches, bool(args.branches), args.mirror])
        if n_selectors == 0:
            p.error("Specify one of: --all-branches, --branches, or --mirror.")
        if n_selectors > 1:
            p.error("--all-branches, --branches, and --mirror are mutually exclusive.")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.print_example_config:
        sys.stdout.write(EXAMPLE_CONFIG)
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.config:
            cfg_path = Path(args.config).expanduser()
            config = load_config(cfg_path)
            jobs = expand_jobs(config, args.job or None)
            logging.info("Loaded %d job(s) from %s", len(jobs), cfg_path)
            failures = 0
            for name, params in jobs:
                logging.info("=== Job: %s ===", name)
                try:
                    run_job(
                        name, params,
                        force_override=args.force,
                        dry_run=args.dry_run,
                        no_tags_override=args.no_tags,
                    )
                except (subprocess.CalledProcessError, RuntimeError) as e:
                    failures += 1
                    logging.error("Job '%s' failed: %s", name, e)
            if failures:
                logging.error("%d job(s) failed.", failures)
                return 1
            return 0

        # CLI mode (no config file)
        branches = None
        if args.branches:
            branches = [b.strip() for b in args.branches.split(",") if b.strip()]
            if not branches:
                logging.error("--branches was empty.")
                return 2

        sync(
            source=expand_env(args.source, field="--source"),
            dest=expand_env(args.dest, field="--dest"),
            local=expand_env(args.local, field="--local"),
            transient=args.transient,
            branches=branches,
            all_branches=args.all_branches,
            mirror=args.mirror,
            tags=not args.no_tags,
            force=args.force,
            dry_run=args.dry_run,
            ssh_key=expand_env(args.ssh_key, field="--ssh-key"),
            source_token=(expand_env(args.source_token, field="--source-token")
                          or expand_env(args.token, field="--token")),
            dest_token=(expand_env(args.dest_token, field="--dest-token")
                        or expand_env(args.token, field="--token")),
            token_user=expand_env(args.token_user, field="--token-user"),
        )
    except subprocess.CalledProcessError as e:
        logging.error("git failed (exit %d): %s", e.returncode, " ".join(e.cmd))
        return e.returncode
    except RuntimeError as e:
        logging.error("%s", e)
        return 1
    except KeyboardInterrupt:
        logging.warning("Interrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
