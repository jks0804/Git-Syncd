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
git_sync_web.py — Web interface for managing git_sync configurations.

A single-user internal tool. Authenticates with one set of credentials
configured via environment variables, with built-in defaults so it works
out of the box.

Usage:
    python3 git_sync_web.py [--config PATH] [--host HOST] [--port PORT]

Environment variables:
    GIT_SYNC_USER         Web UI username   (default: 'stroh')
    GIT_SYNC_PASSWORD     Web UI password   (default: '24763641E@')
    GIT_SYNC_CONFIG       Config file path  (default: ./sync.toml)
    FLASK_SECRET_KEY      Session key       (default: random per process)

Security notes:
    - Binds to 127.0.0.1 by default. Run behind a reverse proxy with TLS for
      networked access. Do NOT expose this directly to the internet.
    - The hardcoded credential defaults are first-run convenience only. For
      any real deployment, override them via GIT_SYNC_USER / GIT_SYNC_PASSWORD.
    - With no FLASK_SECRET_KEY, sessions are invalidated on every restart
      (which is fine for a single-user tool, just means you re-login).
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from functools import wraps
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        sys.exit("This tool needs Python 3.11+ or `pip install tomli`.")

try:
    import tomli_w
except ImportError:
    sys.exit("This tool needs `pip install tomli-w`.")

try:
    from flask import (
        Flask, request, session, redirect, url_for,
        render_template, flash, abort,
    )
except ImportError:
    sys.exit("This tool needs `pip install flask`.")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SYNC_SCRIPT = SCRIPT_DIR / "git_sync.py"

# Mutable so --config can override it before app.run().
CONFIG_PATH = Path(os.environ.get("GIT_SYNC_CONFIG", "sync.toml")).expanduser().resolve()

USERNAME = os.environ.get("GIT_SYNC_USER", "stroh")
PASSWORD = os.environ.get("GIT_SYNC_PASSWORD", "24763641E@")

VALID_JOB_KEYS = {
    "source", "dest", "local", "transient",
    "all_branches", "branches", "mirror",
    "tags", "force", "ssh_key",
    "token", "source_token", "dest_token", "token_user",
    "source_hash", "dest_hash",
}

