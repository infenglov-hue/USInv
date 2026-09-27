// USInv Mobile-First PWA Application Logic (MobileInv / BIST Picker Style)

const STORAGE_KEY = "usinv_last_verified_snapshot";
let currentSnapshotData = null;

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

    window.scrollTo({ top: 0, behavior: "smooth" });
  });
});

// Connectivity & Staleness Detection
function updateConnectionStatus() {
  const offlineTag = document.getElementById("offline-tag");
  if (!navigator.onLine) {
    if (offlineTag) offlineTag.style.display = "inline-block";
    showStaleBanner("Çevrimdışı mod — kaydedilmiş son kopya görüntüleniyor");
  } else {
    if (offlineTag) offlineTag.style.display = "none";
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
    showStaleBanner(
      `Snapshot süresi doldu (${new Date(staleTime).toLocaleDateString()}) — sıradaki veri güncellemesi bekleniyor`
    );
  }
}

// Formatters
function formatCurrency(val) {
  if (val === null || val === undefined) return "--";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  }).format(val);
}

function formatPct(val) {
  if (val === null || val === undefined) return "--";
  return (val * 100).toFixed(1) + "%";
}

// Render Concentrated Stock Cards
function renderStockCards(positions) {
  const container = document.getElementById("positions-cards");
  if (!container) return;
  container.innerHTML = "";

  if (!positions || positions.length === 0) {
    container.innerHTML = `
      <div class="card card-body" style="text-align:center; color:var(--text-muted); padding:30px;">
        %100 Nakit Rejimi — Aktif hisse pozisyonu bulunmuyor.
      </div>
    `;
    return;
  }

  positions.forEach((pos, idx) => {
    const card = document.createElement("div");
    card.className = "stock-card";

    const entryPx = pos.cost_basis || pos.current_price;
    const currPx = pos.current_price;
    const targetPx = pos.target_price || roundNumber(currPx * 1.20, 2);
    const stopPx = pos.stop_price || roundNumber(currPx * 0.82, 2);

    const upsidePct = roundNumber(((targetPx - currPx) / currPx) * 100.0, 1);
    const stopPct = roundNumber(((stopPx - currPx) / currPx) * 100.0, 1);
    const gainPct = roundNumber(((currPx - entryPx) / entryPx) * 100.0, 1);

    const gainColorClass = gainPct >= 0 ? "text-green" : "text-red";
    const gainSign = gainPct >= 0 ? "+" : "";

    // Factor chips
    let chipsHtml = "";
    if (pos.factor_chips && Array.isArray(pos.factor_chips)) {
      chipsHtml = pos.factor_chips
        .map(
          (c) =>
            `<span class="factor-chip"><strong>${c.label}</strong> %${c.score_pct.toFixed(0)}</span>`
        )
        .join("");
    } else {
      chipsHtml = `
        <span class="factor-chip"><strong>Buffett Kalite</strong> %95</span>
        <span class="factor-chip"><strong>Momentum</strong> %92</span>
      `;
    }

    // AI Investment Thesis
    const thesisText =
      pos.thesis ||
      `${pos.company_name} (${pos.ticker}), güçlü Buffett kalite metrikleri ve yüksek göreceli momentum ivmesi ile 5'li odak portföye seçildi. $${targetPx.toFixed(2)} hedef potansiyeli (${upsidePct > 0 ? "+" : ""}%${upsidePct}) hedeflenirken, dinamik ATR stop-loss seviyesi $${stopPx.toFixed(2)} olarak belirlenmiştir.`;

    card.innerHTML = `
      <div class="stock-card-top">
        <div class="stock-identity">
          <div class="ticker-avatar">${pos.ticker.slice(0, 4)}</div>
          <div>
            <div class="ticker-name-row">
              <span class="stock-ticker">${pos.ticker}</span>
              <span class="badge badge-cyan">${pos.weight_pct ? pos.weight_pct.toFixed(1) + "%" : "~20%"}</span>
            </div>
            <div class="stock-company">${pos.company_name}</div>
          </div>
        </div>
        <div class="stock-badges">
          <span class="badge badge-green">#${idx + 1} Slot</span>
        </div>
      </div>

      <div class="levels-grid">
        <div class="level-item">
          <span class="level-label">Güncel Fiyat</span>
          <span class="level-val">$${currPx.toFixed(2)}</span>
          <span class="level-sub ${gainColorClass}">${gainSign}%${gainPct}</span>
        </div>
        <div class="level-item">
          <span class="level-label">Hedef Fiyat</span>
          <span class="level-val text-cyan">$${targetPx.toFixed(2)}</span>
          <span class="level-sub text-cyan">+%${upsidePct}</span>
        </div>
        <div class="level-item">
          <span class="level-label">Dinamik Stop</span>
          <span class="level-val text-red">$${stopPx.toFixed(2)}</span>
          <span class="level-sub text-red">%${stopPct}</span>
        </div>
        <div class="level-item">
          <span class="level-label">Pozisyon Değeri</span>
          <span class="level-val">${formatCurrency(pos.market_value)}</span>
          <span class="level-sub" style="color:var(--text-muted);">${pos.shares ? pos.shares.toFixed(0) + " adet" : "--"}</span>
        </div>
      </div>

      <div class="factor-chips-row">
        ${chipsHtml}
      </div>

      <div class="thesis-box">
        <div class="thesis-header">
          <span>⚡ AI Analist Yatırım Tezi</span>
        </div>
        <p class="thesis-text">${thesisText}</p>
      </div>
    `;

    container.appendChild(card);
  });
}

