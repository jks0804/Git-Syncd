import os
import sqlite3
import subprocess
import shutil
import tempfile
import threading
import stat
import hmac
import hashlib
import secrets
import json
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "gitsyncd.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(DATA_DIR, exist_ok=True)

SESSION_SECRET = os.environ.get("SESSION_SECRET", os.urandom(32).hex())

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.secret_key = SESSION_SECRET
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

scheduler = BackgroundScheduler(daemon=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_branch TEXT NOT NULL DEFAULT 'main',
            dest_url TEXT NOT NULL,
            dest_branch TEXT NOT NULL DEFAULT 'main',
            branches TEXT,
            schedule TEXT,
            ssh_key TEXT,
            git_username TEXT,
            git_password TEXT,
            webhook_secret TEXT,
            created_at TEXT NOT NULL,
            last_sync TEXT,
            last_status TEXT
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            output TEXT,
            trigger TEXT NOT NULL DEFAULT 'manual',
            FOREIGN KEY (config_id) REFERENCES configs(id) ON DELETE CASCADE
        );
    """)
    # Migrate old schema — configs
    for col, defn in [
        ("ssh_key", "TEXT"),
        ("git_username", "TEXT"),
        ("git_password", "TEXT"),
        ("webhook_secret", "TEXT"),
        ("branches", "TEXT"),
        ("trigger", "TEXT NOT NULL DEFAULT 'manual'"),
        ("source_ssh_key", "TEXT"),
        ("source_git_username", "TEXT"),
        ("source_git_password", "TEXT"),
        ("dest_ssh_key", "TEXT"),
        ("dest_git_username", "TEXT"),
        ("dest_git_password", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE configs ADD COLUMN {col} {defn}")
        except Exception:
            pass
    # One-time backfill: copy legacy single-credential columns into per-direction
    # columns so existing configs keep working with the new split-credentials model.
    # Gated by a settings marker so we never overwrite intentionally-cleared values
    # on subsequent restarts.
    try:
        marker = conn.execute(
            "SELECT value FROM settings WHERE key = 'split_creds_backfill_v1'"
        ).fetchone()
        if not marker:
            conn.execute("""
                UPDATE configs
                   SET source_ssh_key      = COALESCE(source_ssh_key,      ssh_key),
                       source_git_username = COALESCE(source_git_username, git_username),
                       source_git_password = COALESCE(source_git_password, git_password),
                       dest_ssh_key        = COALESCE(dest_ssh_key,        ssh_key),
                       dest_git_username   = COALESCE(dest_git_username,   git_username),
                       dest_git_password   = COALESCE(dest_git_password,   git_password)
            """)
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('split_creds_backfill_v1', ?)",
                (datetime.utcnow().isoformat(),),
            )
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE logs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
    except Exception:
        pass
    # Migrate users table
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    conn.commit()

    existing = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    if not existing:
        default_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
        default_user = os.environ.get("ADMIN_USERNAME", "admin")
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, is_admin) VALUES (?, ?, ?, 1)",
            (default_user, generate_password_hash(default_pass), datetime.utcnow().isoformat()),
        )
        conn.commit()
        print(f"[init] Created default admin: {default_user} / {default_pass}")
    else:
        # Ensure the first-ever user is always admin (migration for existing DBs)
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")
        conn.commit()

    conn.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
        if not session.get("is_admin"):
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


def build_git_env(ssh_key_text):
    env = os.environ.copy()
    if not ssh_key_text or not ssh_key_text.strip():
        return env, None
    key_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, dir=DATA_DIR
    )
    key_file.write(ssh_key_text.strip() + "\n")
    key_file.close()
    os.chmod(key_file.name, stat.S_IRUSR | stat.S_IWUSR)
    env["GIT_SSH_COMMAND"] = (
        f'ssh -i {key_file.name} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
    )
    return env, key_file.name


def embed_credentials(url, username, password):
    if not username or not password:
        return url
    from urllib.parse import urlparse, urlunparse, quote
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        u = quote(str(username), safe="")
        p = quote(str(password), safe="")
        netloc = f"{u}:{p}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def _redact_secrets(text, secrets):
    """Replace any occurrence of provided secret strings with ***."""
    if not text:
        return text
    out = text
    seen = set()
    for s in secrets:
        if not s:
            continue
        for variant in (str(s), quote_plus_safe(str(s))):
            if variant and variant not in seen and len(variant) >= 3:
                out = out.replace(variant, "***")
                seen.add(variant)
    return out


def quote_plus_safe(s):
    from urllib.parse import quote
    try:
        return quote(s, safe="")
    except Exception:
        return s


def _sync_branch_pair(src_branch, dst_branch, src_url, dst_url, src_env, dst_env, work_dir, config, trigger, started_at, output_lines):
    """Sync one branch pair. Returns True on success, False on error."""
    sep = "─" * 50
    output_lines.append(f"\n{sep}")
    output_lines.append(f"[{datetime.utcnow().isoformat()}] Branch: {src_branch} → {dst_branch}")
    output_lines.append(sep)

    pair_dir = tempfile.mkdtemp(dir=work_dir, prefix=f"branch_{src_branch}_")
    source_dir = os.path.join(pair_dir, "source")
    dest_dir = os.path.join(pair_dir, "dest")

    output_lines.append(f"[{datetime.utcnow().isoformat()}] Cloning source branch '{src_branch}'...")
    result = subprocess.run(
        ["git", "clone", "--branch", src_branch, "--depth", "1", src_url, source_dir],
        capture_output=True, text=True, timeout=120, env=src_env,
    )
    output_lines.append((result.stdout + result.stderr).strip())
    if result.returncode != 0:
        output_lines.append(f"ERROR: Clone source branch '{src_branch}' failed.")
        return False

    output_lines.append(f"[{datetime.utcnow().isoformat()}] Cloning destination branch '{dst_branch}'...")
    dest_result = subprocess.run(
        ["git", "clone", "--branch", dst_branch, dst_url, dest_dir],
        capture_output=True, text=True, timeout=120, env=dst_env,
    )
    if dest_result.returncode != 0:
        output_lines.append(f"Destination branch '{dst_branch}' not found — cloning default branch...")
        dest_result2 = subprocess.run(
            ["git", "clone", dst_url, dest_dir],
            capture_output=True, text=True, timeout=120, env=dst_env,
        )
        output_lines.append((dest_result2.stdout + dest_result2.stderr).strip())
        if dest_result2.returncode != 0:
            output_lines.append(f"ERROR: Clone destination failed.")
            return False
    else:
        output_lines.append((dest_result.stdout + dest_result.stderr).strip())

    output_lines.append(f"[{datetime.utcnow().isoformat()}] Syncing files...")
    for item in os.listdir(dest_dir):
        if item == ".git":
            continue
        full_path = os.path.join(dest_dir, item)
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)

    for item in os.listdir(source_dir):
        if item == ".git":
            continue
        src_path = os.path.join(source_dir, item)
        dst_path = os.path.join(dest_dir, item)
        if os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)

    git_status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=dest_dir,
    )
    if not git_status.stdout.strip():
        output_lines.append(f"[{datetime.utcnow().isoformat()}] No changes detected for '{src_branch}' → '{dst_branch}'.")
        return True

    subprocess.run(["git", "config", "user.email", "gitsyncd@localhost"], cwd=dest_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "GitSyncd Bot"], cwd=dest_dir, capture_output=True)
    subprocess.run(["git", "add", "-A"], capture_output=True, text=True, cwd=dest_dir)

    commit_result = subprocess.run(
        ["git", "commit", "-m", f"sync({trigger}): {src_branch}→{dst_branch} from {config['source_url']} at {started_at}"],
        capture_output=True, text=True, cwd=dest_dir,
    )
    output_lines.append((commit_result.stdout + commit_result.stderr).strip())
    if commit_result.returncode != 0:
        output_lines.append(f"ERROR: Commit failed for '{src_branch}' → '{dst_branch}'.")
        return False

    push_result = subprocess.run(
        ["git", "push", "origin", f"HEAD:{dst_branch}"],
        capture_output=True, text=True, cwd=dest_dir, timeout=120, env=dst_env,
    )
    output_lines.append((push_result.stdout + push_result.stderr).strip())
    if push_result.returncode != 0:
        output_lines.append(f"ERROR: Push failed for '{src_branch}' → '{dst_branch}'.")
        return False

    output_lines.append(f"[{datetime.utcnow().isoformat()}] ✓ Push complete for '{src_branch}' → '{dst_branch}'.")
    return True


def run_sync(config_id: int, trigger: str = "manual"):
    conn = get_db()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        conn.close()
        return
    config = dict(row)
    branches = _get_branches(row)
    started_at = datetime.utcnow().isoformat()
    log_id = conn.execute(
        "INSERT INTO logs (config_id, started_at, status, trigger) VALUES (?, ?, 'running', ?)",
        (config_id, started_at, trigger),
    ).lastrowid
    conn.commit()
    conn.close()

    output_lines = []
    any_error = False
    work_dir = tempfile.mkdtemp(prefix="gitsyncd_")
    tmp_key_files = []
    secrets_to_redact = []

    def _cred(key, legacy_key):
        return config.get(key) or config.get(legacy_key)

    try:
        src_ssh = _cred("source_ssh_key", "ssh_key")
        dst_ssh = _cred("dest_ssh_key", "ssh_key")
        src_user = _cred("source_git_username", "git_username")
        src_pass = _cred("source_git_password", "git_password")
        dst_user = _cred("dest_git_username", "git_username")
        dst_pass = _cred("dest_git_password", "git_password")
        secrets_to_redact = [src_pass, dst_pass]

        src_env, src_kf = build_git_env(src_ssh)
        dst_env, dst_kf = build_git_env(dst_ssh)
        if src_kf: tmp_key_files.append(src_kf)
        if dst_kf: tmp_key_files.append(dst_kf)

        src_url = embed_credentials(config["source_url"], src_user, src_pass)
        dst_url = embed_credentials(config["dest_url"], dst_user, dst_pass)

        output_lines.append(f"[{started_at}] Trigger: {trigger}")
        output_lines.append(f"[{started_at}] Source:  {config['source_url']}")
        output_lines.append(f"[{started_at}] Dest:    {config['dest_url']}")
        output_lines.append(f"[{started_at}] Branches to sync: {len(branches)}")

        for pair in branches:
            src_branch = pair.get("from", "main")
            dst_branch = pair.get("to", src_branch)
            ok = _sync_branch_pair(
                src_branch, dst_branch,
                src_url, dst_url,
                src_env, dst_env, work_dir, config,
                trigger, started_at, output_lines,
            )
            if not ok:
                any_error = True

        output_lines.append(f"\n[{datetime.utcnow().isoformat()}] All branches processed.")

    except Exception as e:
        any_error = True
        output_lines.append(f"\nFATAL ERROR: {str(e)}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        for kf in tmp_key_files:
            if kf and os.path.exists(kf):
                try:
                    os.unlink(kf)
                except Exception:
                    pass

    status = "error" if any_error else "success"

    finished_at = datetime.utcnow().isoformat()
    full_output = _redact_secrets("\n".join(output_lines).strip(), secrets_to_redact)

    conn2 = get_db()
    conn2.execute(
        "UPDATE logs SET finished_at = ?, status = ?, output = ? WHERE id = ?",
        (finished_at, status, full_output, log_id),
    )
    conn2.execute(
        "UPDATE configs SET last_sync = ?, last_status = ? WHERE id = ?",
        (finished_at, status, config_id),
    )
    conn2.commit()
    conn2.close()


def schedule_config(config_id: int, cron_expr: str):
    job_id = f"sync_{config_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError("Cron expression must have 5 fields: minute hour day month day_of_week")
    trigger = CronTrigger(
        minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4]
    )
    scheduler.add_job(
        lambda cid=config_id: run_sync(cid, "scheduled"),
        trigger, id=job_id, replace_existing=True,
    )


def remove_schedule(config_id: int):
    job_id = f"sync_{config_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def verify_webhook_signature(config, raw_body: bytes) -> bool:
    secret = config.get("webhook_secret")
    if not secret:
        return True  # No secret set — allow all (open webhook)

    # GitHub: X-Hub-Signature-256: sha256=<hex>
    gh_sig = request.headers.get("X-Hub-Signature-256", "")
    if gh_sig.startswith("sha256="):
        expected = "sha256=" + hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, gh_sig)

    # GitLab: X-Gitlab-Token: <secret>
    gl_token = request.headers.get("X-Gitlab-Token", "")
    if gl_token:
        return hmac.compare_digest(gl_token, secret)

    # Generic: X-Webhook-Secret: <secret>
    generic = request.headers.get("X-Webhook-Secret", "")
    if generic:
        return hmac.compare_digest(generic, secret)

    return False


# ── Static pages ─────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect("/")
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/")
def index():
    if not session.get("user_id"):
        return redirect("/login")
    return send_from_directory(STATIC_DIR, "index.html")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_allow_registration():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'allow_registration'").fetchone()
    conn.close()
    return row and row["value"] == "1"


@app.route("/v1/auth/status", methods=["GET"])
def auth_status():
    """Public endpoint: returns registration on/off state."""
    return jsonify({"allow_registration": _get_allow_registration()})


@app.route("/v1/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid username or password"}), 401
    session["user_id"] = row["id"]
    session["username"] = row["username"]
    session["is_admin"] = bool(row["is_admin"])
    return jsonify({"ok": True, "username": row["username"], "is_admin": bool(row["is_admin"])})


@app.route("/v1/auth/register", methods=["POST"])
def auth_register():
    if not _get_allow_registration():
        return jsonify({"error": "Registration is currently disabled"}), 403
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Username already taken"}), 409
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at, is_admin) VALUES (?, ?, ?, 0)",
        (username, generate_password_hash(password), datetime.utcnow().isoformat()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    session["user_id"] = row["id"]
    session["username"] = row["username"]
    session["is_admin"] = False
    return jsonify({"ok": True, "username": row["username"], "is_admin": False}), 201


@app.route("/v1/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/v1/auth/me", methods=["GET"])
def auth_me():
    if not session.get("user_id"):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "username": session.get("username"),
        "user_id": session.get("user_id"),
        "is_admin": session.get("is_admin", False),
    })


@app.route("/v1/auth/password", methods=["PUT"])
@login_required
def change_password():
    data = request.get_json(force=True)
    current = data.get("current_password") or ""
    new_pass = data.get("new_password") or ""
    if not current or not new_pass:
        return jsonify({"error": "current_password and new_password required"}), 400
    if len(new_pass) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not row or not check_password_hash(row["password_hash"], current):
        conn.close()
        return jsonify({"error": "Current password is incorrect"}), 401
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_pass), session["user_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Configs ───────────────────────────────────────────────────────────────────

def _get_branches(row):
    """Return parsed branch list from a config row."""
    raw = row["branches"] if "branches" in row.keys() else None
    if raw:
        try:
            parsed = json.loads(raw)
            if parsed and isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    # Fall back to single-pair from legacy columns
    src = row["source_branch"] if "source_branch" in row.keys() else "main"
    dst = row["dest_branch"] if "dest_branch" in row.keys() else "main"
    return [{"from": src or "main", "to": dst or "main"}]


def _safe_config(row):
    d = dict(row)
    keys = row.keys()
    def _has(col):
        return bool(row[col]) if col in keys else False
    # Strip secret material from response
    for k in (
        "git_password", "ssh_key", "webhook_secret", "branches",
        "source_ssh_key", "source_git_password",
        "dest_ssh_key", "dest_git_password",
    ):
        d.pop(k, None)
    # Legacy flags (kept for back-compat with existing UI code)
    d["has_ssh_key"] = _has("ssh_key")
    d["has_git_password"] = _has("git_password")
    d["has_webhook_secret"] = _has("webhook_secret")
    # Per-direction flags
    d["has_source_ssh_key"] = _has("source_ssh_key")
    d["has_source_git_password"] = _has("source_git_password")
    d["has_dest_ssh_key"] = _has("dest_ssh_key")
    d["has_dest_git_password"] = _has("dest_git_password")
    d["branches"] = _get_branches(row)
    return d


@app.route("/v1/configs", methods=["GET"])
@login_required
def list_configs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM configs ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([_safe_config(r) for r in rows])


@app.route("/v1/configs", methods=["POST"])
@login_required
def create_config():
    data = request.get_json(force=True)
    for field in ["name", "source_url", "dest_url"]:
        if not data.get(field, "").strip():
            return jsonify({"error": f"'{field}' is required"}), 400
    conn = get_db()
    # Parse branch mappings
    raw_branches = data.get("branches")
    if raw_branches and isinstance(raw_branches, list) and len(raw_branches) > 0:
        branches = [{"from": b.get("from", "main").strip() or "main", "to": b.get("to", "main").strip() or "main"} for b in raw_branches]
    else:
        src_b = data.get("source_branch", "main").strip() or "main"
        dst_b = data.get("dest_branch", "main").strip() or "main"
        branches = [{"from": src_b, "to": dst_b}]
    first_src = branches[0]["from"]
    first_dst = branches[0]["to"]

    def _s(k):
        return (data.get(k) or "").strip() or None
    cur = conn.execute(
        """INSERT INTO configs
           (name, source_url, source_branch, dest_url, dest_branch, branches, schedule,
            ssh_key, git_username, git_password, webhook_secret,
            source_ssh_key, source_git_username, source_git_password,
            dest_ssh_key, dest_git_username, dest_git_password,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"].strip(),
            data["source_url"].strip(),
            first_src,
            data["dest_url"].strip(),
            first_dst,
            json.dumps(branches),
            _s("schedule"),
            _s("ssh_key"),
            _s("git_username"),
            _s("git_password"),
            _s("webhook_secret"),
            _s("source_ssh_key"),
            _s("source_git_username"),
            _s("source_git_password"),
            _s("dest_ssh_key"),
            _s("dest_git_username"),
            _s("dest_git_password"),
            datetime.utcnow().isoformat(),
        ),
    )
    config_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if row["schedule"]:
        try:
            schedule_config(config_id, row["schedule"])
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    return jsonify(_safe_config(row)), 201