# How long to wait on a sync subprocess before giving up.
SYNC_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Read the TOML config file, returning a normalized dict.

    Returns empty defaults/jobs sections if the file doesn't exist yet.
    """
    if not CONFIG_PATH.is_file():
        return {"defaults": {}, "jobs": {}}
    with CONFIG_PATH.open("rb") as f:
        data = tomllib.load(f)
    data.setdefault("defaults", {})
    data.setdefault("jobs", {})
    return data


def save_config(data: dict) -> None:
    """Write the config back atomically.

    NOTE: comments and hand-formatted whitespace in the original file are
    NOT preserved (tomli-w writes a fresh canonical form). If you want to
    keep comments, hand-edit the file directly.
    """
    out: dict = {}
    if data.get("defaults"):
        out["defaults"] = data["defaults"]
    if data.get("jobs"):
        out["jobs"] = data["jobs"]

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    with tmp.open("wb") as f:
        tomli_w.dump(out, f)
    tmp.replace(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------

def parse_job_form(form) -> dict:
    """Build a job dict from POSTed form fields. Raises ValueError on invalid input."""
    job: dict = {}

    source = form.get("source", "").strip()
    dest = form.get("dest", "").strip()
    if not source:
        raise ValueError("Source URL is required.")
    if not dest:
        raise ValueError("Destination URL is required.")
    job["source"] = source
    job["dest"] = dest

    # Object-hash of each end. When they differ, git_sync bridges SHA-1<->SHA-256
    # via fast-export|import (re-creates history). Default sha1 (most compatible);
    # only store when set so same-hash jobs stay on the plain mirror-push path.
    source_hash = form.get("source_hash", "sha1")
    dest_hash = form.get("dest_hash", "sha1")
    for label, val in (("source", source_hash), ("destination", dest_hash)):
        if val not in ("sha1", "sha256"):
            raise ValueError(f"Invalid {label} hash '{val}': choose sha1 or sha256.")
    job["source_hash"] = source_hash
    job["dest_hash"] = dest_hash

    storage = form.get("storage", "")
    if storage == "local":
        local = form.get("local", "").strip()
        # Empty path is allowed: the sync falls back to a stable auto-generated
        # cache dir under the system temp dir. Only emit the key when set.
        if local:
            job["local"] = local
    elif storage == "transient":
        job["transient"] = True
    else:
        raise ValueError("Choose either 'local cache' or 'transient' storage.")

    selector = form.get("selector", "")
    if selector == "all_branches":
        job["all_branches"] = True
    elif selector == "branches":
        raw = form.get("branches", "")
        branches = [b.strip() for b in raw.replace("\n", ",").split(",") if b.strip()]
        if not branches:
            raise ValueError("Specify at least one branch name.")
        # Each entry is a branch name, optionally "source:destination" to rename
        # on push. Validate the spec shape here so errors show before saving.
        for spec in branches:
            if spec.count(":") > 1:
                raise ValueError(
                    f"Invalid branch '{spec}': use 'source:destination' "
                    "with at most one colon."
                )
            if ":" in spec and not spec.replace(":", "").strip():
                raise ValueError(f"Invalid branch '{spec}': no branch name given.")
        job["branches"] = branches
    elif selector == "mirror":
        job["mirror"] = True
    else:
        raise ValueError("Choose a branch-selection mode.")

    ssh_key = form.get("ssh_key", "").strip()
    if ssh_key:
        job["ssh_key"] = ssh_key

    # HTTPS access tokens. `token` applies to both ends; source/dest override.
    # May be a literal token or a ${ENV_VAR} reference. Stored as-is.
    token = form.get("token", "").strip()
    source_token = form.get("source_token", "").strip()
    dest_token = form.get("dest_token", "").strip()
    token_user = form.get("token_user", "").strip()
    if token:
        job["token"] = token
    if source_token:
        job["source_token"] = source_token
    if dest_token:
        job["dest_token"] = dest_token
    if token_user:
        job["token_user"] = token_user

    # tags defaults to True, only emit when explicitly disabled
    if form.get("tags") != "on":
        job["tags"] = False
    if form.get("force") == "on":
        job["force"] = True

    return job


def parse_defaults_form(form) -> dict:
    """Build a [defaults] dict from POSTed form fields."""
    defaults: dict = {}
    if form.get("tags_default") == "on":
        defaults["tags"] = False  # checkbox means "skip by default"
    if form.get("force_default") == "on":
        defaults["force"] = True

    # Shared default tokens. Same keys a job uses; git_sync merges defaults
    # under each job ({**defaults, **job}), so a job's own token always wins
    # and a job that leaves a token blank falls back to these.
    token = form.get("token", "").strip()
    source_token = form.get("source_token", "").strip()
    dest_token = form.get("dest_token", "").strip()
    token_user = form.get("token_user", "").strip()
    if token:
        defaults["token"] = token
    if source_token:
        defaults["source_token"] = source_token
    if dest_token:
        defaults["dest_token"] = dest_token
    if token_user:
        defaults["token_user"] = token_user

    return defaults


def form_to_display_dict(form) -> dict:
    """Normalise a posted MultiDict into a regular dict for re-rendering after errors.

    Keeps the user's input verbatim where possible so they don't lose typing.
    """
    return {
        "source": form.get("source", ""),
        "dest": form.get("dest", ""),
        "local": form.get("local", ""),
        "ssh_key": form.get("ssh_key", ""),
        "token": form.get("token", ""),
        "source_token": form.get("source_token", ""),
        "dest_token": form.get("dest_token", ""),
        "token_user": form.get("token_user", ""),
        "source_hash": form.get("source_hash", "sha1"),
        "dest_hash": form.get("dest_hash", "sha1"),
        "branches": form.get("branches", ""),  # keep as string for redisplay
        "all_branches": form.get("selector") == "all_branches",
        "mirror": form.get("selector") == "mirror",
        "transient": form.get("storage") == "transient",
        "tags": form.get("tags") == "on",
        "force": form.get("force") == "on",
    }


def selector_for(job: dict) -> str:
    if job.get("mirror"):
        return "mirror"
    if job.get("branches"):
        return "branches"
    if job.get("all_branches"):
        return "all_branches"
    return ""


def storage_for(job: dict) -> str:
    if job.get("transient"):
        return "transient"
    # Local is the default: an explicit local path, or neither key set (which
    # means a persistent cache at an auto-generated path).
    return "local"


def is_valid_job_name(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in "-_" for c in name)


# ---------------------------------------------------------------------------
# Subprocess: run a sync
# ---------------------------------------------------------------------------

def run_sync(job_name: str | None, dry_run: bool = False, force: bool = False) -> tuple[int, str]:
    """Invoke git_sync.py. Returns (returncode, combined_output).

    job_name=None runs *every* job in the config (omits --job); git_sync runs
    them in order, logs per-job failures, and exits non-zero if any failed.

    force=True adds --force, which overrides the job's own setting for this run
    only (git_sync applies --force in config mode). This is the destructive
    overwrite the web "danger zone" gates behind a confirmation.
    """
    if not SYNC_SCRIPT.is_file():
        return 127, f"git_sync.py not found at {SYNC_SCRIPT}"

    cmd = [
        sys.executable,
        str(SYNC_SCRIPT),
        "--config", str(CONFIG_PATH),
    ]
    if job_name is not None:
        cmd += ["--job", job_name]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")

    # Running every job can take longer than a single one; scale the timeout.
    timeout = SYNC_TIMEOUT_SECONDS if job_name is not None else SYNC_TIMEOUT_SECONDS * 4
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # git_sync logs to stderr; stdout is mostly empty. Combine for display.
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return 124, f"Sync timed out after {timeout} seconds."


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=str(SCRIPT_DIR / "templates"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


@app.before_request
def issue_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)


@app.context_processor
def inject_globals():
    return {
        "csrf_token_value": session.get("csrf_token", ""),
        "config_path": str(CONFIG_PATH),
        "username_display": USERNAME,
    }


def check_csrf():
    """Validate the CSRF token on state-changing requests."""
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not token or not expected or not secrets.compare_digest(token, expected):
            abort(400, "CSRF token mismatch — please reload the page and try again.")


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


# ---- Auth ---------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    if request.method == "POST":
        check_csrf()
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        ok_u = secrets.compare_digest(u, USERNAME)
        ok_p = secrets.compare_digest(p, PASSWORD)
        if ok_u and ok_p:
            session.clear()
            session["authed"] = True
            session["csrf_token"] = secrets.token_hex(16)
            nxt = request.args.get("next") or url_for("index")
            # Only allow relative redirects (don't bounce to external URLs)
            if not nxt.startswith("/"):
                nxt = url_for("index")
            return redirect(nxt)
        flash("Invalid credentials.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


# ---- Jobs ---------------------------------------------------------------

@app.route("/")
@require_auth
def index():
    config = load_config()
    return render_template(
        "index.html",
        defaults=config["defaults"],
        jobs=config["jobs"],
    )


def _after_save_response(name: str):
    """Honor the form's split-button choice after a successful save.

    after_save == 'run'      -> save then run the job, show its result
    after_save == 'dry_run'  -> save then dry-run the job, show its result
    anything else            -> back to the dashboard
    """
    action = request.form.get("after_save", "")
    if action in ("run", "dry_run"):
        dry = action == "dry_run"
        rc, output = run_sync(name, dry_run=dry)
        return render_template(
            "run_result.html",
            name=name, dry_run=dry, force=False, returncode=rc, output=output,
        )
    return redirect(url_for("index"))


@app.route("/jobs/new", methods=["GET", "POST"])
@require_auth
def new_job():
    if request.method == "POST":
        check_csrf()
        try:
            name = request.form.get("name", "").strip()
            if not is_valid_job_name(name):
                raise ValueError(
                    "Job name must contain only letters, digits, dashes, and underscores."
                )
            job = parse_job_form(request.form)
            config = load_config()
            if name in config["jobs"]:
                raise ValueError(f"A job named '{name}' already exists.")
            config["jobs"][name] = job
            save_config(config)
            flash(f"Job '{name}' created.", "success")
            return _after_save_response(name)
        except ValueError as e:
            flash(str(e), "error")
            return render_template(
                "job_form.html",
                action_label="Create job",
                form_action=url_for("new_job"),
                name=request.form.get("name", ""),
                name_editable=True,
                job=form_to_display_dict(request.form),
                selector=request.form.get("selector", ""),
                storage=request.form.get("storage", ""),
            )

    # GET: blank form with sensible defaults
    return render_template(
        "job_form.html",
        action_label="Create job",
        form_action=url_for("new_job"),
        name="",
        name_editable=True,
        job={"tags": True},  # checkbox should default to checked
        selector="",
        storage="",
    )


@app.route("/jobs/<name>/edit", methods=["GET", "POST"])
@require_auth
def edit_job(name):
    config = load_config()
    if name not in config["jobs"]:
        abort(404)

    if request.method == "POST":
        check_csrf()
        try:
            job = parse_job_form(request.form)
            config["jobs"][name] = job
            save_config(config)
            flash(f"Job '{name}' updated.", "success")
            return _after_save_response(name)
        except ValueError as e:
            flash(str(e), "error")
            return render_template(
                "job_form.html",
                action_label="Save changes",
                form_action=url_for("edit_job", name=name),
                name=name,
                name_editable=False,
                job=form_to_display_dict(request.form),
                selector=request.form.get("selector", ""),
                storage=request.form.get("storage", ""),
            )

    # GET: pre-populate from existing job
    job = config["jobs"][name]
    return render_template(
        "job_form.html",
        action_label="Save changes",
        form_action=url_for("edit_job", name=name),
        name=name,
        name_editable=False,
        job=job,
        selector=selector_for(job),
        storage=storage_for(job),
    )


@app.route("/jobs/<name>/delete", methods=["POST"])
@require_auth
def delete_job(name):
    check_csrf()
    config = load_config()
    if name not in config["jobs"]:
        abort(404)
    del config["jobs"][name]
    save_config(config)
    flash(f"Job '{name}' deleted.", "success")
    return redirect(url_for("index"))


@app.route("/jobs/<name>/run", methods=["POST"])
@require_auth
def run_job_view(name):
    check_csrf()
    config = load_config()
    if name not in config["jobs"]:
        abort(404)
    dry_run = request.form.get("dry_run") == "on"
    force = request.form.get("force") == "on"
    rc, output = run_sync(name, dry_run=dry_run, force=force)
    return render_template(
        "run_result.html",
        name=name,
        dry_run=dry_run,
        force=force,
        returncode=rc,
        output=output,
    )


@app.route("/jobs/run-all", methods=["POST"])
@require_auth
def run_all_view():
    check_csrf()
    config = load_config()
    if not config["jobs"]:
        flash("No jobs to run.", "error")
        return redirect(url_for("index"))
    dry_run = request.form.get("dry_run") == "on"
    rc, output = run_sync(None, dry_run=dry_run)
    return render_template(
        "run_result.html",
        name="all jobs",
        all_jobs=True,
        dry_run=dry_run,
        force=False,
        returncode=rc,
        output=output,
    )


# ---- Defaults -----------------------------------------------------------

@app.route("/defaults", methods=["GET", "POST"])
@require_auth
def edit_defaults():
    config = load_config()
    if request.method == "POST":
        check_csrf()
        config["defaults"] = parse_defaults_form(request.form)
        save_config(config)
        flash("Defaults updated.", "success")
        return redirect(url_for("index"))
    return render_template("defaults_form.html", defaults=config["defaults"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="git_sync web UI.")
    parser.add_argument("--config", help="Path to TOML config file.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind to (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port (default: 5000).")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode (DON'T use in production).")
    args = parser.parse_args()

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).expanduser().resolve()

    print(f"git_sync web UI")
    print(f"  Config:    {CONFIG_PATH}")
    print(f"  User:      {USERNAME!r}")
    print(f"  Listening: http://{args.host}:{args.port}/")
    if args.host != "127.0.0.1":
        print(f"  WARNING:   binding to {args.host} — use a reverse proxy with TLS!")
    if PASSWORD == "24763641E@" and not os.environ.get("GIT_SYNC_PASSWORD"):
        print(f"  WARNING:   using built-in default password. "
              f"Set GIT_SYNC_PASSWORD env var to override.")
    print()

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
