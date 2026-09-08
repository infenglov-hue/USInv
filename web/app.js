// USInv PWA Application Logic

const STORAGE_KEY = "usinv_last_verified_snapshot";

// Register Service Worker
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("./sw.js").catch((err) => {
      console.warn("ServiceWorker registration failed:", err);
    });
  });
}

// Navigation Tab Switching
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    const targetView = btn.getAttribute("data-view");
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view-section").forEach((sec) => sec.classList.remove("active"));

    btn.classList.add("active");
    const sec = document.getElementById(`view-${targetView}`);
    if (sec) sec.classList.add("active");
  });
});

// Connectivity & Staleness Detection
function updateConnectionStatus() {
  const offlineTag = document.getElementById("offline-tag");
  if (!navigator.onLine) {
    offlineTag.style.display = "inline-block";
    showStaleBanner("Offline mode — viewing cached snapshot");
  } else {
    offlineTag.style.display = "none";
  }
}

window.addEventListener("online", updateConnectionStatus);
window.addEventListener("offline", updateConnectionStatus);

function showStaleBanner(message) {
  const banner = document.getElementById("stale-banner");
  const msgEl = document.getElementById("stale-msg");
  if (banner && msgEl) {
    msgEl.textContent = message;
    banner.style.display = "block";
  }
}

function checkStaleness(snapshot) {
  if (!snapshot || !snapshot.stale_after) return;
  const staleTime = new Date(snapshot.stale_after).getTime();
  const now = new Date().getTime();
  if (now > staleTime) {
    showStaleBanner(`Snapshot stale (expired ${new Date(staleTime).toLocaleDateString()}) — next pipeline run pending`);
  }
}

// Formatters
function formatCurrency(val) {
  if (val === null || val === undefined) return "--";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(val);
}

function formatPct(val) {
  if (val === null || val === undefined) return "--";
  return (val * 100).toFixed(2) + "%";
}

