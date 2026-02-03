/* ping-webcrawler UI controller (2026 UI/UX)
   - Theme toggle (system default + localStorage override)
   - Start/Stop crawl via JSON API
   - Live progress via SSE EventSource
   - Results polling/refresh + table filter
*/

(function () {
  const $ = (id) => document.getElementById(id);

  // Theme
  const themeToggle = $("themeToggle");
  const THEME_KEY = "pwc-theme"; // "light" | "dark" | null
  function applyTheme(theme) {
    const html = document.documentElement;
    if (theme === "light" || theme === "dark") html.setAttribute("data-theme", theme);
    else html.removeAttribute("data-theme");
  }
  const savedTheme = localStorage.getItem(THEME_KEY);
  applyTheme(savedTheme);

  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "system";
    const next = current === "system" ? "dark" : current === "dark" ? "light" : "system";
    if (next === "system") {
      localStorage.removeItem(THEME_KEY);
      applyTheme(null);
    } else {
      localStorage.setItem(THEME_KEY, next);
      applyTheme(next);
    }
    toast(`Theme: ${next}`);
  });

  // UI elements
  const form = $("crawlForm");
  const startBtn = $("startBtn");
  const stopBtn = $("stopBtn");
  const downloadBtn = $("downloadBtn");
  const statusPill = $("statusPill");
  const statusText = $("statusText");
  const logEl = $("log");
  const clearLogBtn = $("clearLogBtn");
  const pauseLogBtn = $("pauseLogBtn");
  const refreshBtn = $("refreshBtn");
  const resultsBody = $("resultsBody");
  const resultsMeta = $("resultsMeta");
  const filterInput = $("filterInput");

  let es = null;
  let paused = false;
  let lastItems = [];
  let pollTimer = null;

  // Minimal toast
  let toastTimer = null;
  function toast(msg) {
    clearTimeout(toastTimer);
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      el.style.position = "fixed";
      el.style.left = "50%";
      el.style.bottom = "18px";
      el.style.transform = "translateX(-50%)";
      el.style.padding = "10px 12px";
      el.style.borderRadius = "999px";
      el.style.border = "1px solid var(--border)";
      el.style.background = "color-mix(in oklab, var(--surface) 86%, transparent)";
      el.style.boxShadow = "var(--shadow)";
      el.style.fontSize = "13px";
      el.style.zIndex = "9999";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = "1";
    toastTimer = setTimeout(() => (el.style.opacity = "0"), 1800);
  }

  // Status pill
  function setStatus(mode, text) {
    statusPill.className = "pill";
    if (mode === "run") statusPill.classList.add("pill--run");
    if (mode === "ok") statusPill.classList.add("pill--ok");
    if (mode === "stop") statusPill.classList.add("pill--stop");

    statusPill.textContent =
      mode === "run" ? "Running" :
      mode === "stop" ? "Stopped" :
      mode === "ok" ? "Done" : "Idle";

    statusText.textContent = text || "";
  }

  // Log
  function appendLog(line) {
    if (paused) return;
    const div = document.createElement("div");
    div.className = "logLine";
    div.textContent = line;
    logEl.appendChild(div);

    // cap DOM nodes
    while (logEl.childNodes.length > 400) logEl.removeChild(logEl.firstChild);

    logEl.scrollTop = logEl.scrollHeight;
  }

  clearLogBtn.addEventListener("click", () => {
    logEl.innerHTML = "";
    toast("Log cleared");
  });

  pauseLogBtn.addEventListener("click", () => {
    paused = !paused;
    pauseLogBtn.setAttribute("aria-pressed", paused ? "true" : "false");
    pauseLogBtn.textContent = paused ? "Resume" : "Pause";
    toast(paused ? "Log paused" : "Log resumed");
  });

  // Table rendering
  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[c]));
  }

  function msFromSec(sec) {
    if (sec === null || sec === undefined || sec === "") return "";
    const n = Number(sec);
    if (!Number.isFinite(n)) return "";
    return `${Math.round(n * 1000)} ms`;
  }

  function renderTable(items) {
    const q = (filterInput.value || "").trim().toLowerCase();
    const filtered = !q ? items : items.filter((it) =>
      (it.URL || "").toLowerCase().includes(q) ||
      (it.title || "").toLowerCase().includes(q)
    );

    resultsMeta.textContent = `${filtered.length} shown • ${items.length} total`;

    if (!filtered.length) {
      resultsBody.innerHTML = `<tr><td class="muted" colspan="4">No matching results.</td></tr>`;
      return;
    }

    resultsBody.innerHTML = filtered.map((it) => {
      const url = it.URL || "";
      const title = it.title || "";
      const desc = it.description || "";
      const rtt = msFromSec(it.response_time_sec);

      return `
        <tr>
          <td class="urlCell">
            <a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>
            <div class="small">
              <button class="btn btn--ghost btn--sm copyBtn" data-url="${escapeHtml(url)}" type="button">Copy</button>
            </div>
          </td>
          <td><span class="small">${escapeHtml(rtt)}</span></td>
          <td>${escapeHtml(title).slice(0, 140)}</td>
          <td>${escapeHtml(desc).slice(0, 180)}</td>
        </tr>
      `;
    }).join("");

    // copy handlers
    document.querySelectorAll(".copyBtn").forEach((b) => {
      b.addEventListener("click", async () => {
        const u = b.getAttribute("data-url") || "";
        try {
          await navigator.clipboard.writeText(u);
          toast("Copied URL");
        } catch {
          toast("Copy failed");
        }
      });
    });
  }

  filterInput.addEventListener("input", () => renderTable(lastItems));

  async function fetchResults(limit = 80) {
    const r = await fetch(`/api/results?limit=${encodeURIComponent(String(limit))}`, { cache: "no-store" });
    const j = await r.json();
    if (!j.ok) return;
    // newest-first already; keep as-is
    lastItems = j.items || [];
    renderTable(lastItems);
  }

  refreshBtn.addEventListener("click", () => fetchResults().catch(() => toast("Refresh failed")));

  // Start/Stop
  function setControlsRunning(running) {
    startBtn.disabled = !!running;
    stopBtn.disabled = !running;
  }

  function setDownloadEnabled(enabled) {
    downloadBtn.setAttribute("aria-disabled", enabled ? "false" : "true");
    if (!enabled) downloadBtn.href = "javascript:void(0)";
    else downloadBtn.href = "/download";
  }

  async function fetchState() {
    const r = await fetch("/api/state", { cache: "no-store" });
    const j = await r.json();
    if (!j.ok) return null;
    return j.state;
  }

  function connectSSE() {
    if (es) es.close();
    es = new EventSource("/progress");

    es.addEventListener("log", (e) => {
      try {
        const data = JSON.parse(e.data);
        appendLog(data.message || "");
      } catch {
        appendLog(String(e.data || ""));
      }
    });

    es.addEventListener("state", (e) => {
      try {
        const s = JSON.parse(e.data);

        const running = !!s.running;
        const done = !!s.done;
        const stopped = !!s.stopped;

        setControlsRunning(running);
        setDownloadEnabled(!!s.csv_ready);

        if (running) setStatus("run", `${s.count}/${s.max_urls} extracted • run_id=${s.run_id || "-"}`);
        else if (done && !stopped) setStatus("ok", `${s.count} extracted • finished`);
        else if (stopped) setStatus("stop", `${s.count} extracted • stopped`);
        else setStatus("idle", "Ready.");

        // When finished, do a final refresh
        if (done && !running) {
          fetchResults().catch(() => {});
          stopPolling();
        }
      } catch {
        // ignore
      }
    });

    es.onerror = () => {
      // Do not spam. Just show once, allow automatic browser retry.
      setStatus("stop", "SSE disconnected; attempting to reconnect…");
    };
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      fetchResults().catch(() => {});
    }, 2000);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();

    const urls = ($("urls").value || "").trim();
    if (!urls) {
      toast("Paste at least one URL");
      return;
    }

    setControlsRunning(true);
    setStatus("run", "Starting…");
    appendLog("— starting —");

    const payload = {
      urls,
      MAX_DEPTH: Number($("MAX_DEPTH").value || 3),
      MAX_URLS: Number($("MAX_URLS").value || 100),
      REQUEST_TIMEOUT: Number($("REQUEST_TIMEOUT").value || 5),
      WORKERS: Number($("WORKERS").value || 10),
      MAX_CHARS: Number($("MAX_CHARS").value || 800),
      SAME_HOST_ONLY: $("SAME_HOST_ONLY").checked,
      FLUSH_INTERVAL_SEC: 5
    };

    try {
      const r = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      const j = await r.json();
      if (!r.ok || !j.ok) {
        setControlsRunning(false);
        setStatus("stop", j.error || "Failed to start.");
        toast(j.error || "Start failed");
        return;
      }

      connectSSE();
      startPolling();
      toast("Crawl started");

    } catch (e) {
      setControlsRunning(false);
      setStatus("stop", "Start failed (network error).");
      toast("Network error");
    }
  });

  stopBtn.addEventListener("click", async () => {
    try {
      const r = await fetch("/stop", { method: "POST" });
      const j = await r.json();
      if (j.ok) {
        toast("Stop requested");
        setStatus("stop", "Stopping…");
      }
    } catch {
      toast("Stop failed");
    }
  });

  // Boot
  (async function init() {
    connectSSE();
    const st = await fetchState().catch(() => null);
    if (st) {
      setDownloadEnabled(!!st.csv_ready);
      if (st.running) {
        setControlsRunning(true);
        setStatus("run", `${st.count}/${st.max_urls} extracted • run_id=${st.run_id || "-"}`);
        startPolling();
      } else if (st.done && !st.stopped) {
        setStatus("ok", `${st.count} extracted • finished`);
      } else if (st.stopped) {
        setStatus("stop", `${st.count} extracted • stopped`);
      } else {
        setStatus("idle", "Ready.");
      }
    }
    fetchResults().catch(() => {});
  })();
})();