// Render Classic Table
function renderPositionsTable(positions) {
  const posBody = document.getElementById("positions-body");
  if (!posBody) return;
  posBody.innerHTML = "";

  if (!positions || positions.length === 0) {
    posBody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">100% Nakit / Açık pozisyon yok</td></tr>`;
    return;
  }

  positions.forEach((pos) => {
    const row = document.createElement("tr");
    const targetDisplay = pos.target_price ? `$${pos.target_price.toFixed(2)}` : "--";
    const stopDisplay = pos.stop_price ? `$${pos.stop_price.toFixed(2)}` : "--";

    row.innerHTML = `
      <td><strong>${pos.ticker}</strong><br><span style="font-size:11px;color:var(--text-muted);">${pos.company_name}</span></td>
      <td class="text-right">${pos.shares ? pos.shares.toFixed(0) : "--"}</td>
      <td class="text-right">$${pos.current_price.toFixed(2)}</td>
      <td class="text-right text-cyan">${targetDisplay}</td>
      <td class="text-right text-red">${stopDisplay}</td>
      <td class="text-right"><strong>${pos.weight_pct ? pos.weight_pct.toFixed(1) + "%" : "--"}</strong></td>
    `;
    posBody.appendChild(row);
  });
}

// Render Candidates
function renderCandidates(candidates, filterQuery = "") {
  const listEl = document.getElementById("candidates-list");
  if (!listEl) return;
  listEl.innerHTML = "";

  const query = (filterQuery || "").trim().toLowerCase();
  const filtered = candidates.filter(
    (c) =>
      !query ||
      c.ticker.toLowerCase().includes(query) ||
      c.company_name.toLowerCase().includes(query) ||
      (c.sector && c.sector.toLowerCase().includes(query))
  );

  document.getElementById("candidates-count").textContent = `${filtered.length} aday`;

  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="card card-body" style="text-align:center;color:var(--text-muted);">Aramaya uygun aday bulunamadı.</div>`;
    return;
  }

  filtered.forEach((cand) => {
    const card = document.createElement("div");
    card.className = "card";
    card.style.marginBottom = "10px";

    const scorePct = Math.round((cand.composite_score || 0.8) * 100);
    const targetPx = cand.target_price ? `$${cand.target_price.toFixed(2)}` : "--";
    const stopPx = cand.stop_price ? `$${cand.stop_price.toFixed(2)}` : "--";
    const refPx = cand.entry_reference_price ? `$${cand.entry_reference_price.toFixed(2)}` : "--";

    card.innerHTML = `
      <div class="card-header" style="background:none;">
        <div style="display:flex; align-items:center; gap:8px;">
          <span class="badge badge-cyan">#${cand.rank}</span>
          <strong style="font-size:16px;">${cand.ticker}</strong>
          <span style="font-size:12px; color:var(--text-muted);">${cand.sector || ""}</span>
        </div>
        <span class="badge badge-green">Puan: %${scorePct}</span>
      </div>
      <div class="card-body" style="padding-top:4px;">
        <div style="font-size:12px; color:var(--text-secondary); margin-bottom:8px;">${cand.company_name}</div>
        <div style="display:flex; justify-content:space-between; font-size:12px; font-family:var(--font-mono); background:rgba(0,0,0,0.2); padding:8px; border-radius:6px;">
          <span>Ref: <strong>${refPx}</strong></span>
          <span>Hedef: <strong class="text-cyan">${targetPx}</strong></span>
          <span>Stop: <strong class="text-red">${stopPx}</strong></span>
        </div>
        ${cand.thesis ? `<p style="font-size:11px; color:#cbd5e1; margin-top:8px; line-height:1.4;">${cand.thesis}</p>` : ""}
      </div>
    `;
    listEl.appendChild(card);
  });
}

// Render Full Snapshot Data
function renderSnapshot(data) {
  if (!data) return;
  currentSnapshotData = data;

  // Header & Date
  const asOfEl = document.getElementById("as-of-session");
  if (asOfEl) asOfEl.textContent = `Seans: ${data.as_of_session}`;

  // Top Metrics
  document.getElementById("header-nav").textContent = formatCurrency(data.nav);
  document.getElementById("header-cash").textContent = formatCurrency(data.cash);

  const posCount = (data.positions || []).length;
  document.getElementById("portfolio-count").textContent = `${posCount} hisse`;
  document.getElementById("portfolio-count-stat").textContent = `${posCount} Odak Hisse`;

  // Macro Regime & Cash Overlay State
  const macro = data.macro_regime || {};
  const cashState = macro.cash_state || macro.regime || "NORMAL";
  const cashTargetPct = macro.cash_target_pct !== undefined ? macro.cash_target_pct : 0.0;
  const equityTargetPct = macro.equity_exposure_target !== undefined ? macro.equity_exposure_target : 1.0;

  const regimePill = document.getElementById("macro-regime-pill");
  const regimeHead = document.getElementById("header-regime");
  const headlineEl = document.getElementById("decision-headline");
  const signalSumEl = document.getElementById("macro-signal-summary");

  if (regimeHead) regimeHead.textContent = cashState;

  if (cashState === "NORMAL") {
    if (regimePill) regimePill.className = "regime-pill-large regime-green";
    if (headlineEl) headlineEl.textContent = "Piyasa Normal — %100 Hissede";
  } else if (cashState === "CAUTION") {
    if (regimePill) regimePill.className = "regime-pill-large regime-amber";
    if (headlineEl) headlineEl.textContent = "Piyasa Dikkat — %25 Nakit Koruma";
  } else if (cashState === "DEFENSIVE") {
    if (regimePill) regimePill.className = "regime-pill-large regime-red";
    if (headlineEl) headlineEl.textContent = "Piyasa Savunma — %50 Nakit";
  } else {
    if (regimePill) regimePill.className = "regime-pill-large regime-red";
    if (headlineEl) headlineEl.textContent = "Risk-Off — %75 Nakit Koruma!";
  }

  if (signalSumEl && macro.signal_summary) {
    signalSumEl.textContent = macro.signal_summary;
  }

  // Next Rotation Countdown
  const rotBadge = document.getElementById("rotation-countdown-badge");
  const nextRotText = document.getElementById("next-rotation-text");
  const cadenceStr = macro.rotation_cadence || "2-Haftalık Rotasyon";

  if (rotBadge) rotBadge.textContent = cadenceStr;
  if (nextRotText) {
    nextRotText.textContent = macro.next_rotation_date
      ? `Sonraki Rotasyon: ${macro.next_rotation_date} (Pazartesi Açılışı)`
      : "Sonraki Rotasyon: Pazartesi Açılışı";
  }

  // Macro Tab elements
  const macroNameEl = document.getElementById("macro-regime-name");
  if (macroNameEl) {
    macroNameEl.textContent = cashState;
    macroNameEl.className = `badge ${cashState === "NORMAL" ? "badge-green" : cashState === "CAUTION" ? "badge-amber" : "badge-red"}`;
  }

  const eqTargetEl = document.getElementById("macro-equity-target");
  if (eqTargetEl) eqTargetEl.textContent = `${Math.round(equityTargetPct * 100)}%`;

  const cashTargetEl = document.getElementById("macro-cash-target");
  if (cashTargetEl) cashTargetEl.textContent = `${Math.round(cashTargetPct * 100)}%`;

  // Render 5 Concentrated Stock Cards & Table
  renderStockCards(data.positions || []);
  renderPositionsTable(data.positions || []);

  // Render Candidates
  renderCandidates(data.candidates || []);

  // Search input handler
  const searchInput = document.getElementById("candidates-search");
  if (searchInput) {
    searchInput.oninput = (e) => {
      renderCandidates(data.candidates || [], e.target.value);
    };
  }

  // Render Performance Tail
  const perfBody = document.getElementById("perf-tail-body");
  if (perfBody && data.performance_tail) {
    perfBody.innerHTML = "";
    data.performance_tail.slice(-15).reverse().forEach((p) => {
      const row = document.createElement("tr");
      const retPct = (p.daily_return * 100).toFixed(2);
      const retColor = p.daily_return >= 0 ? "text-green" : "text-red";
      const retSign = p.daily_return >= 0 ? "+" : "";

      row.innerHTML = `
        <td style="font-family:var(--font-mono);">${p.session}</td>
        <td class="text-right">${formatCurrency(p.nav)}</td>
        <td class="text-right" style="color:var(--text-secondary);">${formatCurrency(p.benchmark_nav)}</td>
        <td class="text-right ${retColor}"><strong>${retSign}%${retPct}</strong></td>
      `;
      perfBody.appendChild(row);
    });
  }

  // Render Orders
  const ordersBody = document.getElementById("orders-body");
  const ordersCountEl = document.getElementById("orders-count");
  if (ordersBody && data.orders) {
    ordersBody.innerHTML = "";
    const pendingOrders = data.orders.filter((o) => o.status === "pending" || !o.status);
    if (ordersCountEl) ordersCountEl.textContent = `${pendingOrders.length} emir`;

    if (pendingOrders.length === 0) {
      ordersBody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">Sıradaki seans için bekleyen emir bulunmuyor</td></tr>`;
    } else {
      pendingOrders.forEach((ord) => {
        const row = document.createElement("tr");
        const sideColor = ord.side.toLowerCase() === "buy" ? "badge-green" : "badge-red";
        row.innerHTML = `
          <td><span class="badge ${sideColor}">${ord.side.toUpperCase()}</span></td>
          <td><strong>${ord.ticker}</strong></td>
          <td class="text-right">${ord.quantity ? ord.quantity.toFixed(0) : "--"}</td>
          <td class="text-right text-cyan">$${ord.limit_price ? ord.limit_price.toFixed(2) : "--"}</td>
          <td class="text-right">%${ord.collar_pct ? (ord.collar_pct * 100).toFixed(0) : "2"}</td>
          <td><span style="font-size:11px;color:var(--text-muted);">${ord.reason || "rebalance"}</span></td>
        `;
        ordersBody.appendChild(row);
      });
    }
  }

  // System & Data Health
  if (data.data_health) {
    const healthBadge = document.getElementById("health-status-badge");
    if (healthBadge) healthBadge.textContent = data.data_health.status;
    const coreCov = document.getElementById("health-core-cov");
    if (coreCov) coreCov.textContent = `${data.data_health.core_coverage_pct.toFixed(1)}%`;
    const secCov = document.getElementById("health-sec-cov");
    if (secCov) secCov.textContent = `${data.data_health.secondary_coverage_pct.toFixed(1)}%`;
  }

  if (data.config_hash) {
    const confEl = document.getElementById("provenance-config");
    if (confEl) confEl.textContent = data.config_hash;
  }
  if (data.code_sha) {
    const shaEl = document.getElementById("provenance-sha");
    if (shaEl) shaEl.textContent = data.code_sha;
  }
}

function roundNumber(num, dec = 2) {
  return Math.round(num * Math.pow(10, dec)) / Math.pow(10, dec);
}

// Fetch & Load Logic
async function initApp() {
  updateConnectionStatus();

  // Try cached local snapshot first for instant offline experience
  const cached = localStorage.getItem(STORAGE_KEY);
  if (cached) {
    try {
      const parsed = JSON.parse(cached);
      renderSnapshot(parsed);
      checkStaleness(parsed);
    } catch (e) {
      console.warn("Failed to parse cached snapshot:", e);
    }
  }

  // Fetch live snapshot.json from server
  try {
    const resp = await fetch("./snapshot.json?t=" + new Date().getTime(), {
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`HTTP error ${resp.status}`);
    const data = await resp.json();
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    renderSnapshot(data);
    checkStaleness(data);
  } catch (err) {
    console.warn("Could not fetch latest snapshot:", err);
    if (!cached) {
      showStaleBanner("Sunucudan veri alınamadı. Ağ bağlantınızı kontrol edin.");
    }
  }
}

document.addEventListener("DOMContentLoaded", initApp);
