import os
import sqlite3
import subprocess
import shutil
import tempfile
import threading
import stat
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
sync_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
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
            schedule TEXT,
            ssh_key TEXT,
            git_username TEXT,
            git_password TEXT,
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
            FOREIGN KEY (config_id) REFERENCES configs(id) ON DELETE CASCADE
        );
    """)
    # Add new columns if upgrading from old schema
    for col, defn in [
        ("ssh_key", "TEXT"),
        ("git_username", "TEXT"),
        ("git_password", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE configs ADD COLUMN {col} {defn}")
        except Exception:
            pass
    conn.commit()

    # Seed default admin user if none exist
    existing = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    if not existing:
        default_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
        default_user = os.environ.get("ADMIN_USERNAME", "admin")
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (default_user, generate_password_hash(default_pass), datetime.utcnow().isoformat()),
        )
        conn.commit()
        print(f"[init] Created default user: {default_user} / {default_pass}")

    conn.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
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
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        netloc = f"{username}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def run_sync(config_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        conn.close()
        return
    config = dict(row)
    started_at = datetime.utcnow().isoformat()
    log_id = conn.execute(
        "INSERT INTO logs (config_id, started_at, status) VALUES (?, ?, 'running')",
        (config_id, started_at),
    ).lastrowid
    conn.commit()
    conn.close()

    output_lines = []
    status = "success"
    work_dir = tempfile.mkdtemp(prefix="gitsyncd_")
    tmp_key_file = None

    try:
        git_env, tmp_key_file = build_git_env(config.get("ssh_key"))

        src_url = embed_credentials(
            config["source_url"],
            config.get("git_username"),
            config.get("git_password"),
        )
        dst_url = embed_credentials(
            config["dest_url"],
            config.get("git_username"),
            config.get("git_password"),
        )

        source_dir = os.path.join(work_dir, "source")
        dest_dir = os.path.join(work_dir, "dest")

        output_lines.append(f"[{datetime.utcnow().isoformat()}] Cloning source: {config['source_url']} (branch: {config['source_branch']})")
        result = subprocess.run(
            ["git", "clone", "--branch", config["source_branch"], "--depth", "1", src_url, source_dir],
            capture_output=True, text=True, timeout=120, env=git_env,
        )
        output_lines.append(result.stdout + result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"Clone source failed:\n{result.stderr}")

        output_lines.append(f"[{datetime.utcnow().isoformat()}] Cloning destination: {config['dest_url']} (branch: {config['dest_branch']})")
        dest_result = subprocess.run(
            ["git", "clone", "--branch", config["dest_branch"], dst_url, dest_dir],
            capture_output=True, text=True, timeout=120, env=git_env,
        )
        if dest_result.returncode != 0:
            output_lines.append("Dest branch not found, cloning default and creating branch...")
            dest_result2 = subprocess.run(
                ["git", "clone", dst_url, dest_dir],
                capture_output=True, text=True, timeout=120, env=git_env,
            )
            output_lines.append(dest_result2.stdout + dest_result2.stderr)
            if dest_result2.returncode != 0:
                raise RuntimeError(f"Clone dest failed:\n{dest_result2.stderr}")
        else:
            output_lines.append(dest_result.stdout + dest_result.stderr)

        output_lines.append(f"[{datetime.utcnow().isoformat()}] Syncing files from source to destination...")
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
            output_lines.append(f"[{datetime.utcnow().isoformat()}] No changes detected. Nothing to push.")
        else:
            subprocess.run(["git", "config", "user.email", "gitsyncd@localhost"], cwd=dest_dir, capture_output=True)
            subprocess.run(["git", "config", "user.name", "GitSyncd Bot"], cwd=dest_dir, capture_output=True)

            subprocess.run(["git", "add", "-A"], capture_output=True, text=True, cwd=dest_dir)

            commit_result = subprocess.run(
                ["git", "commit", "-m", f"sync: from {config['source_url']} at {started_at}"],
                capture_output=True, text=True, cwd=dest_dir,
            )
            output_lines.append(commit_result.stdout + commit_result.stderr)
            if commit_result.returncode != 0:
                raise RuntimeError(f"Commit failed:\n{commit_result.stderr}")

            push_result = subprocess.run(
                ["git", "push", "origin", f"HEAD:{config['dest_branch']}"],
                capture_output=True, text=True, cwd=dest_dir, timeout=120, env=git_env,
            )
            output_lines.append(push_result.stdout + push_result.stderr)
            if push_result.returncode != 0:
                raise RuntimeError(f"Push failed:\n{push_result.stderr}")

            output_lines.append(f"[{datetime.utcnow().isoformat()}] Push complete.")

    except Exception as e:
        status = "error"
        output_lines.append(f"ERROR: {str(e)}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if tmp_key_file and os.path.exists(tmp_key_file):
            os.unlink(tmp_key_file)

    finished_at = datetime.utcnow().isoformat()
    full_output = "\n".join(output_lines).strip()

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
    scheduler.add_job(run_sync, trigger, args=[config_id], id=job_id, replace_existing=True)


def remove_schedule(config_id: int):
    job_id = f"sync_{config_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


# ── Static pages ────────────────────────────────────────────────────────────

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


# ── Auth endpoints ───────────────────────────────────────────────────────────

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
    return jsonify({"ok": True, "username": row["username"]})


@app.route("/v1/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/v1/auth/me", methods=["GET"])
def auth_me():
    if not session.get("user_id"):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"username": session.get("username"), "user_id": session.get("user_id")})


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


# ── Config endpoints ─────────────────────────────────────────────────────────

def _safe_config(row):
    d = dict(row)
    d.pop("git_password", None)
    has_ssh = bool(d.get("ssh_key", ""))
    has_pass = bool(row["git_password"]) if "git_password" in row.keys() else False
    d["has_ssh_key"] = has_ssh
    d["has_git_password"] = has_pass
    d.pop("ssh_key", None)
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
    required = ["name", "source_url", "dest_url"]
    for field in required:
        if not data.get(field, "").strip():
            return jsonify({"error": f"'{field}' is required"}), 400
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO configs
           (name, source_url, source_branch, dest_url, dest_branch, schedule, ssh_key, git_username, git_password, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"].strip(),
            data["source_url"].strip(),
            data.get("source_branch", "main").strip() or "main",
            data["dest_url"].strip(),
            data.get("dest_branch", "main").strip() or "main",
            data.get("schedule", "").strip() or None,
            data.get("ssh_key", "").strip() or None,
            data.get("git_username", "").strip() or None,
            data.get("git_password", "").strip() or None,
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
    d = _safe_config(row)
    return jsonify(d)


@app.route("/v1/configs/<int:config_id>", methods=["PUT"])
@login_required
def update_config(config_id):
    data = request.get_json(force=True)
    conn = get_db()
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    name = data.get("name", row["name"]).strip()
    source_url = data.get("source_url", row["source_url"]).strip()
    source_branch = data.get("source_branch", row["source_branch"]).strip() or "main"
    dest_url = data.get("dest_url", row["dest_url"]).strip()
    dest_branch = data.get("dest_branch", row["dest_branch"]).strip() or "main"
    schedule = data.get("schedule", row["schedule"])
    if isinstance(schedule, str):
        schedule = schedule.strip() or None

    ssh_key = data.get("ssh_key")
    if ssh_key is None:
        ssh_key = row["ssh_key"]
    else:
        ssh_key = ssh_key.strip() or None

    git_username = data.get("git_username")
    if git_username is None:
        git_username = row["git_username"]
    else:
        git_username = git_username.strip() or None

    git_password = data.get("git_password")
    if git_password is None:
        git_password = row["git_password"]
    elif git_password == "":
        git_password = None
    else:
        git_password = git_password.strip()

    conn.execute(
        """UPDATE configs SET name=?, source_url=?, source_branch=?, dest_url=?, dest_branch=?,
           schedule=?, ssh_key=?, git_username=?, git_password=? WHERE id=?""",
        (name, source_url, source_branch, dest_url, dest_branch,
         schedule, ssh_key, git_username, git_password, config_id),
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


@app.route("/v1/sync/<int:config_id>", methods=["POST"])
@login_required
def trigger_sync(config_id):
    conn = get_db()
    row = conn.execute("SELECT id FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    thread = threading.Thread(target=run_sync, args=(config_id,), daemon=True)
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
