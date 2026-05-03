const API = "/v1";

async function apiFetch(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json", ...options.headers },
    credentials: "same-origin",
    ...options,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, type = "info") {
  let container = document.querySelector(".toast-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "toast-container";
    document.body.appendChild(container);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function formatRelative(isoStr) {
  if (!isoStr) return "Never";
  const diff = Math.floor((Date.now() - new Date(isoStr + "Z").getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function shortUrl(url) {
  return url.replace(/^https?:\/\//, "").replace(/^git@/, "").replace(/\.git$/, "");
}

function statusBadge(status) {
  if (!status) return `<span class="status-badge status-never">Never Synced</span>`;
  const map = { success: "status-success", error: "status-error", running: "status-running" };
  return `<span class="status-badge ${map[status] || "status-never"}">${status}</span>`;
}

function authBadges(c) {
  const badges = [];
  if (c.has_ssh_key) badges.push(`<span class="auth-indicator" title="SSH key saved"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>SSH</span>`);
  if (c.has_git_password) badges.push(`<span class="auth-indicator" title="HTTPS credentials saved"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Token</span>`);
  return badges.join("");
}

function branchPairsHtml(branches) {
  if (!branches || branches.length === 0) return "";
  if (branches.length === 1) {
    const b = branches[0];
    return `<span class="repo-branch">${escHtml(b.from)}${b.from !== b.to ? " → " + escHtml(b.to) : ""}</span>`;
  }
  const pills = branches.map(b =>
    `<span class="branch-pill">${escHtml(b.from)}${b.from !== b.to ? " → " + escHtml(b.to) : ""}</span>`
  ).join("");
  return `<div class="branch-pills">${pills}</div>`;
}

function renderCard(c) {
  const branches = c.branches || [{ from: c.source_branch || "main", to: c.dest_branch || "main" }];
  const scheduleHtml = c.schedule
    ? `<span class="meta-item"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>${c.schedule}</span>`
    : `<span class="meta-item" style="color:var(--text-subtle)">Manual only</span>`;

  const lastSyncHtml = `<span class="meta-item"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.6"/></svg>${formatRelative(c.last_sync)}</span>`;

  return `
  <div class="config-card" id="card-${c.id}">
    <div class="card-header">
      <div class="card-title-row">
        <span class="card-title">${escHtml(c.name)}</span>
        <div class="card-badges">${authBadges(c)}</div>
      </div>
      ${statusBadge(c.last_status)}
    </div>
    <div class="card-body">
      <div class="repo-flow">
        <div class="repo-row">
          <span class="repo-label">SRC</span>
          <span class="repo-url" title="${escHtml(c.source_url)}">${escHtml(shortUrl(c.source_url))}</span>
        </div>
        <div class="repo-row flow-arrow">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>
        </div>
        <div class="repo-row">
          <span class="repo-label">DST</span>
          <span class="repo-url" title="${escHtml(c.dest_url)}">${escHtml(shortUrl(c.dest_url))}</span>
        </div>
      </div>
      <div class="card-branches">${branchPairsHtml(branches)}</div>
      <div class="card-meta">
        ${scheduleHtml}
        ${lastSyncHtml}
      </div>
    </div>
    <div class="card-actions">
      <button class="btn btn-blue btn-sm" onclick="triggerSync(${c.id}, this)">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.6"/></svg>
        Sync Now
      </button>
      <button class="btn btn-ghost btn-sm" onclick="openWebhookModal(${c.id}, '${escHtml(c.name)}')" title="${c.has_webhook_secret ? 'Webhook active' : 'Set up webhook'}">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="${c.has_webhook_secret ? '#3fb950' : 'currentColor'}" stroke-width="2"><path d="M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.15c-.05.21-.08.43-.08.66 0 1.61 1.31 2.92 2.92 2.92s2.92-1.31 2.92-2.92-1.31-2.92-2.92-2.92z"/></svg>
        Webhook
      </button>
      <button class="btn btn-ghost btn-sm" onclick="openLogs(${c.id}, '${escHtml(c.name)}')">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        Logs
      </button>
      <button class="btn btn-ghost btn-sm" onclick="openEditModal(${c.id})">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        Edit
      </button>
      <button class="btn btn-danger btn-sm" onclick="deleteConfig(${c.id}, '${escHtml(c.name)}')">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
        Delete
      </button>
    </div>
  </div>`;
}

function escHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function loadConfigs() {
  try {
    const configs = await apiFetch("/configs");
    const grid = document.getElementById("configs-grid");
    const empty = document.getElementById("empty-state");
    if (configs.length === 0) {
      grid.innerHTML = "";
      empty.classList.remove("hidden");
    } else {
      empty.classList.add("hidden");
      grid.innerHTML = configs.map(renderCard).join("");
    }
  } catch (e) {
    if (e.message !== "Unauthorized") toast("Failed to load configs: " + e.message, "error");
  }
}

async function triggerSync(id, btn) {
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Syncing...`;
  try {
    await apiFetch(`/sync/${id}`, { method: "POST" });
    toast("Sync started in background", "success");
    setTimeout(loadConfigs, 1200);
    setTimeout(loadConfigs, 4000);
    setTimeout(loadConfigs, 9000);
  } catch (e) {
    toast("Sync failed: " + e.message, "error");
    btn.disabled = false;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.6"/></svg> Sync Now`;
  }
}

async function deleteConfig(id, name) {
  if (!confirm(`Delete sync configuration "${name}"?\nThis will also delete all its logs.`)) return;
  try {
    await apiFetch(`/configs/${id}`, { method: "DELETE" });
    toast(`"${name}" deleted`, "info");
    loadConfigs();
  } catch (e) {
    toast("Delete failed: " + e.message, "error");
  }
}

// Auth tab switching
let activeAuthTab = "ssh";
function switchAuthTab(tab) {
  activeAuthTab = tab;
  document.getElementById("auth-ssh").classList.toggle("hidden", tab !== "ssh");
  document.getElementById("auth-https").classList.toggle("hidden", tab !== "https");
  document.getElementById("tab-ssh").classList.toggle("active", tab === "ssh");
  document.getElementById("tab-https").classList.toggle("active", tab === "https");
}

// ── Settings ──────────────────────────────────────────────────────────────────
let _appSettings = {};

async function loadSettings() {
  try {
    _appSettings = await apiFetch("/settings");
    syncSameCheckbox();
  } catch (e) {
    _appSettings = {};
  }
}

function openSettings() {
  document.getElementById("s-default-source").value = _appSettings.default_source_url || "";
  document.getElementById("s-default-dest").value = _appSettings.default_dest_url || "";
  syncSameCheckbox();
  updateSourcePreview();
  updateDestPreview();
  // Admin-only section
  const adminSection = document.getElementById("settings-users-section");
  if (adminSection && _currentUser?.is_admin) {
    adminSection.classList.remove("hidden");
    const regToggle = document.getElementById("s-allow-registration");
    if (regToggle) regToggle.checked = _appSettings.allow_registration === "1";
    loadUsers();
  } else if (adminSection) {
    adminSection.classList.add("hidden");
  }
  document.getElementById("settings-overlay").classList.remove("hidden");
  document.getElementById("s-default-source").focus();
}

function closeSettings() {
  document.getElementById("settings-overlay").classList.add("hidden");
}

function closeSettingsIfOutside(e) {
  if (e.target === document.getElementById("settings-overlay")) closeSettings();
}

function syncSameCheckbox() {
  const src = (document.getElementById("s-default-source")?.value || _appSettings.default_source_url || "").trim();
  const dst = (document.getElementById("s-default-dest")?.value || _appSettings.default_dest_url || "").trim();
  const checkbox = document.getElementById("s-same-instance");
  if (checkbox) checkbox.checked = !dst || dst === src;
}

function toggleSameInstance() {
  const checked = document.getElementById("s-same-instance").checked;
  const destEl = document.getElementById("s-default-dest");
  if (checked) {
    destEl.value = document.getElementById("s-default-source").value;
    destEl.disabled = true;
    updateDestPreview();
  } else {
    destEl.disabled = false;
    destEl.focus();
  }
}

function updateSourcePreview() {
  const val = (document.getElementById("s-default-source")?.value || "").trim().replace(/\/$/, "");
  const el = document.getElementById("s-source-preview");
  if (!el) return;
  el.textContent = val ? `${val}/org/repo.git` : "";
  el.style.display = val ? "block" : "none";
  if (document.getElementById("s-same-instance")?.checked) {
    document.getElementById("s-default-dest").value = val;
    updateDestPreview();
  }
}

function updateDestPreview() {
  const val = (document.getElementById("s-default-dest")?.value || "").trim().replace(/\/$/, "");
  const el = document.getElementById("s-dest-preview");
  if (!el) return;
  el.textContent = val ? `${val}/org/repo.git` : "";
  el.style.display = val ? "block" : "none";
}

async function saveSettings() {
  const btn = document.getElementById("settings-save-btn");
  btn.disabled = true;
  btn.textContent = "Saving…";
  const src = document.getElementById("s-default-source").value.trim().replace(/\/$/, "");
  let dst = document.getElementById("s-default-dest").value.trim().replace(/\/$/, "");
  if (document.getElementById("s-same-instance").checked) dst = src;
  const payload = { default_source_url: src, default_dest_url: dst };
  if (_currentUser?.is_admin) {
    const regToggle = document.getElementById("s-allow-registration");
    if (regToggle) payload.allow_registration = regToggle.checked;
  }
  try {
    _appSettings = await apiFetch("/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    toast("Settings saved", "success");
    closeSettings();
  } catch (e) {
    toast("Failed to save: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Save";
  }
}

// ── Branch mapping rows ───────────────────────────────────────────────────────
function setBranchMappings(branches) {
  const list = document.getElementById("branch-mappings-list");
  list.innerHTML = "";
  const pairs = (branches && branches.length > 0) ? branches : [{ from: "main", to: "main" }];
  pairs.forEach(b => addBranchRow(b.from, b.to));
}

function addBranchRow(from = "", to = "") {
  const list = document.getElementById("branch-mappings-list");
  const idx = list.children.length;
  const row = document.createElement("div");
  row.className = "branch-map-row";
  row.innerHTML = `
    <input type="text" class="branch-input" placeholder="main" value="${escHtml(from)}" required />
    <span class="branch-arrow">→</span>
    <input type="text" class="branch-input" placeholder="main" value="${escHtml(to)}" required />
    <button type="button" class="branch-remove-btn" onclick="removeBranchRow(this)" title="Remove">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>`;
  list.appendChild(row);
  updateRemoveButtons();
}

function removeBranchRow(btn) {
  const list = document.getElementById("branch-mappings-list");
  if (list.children.length <= 1) return;
  btn.closest(".branch-map-row").remove();
  updateRemoveButtons();
}

function updateRemoveButtons() {
  const rows = document.querySelectorAll("#branch-mappings-list .branch-map-row");
  rows.forEach(r => {
    r.querySelector(".branch-remove-btn").disabled = rows.length <= 1;
  });
}

function getBranchMappings() {
  const rows = document.querySelectorAll("#branch-mappings-list .branch-map-row");
  return Array.from(rows).map(row => {
    const inputs = row.querySelectorAll("input");
    return { from: inputs[0].value.trim() || "main", to: inputs[1].value.trim() || "main" };
  }).filter(b => b.from);
}

// Modal - Add/Edit
function openAddModal() {
  document.getElementById("modal-title").textContent = "Add Sync Configuration";
  document.getElementById("form-submit-btn").textContent = "Create";
  document.getElementById("edit-id").value = "";
  document.getElementById("config-form").reset();
  document.getElementById("pw-saved-badge").classList.add("hidden");
  document.getElementById("ssh-key-status").classList.add("hidden");
  switchAuthTab("ssh");
  setBranchMappings([{ from: "main", to: "main" }]);

  // Pre-fill URL fields from default settings
  const srcDefault = _appSettings.default_source_url || "";
  const dstDefault = _appSettings.default_dest_url || "";
  const srcEl = document.getElementById("f-source-url");
  const dstEl = document.getElementById("f-dest-url");
  if (srcDefault) {
    srcEl.value = srcDefault + "/";
    srcEl.placeholder = `${srcDefault}/org/repo.git`;
  }
  if (dstDefault) {
    dstEl.value = dstDefault + "/";
    dstEl.placeholder = `${dstDefault}/org/repo.git`;
  }

  document.getElementById("modal-overlay").classList.remove("hidden");
  if (srcDefault) { srcEl.focus(); srcEl.setSelectionRange(srcEl.value.length, srcEl.value.length); }
}

async function openEditModal(id) {
  try {
    const c = await apiFetch(`/configs/${id}`);
    document.getElementById("modal-title").textContent = "Edit Sync Configuration";
    document.getElementById("form-submit-btn").textContent = "Save";
    document.getElementById("edit-id").value = id;
    document.getElementById("f-name").value = c.name;
    document.getElementById("f-source-url").value = c.source_url;
    document.getElementById("f-dest-url").value = c.dest_url;
    document.getElementById("f-schedule").value = c.schedule || "";
    setBranchMappings(c.branches || [{ from: c.source_branch || "main", to: c.dest_branch || "main" }]);
    document.getElementById("f-ssh-key").value = "";
    document.getElementById("f-git-username").value = c.git_username || "";
    document.getElementById("f-git-password").value = "";

    const sshStatus = document.getElementById("ssh-key-status");
    if (c.has_ssh_key) {
      sshStatus.textContent = "SSH key is saved. Paste a new key to replace it, or leave blank to keep the existing one.";
      sshStatus.className = "key-status key-status-saved";
      sshStatus.classList.remove("hidden");
    } else {
      sshStatus.classList.add("hidden");
    }

    const pwBadge = document.getElementById("pw-saved-badge");
    if (c.has_git_password) {
      pwBadge.classList.remove("hidden");
    } else {
      pwBadge.classList.add("hidden");
    }

    const tab = c.has_git_password || c.git_username ? "https" : "ssh";
    switchAuthTab(tab);

    document.getElementById("modal-overlay").classList.remove("hidden");
  } catch (e) {
    toast("Failed to load config: " + e.message, "error");
  }
}

function closeModal() {
  document.getElementById("modal-overlay").classList.add("hidden");
}

function closeModalIfOutside(e) {
  if (e.target === document.getElementById("modal-overlay")) closeModal();
}

function setCron(val) {
  document.getElementById("f-schedule").value = val;
}

function toggleFormPw() {
  const el = document.getElementById("f-git-password");
  el.type = el.type === "password" ? "text" : "password";
}

async function submitConfigForm(e) {
  e.preventDefault();
  const btn = document.getElementById("form-submit-btn");
  const editId = document.getElementById("edit-id").value;

  const body = {
    name: document.getElementById("f-name").value.trim(),
    source_url: document.getElementById("f-source-url").value.trim(),
    dest_url: document.getElementById("f-dest-url").value.trim(),
    branches: getBranchMappings(),
    schedule: document.getElementById("f-schedule").value.trim() || null,
  };

  if (activeAuthTab === "ssh") {
    const key = document.getElementById("f-ssh-key").value.trim();
    if (key) body.ssh_key = key;
    else if (!editId) body.ssh_key = null;
    body.git_username = null;
    body.git_password = null;
  } else {
    body.git_username = document.getElementById("f-git-username").value.trim() || null;
    const pw = document.getElementById("f-git-password").value;
    if (pw) body.git_password = pw;
    body.ssh_key = null;
  }

  btn.disabled = true;
  btn.textContent = "Saving...";
  try {
    if (editId) {
      await apiFetch(`/configs/${editId}`, { method: "PUT", body: JSON.stringify(body) });
      toast("Configuration updated", "success");
    } else {
      await apiFetch("/configs", { method: "POST", body: JSON.stringify(body) });
      toast("Sync configuration created", "success");
    }
    closeModal();
    loadConfigs();
  } catch (e) {
    toast("Error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = editId ? "Save" : "Create";
  }
}

// Logs modal
async function openLogs(configId, name) {
  document.getElementById("logs-title").textContent = `Logs — ${name}`;
  document.getElementById("logs-overlay").classList.remove("hidden");
  document.getElementById("logs-list").innerHTML = `<div class="logs-empty">Loading…</div>`;
  try {
    const logs = await apiFetch(`/logs?config_id=${configId}`);
    renderLogs(logs);
  } catch (e) {
    document.getElementById("logs-list").innerHTML = `<div class="logs-empty">Failed to load: ${e.message}</div>`;
  }
}

function renderLogs(logs) {
  const list = document.getElementById("logs-list");
  if (!logs.length) {
    list.innerHTML = `<div class="logs-empty">No logs yet for this configuration.</div>`;
    return;
  }
  list.innerHTML = logs.map((l) => {
    const statusClass = l.status === "success" ? "status-success" : l.status === "error" ? "status-error" : "status-running";
    const duration = l.finished_at
      ? `${((new Date(l.finished_at + "Z") - new Date(l.started_at + "Z")) / 1000).toFixed(1)}s`
      : "running…";
    return `
    <div class="log-entry">
      <div class="log-entry-header" onclick="toggleLog(this)">
        <div class="log-entry-meta">
          <span class="status-badge ${statusClass}">${l.status}</span>
          <span>${new Date(l.started_at + "Z").toLocaleString()}</span>
          <span>${duration}</span>
        </div>
        <svg class="log-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
      </div>
      <pre class="log-output">${escHtml(l.output || "(no output)")}</pre>
    </div>`;
  }).join("");
}

function toggleLog(header) {
  const output = header.nextElementSibling;
  const chevron = header.querySelector(".log-chevron");
  output.classList.toggle("expanded");
  chevron.classList.toggle("open");
}

function closeLogs() { document.getElementById("logs-overlay").classList.add("hidden"); }
function closeLogsIfOutside(e) { if (e.target === document.getElementById("logs-overlay")) closeLogs(); }

// User menu
function toggleUserMenu() {
  document.getElementById("user-dropdown").classList.toggle("hidden");
}
document.addEventListener("click", (e) => {
  const menu = document.getElementById("user-menu");
  if (menu && !menu.contains(e.target)) {
    document.getElementById("user-dropdown").classList.add("hidden");
  }
});

async function doLogout() {
  try {
    await apiFetch("/auth/logout", { method: "POST" });
  } catch (_) {}
  window.location.href = "/login";
}

// Change password modal
function openChangePassword() {
  document.getElementById("user-dropdown").classList.add("hidden");
  document.getElementById("pw-form").reset();
  document.getElementById("pw-error").classList.add("hidden");
  document.getElementById("pw-overlay").classList.remove("hidden");
}

function closePw() { document.getElementById("pw-overlay").classList.add("hidden"); }
function closePwIfOutside(e) { if (e.target === document.getElementById("pw-overlay")) closePw(); }

async function submitPasswordChange(e) {
  e.preventDefault();
  const btn = document.getElementById("pw-submit-btn");
  const errEl = document.getElementById("pw-error");
  errEl.classList.add("hidden");
  const current = document.getElementById("pw-current").value;
  const newPw = document.getElementById("pw-new").value;
  const confirm = document.getElementById("pw-confirm").value;
  if (newPw !== confirm) {
    errEl.textContent = "New passwords do not match.";
    errEl.classList.remove("hidden");
    return;
  }
  btn.disabled = true;
  btn.textContent = "Updating…";
  try {
    await apiFetch("/auth/password", {
      method: "PUT",
      body: JSON.stringify({ current_password: current, new_password: newPw }),
    });
    toast("Password updated successfully", "success");
    closePw();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Update Password";
  }
}

// ── Webhook modal ────────────────────────────────────────────────────────────
let _whConfigId = null;

async function openWebhookModal(configId, name) {
  _whConfigId = configId;
  document.getElementById("wh-title").textContent = `Webhook — ${name}`;
  document.getElementById("wh-overlay").classList.remove("hidden");
  document.getElementById("wh-secret-input").value = "";
  try {
    const info = await apiFetch(`/configs/${configId}/webhook`);
    const fullUrl = window.location.origin + info.webhook_url;
    document.getElementById("wh-url").textContent = fullUrl;
    document.getElementById("wh-secret-input").value = info.secret || "";
    document.getElementById("wh-secret-input").type = "password";
  } catch (e) {
    toast("Failed to load webhook info: " + e.message, "error");
  }
}

function closeWh() { document.getElementById("wh-overlay").classList.add("hidden"); }
function closeWhIfOutside(e) { if (e.target === document.getElementById("wh-overlay")) closeWh(); }

function switchPlatform(p) {
  ["github", "gitlab", "generic"].forEach((id) => {
    document.getElementById(`instr-${id}`).classList.toggle("hidden", id !== p);
    document.getElementById(`ptab-${id}`).classList.toggle("active", id === p);
  });
}

function toggleWhSecret() {
  const el = document.getElementById("wh-secret-input");
  el.type = el.type === "password" ? "text" : "password";
}

async function generateWhSecret() {
  if (!_whConfigId) return;
  const btn = document.querySelector("#wh-overlay .btn[onclick='generateWhSecret()']");
  if (btn) btn.disabled = true;
  try {
    const res = await apiFetch(`/configs/${_whConfigId}/webhook`, {
      method: "PUT",
      body: JSON.stringify({ action: "generate" }),
    });
    document.getElementById("wh-secret-input").value = res.secret;
    document.getElementById("wh-secret-input").type = "text";
    toast("New secret generated — click Save to apply", "info");
  } catch (e) {
    toast("Error: " + e.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveWhSecret() {
  if (!_whConfigId) return;
  const secret = document.getElementById("wh-secret-input").value.trim();
  const btn = document.getElementById("wh-save-btn");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    await apiFetch(`/configs/${_whConfigId}/webhook`, {
      method: "PUT",
      body: JSON.stringify({ action: "save", secret }),
    });
    toast("Webhook secret saved", "success");
    loadConfigs();
  } catch (e) {
    toast("Error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Save Secret";
  }
}

async function clearWhSecret() {
  if (!_whConfigId) return;
  if (!confirm("Remove the webhook secret? All incoming webhook requests will be accepted without validation.")) return;
  try {
    await apiFetch(`/configs/${_whConfigId}/webhook`, {
      method: "PUT",
      body: JSON.stringify({ action: "clear" }),
    });
    document.getElementById("wh-secret-input").value = "";
    toast("Webhook secret cleared", "info");
    loadConfigs();
  } catch (e) {
    toast("Error: " + e.message, "error");
  }
}

async function copyWebhookUrl() {
  const url = document.getElementById("wh-url").textContent;
  await navigator.clipboard.writeText(url).catch(() => {});
  const btn = document.getElementById("copy-url-btn");
  const orig = btn.innerHTML;
  btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
  setTimeout(() => { btn.innerHTML = orig; }, 2000);
}

async function copyWhSecret() {
  const val = document.getElementById("wh-secret-input").value;
  if (!val) { toast("No secret to copy", "info"); return; }
  await navigator.clipboard.writeText(val).catch(() => {});
  const btn = document.getElementById("copy-secret-btn");
  const orig = btn.innerHTML;
  btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
  setTimeout(() => { btn.innerHTML = orig; }, 2000);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeModal(); closeLogs(); closePw(); closeWh(); closeSettings(); }
});

document.getElementById("s-default-source").addEventListener("input", updateSourcePreview);
document.getElementById("s-default-dest").addEventListener("input", updateDestPreview);

// ── Current user ──────────────────────────────────────────────────────────────
let _currentUser = null;

async function initUser() {
  try {
    _currentUser = await apiFetch("/auth/me");
    const el = document.getElementById("user-label");
    if (el) el.textContent = _currentUser.username;
    // Show admin badge in dropdown
    const info = document.getElementById("dropdown-info");
    if (info) {
      info.innerHTML = `
        <div class="dropdown-user-info">
          <span class="dropdown-username">${escHtml(_currentUser.username)}</span>
          ${_currentUser.is_admin ? '<span class="admin-badge">Admin</span>' : ''}
        </div>`;
    }
  } catch (_) {}
}

// ── User management ────────────────────────────────────────────────────────────
async function loadUsers() {
  const list = document.getElementById("users-list");
  if (!list) return;
  list.innerHTML = '<div class="users-loading">Loading…</div>';
  try {
    const users = await apiFetch("/users");
    if (users.length === 0) {
      list.innerHTML = '<div class="users-empty">No users found.</div>';
      return;
    }
    list.innerHTML = users.map(u => `
      <div class="user-row" id="urow-${u.id}">
        <div class="user-row-info">
          <span class="user-row-name">${escHtml(u.username)}</span>
          ${u.is_admin ? '<span class="admin-badge">Admin</span>' : ''}
          ${u.id === _currentUser?.user_id ? '<span class="you-badge">you</span>' : ''}
        </div>
        <div class="user-row-actions">
          <button class="btn btn-ghost btn-xs user-role-btn"
            onclick="toggleUserRole(${u.id}, ${u.is_admin ? 0 : 1})"
            ${u.id === _currentUser?.user_id ? 'disabled title="Cannot change your own role"' : ''}
            title="${u.is_admin ? 'Demote to regular user' : 'Promote to admin'}">
            ${u.is_admin ? 'Demote' : 'Make admin'}
          </button>
          <button class="btn btn-ghost btn-xs user-delete-btn"
            onclick="deleteUser(${u.id})"
            ${u.id === _currentUser?.user_id ? 'disabled title="Cannot delete your own account"' : ''}
            title="Delete user">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
          </button>
        </div>
      </div>
    `).join("");
  } catch (e) {
    list.innerHTML = `<div class="users-empty">Failed to load users: ${escHtml(e.message)}</div>`;
  }
}

async function deleteUser(uid) {
  if (!confirm("Delete this user? This cannot be undone.")) return;
  try {
    await apiFetch(`/users/${uid}`, { method: "DELETE" });
    toast("User deleted", "success");
    loadUsers();
  } catch (e) {
    toast("Failed to delete: " + e.message, "error");
  }
}

async function toggleUserRole(uid, makeAdmin) {
  try {
    await apiFetch(`/users/${uid}/role`, {
      method: "PUT",
      body: JSON.stringify({ is_admin: makeAdmin }),
    });
    toast(makeAdmin ? "User promoted to admin" : "User demoted", "success");
    loadUsers();
  } catch (e) {
    toast("Failed: " + e.message, "error");
  }
}

setInterval(loadConfigs, 10000);
initUser();
loadSettings().then(loadConfigs);
