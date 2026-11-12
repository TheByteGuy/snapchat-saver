/* Snapchat Saver dashboard – vanilla JS, no build step. */
(function () {
  "use strict";

  const BOOT = JSON.parse(document.getElementById("boot").textContent || "{}");
  const CSRF = BOOT.csrf || document.querySelector('meta[name="csrf-token"]').content;
  const $ = (sel) => document.querySelector(sel);

  let recipients = [];
  let timezones = [];
  let currentTz = "";
  let sendPoll = null;

  // ------------------------------------------------------------------ helpers

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else if (v === true) node.setAttribute(k, "");
        else node.setAttribute(k, v);
      }
    }
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function toast(message, kind) {
    const t = el("div", { class: "toast toast-" + (kind || "info"), role: kind === "err" ? "alert" : "status" }, message);
    $("#toasts").append(t);
    setTimeout(() => t.remove(), kind === "err" ? 8000 : 4500);
  }

  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data; }
  }

  async function api(method, url, body) {
    const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (method !== "GET") opts.headers["X-CSRF-Token"] = CSRF;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new ApiError("Cannot reach the dashboard server. Is it running?", 0, null);
    }
    if (res.status === 401) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname);
      throw new ApiError("Session expired – please log in again.", 401, null);
    }
    let data = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) data = await res.json().catch(() => null);
    if (!res.ok) {
      const msg = (data && (data.error || data.detail)) || ("Request failed (HTTP " + res.status + ")");
      throw new ApiError(typeof msg === "string" ? msg : JSON.stringify(msg), res.status, data);
    }
    return data;
  }

  async function withBusy(btn, fn) {
    if (btn) { btn.disabled = true; btn.classList.add("loading"); }
    try { return await fn(); }
    catch (e) { toast(e.message || String(e), "err"); }
    finally { if (btn) { btn.disabled = false; btn.classList.remove("loading"); } }
  }

  function fmtDateTime(iso, tz) {
    if (!iso) return "–";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const opts = { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
    if (tz) { opts.timeZone = tz; opts.timeZoneName = "short"; }
    try { return d.toLocaleString(undefined, opts); }
    catch (e) { delete opts.timeZone; delete opts.timeZoneName; return d.toLocaleString(undefined, opts); }
  }

  function statusBadge(status) {
    const map = { success: ["badge-ok", "Success"], partial: ["badge-warn", "Partial"], failed: ["badge-err", "Failed"] };
    const [cls, label] = map[status] || ["badge-unknown", status || "Unknown"];
    return el("span", { class: "badge " + cls }, label);
  }

  function resultsList(results) {
    const ul = el("ul", { class: "results" });
    for (const [name, r] of Object.entries(results || {})) {
      const ok = r && r.ok;
      ul.append(el("li", null,
        el("span", { class: "badge " + (ok ? "badge-ok" : "badge-err") }, ok ? "Sent" : "Failed"),
        el("span", null, name),
        !ok && r && r.error ? el("span", { class: "r-err" }, r.error) : null));
    }
    return ul;
  }

  // ------------------------------------------------------------------ status

  function renderSnapStatus(snap) {
    const badge = $("#st-login");
    const pill = $("#top-pill");
    let cls = "unknown", label = "Unknown";
    if (snap && snap.logged_in === true) { cls = "ok"; label = "Logged in"; }
    else if (snap && snap.logged_in === false) { cls = "err"; label = "Logged out"; }
    badge.className = "badge badge-" + cls;
    badge.textContent = label;
    pill.className = "pill pill-" + cls;
    pill.textContent = "Snapchat: " + label.toLowerCase();
    $("#st-browser").textContent = snap && snap.running ? "Running" : "Stopped";
    const err = $("#st-error");
    if (snap && snap.last_error) { err.textContent = "Last error: " + snap.last_error; err.hidden = false; }
    else err.hidden = true;
  }

  async function loadStatus() {
    try {
      const s = await api("GET", "/api/status");
      renderSnapStatus(s.snapchat);
      const enabled = s.settings && s.settings.enabled;
      $("#st-next").textContent = !enabled ? "Paused" : (s.next_run ? fmtDateTime(s.next_run, s.settings.timezone) : "Not scheduled");
      const today = $("#st-today");
      today.className = "badge " + (s.sent_today ? "badge-ok" : "badge-unknown");
      today.textContent = s.sent_today ? "Yes" : "Not yet";
      if (s.send_test && s.send_test.running && !sendPoll) startSendPolling();
      return s;
    } catch (e) {
      if (e.status !== 401) {
        $("#top-pill").className = "pill pill-warn";
        $("#top-pill").textContent = "Offline";
      }
    }
  }

  // ------------------------------------------------------------------ snapchat login / viewer

  function vncUrl() {
    const page = BOOT.novnc_page || "vnc.html";
    const p = new URLSearchParams({
      autoconnect: "1", reconnect: "1", reconnect_delay: "2000",
      resize: "scale", scale: "true", path: "websockify",
      host: location.hostname, encrypt: location.protocol === "https:" ? "1" : "0",
    });
    if (location.port) p.set("port", location.port);
    return "/novnc/" + page + "?" + p.toString();
  }

  function setupViewer() {
    const link = $("#link-full");
    if (!BOOT.novnc_page) {
      $("#viewer-msg").textContent = "The live browser view is unavailable: noVNC files were not found in " +
        (BOOT.novnc_dir || "NOVNC_DIR") + ". The Docker image includes them; outside Docker install the novnc package " +
        "or set NOVNC_DIR. You can still take screenshots below.";
      $("#btn-toggle-view").hidden = true;
      return;
    }
    link.href = vncUrl();
    link.hidden = false;
  }

  function showViewer(show) {
    const viewer = $("#viewer");
    const btn = $("#btn-toggle-view");
    const existing = viewer.querySelector("iframe");
    if (show && !existing && BOOT.novnc_page) {
      $("#viewer-placeholder").hidden = true;
      viewer.append(el("iframe", { src: vncUrl(), title: "Snapchat browser (remote view)", allow: "clipboard-read; clipboard-write" }));
      btn.textContent = "Hide browser view";
      btn.setAttribute("aria-expanded", "true");
    } else if (!show && existing) {
      existing.remove();
      $("#viewer-placeholder").hidden = false;
      btn.textContent = "Show browser view";
      btn.setAttribute("aria-expanded", "false");
    }
  }

  async function openLogin(btn) {
    await withBusy(btn, async () => {
      toast("Opening Snapchat in the browser… this can take a few seconds.");
      const r = await api("POST", "/api/snapchat/open-login");
      if (r.snapchat) renderSnapStatus(r.snapchat);
      showViewer(true);
      toast("Snapchat login page opened. Log in in the browser view.", "ok");
    });
  }

  async function checkLogin(btn) {
    await withBusy(btn, async () => {
      const r = await api("POST", "/api/snapchat/check-login");
      if (r.snapchat) renderSnapStatus(Object.assign({}, r.snapchat, { logged_in: r.logged_in }));
      toast(r.logged_in ? "Snapchat is logged in." : "Snapchat is NOT logged in.", r.logged_in ? "ok" : "err");
    });
  }

  async function takeScreenshot(btn) {
    await withBusy(btn, async () => {
      const res = await fetch("/api/snapchat/screenshot", { credentials: "same-origin" });
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        throw new Error((d && d.error) || "Screenshot failed (HTTP " + res.status + ")");
      }
      const blob = await res.blob();
      const img = $("#screenshot-img");
      if (img.src && img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(blob);
      img.hidden = false;
    });
  }

  // ------------------------------------------------------------------ recipients

  function renderRecipients() {
    const list = $("#recipient-list");
    list.replaceChildren();
    for (const r of recipients) {
      const toggle = el("input", {
        type: "checkbox", class: "switch", role: "switch",
        "aria-label": (r.enabled ? "Disable " : "Enable ") + r.name,
        title: r.enabled ? "Enabled" : "Disabled",
      });
      toggle.checked = !!r.enabled;
      toggle.addEventListener("change", () => toggleRecipient(r, toggle));
      const remove = el("button", { type: "button", class: "btn btn-sm btn-danger", "aria-label": "Remove " + r.name }, "Remove");
      remove.addEventListener("click", () => removeRecipient(r, remove));
      list.append(el("li", null, el("span", { class: "name" + (r.enabled ? "" : " disabled") }, r.name), toggle, remove));
    }
    $("#recipient-empty").hidden = recipients.length > 0;
    renderSendTestChoices();
  }

  async function loadRecipients() {
    try {
      const d = await api("GET", "/api/recipients");
      recipients = d.recipients || [];
      renderRecipients();
    } catch (e) { toast(e.message, "err"); }
  }

  async function addRecipients(names, btn) {
    return withBusy(btn, async () => {
      const d = await api("POST", "/api/recipients", names.length === 1 ? { name: names[0] } : { names });
      recipients = d.recipients || recipients;
      renderRecipients();
      toast(names.length === 1 ? "Added " + names[0] + "." : "Added " + names.length + " recipients.", "ok");
      return true;
    });
  }

  async function toggleRecipient(r, input) {
    input.disabled = true;
    try {
      const d = await api("PATCH", "/api/recipients/" + r.id, { enabled: input.checked });
      recipients = d.recipients || recipients;
      renderRecipients();
    } catch (e) {
      input.checked = !input.checked;
      toast(e.message, "err");
    } finally { input.disabled = false; }
  }

  async function removeRecipient(r, btn) {
    if (!confirm("Remove " + r.name + " from the recipients?")) return;
    await withBusy(btn, async () => {
      const d = await api("DELETE", "/api/recipients/" + r.id);
      recipients = d.recipients || [];
      renderRecipients();
      toast("Removed " + r.name + ".", "ok");
    });
  }

  let friends = [];
  function renderFriends() {
    const q = $("#friends-filter").value.trim().toLowerCase();
    const list = $("#friends-list");
    list.replaceChildren();
    const shown = friends.filter((f) => !q || f.name.toLowerCase().includes(q));
    for (const f of shown) {
      const cb = el("input", { type: "checkbox", value: f.name, disabled: f.added });
      cb.checked = !!f.checked;
      cb.addEventListener("change", () => { f.checked = cb.checked; });
      list.append(el("li", null, el("label", null, cb, el("span", null, f.name),
        f.added ? el("span", { class: "badge badge-info badge-plain tag" }, "added") : null)));
    }
    if (!shown.length) list.append(el("li", { class: "muted small" }, friends.length ? "No match." : "No friends found. Make sure Snapchat is logged in, or add names manually."));
  }

  async function loadFriends(btn) {
    await withBusy(btn, async () => {
      toast("Loading friends from Snapchat… this can take a while.");
      const d = await api("GET", "/api/friends");
      friends = (d.friends || []).map((f) => ({ name: f.name, added: f.added, checked: false }));
      $("#friends-panel").hidden = false;
      $("#friends-filter").value = "";
      renderFriends();
    });
  }

  // ------------------------------------------------------------------ settings

  function renderTzOptions() {
    const q = $("#tz-search").value.trim().toLowerCase().replace(/\s+/g, "_");
    const sel = $("#set-tz");
    sel.replaceChildren();
    let list = q ? timezones.filter((z) => z.toLowerCase().includes(q)) : timezones;
    if (currentTz && !q) list = [currentTz].concat(list.filter((z) => z !== currentTz));
    for (const z of list.slice(0, 400)) {
      const o = el("option", { value: z }, z.replace(/_/g, " "));
      if (z === currentTz) o.selected = true;
      sel.append(o);
    }
  }

  function applySettings(s) {
    $("#set-enabled").checked = !!s.enabled;
    $("#set-time").value = s.send_time || "09:00";
    currentTz = s.timezone || "UTC";
    $("#tz-current").textContent = currentTz;
    $("#set-webhook").value = s.discord_webhook_url || "";
    $("#set-notify-success").checked = !!s.notify_on_success;
    renderTzOptions();
  }

  async function loadSettings() {
    try {
      const [s, tz] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/timezones")]);
      timezones = tz.timezones || [];
      applySettings(s.settings);
    } catch (e) { toast(e.message, "err"); }
  }

  async function saveSettings(patch, btn, okMsg) {
    await withBusy(btn, async () => {
      const d = await api("PUT", "/api/settings", patch);
      applySettings(d.settings);
      toast(okMsg, "ok");
      loadStatus();
    });
  }

  // ------------------------------------------------------------------ preview / alerts

  async function generatePreview(btn) {
    await withBusy(btn, async () => {
      const d = await api("POST", "/api/preview");
      $("#preview").hidden = false;
      $("#preview-fact").textContent = d.fact || "";
      if (d.image_url) {
        $("#preview-img").src = d.image_url + (d.image_url.includes("?") ? "&" : "?") + "t=" + Date.now();
        $("#preview-link").href = d.image_url;
      }
      toast("Preview ready.", "ok");
    });
  }

  // ------------------------------------------------------------------ send test

  function renderSendTestChoices() {
    const list = $("#sendtest-list");
    const prev = new Set([...list.querySelectorAll("input:checked")].map((i) => i.value));
    const first = list.childElementCount === 0;
    list.replaceChildren();
    for (const r of recipients) {
      const cb = el("input", { type: "checkbox", value: r.name });
      cb.checked = first ? !!r.enabled : prev.has(r.name);
      list.append(el("li", null, el("label", null, cb, el("span", null, r.name),
        r.enabled ? null : el("span", { class: "badge badge-unknown badge-plain tag" }, "disabled"))));
    }
    $("#sendtest-empty").hidden = recipients.length > 0;
  }

  function renderSendProgress(st) {
    const box = $("#sendtest-progress");
    const btn = $("#btn-send-test");
    if (!st || st.state === "idle") { box.hidden = true; btn.disabled = false; return; }
    box.hidden = false;
    box.replaceChildren();
    if (st.running) {
      btn.disabled = true;
      box.append(
        el("div", null, el("b", null, "Sending… "), Math.round(st.elapsed || 0) + "s elapsed"),
        el("div", { class: "bar" }, el("span")),
        el("div", { class: "muted small" }, "To: " + (st.recipients || []).join(", ") +
          ". Generating the image and sending through Snapchat can take a few minutes."));
      return;
    }
    btn.disabled = false;
    if (st.state === "error") {
      box.append(el("div", null, el("span", { class: "badge badge-err" }, "Error"), " ", st.error || "Unknown error"));
      return;
    }
    const r = st.result || {};
    box.append(el("div", { class: "meta" }, statusBadge(r.status), " ",
      el("span", { class: "muted small" }, "finished in " + Math.round(st.elapsed || 0) + "s")));
    if (r.error) box.append(el("p", { class: "error-text" }, r.error));
    if (r.fact) box.append(el("p", { class: "small" }, r.fact));
    box.append(resultsList(r.results));
  }

  async function pollSend() {
    try {
      const st = await api("GET", "/api/send-test/status");
      renderSendProgress(st);
      if (!st.running) {
        stopSendPolling();
        if (st.state === "done") {
          const status = st.result && st.result.status;
          const err = st.result && st.result.error;
          toast("Test send finished: " + (status || "done") + (err ? " – " + err : "") + ".", status === "success" ? "ok" : "err");
        } else if (st.state === "error") toast("Test send failed: " + st.error, "err");
        loadHistory();
        loadStatus();
      }
    } catch (e) { /* transient; keep polling */ }
  }

  function startSendPolling() {
    stopSendPolling();
    sendPoll = setInterval(pollSend, 2000);
    pollSend();
  }
  function stopSendPolling() { if (sendPoll) clearInterval(sendPoll); sendPoll = null; }

  async function sendTest(btn) {
    const chosen = [...$("#sendtest-list").querySelectorAll("input:checked")].map((i) => i.value);
    if (!chosen.length) { toast("Tick at least one recipient.", "err"); return; }
    if (!confirm("Send a real snap now to: " + chosen.join(", ") + "?")) return;
    await withBusy(btn, async () => {
      const d = await api("POST", "/api/send-test", { recipients: chosen });
      renderSendProgress(d.send_test);
      startSendPolling();
    });
  }

  // ------------------------------------------------------------------ history

  async function loadHistory() {
    try {
      const d = await api("GET", "/api/history?limit=30");
      const list = $("#history-list");
      list.replaceChildren();
      for (const h of d.history || []) {
        const thumb = h.image_url
          ? el("a", { href: h.image_url, target: "_blank", rel: "noopener" }, el("img", { class: "thumb", src: h.image_url, alt: "Image sent on " + h.date, loading: "lazy" }))
          : el("div", { class: "thumb", "aria-hidden": "true" });
        const results = h.results || {};
        const n = Object.keys(results).length;
        const okN = Object.values(results).filter((r) => r && r.ok).length;
        list.append(el("li", null, thumb, el("div", null,
          el("div", { class: "meta" },
            el("span", { class: "date" }, h.date),
            statusBadge(h.status),
            h.test ? el("span", { class: "badge badge-info badge-plain" }, "test") : null,
            el("span", { class: "muted small" }, fmtDateTime(h.created_at))),
          el("p", { class: "fact" }, h.fact || ""),
          n ? el("details", null, el("summary", null, okN + "/" + n + " recipients"), resultsList(results)) : null)));
      }
      $("#history-empty").hidden = (d.history || []).length > 0;
    } catch (e) { toast(e.message, "err"); }
  }

  // ------------------------------------------------------------------ wiring

  function wire() {
    $("#btn-check-login").addEventListener("click", (e) => checkLogin(e.currentTarget));
    $("#btn-open-login").addEventListener("click", (e) => openLogin(e.currentTarget));
    $("#btn-toggle-view").addEventListener("click", () => showViewer(!$("#viewer iframe")));
    $("#btn-screenshot").addEventListener("click", (e) => takeScreenshot(e.currentTarget));

    $("#btn-friends").addEventListener("click", (e) => loadFriends(e.currentTarget));
    $("#friends-filter").addEventListener("input", renderFriends);
    $("#btn-close-friends").addEventListener("click", () => { $("#friends-panel").hidden = true; });
    $("#btn-add-friends").addEventListener("click", async (e) => {
      const names = friends.filter((f) => f.checked && !f.added).map((f) => f.name);
      if (!names.length) { toast("Tick at least one friend.", "err"); return; }
      if (await addRecipients(names, e.currentTarget)) {
        for (const f of friends) if (f.checked) { f.added = true; f.checked = false; }
        renderFriends();
      }
    });
    $("#add-recipient-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("#recipient-name");
      const name = input.value.trim();
      if (!name) { toast("Type a name first.", "err"); input.focus(); return; }
      if (await addRecipients([name], e.submitter || null)) input.value = "";
    });

    $("#tz-search").addEventListener("input", renderTzOptions);
    $("#set-tz").addEventListener("change", (e) => { currentTz = e.target.value; $("#tz-current").textContent = currentTz; });
    $("#schedule-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const time = $("#set-time").value;
      if (!/^\d{2}:\d{2}$/.test(time)) { toast("Pick a valid send time.", "err"); return; }
      saveSettings({ enabled: $("#set-enabled").checked, send_time: time, timezone: currentTz },
        e.submitter || null, "Schedule saved.");
    });
    $("#alerts-form").addEventListener("submit", (e) => {
      e.preventDefault();
      saveSettings({ discord_webhook_url: $("#set-webhook").value.trim(), notify_on_success: $("#set-notify-success").checked },
        e.submitter || null, "Alert settings saved.");
    });
    $("#btn-test-alert").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
      await api("POST", "/api/notify-test");
      toast("Test alert sent to Discord.", "ok");
    }));

    $("#btn-preview").addEventListener("click", (e) => generatePreview(e.currentTarget));
    $("#btn-send-test").addEventListener("click", (e) => sendTest(e.currentTarget));
    $("#btn-history").addEventListener("click", loadHistory);
  }

  wire();
  setupViewer();
  loadStatus();
  loadRecipients();
  loadSettings();
  loadHistory();
  api("GET", "/api/send-test/status").then((st) => {
    renderSendProgress(st);
    if (st && st.running) startSendPolling();
  }).catch(() => {});
  setInterval(() => { if (!document.hidden) loadStatus(); }, 30000);
})();