// Renderers
function renderSnapshot(data) {
  if (!data) return;

  // Header & Summary
  document.getElementById("as-of-session").textContent = `As of: ${data.as_of_session}`;
  document.getElementById("header-nav").textContent = formatCurrency(data.nav);
  document.getElementById("header-cash").textContent = formatCurrency(data.cash);
  document.getElementById("header-regime").textContent = data.macro_regime.regime;

  // Portfolio
  document.getElementById("portfolio-count").textContent = `${data.positions.length} positions`;
  const posBody = document.getElementById("positions-body");
  posBody.innerHTML = "";
  if (data.positions.length === 0) {
    posBody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">100% Cash / No active positions</td></tr>`;
  } else {
    data.positions.forEach((pos) => {
      const row = document.createElement("tr");
      const stopDisplay = pos.stop_price ? `$${pos.stop_price.toFixed(2)}` : "None";
      row.innerHTML = `
        <td><strong>${pos.ticker}</strong><br><span style="font-size:10px;color:var(--text-muted);">${pos.company_name}</span></td>
        <td class="text-right">${pos.shares.toFixed(0)}</td>
        <td class="text-right">$${pos.current_price.toFixed(2)}</td>
        <td class="text-right">${formatCurrency(pos.market_value)}</td>
        <td class="text-right">${pos.weight_pct.toFixed(1)}%</td>
        <td class="text-right"><span style="color:var(--accent-red);">${stopDisplay}</span></td>
      `;
      posBody.appendChild(row);
    });
  }

  // Candidates
  document.getElementById("candidates-count").textContent = `Top ${data.candidates.length}`;
  const candList = document.getElementById("candidates-list");
  candList.innerHTML = "";
  data.candidates.forEach((cand) => {
    const card = document.createElement("div");
    card.className = "card";
    const flagBadge = cand.red_flag_status === "CLEAN" 
      ? `<span class="badge badge-green">CLEAN</span>` 
      : `<span class="badge badge-yellow">${cand.red_flag_status}</span>`;
    
    let factorItems = "";
    if (cand.factor_ranks) {
      for (const [fname, fval] of Object.entries(cand.factor_ranks)) {
        factorItems += `
          <div class="factor-item">
            <div class="factor-name"><span>${fname}</span> <strong>${(fval * 100).toFixed(0)}%</strong></div>
          </div>
        `;
      }
    }

    card.innerHTML = `
      <div class="card-header">
        <div>
          <span class="badge badge-blue">#${cand.rank}</span>
          <strong style="margin-left:6px;">${cand.ticker}</strong>
          <span style="font-size:11px;color:var(--text-secondary);margin-left:6px;">${cand.sector}</span>
        </div>
        <div>${flagBadge}</div>
      </div>
      <div class="card-body">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div><span class="summary-label">Composite Score</span>: <strong>${cand.composite_score.toFixed(3)}</strong></div>
          <div><span class="summary-label">Bucket</span>: <code>${cand.core_or_large}</code></div>
        </div>
        <div class="factor-bar-grid">${factorItems}</div>
      </div>
    `;
    candList.appendChild(card);
  });

  // Macro & Regime
  document.getElementById("macro-regime-name").textContent = data.macro_regime.regime;
  document.getElementById("macro-regime-name").className = `badge ${data.macro_regime.regime === "NORMAL" ? "badge-green" : "badge-red"}`;
  document.getElementById("macro-overlay").textContent = data.macro_regime.overlay;
  document.getElementById("macro-equity-target").textContent = `${(data.macro_regime.equity_exposure_target * 100).toFixed(0)}%`;
  document.getElementById("macro-signal-summary").textContent = data.macro_regime.signal_summary;

  // Performance Tail
  const tailBody = document.getElementById("perf-tail-body");
  tailBody.innerHTML = "";
  data.performance_tail.slice(-15).reverse().forEach((pt) => {
    const row = document.createElement("tr");
    const retColor = pt.daily_return >= 0 ? "var(--accent-green)" : "var(--accent-red)";
    row.innerHTML = `
      <td>${pt.session}</td>
      <td class="text-right">${formatCurrency(pt.nav)}</td>
      <td class="text-right">${formatCurrency(pt.benchmark_nav)}</td>
      <td class="text-right" style="color:${retColor}">${(pt.daily_return * 100).toFixed(2)}%</td>
    `;
    tailBody.appendChild(row);
  });

  // Orders
  document.getElementById("orders-count").textContent = `${data.orders.length} orders`;
  const ordersBody = document.getElementById("orders-body");
  ordersBody.innerHTML = "";
  if (data.orders.length === 0) {
    ordersBody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">No pending orders</td></tr>`;
  } else {
    data.orders.forEach((ord) => {
      const row = document.createElement("tr");
      const sideBadge = ord.side.toUpperCase() === "BUY" ? "badge-green" : "badge-red";
      row.innerHTML = `
        <td><span class="badge ${sideBadge}">${ord.side.toUpperCase()}</span></td>
        <td><strong>${ord.ticker}</strong></td>
        <td class="text-right">${ord.quantity.toFixed(0)}</td>
        <td class="text-right">$${ord.limit_price.toFixed(2)}</td>
        <td class="text-right">±${(ord.collar_pct * 100).toFixed(1)}%</td>
        <td><code>${ord.status}</code></td>
      `;
      ordersBody.appendChild(row);
    });
  }

  // Health & Provenance
  document.getElementById("health-status-badge").textContent = data.data_health.status;
  document.getElementById("health-status-badge").className = `badge ${data.data_health.status === "HEALTHY" ? "badge-green" : "badge-red"}`;
  document.getElementById("health-core-cov").textContent = `${data.data_health.core_coverage_pct.toFixed(2)}%`;
  document.getElementById("health-sec-cov").textContent = `${data.data_health.secondary_coverage_pct.toFixed(2)}%`;
  document.getElementById("health-id-gaps").textContent = data.data_health.identity_gaps;
  document.getElementById("health-sector-gaps").textContent = data.data_health.sector_gaps;

  document.getElementById("provenance-config").textContent = data.config_hash;
  document.getElementById("provenance-sha").textContent = data.code_sha;
  document.getElementById("provenance-manifest").textContent = data.data_manifest_hash;
  document.getElementById("provenance-generated").textContent = data.generated_at;
}

// Fetch Snapshot with Offline Fallback
async function loadSnapshot() {
  try {
    const resp = await fetch("./snapshot.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    renderSnapshot(data);
    checkStaleness(data);
  } catch (err) {
    console.warn("Failed to fetch fresh snapshot.json; falling back to offline cache:", err);
    const cached = localStorage.getItem(STORAGE_KEY);
    if (cached) {
      const data = JSON.parse(cached);
      renderSnapshot(data);
      showStaleBanner("Offline fallback: displaying previously verified snapshot");
    } else {
      showStaleBanner("Unable to load snapshot data. Check connection.");
    }
  }
  updateConnectionStatus();
}

document.addEventListener("DOMContentLoaded", loadSnapshot);