@app.route("/v1/configs/<int:config_id>", methods=["GET"])
@login_required
def get_config(config_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_safe_config(row))


@app.route("/v1/configs/<int:config_id>", methods=["PUT"])
@login_required
def update_config(config_id):
    data = request.get_json(force=True)
    conn = get_db()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    def _pick(key, fallback):
        val = data.get(key)
        return fallback if val is None else (val.strip() or None)

    name = (data.get("name") or row["name"]).strip()
    source_url = (data.get("source_url") or row["source_url"]).strip()
    dest_url = (data.get("dest_url") or row["dest_url"]).strip()
    schedule = _pick("schedule", row["schedule"])
    ssh_key = _pick("ssh_key", row["ssh_key"])
    git_username = _pick("git_username", row["git_username"])
    git_password = _pick("git_password", row["git_password"])
    source_ssh_key = _pick("source_ssh_key", row["source_ssh_key"])
    source_git_username = _pick("source_git_username", row["source_git_username"])
    source_git_password = _pick("source_git_password", row["source_git_password"])
    dest_ssh_key = _pick("dest_ssh_key", row["dest_ssh_key"])
    dest_git_username = _pick("dest_git_username", row["dest_git_username"])
    dest_git_password = _pick("dest_git_password", row["dest_git_password"])

    # Branch mappings
    raw_branches = data.get("branches")
    if raw_branches and isinstance(raw_branches, list) and len(raw_branches) > 0:
        branches = [{"from": b.get("from", "main").strip() or "main", "to": b.get("to", "main").strip() or "main"} for b in raw_branches]
    else:
        branches = _get_branches(row)
    first_src = branches[0]["from"]
    first_dst = branches[0]["to"]

    conn.execute(
        """UPDATE configs SET name=?, source_url=?, source_branch=?, dest_url=?, dest_branch=?,
           branches=?, schedule=?, ssh_key=?, git_username=?, git_password=?,
           source_ssh_key=?, source_git_username=?, source_git_password=?,
           dest_ssh_key=?, dest_git_username=?, dest_git_password=?
           WHERE id=?""",
        (name, source_url, first_src, dest_url, first_dst,
         json.dumps(branches), schedule, ssh_key, git_username, git_password,
         source_ssh_key, source_git_username, source_git_password,
         dest_ssh_key, dest_git_username, dest_git_password,
         config_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()

    remove_schedule(config_id)
    if schedule:
        try:
            schedule_config(config_id, schedule)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    return jsonify(_safe_config(updated))


@app.route("/v1/configs/<int:config_id>", methods=["DELETE"])
@login_required
def delete_config(config_id):
    remove_schedule(config_id)
    conn = get_db()
    conn.execute("DELETE FROM logs WHERE config_id = ?", (config_id,))
    conn.execute("DELETE FROM configs WHERE id = ?", (config_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/v1/settings", methods=["GET"])
@login_required
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/v1/settings", methods=["PUT"])
@login_required
def update_settings():
    data = request.get_json(force=True)
    allowed_keys = {"default_source_url", "default_dest_url"}
    # allow_registration is admin-only
    if session.get("is_admin"):
        allowed_keys.add("allow_registration")
    conn = get_db()
    for key, value in data.items():
        if key not in allowed_keys:
            continue
        if key == "allow_registration":
            # Boolean stored as "1"/"0"
            bool_val = "1" if (value and str(value) not in ("0", "false", "False", "")) else "0"
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, bool_val),
            )
        else:
            value = (value or "").strip().rstrip("/")
            if value:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            else:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return jsonify({r["key"]: r["value"] for r in rows})


# ── User management (admin only) ─────────────────────────────────────────────

@app.route("/v1/users", methods=["GET"])
@admin_required
def list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/v1/users/<int:uid>", methods=["DELETE"])
@admin_required
def delete_user(uid):
    if uid == session.get("user_id"):
        return jsonify({"error": "You cannot delete your own account"}), 400
    conn = get_db()
    row = conn.execute("SELECT id, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "User not found"}), 404
    # Don't allow deleting the last admin
    if row["is_admin"]:
        count = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_admin = 1").fetchone()["c"]
        if count <= 1:
            conn.close()
            return jsonify({"error": "Cannot delete the only admin account"}), 400
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/v1/users/<int:uid>/role", methods=["PUT"])
@admin_required
def set_user_role(uid):
    data = request.get_json(force=True)
    is_admin = 1 if data.get("is_admin") else 0
    if uid == session.get("user_id") and not is_admin:
        return jsonify({"error": "You cannot remove your own admin role"}), 400
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "User not found"}), 404
    if not is_admin:
        count = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_admin = 1").fetchone()["c"]
        if count <= 1:
            conn.close()
            return jsonify({"error": "Cannot remove the only admin account"}), 400
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (is_admin, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Webhook management (authenticated) ───────────────────────────────────────

@app.route("/v1/configs/<int:config_id>/webhook", methods=["GET"])
@login_required
def get_webhook_info(config_id):
    conn = get_db()
    row = conn.execute("SELECT id, webhook_secret FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "webhook_url": f"/webhooks/{config_id}",
        "has_secret": bool(row["webhook_secret"]),
        "secret": row["webhook_secret"] or "",
    })


@app.route("/v1/configs/<int:config_id>/webhook", methods=["PUT"])
@login_required
def save_webhook_secret(config_id):
    data = request.get_json(force=True)
    action = data.get("action", "save")
    conn = get_db()
    row = conn.execute("SELECT id FROM configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    if action == "generate":
        new_secret = secrets.token_hex(24)
    elif action == "clear":
        new_secret = None
    else:
        new_secret = (data.get("secret") or "").strip() or None

    conn.execute("UPDATE configs SET webhook_secret = ? WHERE id = ?", (new_secret, config_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "secret": new_secret or "", "has_secret": bool(new_secret)})


# ── Public webhook receiver ───────────────────────────────────────────────────

@app.route("/webhooks/<int:config_id>", methods=["POST"])
def receive_webhook(config_id):
    raw_body = request.get_data()
    conn = get_db()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    config = dict(row)
    if not verify_webhook_signature(config, raw_body):
        return jsonify({"error": "Invalid signature"}), 403

    # Determine event source for the log label
    trigger_label = "webhook"
    if request.headers.get("X-GitHub-Event"):
        trigger_label = f"github:{request.headers.get('X-GitHub-Event')}"
    elif request.headers.get("X-Gitlab-Event"):
        trigger_label = f"gitlab:{request.headers.get('X-Gitlab-Event')}"

    thread = threading.Thread(target=run_sync, args=(config_id, trigger_label), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Sync triggered"})


# ── Sync + logs ───────────────────────────────────────────────────────────────

@app.route("/v1/sync/<int:config_id>", methods=["POST"])
@login_required
def trigger_sync(config_id):
    conn = get_db()
    row = conn.execute("SELECT id FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    thread = threading.Thread(target=run_sync, args=(config_id, "manual"), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Sync started"})


@app.route("/v1/logs", methods=["GET"])
@login_required
def list_logs():
    config_id = request.args.get("config_id")
    conn = get_db()
    if config_id:
        rows = conn.execute(
            "SELECT l.*, c.name as config_name FROM logs l JOIN configs c ON l.config_id = c.id WHERE l.config_id = ? ORDER BY l.started_at DESC LIMIT 50",
            (config_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT l.*, c.name as config_name FROM logs l JOIN configs c ON l.config_id = c.id ORDER BY l.started_at DESC LIMIT 100",
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/v1/logs/<int:log_id>", methods=["GET"])
@login_required
def get_log(log_id):
    conn = get_db()
    row = conn.execute(
        "SELECT l.*, c.name as config_name FROM logs l JOIN configs c ON l.config_id = c.id WHERE l.id = ?",
        (log_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


if __name__ == "__main__":
    init_db()
    scheduler.start()

    conn = get_db()
    rows = conn.execute("SELECT id, schedule FROM configs WHERE schedule IS NOT NULL").fetchall()
    conn.close()
    for row in rows:
        try:
            schedule_config(row["id"], row["schedule"])
        except Exception:
            pass

    port = int(os.environ.get("PORT", 20652))
    app.run(host="0.0.0.0", port=port, debug=False)
