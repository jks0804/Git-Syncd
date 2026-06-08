#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 stroh
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the GNU
# General Public License (LICENSE file) for more details.
"""
git_sync.py — Sync one git repository to another.

Modes:
  --local PATH    Keep a persistent bare mirror at PATH (faster on re-runs).
  (neither flag)  Persistent mirror at an auto-generated, stable cache dir
                  under the system temp dir (reused across runs per source).
  --transient     Use a temp directory that is deleted when finished.

Branch selection (pick one):
  --all-branches          Sync every branch from source.
  --branches main,dev     Sync only the listed branches. Each entry may rename
                          the branch on the destination with a colon:
                          --branches main:master,dev   (source:destination).
  --mirror                Full mirror push: every ref (branches, tags, notes,
                          remote-tracking) — destructive on destination.

Other useful flags:
  --no-tags     Skip pushing tags (ignored with --mirror, which always pushes everything).
  --force       Force-push to destination.
  --dry-run     Print what git would do without actually pushing.
  --source-hash / --dest-hash  sha1|sha256. When they differ, history is
                bridged (fast-export|fast-import) into the destination's hash —
                commit IDs are re-created and signatures stripped.
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
import hashlib
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


def has_local_tags(local_path: Path) -> bool:
    """True if the bare mirror has any tags (refs/tags/*).

    Used to skip the tags push when there's nothing to push: `git push --tags`
    against a remote with no refs in common errors with "No refs in common and
    none specified; doing nothing" instead of being a harmless no-op, which
    otherwise makes a dry-run to a fresh destination fail spuriously.
    """
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/tags/"],
        cwd=str(local_path), text=True, capture_output=True, check=True,
    )
    return bool(result.stdout.strip())


def parse_branch_mapping(entries: list[str]) -> list[tuple[str, str]]:
    """Turn branch entries into (source, destination) name pairs.

    Each entry maps a source branch to a destination branch:
      "main"          -> ("main", "main")        same name on both ends
      "main:master"   -> ("main", "master")      rename while syncing
      "main:"         -> ("main", "main")         trailing colon: default dest
      ":master"       -> ("master", "master")     leading colon: default source

    When only one side is given, the other defaults to it — so you only need
    the colon form when the names genuinely differ. At most one ':' is allowed.
    Empty entries are skipped; an entry with no usable name is an error.
    """
    mapping: list[tuple[str, str]] = []
    for raw in entries:
        spec = raw.strip()
        if not spec:
            continue
        if spec.count(":") > 1:
            raise RuntimeError(
                f"Invalid branch spec {raw!r}: use 'source:destination' "
                "with at most one colon."
            )
        if ":" in spec:
            src, _, dst = spec.partition(":")
            src, dst = src.strip(), dst.strip()
            if not src and not dst:
                raise RuntimeError(f"Invalid branch spec {raw!r}: no branch name given.")
            # Default the missing side to the side that was typed.
            src = src or dst
            dst = dst or src
        else:
            src = dst = spec
        mapping.append((src, dst))
    if not mapping:
        raise RuntimeError("No branch names given.")
    return mapping


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
        mapping = parse_branch_mapping(branches)
        available = set(list_local_branches(local_path))
        missing = [src for src, _ in mapping if src not in available]
        if missing:
            raise RuntimeError(
                f"Branch(es) not found in source: {', '.join(missing)}. "
                f"Available: {', '.join(sorted(available))}"
            )
        pretty = ", ".join(f"{src} -> {dst}" if src != dst else src
                           for src, dst in mapping)
        logging.info("Pushing branch(es): %s", pretty)
        refspecs = [f"refs/heads/{src}:refs/heads/{dst}" for src, dst in mapping]
        run(base + refspecs, cwd=local_path)

    if tags:
        if not has_local_tags(local_path):
            logging.info("No tags in source; skipping tag push.")
        else:
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

# Where auto-generated persistent caches live when a job picks local storage
# but doesn't specify a path.
DEFAULT_CACHE_ROOT = Path(tempfile.gettempdir()) / "git-sync-cache"


def default_cache_path(source: str) -> Path:
    """Derive a stable per-source cache directory under the system temp dir.

    Used as the fallback when local (persistent) storage is selected but no
    path is given. The path is deterministic for a given source URL, so
    re-runs reuse the same mirror and fetch only deltas. A short hash of the
    source keeps distinct sources from colliding even if their readable
    slugs match.
    """
    # Human-readable slug from the last path segment of the source.
    tail = source.rstrip("/").rsplit("/", 1)[-1]
    tail = tail[:-4] if tail.endswith(".git") else tail
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip("-") or "repo"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    return DEFAULT_CACHE_ROOT / f"{slug}-{digest}"


# ---------------------------------------------------------------------------
# Cross-hash bridging (SHA-1 <-> SHA-256)
# ---------------------------------------------------------------------------
#
# git cannot push/fetch between repositories that use different object hash
# algorithms ("the receiving end does not support this repository's hash
# algorithm"). To sync e.g. a SHA-256 Gitea repo to a SHA-1 GitHub repo, we
# re-stream history through `git fast-export | git fast-import` into an
# intermediate repo of the destination's hash, then push that. This RE-CREATES
# history in the new hash: commit IDs change and signatures are stripped, but
# trees, blobs, messages, branches and tags are preserved. Marks files make
# re-syncs incremental.

VALID_HASHES = {"sha1", "sha256"}


def repo_object_format(repo_path: Path) -> str:
    """Return a repo's object hash algorithm ('sha1' or 'sha256')."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-object-format"],
        cwd=str(repo_path), text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _has_any_refs(repo_path: Path) -> bool:
    result = subprocess.run(
        ["git", "for-each-ref", "--count=1"],
        cwd=str(repo_path), text=True, capture_output=True, check=True,
    )
    return bool(result.stdout.strip())


def ensure_bridge_repo(bridge_path: Path, object_format: str) -> None:
    """Create (if absent) a bare repo of object_format to fast-import into."""
    if is_bare_mirror(bridge_path):
        return
    if bridge_path.exists() and any(bridge_path.iterdir()):
        raise RuntimeError(
            f"{bridge_path} exists and is not a bridge repo. "
            "Refusing to overwrite."
        )
    bridge_path.mkdir(parents=True, exist_ok=True)
    logging.info("Creating %s bridge repo at %s", object_format, bridge_path)
    run(["git", "init", "--bare", f"--object-format={object_format}", str(bridge_path)])


def bridge_history(mirror_path: Path, bridge_path: Path,
                   marks_src: Path, marks_dst: Path, *, incremental: bool) -> None:
    """Re-stream all history from mirror_path into bridge_path (different hash).

    Runs `git fast-export | git fast-import`. With incremental=True, both ends
    resume from their marks files so only new commits are processed; otherwise
    a full re-export is done (still correct — fast-import is deterministic, so
    re-created commit IDs match — just slower).
    """
    export_cmd = ["git", "-C", str(mirror_path), "fast-export", "--all",
                  "--signed-tags=strip", "--tag-of-filtered-object=rewrite"]
    import_cmd = ["git", "-C", str(bridge_path), "fast-import", "--quiet", "--force"]
    if incremental:
        export_cmd += [f"--import-marks={marks_src}"]
        import_cmd += [f"--import-marks={marks_dst}"]
    export_cmd += [f"--export-marks={marks_src}"]
    import_cmd += [f"--export-marks={marks_dst}"]

    logging.info("Bridging history into %s repo (re-hashing objects%s)...",
                 repo_object_format(bridge_path),
                 ", incremental" if incremental else "")
    logging.debug("$ %s | %s", " ".join(export_cmd), " ".join(import_cmd))

    exporter = subprocess.Popen(export_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    importer = subprocess.Popen(import_cmd, stdin=exporter.stdout, stderr=subprocess.PIPE)
    # Let the exporter receive SIGPIPE if the importer dies.
    assert exporter.stdout is not None
    exporter.stdout.close()
    _, imp_err = importer.communicate()
    exporter.wait()
    exp_err = exporter.stderr.read() if exporter.stderr else b""
    if exporter.returncode:
        raise RuntimeError(
            f"git fast-export failed (exit {exporter.returncode}): "
            f"{exp_err.decode(errors='replace').strip()}"
        )
    if importer.returncode:
        raise RuntimeError(
            f"git fast-import failed (exit {importer.returncode}): "
            f"{imp_err.decode(errors='replace').strip()}"
        )


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
    source_hash: str | None = None,
    dest_hash: str | None = None,
) -> None:
    for label, h in (("source_hash", source_hash), ("dest_hash", dest_hash)):
        if h is not None and h not in VALID_HASHES:
            raise RuntimeError(
                f"{label} must be one of {sorted(VALID_HASHES)}, got {h!r}."
            )
    # Bridge only when both ends are declared AND differ. Same/!declared -> the
    # normal exact mirror-push path (unchanged behaviour).
    bridge_needed = bool(source_hash and dest_hash and source_hash != dest_hash)

    cleanup_paths: list[Path] = []
    try:
        if transient:
            local_path = Path(tempfile.mkdtemp(prefix="git-sync-"))
            cleanup_paths.append(local_path)
            logging.info("Using transient working dir: %s", local_path)
        elif local:
            local_path = Path(local).expanduser().resolve()
        else:
            # Local (persistent) storage with no path given: fall back to a
            # stable, reusable cache dir under the system temp dir.
            local_path = default_cache_path(source).resolve()
            logging.info("No local path set; using generated cache dir: %s", local_path)

        token_creds = [
            (source, source_token, token_user),
            (dest, dest_token, token_user),
        ]
        with ssh_key_env(ssh_key), token_credentials(token_creds):
            ensure_mirror(local_path, source)
            fetch_source(local_path)

            # Validate the declared source hash against the real repo so a
            # wrong dropdown choice fails clearly instead of mid-push.
            if source_hash:
                actual = repo_object_format(local_path)
                if actual != source_hash:
                    raise RuntimeError(
                        f"Declared source hash is {source_hash!r} but the source "
                        f"repository is actually {actual!r}. Correct the job's "
                        "source hash."
                    )

            if bridge_needed:
                push_repo = _build_bridge(
                    local_path, dest_hash, transient=transient,
                    cleanup_paths=cleanup_paths,
                )
            else:
                push_repo = local_path

            set_destination(push_repo, dest)
            push(
                push_repo,
                branches=branches,
                mirror=mirror,
                all_branches=all_branches,
                tags=tags and not mirror,  # --mirror already covers tags
                force=force,
                dry_run=dry_run,
            )
        logging.info(
            "Sync complete.%s%s",
            f" (bridged {source_hash}→{dest_hash}, commit IDs re-created)" if bridge_needed else "",
            " (dry-run)" if dry_run else "",
        )
    finally:
        for path in cleanup_paths:
            if not path.exists():
                continue
            logging.info("Removing transient path %s", path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass


def _build_bridge(mirror_path: Path, dest_hash: str, *, transient: bool,
                  cleanup_paths: list[Path]) -> Path:
    """Build/refresh the dest-hash bridge repo from the source mirror.

    Returns the bridge repo path to push from. Bridge repo + marks live beside
    the mirror so a persistent cache makes re-syncs incremental; in transient
    mode they're registered for cleanup.
    """
    bridge_path = Path(str(mirror_path) + f".bridge-{dest_hash}")
    marks_src = Path(str(mirror_path) + ".bridge-src.marks")
    marks_dst = Path(str(mirror_path) + ".bridge-dst.marks")
    if transient:
        cleanup_paths += [bridge_path, marks_src, marks_dst]

    # If a previous bridge used a different hash, rebuild from scratch.
    if is_bare_mirror(bridge_path) and repo_object_format(bridge_path) != dest_hash:
        logging.info("Bridge hash changed; rebuilding bridge repo.")
        shutil.rmtree(bridge_path, ignore_errors=True)
        marks_src.unlink(missing_ok=True)
        marks_dst.unlink(missing_ok=True)

    ensure_bridge_repo(bridge_path, dest_hash)
    # Marks must stay consistent with the bridge's actual contents: only resume
    # incrementally when the bridge already has history AND both marks exist.
    incremental = (_has_any_refs(bridge_path)
                   and marks_src.exists() and marks_dst.exists())
    if not incremental:
        marks_src.unlink(missing_ok=True)
        marks_dst.unlink(missing_ok=True)
    bridge_history(mirror_path, bridge_path, marks_src, marks_dst,
                   incremental=incremental)
    return bridge_path


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
# "source:destination" renames a branch on the way over; a plain name keeps it.
# Here source 'main' lands on destination 'master', and 'release' stays as-is.
branches  = ["main:master", "release"]
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

# Cross-hash sync: a SHA-256 Gitea repo mirrored to a SHA-1 GitHub repo.
# When source_hash != dest_hash, history is re-streamed (fast-export|import)
# into the destination's hash. NOTE: commit IDs change and signatures are
# stripped — it's a re-created history, not a byte-identical mirror. Needs a
# persistent local cache to keep the re-sync incremental.
[jobs.gitea-to-github]
source       = "https://gitea.local/me/project.git"
dest         = "https://github.com/me/project.git"
local        = "~/.cache/git-sync/project"
all_branches = true
source_hash  = "sha256"   # your internal Gitea
dest_hash    = "sha1"     # GitHub (only supports sha1)
dest_token   = "${GITHUB_TOKEN}"
'''


# Keys recognised inside a [jobs.<name>] block (and in [defaults]).
_VALID_JOB_KEYS = {
    "source", "dest", "local", "transient",
    "all_branches", "branches", "mirror",
    "tags", "force", "ssh_key",
    "token", "source_token", "dest_token", "token_user",
    "source_hash", "dest_hash",
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

    source_hash = params.get("source_hash")
    dest_hash = params.get("dest_hash")
    for key, val in (("source_hash", source_hash), ("dest_hash", dest_hash)):
        if val is not None and val not in VALID_HASHES:
            raise RuntimeError(
                f"Job '{name}': '{key}' must be one of {sorted(VALID_HASHES)}, got {val!r}."
            )

    transient = bool(params.get("transient", False))
    if transient and local:
        raise RuntimeError(f"Job '{name}': 'transient' and 'local' are mutually exclusive.")
    # Neither set -> persistent local cache at an auto-generated path under
    # the system temp dir (see default_cache_path / sync()).

    selectors = [bool(params.get("all_branches")), bool(params.get("branches")), bool(params.get("mirror"))]
    if sum(selectors) != 1:
        raise RuntimeError(
            f"Job '{name}': must specify exactly one of 'all_branches', 'branches', or 'mirror'."
        )

    branches = params.get("branches")
    if branches is not None:
        if not isinstance(branches, list) or not all(isinstance(b, str) for b in branches):
            raise RuntimeError(f"Job '{name}': 'branches' must be a list of strings.")
        # Validate the source:destination specs now so typos surface with the
        # job name rather than mid-push.
        try:
            parse_branch_mapping(branches)
        except RuntimeError as e:
            raise RuntimeError(f"Job '{name}': {e}") from e

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
        source_hash=source_hash,
        dest_hash=dest_hash,
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
    p.add_argument("--local", help="Persistent local mirror path. If omitted (and "
                                   "--transient is not set), a stable cache dir is "
                                   "auto-generated under the system temp dir.")
    p.add_argument("--transient", action="store_true",
                   help="Use a temp dir that is deleted after the sync.")
    p.add_argument("--all-branches", action="store_true",
                   help="Sync every branch from source.")
    p.add_argument("--branches", help="Comma-separated branch list to sync. Use "
                                      "'source:destination' to push to a differently "
                                      "named branch, e.g. main:master,dev.")
    p.add_argument("--mirror", action="store_true",
                   help="Full mirror push (all refs). Destructive on destination.")
    p.add_argument("--ssh-key", help="Path to SSH private key to use for this sync.")
    p.add_argument("--token", help="Access token for HTTPS auth on both source and dest. "
                                   "Accepts ${ENV_VAR}. Use --token-user to set the username.")
    p.add_argument("--source-token", help="Token for the source remote only (overrides --token).")
    p.add_argument("--dest-token", help="Token for the destination remote only (overrides --token).")
    p.add_argument("--token-user", help="Username paired with the token (default: oauth2).")
    p.add_argument("--source-hash", choices=sorted(VALID_HASHES),
                   help="Object hash of the source repo. Set with --dest-hash when "
                        "they differ to bridge SHA-1<->SHA-256 (re-creates history).")
    p.add_argument("--dest-hash", choices=sorted(VALID_HASHES),
                   help="Object hash of the destination repo (see --source-hash).")

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
        if args.source_hash: offending.append("--source-hash")
        if args.dest_hash: offending.append("--dest-hash")
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
        if args.local and args.transient:
            p.error("--local and --transient are mutually exclusive.")
        # Neither flag -> persistent local cache at an auto-generated path
        # under the system temp dir.
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
            source_hash=args.source_hash,
            dest_hash=args.dest_hash,
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
