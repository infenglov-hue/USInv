/**
 * US Picker - Core Application Logic (BIST Picker PWA v24 port)
 * Rebuilt from scratch to ensure complete parity with the Android Kotlin application.
 */

window.livePrices = {};
window.livePriceDetails = {};
window.snapshotPrices = {};
window.feedManifest = null;
window.feedBacktestAudit = null;
window.liveFeedGeneratedAt = null;
let dbInstance = null;
let activeDetailTicker = null;
let livePriceRefreshTimer = null;
let returnCostSettings = PortfolioCosts.normalizeSettings();

const LIVE_PRICE_FEED_URL = 'https://raw.githubusercontent.com/infenglov-hue/USInv/live-data/live_prices.json';
const LIVE_PRICE_REFRESH_MS = 60_000;
const LIVE_PRICE_REQUEST_TIMEOUT_MS = 7_000;
const RECENT_QUOTE_MAX_AGE_MS = 45 * 60_000;
const USABLE_QUOTE_MAX_AGE_MS = 7 * 24 * 60 * 60_000;

// --- Global Chart Instances ---
let priceChartInstance = null;
let factorChartInstance = null;
let backtestChartInstance = null;

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function finiteNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function mixedScalePercent(value) {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return Math.abs(parsed) <= 1 ? parsed * 100 : parsed;
}

const QUALITY_FLAG_META = {
  PIOTROSKI_LOW: {
    label: 'Piotroski zayıf',
    detail: 'Finansal sağlık skoru düşük; bilanço kalitesi ayrıca kontrol edilmeli.'
  },
  LIMITED_DATA: {
    label: 'Veri sınırlı',
    detail: 'Model kararı eksik veriyle üretilmiş; güven seviyesi daha düşük.'
  },
  DCF_OVERVALUED: {
    label: 'DCF pahalı',
    detail: 'DCF içsel değeri fiyata göre düşük; hedef fiyat skor/momentum varsayımından geliyor olabilir.'
  },
  WEAK_TECHNICAL: {
    label: 'Teknik zayıf',
    detail: 'Trend/teknik görünüm modelin risk eşiğine yakın veya zayıf.'
  }
};

function parseQualityFlags(...payloads) {
  for (const payload of payloads) {
    if (!payload) continue;
    try {
      const parsed = Array.isArray(payload) ? payload : JSON.parse(payload);
      if (Array.isArray(parsed)) {
        return parsed
          .map(item => String(item || '').trim())
          .filter(Boolean);
      }
    } catch {
      // Ignore malformed payloads; the UI should degrade to "no flags".
    }
  }
  return [];
}

function qualityFlagLabel(flag) {
  return QUALITY_FLAG_META[flag]?.label || String(flag || 'Bilinmeyen uyarı');
}

function qualityFlagDetail(flag) {
  return QUALITY_FLAG_META[flag]?.detail || 'Model denetim bayrağı.';
}

const TARGET_SOURCE_META = {
  DCF_INTRINSIC: {
    label: 'DCF hedef',
    detail: 'Hedef fiyat DCF içsel değerinden geliyor.'
  },
  DCF_CAPPED: {
    label: 'DCF capli hedef',
    detail: 'DCF hedefi üst sınırı aştığı için cap uygulanmış.'
  },
  SCORE_IMPLIED: {
    label: 'Model hedef',
    detail: 'Hedef fiyat değerleme değil; skor, momentum ve güven katsayısından türetilmiş model hedefi.'
  },
  SCORE_CAPPED: {
    label: 'Capli model hedef',
    detail: 'Model hedefi üst sınırı aştığı için cap uygulanmış.'
  }
};

function targetSourceMeta(source) {
  return TARGET_SOURCE_META[String(source || '').toUpperCase()] || {
    label: 'Hedef',
    detail: 'Hedef fiyat kaynağı snapshot içinde belirtilmemiş.'
  };
}

function inferTargetSource(source, qualityFlags = []) {
  const normalized = String(source || '').toUpperCase();
  if (normalized && TARGET_SOURCE_META[normalized]) return normalized;
  if (qualityFlags.includes('DCF_OVERVALUED')) return 'SCORE_IMPLIED';
  return normalized || null;
}

// Sector slugs in the snapshot are internal English identifiers; map them to
// user-facing Turkish labels (fallback: prettified slug).
const SECTOR_LABELS_TR = {
  agriculture: 'Tarım',
  airlines: 'Havayolu',
  automotive: 'Otomotiv',
  banking_private: 'Bankacılık (Özel)',
  banking_state: 'Bankacılık (Kamu)',
  cement: 'Çimento',
  chemicals: 'Kimya',
  construction: 'İnşaat',
  defense: 'Savunma',
  energy_oil_gas: 'Enerji (Petrol & Gaz)',
  energy_power: 'Enerji (Elektrik)',
  financial_services: 'Finansal Hizmetler',
  food_production: 'Gıda Üretimi',
  food_retail: 'Gıda Perakendesi',
  glass: 'Cam',
  healthcare_pharma: 'Sağlık & İlaç',
  holding_diversified: 'Holding (Çeşitlendirilmiş)',
  holding_industrial: 'Holding (Sınai)',
  industrial_manufacturing: 'Sanayi İmalatı',
  insurance: 'Sigorta',
  logistics: 'Lojistik',
  media: 'Medya',
  mining: 'Madencilik',
  other: 'Diğer',
  paper: 'Kağıt & Ambalaj',
  real_estate: 'Gayrimenkul (GYO)',
  retail_general: 'Perakende (Genel)',
  retail_specialty: 'Perakende (Özel)',
  steel: 'Demir-Çelik',
  technology_hardware: 'Teknoloji (Donanım)',
  technology_software: 'Yazılım',
  Software: 'Yazılım',
  telecom: 'Telekomünikasyon',
  textiles: 'Tekstil',
  tourism_hotels: 'Turizm & Otelcilik',
};

function sectorLabel(slug) {
  if (!slug) return '—';
  if (SECTOR_LABELS_TR[slug]) return SECTOR_LABELS_TR[slug];
  return String(slug).replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase());
}

function quoteAgeMs(quoteTime) {
  const timestamp = Date.parse(String(quoteTime || ''));
  return Number.isFinite(timestamp) ? Math.max(0, Date.now() - timestamp) : Infinity;
}

function formatQuoteTime(quoteTime) {
  const timestamp = Date.parse(String(quoteTime || ''));
  if (!Number.isFinite(timestamp)) return '';
  return new Intl.DateTimeFormat('tr-TR', {
    timeZone: 'Europe/Istanbul',
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(timestamp));
}

function getQuoteStatus(ticker) {
  const detail = window.livePriceDetails[ticker];
  if (!detail) {
    return {
      label: 'SNAPSHOT',
      className: 'badge badge-snapshot',
      title: 'Son yayınlanan snapshot kapanış fiyatı'
    };
  }

  const age = quoteAgeMs(detail.quote_time);
  if (age <= RECENT_QUOTE_MAX_AGE_MS) {
    return {
      label: '15 DK GEC.',
      className: 'badge badge-live',
      title: `Yahoo Finance yaklaşık 15 dakika gecikmeli fiyat • ${formatQuoteTime(detail.quote_time)}`
    };
  }
  return {
    label: 'SON İŞLEM',
    className: 'badge badge-delayed',
    title: `Son piyasa işlemi • ${formatQuoteTime(detail.quote_time)}`
  };
}

// ==========================================================================
// 1. PWA Service Worker & Cache Busting Setup
// ==========================================================================

if ('serviceWorker' in navigator) {
  const hadServiceWorkerController = Boolean(navigator.serviceWorker.controller);
  let isReloadingForServiceWorkerUpdate = false;

  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadServiceWorkerController || isReloadingForServiceWorkerUpdate) return;
    isReloadingForServiceWorkerUpdate = true;
    window.location.reload();
  });

  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js', { updateViaCache: 'none' })
      .then(async reg => {
        console.log('[PWA] Service Worker registered:', reg.scope);
        try {
          await reg.update();
        } catch (updateError) {
          console.warn('[PWA] Update check skipped:', updateError);
        }

        if (reg.waiting) {
          reg.waiting.postMessage({ type: 'SKIP_WAITING' });
        }

        reg.addEventListener('updatefound', () => {
          const installingWorker = reg.installing;
          if (!installingWorker) return;

          installingWorker.addEventListener('statechange', () => {
            if (installingWorker.state === 'installed' && navigator.serviceWorker.controller) {
              installingWorker.postMessage({ type: 'SKIP_WAITING' });
            }
          });
        });
      })
      .catch(err => console.error('[PWA] Service Worker registration failed:', err));
  });
}

// ==========================================================================
// 2. IndexedDB Snapshot Storage
// ==========================================================================

const IDB_NAME = 'BistPickerCache';
const IDB_VERSION = 3;
const STORE_NAME = 'snapshots';
const LEGACY_USER_STORE_NAME = 'user-data';
const RETURN_COST_SETTINGS_KEY = 'bp_return_cost_settings_v1';

function openIDB() {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      reject(new Error('IndexedDB open timeout (iOS Safari workaround)'));
    }, 10000);

    try {
      const request = indexedDB.open(IDB_NAME, IDB_VERSION);
      request.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME, { keyPath: 'id' });
        }
        if (db.objectStoreNames.contains(LEGACY_USER_STORE_NAME)) {
          db.deleteObjectStore(LEGACY_USER_STORE_NAME);
        }
      };
      request.onsuccess = (e) => {
        clearTimeout(timeout);
        resolve(e.target.result);
      };
      request.onerror = (e) => {
        clearTimeout(timeout);
        reject(request.error || new Error('IndexedDB error'));
      };
    } catch (err) {
      clearTimeout(timeout);
      reject(err);
    }
  });
}

async function getCachedSnapshot() {
  try {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly');
      const store = tx.objectStore(STORE_NAME);
      const req = store.get(1);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  } catch (err) {
    console.warn('[DB] IndexedDB load failed:', err);
    return null;
  }
}

async function setCachedSnapshot(data) {
  try {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      const store = tx.objectStore(STORE_NAME);
      const req = store.put({ id: 1, ...data });
      req.onsuccess = () => resolve(true);
      req.onerror = () => reject(req.error);
    });
  } catch (err) {
    console.error('[DB] IndexedDB save failed:', err);
    throw err;
  }
}

async function deleteCachedSnapshot() {
  try {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      const req = tx.objectStore(STORE_NAME).delete(1);
      req.onsuccess = () => resolve(true);
      req.onerror = () => reject(req.error);
    });
  } catch (err) {
    console.warn('[DB] IndexedDB cleanup failed:', err);
    return false;
  }
}

function loadReturnCostSettings() {
  try {
    return PortfolioCosts.normalizeSettings(JSON.parse(
      localStorage.getItem(RETURN_COST_SETTINGS_KEY) || '{}'
    ));
  } catch {
    return PortfolioCosts.normalizeSettings();
  }
}

function saveReturnCostSettings(settings) {
  returnCostSettings = PortfolioCosts.normalizeSettings(settings);
  try {
    localStorage.setItem(RETURN_COST_SETTINGS_KEY, JSON.stringify(returnCostSettings));
  } catch { /* private mode / quota */ }
  return returnCostSettings;
}

function modelPositionResult(position, exitPrice, options = {}) {
  const entryPrice = finiteNumber(position.entry_price, null);
  const normalizedExit = finiteNumber(exitPrice, null);
  return {
    entryPrice,
    exitPrice: normalizedExit,
    returnFraction: PortfolioCosts.calculateTradeReturn({
      entryPrice,
      exitPrice: normalizedExit,
      buyCostPct: returnCostSettings.buyCostPct,
      sellCostPct: returnCostSettings.sellCostPct,
      view: returnCostSettings.returnView,
      includeBuyCost: options.includeBuyCost !== false,
      includeSellCost: options.includeSellCost !== false,
    }),
  };
}

function formatCostRate(value) {
  const number = finiteNumber(value);
  return number.toLocaleString('tr-TR', { maximumFractionDigits: 3 });
}

function refreshReturnCostUi() {
  const settings = PortfolioCosts.normalizeSettings(returnCostSettings);
  const totalCost = settings.buyCostPct + settings.sellCostPct;
  const brokerLabel = totalCost === 0
    ? `${settings.broker} · maliyet %0`
    : `${settings.broker} · alış %${formatCostRate(settings.buyCostPct)} / satış %${formatCostRate(settings.sellCostPct)}`;
  const label = document.getElementById('cost-profile-label');
  if (label) label.textContent = brokerLabel;

  document.querySelectorAll('[data-return-view]').forEach(button => {
    const active = button.dataset.returnView === settings.returnView;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });

  const historyMode = document.getElementById('history-return-mode');
  if (historyMode) {
    historyMode.textContent = `${settings.returnView === 'gross' ? 'BRÜT' : 'NET'} · ${settings.broker.toUpperCase()}`;
  }
}

function rerenderModelReturns() {
  refreshReturnCostUi();
  if (!dbInstance) return;
  if (document.getElementById('page-history')?.classList.contains('active')) {
    renderHistoryPage();
  }
}

function setReturnView(view) {
  saveReturnCostSettings({
    ...returnCostSettings,
    returnView: view === 'gross' ? 'gross' : 'net',
  });
  rerenderModelReturns();
}

function openCostProfileEditor() {
  const settings = PortfolioCosts.normalizeSettings(returnCostSettings);
  document.getElementById('broker-name').value = settings.broker;
  document.getElementById('buy-cost-pct').value = settings.buyCostPct;
  document.getElementById('sell-cost-pct').value = settings.sellCostPct;
  openSheet('cost-profile-sheet');
}

function saveCostProfile(event) {
  event.preventDefault();
  saveReturnCostSettings({
    ...returnCostSettings,
    broker: document.getElementById('broker-name').value,
    buyCostPct: document.getElementById('buy-cost-pct').value,
    sellCostPct: document.getElementById('sell-cost-pct').value,
  });
  requestSheetClose();
  rerenderModelReturns();
}

function validateSnapshotManifest(value) {
  if (!value || typeof value !== 'object' || !value.snapshot || typeof value.snapshot !== 'object') {
    throw new Error('Manifest yapısı geçersiz.');
  }

  const filename = String(value.snapshot.filename || '');
  const sha256 = String(value.snapshot.sha256 || '').toLowerCase();
  const sizeBytes = Number(value.snapshot.size_bytes);

  if (!/^[a-zA-Z0-9._-]+$/.test(filename)) {
    throw new Error('Manifest snapshot dosya adı geçersiz.');
  }
  if (!/^[a-f0-9]{64}$/.test(sha256)) {
    throw new Error('Manifest snapshot SHA-256 değeri geçersiz.');
  }
  if (!Number.isSafeInteger(sizeBytes) || sizeBytes <= 0) {
    throw new Error('Manifest snapshot boyutu geçersiz.');
  }

  return value;
}

async function sha256Hex(arrayBuffer) {
  if (!globalThis.crypto?.subtle) {
    console.warn('[DB] Web Crypto unavailable; snapshot hash validation skipped.');
    return null;
  }
  const digest = await crypto.subtle.digest('SHA-256', arrayBuffer);
  return Array.from(new Uint8Array(digest))
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('');
}

// ==========================================================================
// 3. Database Downloader (With Real-time Progress Tracking)
// ==========================================================================

async function fetchWithProgress(url, onProgress, options = {}) {
  const { timeout = 180000 } = options;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);

    const contentLength = response.headers.get('content-length');
    const total = contentLength ? parseInt(contentLength, 10) : 0;
    
    if (total === 0) {
      console.warn('[Fetch] Content-Length is missing, fallback to buffered ArrayBuffer');
      const buf = await response.arrayBuffer();
      return buf;
    }

    if (!response.body?.getReader) {
      console.warn('[Fetch] Streaming response unsupported, using buffered download');
      const buf = await response.arrayBuffer();
      onProgress(100);
      return buf;
    }

    const reader = response.body.getReader();
    let loaded = 0;
    const chunks = [];

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.length;
      
      const percent = Math.max(0, Math.min(100, Math.round((loaded / total) * 100)));
      onProgress(percent);
    }

    // Combine chunks
    const result = new Uint8Array(loaded);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return result.buffer;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function downloadSnapshot(manifest, onProgress) {
  const downloadUrl = `./${manifest.snapshot.filename}?t=${Date.now()}`;
  const arrayBuffer = await fetchWithProgress(downloadUrl, onProgress);

  if (arrayBuffer.byteLength !== manifest.snapshot.size_bytes) {
    throw new Error(
      `Snapshot boyutu uyuşmuyor (${arrayBuffer.byteLength}/${manifest.snapshot.size_bytes}).`
    );
  }

  const actualHash = await sha256Hex(arrayBuffer);
  if (actualHash && actualHash !== manifest.snapshot.sha256) {
    throw new Error('Snapshot bütünlük kontrolü başarısız (SHA-256 uyuşmuyor).');
  }

  return pako.ungzip(new Uint8Array(arrayBuffer));
}

// ==========================================================================
// 4. SQL Engine Helper Wrappers (Room Queries Parity)
// ==========================================================================

function queryAll(sql, params = {}) {
  if (!dbInstance) {
    console.error('[SQL] DB is not initialized!');
    return [];
  }
  let stmt = null;
  try {
    stmt = dbInstance.prepare(sql);
    stmt.bind(params);
    const list = [];
    while (stmt.step()) {
      list.push(stmt.getAsObject());
    }
    return list;
  } catch (e) {
    console.error('[SQL] Error executing query:', sql, params, e);
    return [];
  } finally {
    if (stmt) stmt.free();
  }
}

function queryOne(sql, params = {}) {
  const rows = queryAll(sql, params);
  return rows.length > 0 ? rows[0] : null;
}

// Load snapshot prices immediately so offline startup always has a fallback.
function loadSnapshotPrices(tickers) {
  window.livePrices = {};
  window.livePriceDetails = {};
  window.snapshotPrices = {};
  tickers.forEach(t => {
    const row = queryOne(`
      SELECT close FROM price_history_730d
      WHERE company_id = (SELECT id FROM companies WHERE ticker = :ticker LIMIT 1)
      ORDER BY date DESC LIMIT 1
    `, { ':ticker': t });
    if (row) {
      window.snapshotPrices[t] = row.close;
      window.livePrices[t] = row.close;
    }
  });

  const spyRow = queryOne(`
    SELECT close FROM price_history_730d
    WHERE company_id = (SELECT id FROM companies WHERE ticker = 'SPY' LIMIT 1)
    ORDER BY date DESC LIMIT 1
  `);
  if (spyRow) {
    window.snapshotPrices['SPY'] = spyRow.close;
    window.livePrices['SPY'] = spyRow.close;
  }
}

function validateLivePriceFeed(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    value.schema_version !== 1 ||
    !value.prices ||
    typeof value.prices !== 'object'
  ) {
    throw new Error('Canlı fiyat feed yapısı geçersiz.');
  }
  const generatedAge = quoteAgeMs(value.generated_at);
  if (generatedAge > USABLE_QUOTE_MAX_AGE_MS) {
    throw new Error('Canlı fiyat feed verisi çok eski.');
  }
  return value;
}

function rerenderLivePriceConsumers() {
  const activeTab = document.querySelector('.nav-item.active')?.dataset.tab;
  if (activeTab === 'picks' || activeTab === 'history') {
    renderPage(activeTab);
  }
  if (
    activeDetailTicker &&
    !window.__bpSheetDragging &&
    document.getElementById('detail-sheet').classList.contains('open')
  ) {
    // Skipped mid-drag: rebuilding the sheet's DOM would detach the touch
    // target and freeze the gesture halfway.
    openStockDetail(activeDetailTicker);
  }
}

async function refreshLivePrices() {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    LIVE_PRICE_REQUEST_TIMEOUT_MS
  );
  try {
    const response = await fetch(`${LIVE_PRICE_FEED_URL}?t=${Date.now()}`, {
      cache: 'no-store',
      signal: controller.signal
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const feed = validateLivePriceFeed(await response.json());
    window.liveFeedGeneratedAt = feed.generated_at || null;
    const nextPrices = { ...window.snapshotPrices };
    const nextDetails = {};
    let updated = 0;
    for (const [ticker, quote] of Object.entries(feed.prices)) {
      if (!quote || typeof quote !== 'object') continue;
      const price = finiteNumber(quote.price, 0);
      if (price <= 0 || quoteAgeMs(quote.quote_time) > USABLE_QUOTE_MAX_AGE_MS) {
        continue;
      }
      nextPrices[ticker] = price;
      nextDetails[ticker] = {
        price,
        quote_time: quote.quote_time,
        source: feed.source || 'Yahoo Finance'
      };
      updated += 1;
    }
    if (updated > 0) {
      window.livePrices = nextPrices;
      window.livePriceDetails = nextDetails;
      console.info(`[Price] Updated ${updated} near-live quotes.`);
      rerenderLivePriceConsumers();
      return true;
    }
    throw new Error('Feed içinde kullanılabilir fiyat bulunamadı.');
  } catch (error) {
    console.warn('[Price] Near-live feed unavailable; snapshot prices retained:', error);
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

function startLivePriceRefresh() {
  if (livePriceRefreshTimer) clearInterval(livePriceRefreshTimer);
  refreshLivePrices();
  livePriceRefreshTimer = setInterval(refreshLivePrices, LIVE_PRICE_REFRESH_MS);
}

// ==========================================================================
// 5. Weekly Performance Calculations (WeeklyPerformanceManager.kt Parity)
// ==========================================================================

function getPriceOnOrBefore(ticker, dateStr) {
  const row = queryOne(`
    SELECT close FROM price_history_730d 
    WHERE company_id = (SELECT id FROM companies WHERE ticker = :ticker LIMIT 1) 
      AND date <= :date 
    ORDER BY date DESC LIMIT 1
  `, { ':ticker': ticker, ':date': dateStr });
  return row ? row.close : null;
}

function getLatestSnapshotPrice(ticker) {
  const row = queryOne(`
    SELECT close FROM price_history_730d
    WHERE company_id = (SELECT id FROM companies WHERE ticker = :ticker LIMIT 1)
    ORDER BY date DESC LIMIT 1
  `, { ':ticker': ticker });
  return row ? finiteNumber(row.close, null) : null;
}

const LIVE_TRACKING_START_DATE = '2026-05-21';

function modelCyclePositionResult({
  ticker,
  selectionDate,
  cycleStart,
  modelEntryPrice,
  modelExitPrice,
  includeSellCost,
}) {
  const entryPrice = finiteNumber(modelEntryPrice, null);
  const exitPrice = finiteNumber(modelExitPrice, null);
  const returnFraction = PortfolioCosts.calculateTradeReturn({
    entryPrice,
    exitPrice,
    buyCostPct: returnCostSettings.buyCostPct,
    sellCostPct: returnCostSettings.sellCostPct,
    view: returnCostSettings.returnView,
    includeBuyCost: selectionDate === cycleStart,
    includeSellCost,
  });
  return {
    ticker,
    entryPrice,
    exitPrice,
    returnPct: returnFraction,
  };
}

// B1 continuity (v16): held positions keep their ORIGINAL selection_date and
// no longer produce an exit row every rotation, so cohort-by-selection_date
// grouping under-counts held names. When the snapshot carries
// portfolio_cycle_marks (one ref price per position per rotation), period
// records are built mark-to-mark; legacy snapshots fall back to the old
// cohort logic below.
function buildWeeklyPerformanceRecords(dbPositions, livePrices) {
  let marks = [];
  const todayDate = localIsoDate();
  try {
    marks = queryAll(`
      SELECT * FROM portfolio_cycle_marks
      WHERE cycle_date >= :startDate AND cycle_date <= :todayDate
      ORDER BY cycle_date ASC, sort_order ASC
    `, {
      ':startDate': LIVE_TRACKING_START_DATE,
      ':todayDate': todayDate,
    });
  } catch { /* pre-B1 snapshot — no marks table */ }

  if (marks.length > 0) {
    // Marks start at the first post-B1 rotation; earlier periods still come
    // from the legacy cohort reconstruction so history doesn't lose them.
    const firstMarkDate = marks[0].cycle_date;
    const markRecords = buildCycleMarkRecords(marks, dbPositions, livePrices);
    const legacyRecords = buildLegacyCohortRecords(dbPositions, livePrices)
      .filter(rec => rec.weekStartDate < firstMarkDate && rec.isCompleted);
    return [...legacyRecords, ...markRecords]
      .sort((a, b) => a.weekStartDate.localeCompare(b.weekStartDate));
  }
  return buildLegacyCohortRecords(dbPositions, livePrices);
}

function buildCycleMarkRecords(marks, dbPositions, livePrices) {
  const todayDate = localIsoDate();
  const exitRows = queryAll(`
    SELECT * FROM portfolio_history
    WHERE exit_date IS NOT NULL AND exit_date > :startDate
      AND exit_date <= :todayDate
    ORDER BY exit_date ASC, sort_order ASC
  `, {
    ':startDate': LIVE_TRACKING_START_DATE,
    ':todayDate': todayDate,
  });
  const pendingExitRows = queryAll(`
    SELECT * FROM portfolio_history
    WHERE exit_date > :todayDate
    ORDER BY exit_date ASC, sort_order ASC
  `, { ':todayDate': todayDate });

  const byCycle = new Map();
  marks.forEach(mark => {
    if (!byCycle.has(mark.cycle_date)) byCycle.set(mark.cycle_date, []);
    byCycle.get(mark.cycle_date).push(mark);
  });
  const cycleDates = [...byCycle.keys()].sort();
  const records = [];

  const findExit = (ticker, afterDate, uptoDate) => exitRows.find(row =>
    row.ticker === ticker && row.exit_date > afterDate
    && (!uptoDate || row.exit_date <= uptoDate));
  const findPendingExit = ticker => pendingExitRows.find(row => row.ticker === ticker);
  const selectionDateFor = (ticker, cycleDate) => {
    const candidates = [
      ...dbPositions.filter(row => row.ticker === ticker),
      ...exitRows.filter(row => row.ticker === ticker),
    ]
      .filter(row => row.selection_date && row.selection_date <= cycleDate)
      .sort((a, b) => b.selection_date.localeCompare(a.selection_date));
    return candidates[0]?.selection_date || cycleDate;
  };

  // Completed periods: consecutive mark dates C1 -> C2. Per stock the period
  // return is (next mark ref | realized exit) / this mark ref.
  for (let i = 0; i < cycleDates.length - 1; i++) {
    const c1 = cycleDates[i];
    const c2 = cycleDates[i + 1];
    const nextByTicker = new Map(byCycle.get(c2).map(m => [m.ticker, m]));
    const stockRecords = [];
    byCycle.get(c1).forEach(mark => {
      const ref = finiteNumber(mark.ref_price);
      if (!(ref > 0)) return;
      const nextMark = nextByTicker.get(mark.ticker);
      let endPrice = nextMark ? finiteNumber(nextMark.ref_price, null) : null;
      let exitRow = null;
      if (endPrice == null) {
        exitRow = findExit(mark.ticker, c1, c2);
        if (exitRow && exitRow.exit_price != null) endPrice = finiteNumber(exitRow.exit_price, null);
      }
      if (endPrice == null || !(endPrice > 0)) return;
      stockRecords.push(modelCyclePositionResult({
        ticker: mark.ticker,
        selectionDate: exitRow?.selection_date || selectionDateFor(mark.ticker, c1),
        cycleStart: c1,
        modelEntryPrice: ref,
        modelExitPrice: endPrice,
        includeSellCost: !nextMark,
      }));
    });
    if (stockRecords.length === 0) continue;

    const bistStart = getPriceOnOrBefore('SPY', c1);
    const bistEnd = getPriceOnOrBefore('SPY', c2);
    if (!(bistStart > 0) || bistEnd == null) continue;
    records.push({
      weekStartDate: c1,
      weekEndDate: c2,
      positions: stockRecords,
      portfolioReturn: stockRecords.reduce((s, r) => s + r.returnPct, 0) / stockRecords.length,
      bist100StartPrice: bistStart,
      bist100EndPrice: bistEnd,
      bist100Return: bistEnd / bistStart - 1.0,
      isCompleted: true,
    });
  }

  // Active period: latest marks vs live/current prices, plus positions the
  // pipeline already closed after that rotation (realized returns).
  const cLast = cycleDates[cycleDates.length - 1];
  const posByTicker = new Map(dbPositions.map(p => [p.ticker, p]));
  const activeStocks = [];
  byCycle.get(cLast).forEach(mark => {
    const ref = finiteNumber(mark.ref_price);
    if (!(ref > 0)) return;
    const pos = posByTicker.get(mark.ticker);
    if (pos && (!pos.selection_date || pos.selection_date <= todayDate)) {
      const latest = finiteNumber(livePrices[mark.ticker] ?? pos.current_price, null);
      if (latest != null && latest > 0) {
        activeStocks.push(modelCyclePositionResult({
          ticker: mark.ticker,
          selectionDate: pos.selection_date || cLast,
          cycleStart: cLast,
          modelEntryPrice: ref,
          modelExitPrice: latest,
          includeSellCost: true,
        }));
      }
      return;
    }
    const pendingExit = findPendingExit(mark.ticker);
    if (pendingExit && finiteNumber(pendingExit.exit_price, null) > 0) {
      activeStocks.push(modelCyclePositionResult({
        ticker: mark.ticker,
        selectionDate: pendingExit.selection_date || cLast,
        cycleStart: cLast,
        modelEntryPrice: ref,
        modelExitPrice: finiteNumber(pendingExit.exit_price),
        includeSellCost: false,
      }));
      return;
    }
    const exitRow = findExit(mark.ticker, cLast, null);
    if (exitRow && exitRow.exit_price != null && finiteNumber(exitRow.exit_price) > 0) {
      activeStocks.push(modelCyclePositionResult({
        ticker: mark.ticker,
        selectionDate: exitRow.selection_date || cLast,
        cycleStart: cLast,
        modelEntryPrice: ref,
        modelExitPrice: finiteNumber(exitRow.exit_price),
        includeSellCost: true,
      }));
    }
  });

  const bistStart = getPriceOnOrBefore('SPY', cLast);
  const latestPriceDate = getPriceDataState().stockPriceDate;
  const endDate = latestPriceDate || cLast;
  const bistEnd = getBenchmarkEndPrice(endDate);
  if (activeStocks.length > 0 && bistStart > 0 && bistEnd != null) {
    records.push({
      weekStartDate: cLast,
      weekEndDate: endDate,
      positions: activeStocks,
      portfolioReturn: activeStocks.reduce((s, r) => s + r.returnPct, 0) / activeStocks.length,
      bist100StartPrice: bistStart,
      bist100EndPrice: bistEnd,
      bist100Return: bistEnd / bistStart - 1.0,
      isCompleted: false,
    });
  }

  return records.sort((a, b) => a.weekStartDate.localeCompare(b.weekStartDate));
}

function buildLegacyCohortRecords(dbPositions, livePrices) {
  const todayDate = localIsoDate();
  const completedRows = queryAll(`
    SELECT * FROM portfolio_history
    WHERE selection_date >= :startDate
      AND selection_date IS NOT NULL
      AND exit_date IS NOT NULL
      AND exit_date <= :todayDate
    ORDER BY selection_date ASC, exit_date ASC, sort_order ASC
  `, {
    ':startDate': LIVE_TRACKING_START_DATE,
    ':todayDate': todayDate,
  });

  const activeStart = dbPositions
    .filter(position => !position.selection_date || position.selection_date <= todayDate)
    .map(position => position.selection_date)
    .filter(Boolean)
    .sort()
    .pop() || null;

  // Group by selection COHORT (rotation date) only. The old
  // `selection|exit` pair key split a cohort into two pseudo-weeks when
  // the pipeline stop-exited one name midweek (auto exits are live since
  // 2026-07-02), double-counting that week in the cumulative product.
  // A stopped position keeps its own realized exit; the freed slot sits
  // in cash, so the equal-weight mean of per-position returns is exact.
  // The still-active cohort is excluded here — it's rendered by the live
  // block below together with its already-realized exits.
  const cohorts = new Map();
  completedRows.forEach(row => {
    if (activeStart && row.selection_date === activeStart) return;
    if (!cohorts.has(row.selection_date)) cohorts.set(row.selection_date, []);
    cohorts.get(row.selection_date).push(row);
  });

  const records = [];
  cohorts.forEach((rows, startDate) => {
    const stockRecords = rows
      .filter(row => row.entry_price > 0 && row.exit_price != null)
      .map(row => {
        const result = modelPositionResult(row, row.exit_price);
        return {
          ticker: row.ticker,
          entryPrice: result.entryPrice,
          exitPrice: result.exitPrice,
          returnPct: result.returnFraction,
        };
      })
      .filter(row => row.returnPct != null);
    if (stockRecords.length === 0) return;

    const endDate = rows.map(row => row.exit_date).sort().pop();
    const bistStart = getPriceOnOrBefore('SPY', startDate);
    const bistEnd = getPriceOnOrBefore('SPY', endDate);
    if (!(bistStart > 0) || bistEnd == null) return;

    records.push({
      weekStartDate: startDate,
      weekEndDate: endDate,
      positions: stockRecords,
      portfolioReturn: stockRecords.reduce((sum, stock) => sum + stock.returnPct, 0) / stockRecords.length,
      bist100StartPrice: bistStart,
      bist100EndPrice: bistEnd,
      bist100Return: bistEnd / bistStart - 1.0,
      isCompleted: true
    });
  });

  if (activeStart && activeStart >= LIVE_TRACKING_START_DATE) {
    const liveStocks = dbPositions
      .filter(position => position.selection_date === activeStart)
      .map(position => {
        const modelEntry = position.entry_price || position.current_price;
        const latest = livePrices[position.ticker] || position.current_price || modelEntry;
        const result = modelPositionResult(position, latest);
        if (!(result.entryPrice > 0) || result.exitPrice == null || result.returnFraction == null) return null;
        return {
          ticker: position.ticker,
          entryPrice: result.entryPrice,
          exitPrice: result.exitPrice,
          returnPct: result.returnFraction,
        };
      })
      .filter(Boolean);

    // Positions from this cohort that the pipeline already closed
    // (stop-loss / target) contribute their REALIZED return to the week.
    const realizedStocks = completedRows
      .filter(row => row.selection_date === activeStart
        && row.entry_price > 0 && row.exit_price != null)
      .map(row => {
        const result = modelPositionResult(row, row.exit_price);
        return {
          ticker: row.ticker,
          entryPrice: result.entryPrice,
          exitPrice: result.exitPrice,
          returnPct: result.returnFraction,
        };
      })
      .filter(row => row.returnPct != null);

    const activeStocks = [...liveStocks, ...realizedStocks];

    const bistStart = getPriceOnOrBefore('SPY', activeStart);
    const latestPriceDate = getPriceDataState().stockPriceDate;
    const endDate = latestPriceDate || activeStart;
    const bistEnd = getBenchmarkEndPrice(endDate);

    if (activeStocks.length > 0 && bistStart > 0 && bistEnd != null) {
      records.push({
        weekStartDate: activeStart,
        weekEndDate: endDate,
        positions: activeStocks,
        portfolioReturn: activeStocks.reduce((sum, stock) => sum + stock.returnPct, 0) / activeStocks.length,
        bist100StartPrice: bistStart,
        bist100EndPrice: bistEnd,
        bist100Return: bistEnd / bistStart - 1.0,
        isCompleted: false
      });
    }
  }

  return records.sort((a, b) => a.weekStartDate.localeCompare(b.weekStartDate));
}

// ==========================================================================
// 6. Navigation Router
// ==========================================================================

function setupNavigation() {
  const navItems = document.querySelectorAll('.nav-item');
  const tabPages = document.querySelectorAll('.tab-page');
  const scroller = document.querySelector('.page-content');
  // Native-app behaviour: each tab remembers its scroll position, and
  // re-tapping the active tab scrolls it back to the top.
  const tabScroll = {};
  let currentTab = document.querySelector('.nav-item.active')?.dataset.tab || 'picks';

  navItems.forEach(item => {
    item.addEventListener('click', () => {
      const tab = item.getAttribute('data-tab');

      if (tab === currentTab) {
        scroller?.scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }
      if (scroller) tabScroll[currentTab] = scroller.scrollTop;

      navItems.forEach(i => i.classList.remove('active'));
      tabPages.forEach(p => p.classList.remove('active'));

      item.classList.add('active');
      const targetPage = document.getElementById(`page-${tab}`);
      if (targetPage) targetPage.classList.add('active');

      renderPage(tab);
      if (scroller) scroller.scrollTop = tabScroll[tab] || 0;
      currentTab = tab;
    });
  });
}

function renderPage(tab) {
  if (tab === 'picks') renderPicksPage();
  else if (tab === 'browse') renderBrowsePage();
  else if (tab === 'market') renderMarketPage();
  else if (tab === 'history') renderHistoryPage();
}

// ==========================================================================
// 7. Page Render Implementations
// ==========================================================================

// --- Stale feed detection ---
// Business days elapsed strictly after dateStr up to today. Morning cron
// runs legitimately carry the previous close, so 0-1 business days is fresh.
function businessDaysSince(dateStr) {
  const start = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(start.getTime())) return null;
  const now = new Date();
  let count = 0;
  const cursor = new Date(start);
  cursor.setDate(cursor.getDate() + 1);
  while (cursor <= now && count < 60) {
    const dow = cursor.getDay();
    if (dow !== 0 && dow !== 6) count += 1;
    cursor.setDate(cursor.getDate() + 1);
  }
  return count;
}

function getSnapshotMetadata() {
  return queryOne('SELECT snapshot_date, latest_price_date, exported_at FROM snapshot_metadata WHERE id = 1') || {};
}

function getMarketWidePriceDate() {
  const rows = queryAll(`
    SELECT ph.date AS date, COUNT(DISTINCT ph.company_id) AS company_count
    FROM price_history_730d ph
    JOIN companies c ON c.id = ph.company_id
    WHERE c.ticker != 'SPY'
      AND c.is_active = 1
    GROUP BY ph.date
    ORDER BY ph.date DESC
    LIMIT 20
  `);
  if (rows.length === 0) {
    return getSnapshotMetadata().latest_price_date || null;
  }

  const maxCoverage = rows.reduce(
    (max, row) => Math.max(max, finiteNumber(row.company_count, 0)),
    0
  );
  const coverageFloor = Math.max(1, Math.floor(maxCoverage * 0.5));
  const broadDate = rows.find(row => finiteNumber(row.company_count, 0) >= coverageFloor);
  return broadDate?.date || rows[0]?.date || null;
}

function getPriceDataState() {
  const metadata = getSnapshotMetadata();
  const stockPriceDate = getMarketWidePriceDate();
  const metadataPriceDate = metadata.latest_price_date || null;
  return {
    portfolioDate: metadata.snapshot_date || null,
    exportedAt: metadata.exported_at || null,
    metadataPriceDate,
    stockPriceDate: stockPriceDate || metadataPriceDate,
    hasMetadataMismatch: Boolean(
      stockPriceDate && metadataPriceDate && stockPriceDate !== metadataPriceDate
    )
  };
}

function summarizeQuoteCoverage(tickers) {
  const summary = {
    total: tickers.length,
    recent: 0,
    delayed: 0,
    snapshot: 0,
    latestQuoteTime: null,
    labels: []
  };

  tickers.forEach(ticker => {
    const status = getQuoteStatus(ticker);
    summary.labels.push(status.label);
    if (status.label === '15 DK GEC.') {
      summary.recent += 1;
    } else if (status.label === 'SON İŞLEM') {
      summary.delayed += 1;
    } else {
      summary.snapshot += 1;
    }

    const detail = window.livePriceDetails[ticker];
    const quoteTimestamp = Date.parse(String(detail?.quote_time || ''));
    const latestTimestamp = Date.parse(String(summary.latestQuoteTime || ''));
    if (Number.isFinite(quoteTimestamp)
        && (!Number.isFinite(latestTimestamp) || quoteTimestamp > latestTimestamp)) {
      summary.latestQuoteTime = detail.quote_time;
    }
  });

  return summary;
}

function formatQuoteCoverage(summary) {
  if (!summary || summary.total === 0) return '—';
  const parts = [`${summary.recent}/${summary.total} taze`];
  if (summary.delayed > 0) parts.push(`${summary.delayed} son işlem`);
  if (summary.snapshot > 0) parts.push(`${summary.snapshot} snapshot`);
  return parts.join(' • ');
}

function getBenchmarkEndPrice(endDate) {
  const nearLive = getQuoteStatus('SPY').label !== 'SNAPSHOT';
  const liveBenchmark = finiteNumber(window.livePrices.SPY, 0);
  if (nearLive && liveBenchmark > 0) return liveBenchmark;
  return getPriceOnOrBefore('SPY', endDate);
}

function renderDataHealthCard(positions) {
  const card = document.getElementById('data-health-card');
  if (!card) return;

  const state = getPriceDataState();
  setTextById('health-portfolio-date', state.portfolioDate || '—');
  setTextById('health-signal-date', state.stockPriceDate || '—');

  const quoteSummary = summarizeQuoteCoverage(positions.map(pos => pos.ticker));
  setTextById('health-live-source', formatQuoteCoverage(quoteSummary));

  const rotationDate = positions
    .map(pos => pos.cycle_ref_date || pos.selection_date)
    .filter(Boolean)
    .sort()
    .pop() || null;
  const rotation = getRotationInfo(rotationDate);
  setTextById('health-next-rotation', rotation.nextLabel || '—');

  const noteEl = document.getElementById('health-data-note');
  if (!noteEl) return;
  const notes = [];
  if (state.hasMetadataMismatch) {
    notes.push(`Endeks tarihi ${state.metadataPriceDate}, hisse kapanışı ${state.stockPriceDate}. Portföy fiyatlarında hisse kapanışı esas alınıyor.`);
  }
  if (quoteSummary.delayed > 0) {
    const latestLabel = formatQuoteTime(quoteSummary.latestQuoteTime);
    notes.push(`Canlı feed taze değil: ${quoteSummary.delayed} hisse son işlem fiyatıyla gösteriliyor${latestLabel ? ` (son quote ${latestLabel})` : ''}.`);
  }
  if (quoteSummary.snapshot > 0) {
    notes.push(`${quoteSummary.snapshot} hisse canlı feed'de yok; snapshot kapanışı kullanılıyor.`);
  }

  if (notes.length > 0) {
    card.classList.add('warning');
    noteEl.style.display = 'block';
    noteEl.textContent = notes.join(' ');
  } else {
    card.classList.remove('warning');
    noteEl.style.display = 'none';
    noteEl.textContent = '';
  }
}

function renderStaleFeedBanner() {
  const container = document.getElementById('page-picks');
  if (!container) return;
  let banner = document.getElementById('stale-feed-banner');

  const priceState = getPriceDataState();
  const priceDate = priceState.stockPriceDate;
  const staleDays = priceDate ? businessDaysSince(priceDate) : null;

  if (staleDays == null || staleDays <= 1) {
    if (banner) banner.remove();
    return;
  }

  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'stale-feed-banner';
    container.insertBefore(banner, container.firstElementChild);
  }
  const severe = staleDays >= 5;
  banner.className = severe ? 'stale-banner stale-banner-severe' : 'stale-banner';
  const message = `Veri ${staleDays} iş günü eski (son fiyat: ${escapeHtml(priceDate)}). ${severe ? 'Feed durmuş olabilir — GitHub Actions kontrol edilmeli.' : 'Model kararları güncel olmayabilir.'}`;
  banner.innerHTML = `
    <span class="material-symbols-rounded">${severe ? 'error' : 'warning'}</span>
    <span>${message}</span>
  `;
}

// --- PAGE 1: PICKS (Home) ---
function renderPicksPage() {
  const home = queryOne('SELECT * FROM home_summary WHERE id = 1');
  const updateDateEl = document.getElementById('app-update-date');
  const priceState = getPriceDataState();
  if (priceState.portfolioDate || priceState.stockPriceDate) {
    updateDateEl.textContent = `Portföy: ${priceState.portfolioDate || '—'} • Sinyal: ${priceState.stockPriceDate || '—'}`;
  } else if (home && home.macro_date) {
    updateDateEl.textContent = `Güncelleme: ${home.macro_date}`;
  }
  renderStaleFeedBanner();

  const positions = queryAll(`
    SELECT op.*, c.name as fullname 
    FROM open_positions op 
    LEFT JOIN companies c ON c.ticker = op.ticker 
    ORDER BY op.sort_order ASC
  `);
  
  const portListEl = document.getElementById('portfolio-list');
  document.getElementById('picks-count-badge').textContent = `${positions.length} Hisse`;
  renderDataHealthCard(positions);
  renderDecisionSummary(home, positions);
  
  portListEl.innerHTML = '';
  if (positions.length === 0) {
    portListEl.innerHTML = `<p style="color:var(--text-muted); font-size:12px; text-align:center; padding:24px;">Portföyde hisse bulunmuyor.</p>`;
  } else {
    positions.forEach(pos => {
      const live = finiteNumber(window.livePrices[pos.ticker] ?? pos.current_price, 0);
      const rawEntry = finiteNumber(pos.entry_price);
      const entry = rawEntry > 0 ? rawEntry : (live > 0 ? live : 1.0);
      // Home always shows the public model position. Cost assumptions belong
      // to History, where gross and cost-adjusted model returns can be compared.
      const pnl = ((live / entry) - 1) * 100;
      const pnlClass = pnl >= 0 ? 'pos-text' : 'neg-text';
      const safeTicker = escapeHtml(pos.ticker);
      const safeName = escapeHtml(pos.fullname || pos.name || '—');
      const quoteStatus = getQuoteStatus(pos.ticker);
      const qualityFlags = parseQualityFlags(pos.quality_flags_json);
      const qualityFlagLine = qualityFlags.length > 0
        ? `<div class="portfolio-risk-chips">${qualityFlags.slice(0, 2).map(flag => (
            `<span class="mini-risk-chip" title="${escapeHtml(qualityFlagDetail(flag))}">${escapeHtml(qualityFlagLabel(flag))}</span>`
          )).join('')}</div>`
        : '';

      // Period P&L (since the latest rotation) — only shown when the
      // position is actually carried from an earlier rotation.
      const cycleRef = finiteNumber(pos.cycle_ref_price, null);
      let periodLine = '';
      if (cycleRef != null && cycleRef > 0 && live > 0
          && pos.cycle_ref_date && pos.cycle_ref_date !== pos.selection_date) {
        const periodPnl = ((live / cycleRef) - 1) * 100;
        const periodClass = periodPnl >= 0 ? 'pos-text' : 'neg-text';
        periodLine = `<div class="${periodClass} tabular-nums" style="font-size:9px; margin-top:1px;">Dönem: ${periodPnl >= 0 ? '+' : ''}${periodPnl.toFixed(1)}%</div>`;
      }

      // Glanceable cost basis: entry price + entry date without opening detail.
      let entryLine = '';
      if (rawEntry > 0) {
        let sinceShort = '';
        let signalShort = '';
        if (pos.selection_date) {
          const d = new Date(pos.selection_date + 'T00:00:00');
          if (!Number.isNaN(d.getTime())) {
            sinceShort = ' · portföy ' + d.toLocaleDateString('tr-TR', { day: 'numeric', month: 'short' });
          }
        }
        if (pos.signal_date && pos.signal_date !== pos.selection_date) {
          const d = new Date(pos.signal_date + 'T00:00:00');
          if (!Number.isNaN(d.getTime())) {
            signalShort = ' · veri ' + d.toLocaleDateString('tr-TR', { day: 'numeric', month: 'short' });
          }
        }
        entryLine = `<div class="entry-line tabular-nums">Model ref ${rawEntry.toFixed(2)}${signalShort}${sinceShort}</div>`;
      }

      // Stop proximity: warn at a glance when price approaches the stop —
      // and KEEP warning once it breaches (backend exit lands next cron;
      // a vanishing chip would read as "danger passed").
      const stopPrice = finiteNumber(pos.stop_loss_price, 0);
      let stopChip = '';
      if (stopPrice > 0 && live > 0) {
        if (live <= stopPrice) {
          stopChip = `<div class="stop-chip danger tabular-nums" title="Stop: $${stopPrice.toFixed(2)}">Stop kırıldı</div>`;
        } else {
          const stopDistPct = ((live - stopPrice) / live) * 100;
          if (stopDistPct <= 8) {
            const sev = stopDistPct <= 4 ? 'danger' : 'warn';
            stopChip = `<div class="stop-chip ${sev} tabular-nums" title="Stop: $${stopPrice.toFixed(2)}">Stop'a %${stopDistPct.toFixed(1)}</div>`;
          }
        }
      }

      const row = document.createElement('div');
      row.className = 'list-item-row';
      row.innerHTML = `
        <div>
          <div class="ticker-block">
            <span class="ticker-name">${safeTicker}</span>
            <span class="${quoteStatus.className}" title="${escapeHtml(quoteStatus.title)}">${quoteStatus.label}</span>
          </div>
          <div class="company-fullname">${safeName}</div>
          ${entryLine}
          ${qualityFlagLine}
        </div>
        <div style="text-align: right;">
          <div class="tabular-nums" style="font-weight:700; font-size:14px;">$${live.toFixed(2)}</div>
          <div class="${pnlClass} tabular-nums" style="font-size:11px; font-weight:800; margin-top:2px;">
            ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%
          </div>
          ${periodLine}
          ${stopChip}
        </div>
      `;
      row.addEventListener('click', () => openStockDetail(pos.ticker));
      portListEl.appendChild(row);
    });
  }

  // --- REBALANCE SIGNALS: derived from ACTUAL portfolio decisions.
  // Previously re-derived client-side from scoring_latest top-12, which
  // could contradict the real selector (sector caps, correlation filter,
  // index-aware incumbent window). Now: rotation diff from open_positions
  // + portfolio_history — exactly what the backend actually did.
  const decisions = computeRebalanceDecisions(positions);
  renderTradeDayCard(decisions);
  renderIntraweekExits(decisions);

  const signalsListEl = document.getElementById('signals-list');
  signalsListEl.innerHTML = '';

  const finalSignals = [...decisions.sells, ...decisions.buys, ...decisions.holds];

  if (finalSignals.length === 0) {
    signalsListEl.innerHTML = `<p style="color:var(--text-muted); font-size:11px; font-style:italic;">Rotasyon kararı bulunmuyor.</p>`;
  } else {
    finalSignals.forEach(sig => {
      const colorMap = { BUY: 'var(--success)', SELL: 'var(--danger)', HOLD: 'var(--secondary)' };
      const labelMap = { BUY: 'AL', SELL: 'SAT', HOLD: 'TUT' };

      const row = document.createElement('div');
      row.className = 'list-item-row';
      row.style.borderBottom = 'none';
      row.style.padding = '8px 0';
      row.innerHTML = `
        <div>
          <div style="font-weight:700; font-size:12px;">${escapeHtml(sig.ticker)}</div>
          <div style="font-size:10px; color:var(--text-muted); margin-top:2px;">${escapeHtml(sig.reason)}</div>
        </div>
        <div style="font-weight:900; font-size:12px; color:${colorMap[sig.action]}">${labelMap[sig.action]}</div>
      `;
      row.addEventListener('click', () => openStockDetail(sig.ticker));
      signalsListEl.appendChild(row);
    });
  }
}

// Days until the manifest's next rotation date; null when unknown/stale.
function rotationCountdownText() {
  const nextIso = window.feedManifest?.rotation?.next_rotation_date;
  if (!nextIso) return null;
  const next = new Date(nextIso + 'T00:00:00');
  if (Number.isNaN(next.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((next - today) / 86400000);
  if (days < 0) return null; // stale manifest — don't show a wrong countdown
  const dateLabel = next.toLocaleDateString('tr-TR', { day: 'numeric', month: 'long', weekday: 'long' });
  let text;
  if (days === 0) text = `Rotasyon günü BUGÜN — ${dateLabel}`;
  else if (days === 1) text = `Sonraki rotasyon YARIN — ${dateLabel}`;
  else text = `Sonraki rotasyona ${days} gün — ${dateLabel}`;
  return { days, text };
}

// --- Rotation cadence helpers ---------------------------------------------
// Cadence is published by the backend in manifest.rotation (bi-weekly since
// 2026-07-04, C1 calibration). Falls back to 2 weeks when the manifest
// predates the field. Next rotation = last rotation Monday + N weeks.
function getRotationInfo(rotationDate) {
  const weeks = Number(window.feedManifest?.rotation?.rotation_weeks) > 0
    ? Number(window.feedManifest.rotation.rotation_weeks) : 2;
  let next = null;
  if (window.feedManifest?.rotation?.next_rotation_date) {
    const d = new Date(window.feedManifest.rotation.next_rotation_date + 'T00:00:00');
    if (!Number.isNaN(d.getTime())) next = d;
  }
  if (!next && rotationDate) {
    const d = new Date(rotationDate + 'T00:00:00');
    if (!Number.isNaN(d.getTime())) {
      d.setDate(d.getDate() + weeks * 7);
      next = d;
    }
  }
  const nextLabel = next
    ? next.toLocaleDateString('tr-TR', { day: 'numeric', month: 'long', weekday: 'long' })
    : null;
  return { weeks, nextLabel };
}

// --- Rebalance decisions from actual position data -----------------------
// rotationDate = the latest ALPHA selection date. On that date the selector
// exits every prior position and re-enters the kept ones, so:
//   HOLD = exited on rotationDate AND currently open
//   BUY  = currently open, selected on rotationDate, NOT exited that day
//   SELL = exited on rotationDate AND no longer open
// Positions carried with an older selection_date (future continuity mode)
// also count as HOLD. Risk exits AFTER the rotation (stop-loss / target /
// thesis breaker, applied automatically by the backend) are listed apart.
function computeRebalanceDecisions(positions) {
  const result = { rotationDate: null, buys: [], holds: [], sells: [], riskExits: [] };

  // B1 continuity (v16): held positions keep their original selection_date,
  // so the latest rotation is MAX(cycle_ref_date). Computed in JS from the
  // already-loaded rows — queryOne swallows SQL errors, so a COALESCE(...)
  // over a column that legacy snapshots lack would silently return null.
  // Missing column -> undefined -> falls back to selection_date per row.
  result.rotationDate = positions
    .map(pos => pos.cycle_ref_date || pos.selection_date)
    .filter(Boolean)
    .sort()
    .pop() || null;
  if (!result.rotationDate) return result;

  const exitedOnRotation = queryAll(`
    SELECT ticker, selection_date, entry_price, exit_price, pnl_pct
    FROM portfolio_history
    WHERE portfolio = 'ALPHA' AND exit_date = :d
    ORDER BY sort_order ASC
  `, { ':d': result.rotationDate });
  const exitedTickers = new Set(exitedOnRotation.map(r => r.ticker));
  const openTickers = new Set(positions.map(p => p.ticker));

  positions.forEach(pos => {
    if (pos.selection_date === result.rotationDate && !exitedTickers.has(pos.ticker)) {
      result.buys.push({
        ticker: pos.ticker, action: 'BUY',
        reason: `Rotasyon: portföye yeni girdi (model ref $${finiteNumber(pos.entry_price).toFixed(2)} — ${formatDateToTurkish(pos.signal_date || pos.selection_date)} tamamlanmış kapanışı). Stop uyarısı gelmedikçe sonraki rotasyona kadar tut.`,
      });
    } else {
      // Continuity: original entry date + real P&L since entry make the
      // hold instruction concrete.
      const heldSince = pos.selection_date
        ? ` ${formatDateToTurkish(pos.selection_date)}'den beri portföyde` : '';
      result.holds.push({
        ticker: pos.ticker, action: 'HOLD',
        reason: `Rotasyonda korundu; işlem gerekmez.${heldSince ? heldSince + '.' : ''} Sonraki rotasyona kadar tutmaya devam et.`,
      });
    }
  });

  const seenSells = new Set();
  exitedOnRotation.forEach(row => {
    if (openTickers.has(row.ticker) || seenSells.has(row.ticker)) return;
    seenSells.add(row.ticker);
    const pnl = row.pnl_pct != null ? ` (${row.pnl_pct >= 0 ? '+' : ''}${finiteNumber(row.pnl_pct).toFixed(1)}%)` : '';
    result.sells.push({
      ticker: row.ticker, action: 'SELL',
      reason: `Rotasyonda çıkarıldı${pnl}.`,
    });
  });

  result.riskExits = queryAll(`
    SELECT ticker, selection_date, entry_price,
           exit_date, exit_price, pnl_pct, exit_reason
    FROM portfolio_history
    WHERE portfolio = 'ALPHA' AND exit_date > :d
      AND exit_reason IN ('STOP_LOSS', 'TARGET', 'THESIS_BREAKER')
    ORDER BY exit_date DESC, sort_order ASC
  `, { ':d': result.rotationDate });

  return result;
}

// --- Trade-day checklist card (rotation day only) -------------------------
function tradeDoneKey(rotationDate) { return `bp_trade_done_${rotationDate}`; }

function loadTradeDone(rotationDate) {
  try { return new Set(JSON.parse(localStorage.getItem(tradeDoneKey(rotationDate)) || '[]')); }
  catch { return new Set(); }
}

function saveTradeDone(rotationDate, doneSet) {
  try { localStorage.setItem(tradeDoneKey(rotationDate), JSON.stringify([...doneSet])); } catch { /* quota */ }
}

function renderTradeDayCard(decisions) {
  const container = document.getElementById('trade-day-container');
  if (!container) return;
  container.innerHTML = '';

  const todayIso = localIsoDate();
  const actionable = [...decisions.sells, ...decisions.buys];
  const daysUntilRotation = daysBetweenIsoDates(todayIso, decisions.rotationDate);
  if (daysUntilRotation == null || daysUntilRotation < 0 || daysUntilRotation > 1 || actionable.length === 0) return;
  const isTomorrow = daysUntilRotation === 1;

  const done = loadTradeDone(decisions.rotationDate);
  const card = document.createElement('div');
  card.className = 'trade-day-card';

  const rows = actionable.map(sig => {
    const isDone = done.has(sig.ticker);
    const label = sig.action === 'SELL' ? 'SAT' : 'AL';
    const cls = sig.action === 'SELL' ? 'trade-sell' : 'trade-buy';
    return `
      <div class="trade-step ${isDone ? 'trade-done' : ''}" data-ticker="${escapeHtml(sig.ticker)}">
        <button type="button" class="trade-toggle" data-trade-toggle="${escapeHtml(sig.ticker)}">
          <span class="material-symbols-rounded trade-check">${isDone ? 'check_circle' : 'radio_button_unchecked'}</span>
          <span class="trade-ticker">${escapeHtml(sig.ticker)}</span>
          <span class="trade-action ${cls}">${label}</span>
        </button>
      </div>`;
  }).join('');

  const rotation = getRotationInfo(decisions.rotationDate);
  const actionDay = isTomorrow ? 'Yarın' : 'Bugün';
  const nextNote = rotation.nextLabel
    ? ` ${actionDay} aldıklarını ${rotation.weeks} hafta tut — sonraki rotasyon: ${rotation.nextLabel}.`
    : ` ${actionDay} aldıklarını ${rotation.weeks} hafta tut.`;
  card.innerHTML = `
    <div class="trade-day-header">
      <span class="material-symbols-rounded">task_alt</span>
      <div>
        <h3>${isTomorrow ? 'Yarın İşlem Günü' : 'Bugün İşlem Günü'}</h3>
        <p>Portföy açılıştan önce hazır. Önce SAT, sonra AL; emirleri ${isTomorrow ? 'yarın ' : ''}açılışta ver.${nextNote}</p>
      </div>
    </div>
    ${rows}`;

  card.querySelectorAll('.trade-step').forEach(row => {
    const ticker = row.getAttribute('data-ticker');
    row.querySelector('[data-trade-toggle]')?.addEventListener('click', () => {
      const current = loadTradeDone(decisions.rotationDate);
      if (current.has(ticker)) current.delete(ticker); else current.add(ticker);
      saveTradeDone(decisions.rotationDate, current);
      try { navigator.vibrate?.(12); } catch { /* unsupported */ }
      renderTradeDayCard(decisions);
    });
  });

  container.appendChild(card);
}

// --- Intraweek risk exits (backend auto-closed positions) ------------------
function renderIntraweekExits(decisions) {
  const container = document.getElementById('intraweek-exits-container');
  if (!container) return;
  container.innerHTML = '';
  if (!decisions.riskExits.length) return;

  const reasonLabel = {
    STOP_LOSS: 'Stop-loss tetiklendi',
    TARGET: 'Hedef fiyata ulaştı',
    THESIS_BREAKER: 'İçeriden satış uyarısı',
  };

  const rows = decisions.riskExits.map(row => {
    const pnl = row.pnl_pct != null
      ? `<span class="${row.pnl_pct >= 0 ? 'pos-text' : 'neg-text'}">${row.pnl_pct >= 0 ? '+' : ''}${finiteNumber(row.pnl_pct).toFixed(1)}%</span>` : '';
    const price = row.exit_price != null ? `$${finiteNumber(row.exit_price).toFixed(2)}` : '';
    return `
      <div class="risk-exit-row" data-risk-exit="${escapeHtml(row.ticker)}">
        <div>
          <strong>${escapeHtml(row.ticker)}</strong>
          <span class="risk-exit-reason">${reasonLabel[row.exit_reason] || escapeHtml(row.exit_reason)} • ${escapeHtml(row.exit_date || '')}</span>
        </div>
        <div class="risk-exit-price">${price} ${pnl}</div>
      </div>`;
  }).join('');

  container.innerHTML = `
    <div class="risk-exit-card">
      <div class="risk-exit-header">
        <span class="material-symbols-rounded">notification_important</span>
        <div>
          <h3>Rotasyon Arasında Çıkıldı</h3>
          <p>Model bu pozisyonları otomatik kapattı (stop/hedef). Sen de aynı gün brokerında sat; yeri bir sonraki rotasyona kadar nakitte kalır.</p>
        </div>
      </div>
      ${rows}
    </div>`;
}

// --- PAGE 2: BROWSE ---
let currentFilterMode = 'ALL';

function setupBrowseFilters() {
  const pills = document.querySelectorAll('.filter-pill');
  pills.forEach(pill => {
    pill.addEventListener('click', () => {
      pills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      currentFilterMode = pill.getAttribute('data-mode');
      renderBrowsePage();
    });
  });

  // Debounced so fast typing re-queries SQLite once, not per keystroke.
  let searchDebounce = null;
  document.getElementById('browse-search').addEventListener('input', () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(renderBrowsePage, 140);
  });
  document.getElementById('filter-sector').addEventListener('change', renderBrowsePage);
  document.getElementById('filter-risk').addEventListener('change', renderBrowsePage);
  document.getElementById('filter-sort').addEventListener('change', renderBrowsePage);
}

function populateSectorDropdown() {
  const sectors = queryAll('SELECT DISTINCT sector FROM scoring_latest WHERE sector IS NOT NULL ORDER BY sector');
  const dropdown = document.getElementById('filter-sector');
  dropdown.innerHTML = '<option value="">Sektör: Tümü</option>';
  sectors
    .map(s => ({ value: s.sector, label: sectorLabel(s.sector) }))
    .sort((a, b) => a.label.localeCompare(b.label, 'tr'))
    .forEach(({ value, label }) => {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = label;
      dropdown.appendChild(opt);
    });
}

function renderBrowsePage() {
  const searchVal = document.getElementById('browse-search').value.trim().toUpperCase();
  const sectorVal = document.getElementById('filter-sector').value;
  const riskVal = document.getElementById('filter-risk').value;
  const sortVal = document.getElementById('filter-sort').value;

  let query = `SELECT * FROM scoring_latest WHERE is_active = 1`;
  const params = {};

  if (currentFilterMode === 'ALPHA_CORE') {
    query += ` AND alpha_core_eligible = 1`;
  } else if (currentFilterMode === 'ALPHA_X') {
    query += ` AND alpha_x_eligible = 1`;
  } else if (currentFilterMode === 'RESEARCH') {
    query += ` AND (alpha_research_bucket = 'QualityShadow' OR alpha_research_bucket = 'FreeFloatShadow' OR alpha_research_bucket = 'NonCoreResearch')`;
  }

  if (searchVal) {
    query += ` AND (ticker LIKE :search OR name LIKE :search)`;
    params[':search'] = `%${searchVal}%`;
  }

  if (sectorVal) {
    query += ` AND sector = :sector`;
    params[':sector'] = sectorVal;
  }

  if (riskVal) {
    query += ` AND risk = :risk`;
    params[':risk'] = riskVal;
  }

  const countQuery = query.replace(
    'SELECT * FROM scoring_latest',
    'SELECT COUNT(*) AS total FROM scoring_latest'
  );
  const totalRows = finiteNumber(queryOne(countQuery, params)?.total);

  if (sortVal === 'SCORE_DESC') {
    query += ` ORDER BY ranking_score DESC`;
  } else if (sortVal === 'TICKER_ASC') {
    query += ` ORDER BY ticker ASC`;
  } else if (sortVal === 'RISK_ASC') {
    query += ` ORDER BY CASE risk WHEN 'LOW' THEN 0 WHEN 'MEDIUM' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END ASC`;
  }

  query += ' LIMIT 200';
  const rows = queryAll(query, params);
  const listEl = document.getElementById('browse-list');
  const resultInfo = document.getElementById('browse-result-info');
  listEl.innerHTML = '';
  resultInfo.textContent = totalRows > rows.length
    ? `${totalRows} sonuçtan ilk ${rows.length} kayıt gösteriliyor. Arama veya filtre kullanarak daraltın.`
    : `${totalRows} sonuç`;

  if (rows.length === 0) {
    listEl.innerHTML = `<p style="color:var(--text-muted); font-size:12px; text-align:center; padding:32px;">Hisse bulunamadı.</p>`;
    return;
  }

  rows.forEach(row => {
    // Show the same metric the default sort uses (ranking_score, 0-100).
    // The old "+65.2%" rendering formatted the alpha SCORE like a return
    // and made the score-sorted list look shuffled.
    const scoreVal = finiteNumber(row.ranking_score, finiteNumber(row.alpha));
    const riskText = escapeHtml(row.risk || 'UNKNOWN');

    const item = document.createElement('div');
    item.className = 'list-item-row';
    item.innerHTML = `
      <div>
        <div class="ticker-block">
          <span class="ticker-name">${escapeHtml(row.ticker)}</span>
        </div>
        <div class="company-fullname">${escapeHtml(row.name || '—')}</div>
      </div>
      <div style="text-align: right;">
        <div class="tabular-nums" style="font-weight: 800; font-size: 14px; color: var(--primary);">
          ${scoreVal.toFixed(1)}
        </div>
        <div class="score-bar"><i style="width: ${Math.max(0, Math.min(100, scoreVal))}%;"></i></div>
        <div style="font-size: 10px; color: var(--text-muted); font-weight: bold; margin-top:2px;">
          SKOR • ${riskText} RİSK
        </div>
      </div>
    `;
    item.addEventListener('click', () => openStockDetail(row.ticker));
    listEl.appendChild(item);
  });
}

// --- PAGE 3: MARKET (Macro Indicators) ---
function renderMarketPage() {
  const home = queryOne('SELECT * FROM home_summary WHERE id = 1');
  if (!home) return;

  const policyRate = home.policy_rate_pct || 0.0;
  const inflation = home.cpi_yoy_pct || 0.0;
  const realYield = policyRate - inflation;
  const isPositive = realYield > 0;

  document.getElementById('macro-policy-rate').textContent = `${(policyRate * 100).toFixed(1)}%`;
  document.getElementById('macro-inflation').textContent = `${(inflation * 100).toFixed(1)}%`;
  
  const yieldValEl = document.getElementById('macro-real-yield-val');
  const yieldBadgeEl = document.getElementById('macro-real-yield-badge');
  const yieldPinEl = document.getElementById('macro-yield-pin');
  const yieldDescEl = document.getElementById('macro-yield-desc');

  yieldValEl.textContent = `${realYield >= 0 ? '+' : ''}${(realYield * 100).toFixed(2)}%`;
  yieldValEl.className = `yield-value ${isPositive ? 'pos-text' : 'neg-text'}`;

  if (isPositive) {
    yieldBadgeEl.textContent = 'POZİTİF REEL';
    yieldBadgeEl.style.backgroundColor = 'rgba(34, 197, 94, 0.15)';
    yieldBadgeEl.style.color = 'var(--success)';
    yieldDescEl.textContent = "Pozitif Reel Faiz: Nakit ve risksiz mevduat getirileri enflasyon karşısında değerini korur. Seçici hisse yatırımları sürdürülmelidir.";
  } else {
    yieldBadgeEl.textContent = 'NEGATİF REEL';
    yieldBadgeEl.style.backgroundColor = 'rgba(239, 68, 68, 0.15)';
    yieldBadgeEl.style.color = 'var(--danger)';
    yieldDescEl.textContent = "Negatif Reel Faiz: Nakit ve mevduat tutmak reel değer kaybı yaratır. Hisse senetleri ve reel varlıklar enflasyona karşı doğal korumadır.";
  }

  // Pin slider calculations (clamped between -15% and +15%)
  const clampedYield = Math.max(-0.15, Math.min(0.15, realYield));
  const pinPercent = ((clampedYield + 0.15) / 0.30) * 100;
  yieldPinEl.style.left = `${pinPercent}%`;
  yieldPinEl.style.borderColor = isPositive ? 'var(--success)' : 'var(--danger)';

  // Cash allocation split bar
  const cashState = home.cash_state || 'NORMAL';
  const cashPct = home.cash_pct || 0.0;
  const targetState = home.cash_target_state || 'NORMAL';
  
  const stateColorMap = {
    NORMAL: 'var(--success)',
    CAUTION: 'var(--warning)',
    DEFENSIVE: 'var(--warning)',
    RISK_OFF: 'var(--danger)'
  };
  const labelMap = {
    NORMAL: 'NORMAL (Hisse Ağırlıklı)',
    CAUTION: 'İHTİYAT (Seçici Nakit)',
    DEFENSIVE: 'SAVUNMA (Yüksek Nakit)',
    RISK_OFF: 'RİSK DIŞI (%75 Nakit)'
  };

  const cashStateEl = document.getElementById('macro-cash-state');
  cashStateEl.textContent = labelMap[cashState] || cashState;
  cashStateEl.style.color = stateColorMap[cashState] || 'var(--text)';

  document.getElementById('macro-cash-target').textContent = targetState;
  document.getElementById('macro-cash-split').textContent = `${(cashPct * 100).toFixed(0)}% Nakit / ${((1 - cashPct) * 100).toFixed(0)}% Hisse`;
  
  const cashProgress = document.getElementById('macro-cash-progress');
  cashProgress.style.width = `${cashPct * 100}%`;
  cashProgress.style.backgroundColor = stateColorMap[cashState] || 'var(--primary)';

  // cash_notes carries raw state-machine tokens (e.g. "steady"); translate
  // the known ones instead of leaking internals to the UI.
  const cashNoteMap = {
    steady: 'Sinyal sabit; kademe değişikliği beklenmiyor.',
    cooldown: 'Geçiş sonrası bekleme süresi aktif; kademe kilitli.',
    confirming_up: 'Stres sinyali doğrulanıyor; nakit kademesi yükselebilir.',
    confirming_down: 'Rahatlama sinyali doğrulanıyor; hisse ağırlığı artabilir.',
  };
  const notesEl = document.getElementById('macro-cash-notes');
  const rawNote = String(home.cash_notes || '').trim();
  const rawSignal = finiteNumber(home.cash_raw_signal, null);
  const rawSignalText = rawSignal !== null ? `Ham stres sinyali ${rawSignal}/4. ` : '';
  const thresholdText = 'Nakit artışı için sinyalin en az 2/4 seviyesinde 5 iş günü kalması gerekir; sonraki kademeler 20 günlük bekleme sonrası tek tek yükselir.';
  if (rawNote || rawSignal !== null) {
    notesEl.textContent = `${rawSignalText}${cashNoteMap[rawNote.toLowerCase()] || rawNote || 'Nakit sinyali okunuyor.'} ${thresholdText}`;
    notesEl.style.display = 'block';
  } else {
    notesEl.style.display = 'none';
  }

  // Piyasa rejimi
  const regimeEl = document.getElementById('macro-regime');
  regimeEl.textContent = home.regime || '—';
  regimeEl.style.color = home.regime === 'RISK_ON' ? 'var(--success)' : 'var(--danger)';
  const tenYear = home.bond_yield_10y_pct;
  document.getElementById('macro-usdtry').textContent = tenYear != null ? `${(tenYear * 100).toFixed(2)}%` : '—';
  const hyOas = home.high_yield_oas_bps;
  const hyEl = document.getElementById('macro-hy-oas');
  if (hyEl) hyEl.textContent = hyOas != null ? `${Math.round(hyOas)} bp` : '—';
}

// --- PAGE 4: HISTORY ---
function renderHistoryPage() {
  refreshReturnCostUi();
  const dbPos = queryAll('SELECT * FROM open_positions');
  const records = buildWeeklyPerformanceRecords(dbPos, window.livePrices);

  // Cumulative performance math
  let cumPort = 1.0;
  let cumBist = 1.0;
  records.forEach(rec => {
    cumPort *= (1.0 + rec.portfolioReturn);
    cumBist *= (1.0 + rec.bist100Return);
  });

  const totalPort = (cumPort - 1.0) * 100;
  const totalBist = (cumBist - 1.0) * 100;
  const alpha = totalPort - totalBist;

  const startDate = records[0]?.weekStartDate || "2026-05-18";
  document.getElementById('history-start-date').textContent = `Başlangıç: ${formatDateToTurkish(startDate)}`;

  const portEl = document.getElementById('history-total-port');
  portEl.textContent = `${totalPort >= 0 ? '+' : ''}${totalPort.toFixed(2)}%`;
  portEl.className = `value ${totalPort >= 0 ? 'pos-text' : 'neg-text'}`;

  const bistEl = document.getElementById('history-total-bist');
  bistEl.textContent = `${totalBist >= 0 ? '+' : ''}${totalBist.toFixed(2)}%`;
  bistEl.className = `value ${totalBist >= 0 ? 'pos-text' : 'neg-text'}`;

  const alphaEl = document.getElementById('history-total-alpha');
  alphaEl.textContent = `Endekse Karşı Fark: ${alpha >= 0 ? '+' : ''}${alpha.toFixed(2)}%`;
  alphaEl.style.backgroundColor = alpha >= 0 ? 'rgba(34, 197, 94, 0.08)' : 'rgba(239, 68, 68, 0.08)';
  alphaEl.style.color = alpha >= 0 ? 'var(--success)' : 'var(--danger)';

  const hasActive = records.some(r => !r.isCompleted);
  const trackingBadge = document.getElementById('live-tracking-badge');
  if (hasActive) {
    const quoteStatus = getQuoteStatus('SPY');
    trackingBadge.textContent = quoteStatus.label;
    trackingBadge.className = quoteStatus.className;
    trackingBadge.title = quoteStatus.title;
    trackingBadge.style.display = 'inline-flex';
  } else {
    trackingBadge.style.display = 'none';
  }

  // Render list of weekly rows
  const listEl = document.getElementById('weekly-history-list');
  listEl.innerHTML = '';

  records.slice().reverse().forEach(rec => {
    const portfolioReturn = finiteNumber(rec.portfolioReturn);
    const bistReturn = finiteNumber(rec.bist100Return);
    const diff = (portfolioReturn - bistReturn) * 100;
    const diffClass = diff >= 0 ? 'pos-text' : 'neg-text';
    const safeWeekLabel = escapeHtml(formatDateToTurkish(rec.weekStartDate));

    const card = document.createElement('div');
    card.className = 'collapsible-card';
    card.innerHTML = `
      <div class="collapsible-header">
        <div class="collapsible-header-left">
          <span class="collapsible-header-title">${safeWeekLabel} Dönemi</span>
          <span class="collapsible-header-subtitle ${diffClass}">Dönem Farkı: ${diff >= 0 ? '+' : ''}${diff.toFixed(2)}%</span>
        </div>
        <div class="collapsible-header-right">
          ${!rec.isCompleted ? '<span class="badge badge-live">AKTİF</span>' : ''}
          <span class="material-symbols-rounded icon-arrow">expand_more</span>
        </div>
      </div>
      <div class="collapsible-body">
        <div class="collapsible-body-metrics">
          <div>
            <span style="color:var(--text-muted);">Portföy Getirisi:</span>
            <strong class="${portfolioReturn >= 0 ? 'pos-text' : 'neg-text'}">${(portfolioReturn * 100).toFixed(2)}%</strong>
          </div>
          <div>
            <span style="color:var(--text-muted);">S&P 500 (SPY) Getirisi:</span>
            <strong class="${bistReturn >= 0 ? 'pos-text' : 'neg-text'}">${(bistReturn * 100).toFixed(2)}%</strong>
          </div>
        </div>
        <div style="border-top:1px dashed var(--outline); padding-top:10px;">
          <div style="font-size:9px; font-weight:800; color:var(--text-muted); margin-bottom:8px; text-transform:uppercase;">SEÇİLEN HİSSELER VE GETİRİLERİ</div>
          <div class="spaced-y"></div>
        </div>
      </div>
    `;

    // Collapsible click trigger
    card.querySelector('.collapsible-header').addEventListener('click', () => {
      card.classList.toggle('open');
    });

    listEl.appendChild(card);

    // Populate positions
    const detailsContainer = card.querySelector('.spaced-y');
    if (rec.positions && rec.positions.length > 0) {
      rec.positions.forEach(pos => {
        const stockPct = finiteNumber(pos.returnPct) * 100;
        const entryPrice = finiteNumber(pos.entryPrice);
        const exitPrice = finiteNumber(pos.exitPrice);
        const pClass = stockPct >= 0 ? 'pos-text' : 'neg-text';
        const row = document.createElement('div');
        row.className = 'stock-performance-item';
        row.innerHTML = `
          <span style="font-size:11px; font-weight:700; color:var(--text);">${escapeHtml(pos.ticker)}</span>
          <div class="tabular-nums" style="font-size:10px; color:var(--text-muted);">
            $${entryPrice.toFixed(2)} → $${exitPrice.toFixed(2)} · model ref
            <span class="${pClass}" style="font-weight:800; margin-left:8px;">${stockPct >= 0 ? '+' : ''}${stockPct.toFixed(2)}%</span>
          </div>
        `;
        detailsContainer.appendChild(row);
      });
    }
  });
}

function formatDateToTurkish(dateStr) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr || ''));
  if (!match) return String(dateStr || '—');
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(year, month - 1, day);
  if (
    date.getFullYear() !== year ||
    date.getMonth() !== month - 1 ||
    date.getDate() !== day
  ) {
    return String(dateStr || '—');
  }
  const months = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"];
  return `${day} ${months[month - 1]} ${year}`;
}

function daysBetweenIsoDates(startDate, endDate) {
  const startMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(startDate || ''));
  const endMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(endDate || ''));
  if (!startMatch || !endMatch) return 0;
  const start = Date.UTC(Number(startMatch[1]), Number(startMatch[2]) - 1, Number(startMatch[3]));
  const end = Date.UTC(Number(endMatch[1]), Number(endMatch[2]) - 1, Number(endMatch[3]));
  return Math.max(0, Math.round((end - start) / 86400000));
}

function localIsoDate(value = new Date()) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

function formatPercent(value, digits = 1) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return '—';
  return `${parsed >= 0 ? '+' : ''}${parsed.toFixed(digits)}%`;
}

function setTextById(elementId, value) {
  const element = document.getElementById(elementId);
  if (element) element.textContent = value;
}

function getInvestorAuditSummary() {
  const audit = window.feedBacktestAudit;
  if (!audit || typeof audit !== 'object') {
    return {
      audit: null,
      baseCase: null,
      worstStress: null,
      hasWarnings: true,
      primaryWarning: 'Investor-grade audit manifest içinde bulunamadı.'
    };
  }

  const cases = Array.isArray(audit.cases) ? audit.cases : [];
  const baseCase = cases.find(item => item.case === 'base') || cases[0] || null;
  const stressCases = cases.filter(item => item.case && item.case !== 'base');
  const worstStress = stressCases.length > 0
    ? stressCases.reduce((worst, item) => (
        Number(item.total_return_pct ?? Infinity) < Number(worst.total_return_pct ?? Infinity) ? item : worst
      ), stressCases[0])
    : null;
  const warningGate = (audit.gates || []).find(gate => gate.status !== 'pass');

  return {
    audit,
    baseCase,
    worstStress,
    hasWarnings: Boolean(warningGate),
    primaryWarning: warningGate?.detail || ''
  };
}

function localizeAuditWarning(detail) {
  const text = String(detail || '').trim();
  if (!text) return 'Audit uyarısı detaylandırılmadı.';

  const inactiveMatch = /^(\d+)\s+inactive priced companies/i.exec(text);
  if (inactiveMatch) {
    return `${inactiveMatch[1]} pasif fiyatlı şirket için delist/as-of evren kapsamı tamamlanmalı.`;
  }

  const worstStressMatch = /^Worst stress alpha:\s*([-+]?\d+(?:\.\d+)?)%/i.exec(text);
  if (worstStressMatch) {
    return `En sert slippage stresinde alpha ${formatPercent(Number(worstStressMatch[1]), 1)}.`;
  }

  return text;
}

function renderDecisionSummary(home, positions) {
  const card = document.querySelector('.decision-summary-card');
  if (!card) return;

  const cashState = String(home?.cash_state || 'NORMAL');
  const cashPct = Math.max(0, Math.min(1, finiteNumber(home?.cash_pct, 0)));
  const stockPct = Math.max(0, 1 - cashPct);
  const auditSummary = getInvestorAuditSummary();
  const { audit, worstStress, hasWarnings, primaryWarning } = auditSummary;
  let baseCase = auditSummary.baseCase;

  // The investor-grade audit JSON has never shipped in the manifest (the CI
  // backtest only maintains the NAV series), so without this fallback the
  // card permanently shows em-dashes. Fall back to the snapshot NAV series.
  let navFallback = false;
  let navStartDate = null;
  if (!audit) {
    const perf = queryAll('SELECT date, strategy_return, benchmark_return FROM model_performance ORDER BY date ASC');
    const navSummary = summarizeBacktestFromNav(perf);
    if (navSummary.cases.length > 0) {
      baseCase = navSummary.cases[0];
      navFallback = true;
      navStartDate = perf[0]?.date || null;
    }
  }

  const quoteSummary = summarizeQuoteCoverage(positions.map(pos => pos.ticker));
  const priceSourceText = positions.length > 0
    ? `Fiyat kaynağı: ${formatQuoteCoverage(quoteSummary)}`
    : 'Fiyat kaynağı: portföy boş';

  let weeklyRule = 'AL/SAT/TUT sinyallerine göre 5 hisse hedefini koru; aldığını sonraki rotasyona kadar (2 hafta) tut.';
  if (positions.length === 0) {
    weeklyRule = 'Portföy boş; veri yenile ve Main V2 seçimlerini kontrol et.';
  } else if (cashState === 'RISK_OFF') {
    weeklyRule = 'RISK_OFF aktif; model hisselerini izle ama pozisyonu korumacı tut.';
  } else if (cashState === 'DEFENSIVE') {
    weeklyRule = 'DEFENSIVE mod; hisse ağırlığını yarıya yakın tut.';
  } else if (cashState === 'CAUTION') {
    weeklyRule = 'CAUTION mod; yeni alımlarda nakit tamponunu koru.';
  }

  setTextById('decision-weekly-rule', weeklyRule);

  // Countdown to the next rotation — the single question the user asks most.
  const countdownEl = document.getElementById('decision-countdown');
  if (countdownEl) {
    const countdown = rotationCountdownText();
    countdownEl.style.display = countdown ? 'inline-flex' : 'none';
    if (countdown) {
      countdownEl.textContent = countdown.text;
      countdownEl.classList.toggle('today', countdown.days === 0);
    }
  }

  setTextById('decision-position-count', `${positions.length} / 5`);
  setTextById('decision-cash-state', `${cashState} • ${Math.round(cashPct * 100)}%`);
  setTextById('decision-model-return', baseCase ? formatPercent(baseCase.total_return_pct, 1) : '—');
  setTextById('decision-bist-return', baseCase ? formatPercent(baseCase.benchmark_return_pct, 1) : '—');
  setTextById('decision-stress-return', worstStress
    ? `${formatPercent(worstStress.total_return_pct, 1)} @ ${Number(worstStress.friction_round_trip_pct || 0).toFixed(2)}%`
    : '—');
  setTextById('decision-price-source', priceSourceText);

  const badgeEl = document.getElementById('decision-audit-badge');
  if (badgeEl) {
    badgeEl.textContent = navFallback ? 'NAV' : (!audit ? 'SINIRLI' : (hasWarnings ? 'AUDIT UYARI' : 'AUDIT PASS'));
    badgeEl.className = !audit || hasWarnings ? 'badge badge-delayed' : 'badge badge-live';
  }

  const navYear = navStartDate ? navStartDate.slice(0, 4) : null;
  const auditNote = navFallback
    ? `Getiriler snapshot NAV serisinden (${navYear ? navYear + " başlangıçlı" : 'haftalık'} simülasyon). Slippage stres testi ancak investor-grade audit yayınlanınca görünür.`
    : !audit
      ? 'Backtest audit manifestten okunamadı; History içindeki NAV eğrisi sınırlı fikir verir.'
      : hasWarnings
        ? `Backtest güçlü, fakat kontrol uyarısı var: ${localizeAuditWarning(primaryWarning)}`
        : `${audit.execution_mode === 'same_day_open' ? 'T-1→T açılış' : 'T+1'} backtest ${formatPercent(baseCase?.alpha_pct, 1)} alpha üretiyor; yine de işlem boyutu kişisel risk limitine göre ayarlanmalı.`;
  setTextById('decision-audit-note', auditNote);

  const cashAccent = cashState === 'NORMAL'
    ? 'var(--success)'
    : (cashState === 'CAUTION' ? 'var(--warning)' : 'var(--danger)');
  const cashStateEl = document.getElementById('decision-cash-state');
  if (cashStateEl) cashStateEl.style.color = cashAccent;
  card.style.setProperty('--stock-allocation', `${Math.round(stockPct * 100)}%`);
}

// ==========================================================================
// 8. Stock Details Overlay Sheet (Parity Visuals & Options)
// ==========================================================================

function openStockDetail(ticker) {
  activeDetailTicker = ticker;
  const score = queryOne('SELECT * FROM scoring_latest WHERE ticker = :ticker LIMIT 1', { ':ticker': ticker });
  if (!score) return;

  const company = queryOne('SELECT * FROM companies WHERE ticker = :ticker LIMIT 1', { ':ticker': ticker });
  const openPos = queryOne('SELECT * FROM open_positions WHERE ticker = :ticker LIMIT 1', { ':ticker': ticker });
  
  // Basic Headers
  document.getElementById('detail-ticker').textContent = ticker;
  document.getElementById('detail-name').textContent = score.name || company?.name || '—';
  document.getElementById('detail-in-portfolio-badge').style.display = openPos ? 'inline-flex' : 'none';

  // Sectoral metadata
  const sector = score.sector || company?.sector_custom || null;
  document.getElementById('detail-sector').textContent = sectorLabel(sector);
  const freeFloatPct = mixedScalePercent(company?.free_float_pct);
  document.getElementById('detail-float').textContent = freeFloatPct !== null
    ? `${freeFloatPct.toFixed(1)}%`
    : '—';
  document.getElementById('detail-class').textContent = score.is_bist100 ? 'S&P 500' : 'S&P DIŞI';

  // Live Price and Model Score
  const livePrice = finiteNumber(
    window.livePrices[ticker] ?? score.current_price ?? getLatestSnapshotPrice(ticker),
    0
  );
  document.getElementById('detail-price-val').textContent = livePrice > 0
    ? `$${livePrice.toFixed(2)}`
    : '—';
  document.getElementById('detail-model-score').textContent = score.ranking_score ? score.ranking_score.toFixed(1) : '—';
  const detailQuoteStatus = getQuoteStatus(ticker);
  const detailIndicator = document.getElementById('detail-live-indicator');
  detailIndicator.textContent = detailQuoteStatus.label;
  detailIndicator.className = detailQuoteStatus.className;
  detailIndicator.title = detailQuoteStatus.title;
  detailIndicator.style.display = livePrice > 0 ? 'inline-flex' : 'none';

  // Smart rebalance warnings — from the ACTUAL rotation decisions (same
  // source as the Picks page), not a client-side top-10/12 re-derivation
  // that could contradict what the selector really did.
  const banner = document.getElementById('detail-signal-banner');
  const bannerText = document.getElementById('detail-signal-text');

  const allPositions = queryAll('SELECT * FROM open_positions ORDER BY sort_order ASC');
  const detailDecisions = computeRebalanceDecisions(allPositions);
  const detailQualityFlags = parseQualityFlags(score.quality_flags_json, openPos?.quality_flags_json);

  const riskExit = detailDecisions.riskExits.find(r => r.ticker === ticker);
  if (riskExit) {
    banner.style.display = 'flex';
    banner.className = 'signal-banner';
    banner.style.backgroundColor = '';
    banner.style.borderColor = '';
    bannerText.textContent = `Model bu pozisyonu ${riskExit.exit_date} tarihinde otomatik kapattı (${riskExit.exit_reason === 'TARGET' ? 'hedef fiyat' : riskExit.exit_reason === 'THESIS_BREAKER' ? 'içeriden satış uyarısı' : 'stop-loss'}). Elinde varsa sat.`;
  } else if (detailDecisions.sells.some(s => s.ticker === ticker)) {
    banner.style.display = 'flex';
    banner.className = 'signal-banner';
    banner.style.backgroundColor = '';
    banner.style.borderColor = '';
    bannerText.textContent = 'Model bu hisseyi bu rotasyonda portföyden çıkardı — SATIŞ.';
  } else if (detailDecisions.buys.some(s => s.ticker === ticker)) {
    banner.style.display = 'flex';
    banner.className = 'signal-banner pos-text';
    banner.style.backgroundColor = 'rgba(34, 197, 94, 0.08)';
    banner.style.borderColor = 'rgba(34, 197, 94, 0.2)';
    bannerText.textContent = 'Model bu hisseyi bu rotasyonda portföye aldı — ALIŞ. 2 hafta tut; sonraki rotasyona kadar satma (stop uyarısı hariç).';
  } else {
    banner.style.display = 'none';
  }

  // Price lane slider — open positions carry the real stop/target set at
  // selection time; scoring_latest columns are only a fallback.
  const stopLoss = finiteNumber(openPos?.stop_loss_price ?? score.stop_loss_price, 0);
  const targetPrice = finiteNumber(openPos?.target_price ?? score.target_price, 0);
  const targetSource = inferTargetSource(
    openPos?.target_source || score.target_source,
    detailQualityFlags
  );
  const targetMeta = targetSourceMeta(targetSource);

  document.getElementById('lane-stop-label').textContent = `Stop: ${stopLoss > 0 ? '$' + stopLoss.toFixed(2) : '—'}`;
  const laneTargetLabel = document.getElementById('lane-target-label');
  laneTargetLabel.textContent = `${targetMeta.label}: ${targetPrice > 0 ? '$' + targetPrice.toFixed(2) : '—'}`;
  laneTargetLabel.title = targetMeta.detail;
  
  const laneProgressBar = document.getElementById('price-lane-progress');
  const lanePin = document.getElementById('price-lane-pin');

  if (stopLoss > 0 && targetPrice > 0) {
    const range = targetPrice - stopLoss;
    const progress = range > 0 ? ((livePrice - stopLoss) / range) * 100 : 0;
    const clampedProgress = Math.max(0, Math.min(100, progress));

    laneProgressBar.style.width = `${clampedProgress}%`;
    laneProgressBar.style.backgroundColor = (livePrice >= stopLoss) ? 'var(--success)' : 'var(--danger)';
    lanePin.style.left = `${clampedProgress}%`;
  } else {
    laneProgressBar.style.width = '0%';
    lanePin.style.left = '50%';
  }

  // Investment Thesis Bullets
  const thesisList = document.getElementById('detail-thesis-list');
  thesisList.innerHTML = '';
  
  // Factor columns are 0-100 normalized percentiles; a bullet means the
  // stock is in roughly the top quarter for that factor. (The old 0.75
  // thresholds fired for nearly every stock, and piotroskiRaw was a typo
  // that rendered "undefined/9".)
  const thesisItems = [];
  const piotroskiRaw = finiteNumber(score.piotroski_raw, null);
  const above200ma = score.above_200ma === null || score.above_200ma === undefined
    ? null
    : Number(score.above_200ma);
  if (finiteNumber(score.buffett) >= 75) thesisItems.push("Güçlü Buffett kalite profili: reel kârlılık ve istikrarlı nakit yaratımı.");
  if (finiteNumber(score.graham) >= 75) thesisItems.push("Graham kriterlerine göre yüksek güvenlik marjı (içsel değer iskontosu).");
  if (finiteNumber(score.dcf_mos) >= 75) thesisItems.push("DCF değerlemesine göre belirgin güvenlik marjı.");
  if (finiteNumber(score.momentum) >= 75) thesisItems.push("Hisse fiyatı güçlü momentum ve trend desteği barındırıyor.");
  if (above200ma === 1) thesisItems.push("Fiyat 200 günlük ortalamanın üzerinde; düşen bıçak filtresi pozitif.");
  if (above200ma === 0) thesisItems.push("Uyarı: fiyat 200 günlük ortalamanın altında; trend teyidi zayıf.");
  if (piotroskiRaw !== null && piotroskiRaw >= 7) thesisItems.push(`Finansal sağlık rasyoları güçlü (Piotroski F-Score: ${piotroskiRaw.toFixed(0)}/9).`);
  
  if (thesisItems.length === 0) {
    thesisItems.push("Şirket genel model ağırlıkları bakımından dengeli skor yapısına sahip.");
  }
  thesisItems.forEach(t => {
    const li = document.createElement('li');
    li.textContent = t;
    thesisList.appendChild(li);
  });

  // Basic financial grids
  const metrics = queryOne('SELECT * FROM adjusted_metrics_latest WHERE company_id = :id LIMIT 1', { ':id': score.company_id });
  
  document.getElementById('fin-roe').textContent = metrics?.roe_adjusted ? `${(metrics.roe_adjusted * 100).toFixed(1)}%` : '—';
  
  const fcfVal = metrics?.free_cash_flow;
  document.getElementById('fin-fcf').textContent = fcfVal ? formatLargeMoney(fcfVal) : '—';
  
  document.getElementById('fin-eps-growth').textContent = metrics?.real_eps_growth_pct ? `${(metrics.real_eps_growth_pct * 100).toFixed(1)}%` : '—';

  // Sector benchmark card
  const bench = queryOne('SELECT * FROM sector_benchmarks WHERE sector = :sector LIMIT 1', { ':sector': sector });
  const benchText = document.getElementById('detail-benchmark-text');
  if (bench) {
    const peerCount = finiteNumber(bench.company_count, 0);
    benchText.textContent = peerCount > 0
      ? `Şirket, ${sectorLabel(sector)} sektöründeki ${peerCount.toFixed(0)} aktif firma ile karşılaştırılmıştır.`
      : `Şirket, ${sectorLabel(sector)} sektör medyanı ile karşılaştırılmıştır.`;
    document.getElementById('bench-company-roe').textContent = metrics?.roe_adjusted ? `${(metrics.roe_adjusted * 100).toFixed(1)}%` : '—';
    document.getElementById('bench-sector-roe').textContent = bench.roe_median ? `${(bench.roe_median * 100).toFixed(1)}%` : '—';
    document.getElementById('bench-company-roa').textContent = metrics?.roa_adjusted ? `${(metrics.roa_adjusted * 100).toFixed(1)}%` : '—';
    document.getElementById('bench-sector-roa').textContent = bench.roa_median ? `${(bench.roa_median * 100).toFixed(1)}%` : '—';
  } else {
    benchText.textContent = 'Sektör karşılaştırma verisi bulunmuyor.';
  }

  // COLLAPSIBLE ADVANCED PARAMETERS (TAS 29)
  const auditWarningCard = document.getElementById('audit-warning-card');
  const relatedPct = metrics?.related_party_revenue_pct || 0.0;
  if (relatedPct > 0.15) {
    auditWarningCard.style.display = 'flex';
    document.getElementById('audit-related-pct').textContent = (relatedPct * 100).toFixed(1);
  } else {
    auditWarningCard.style.display = 'none';
  }

  document.getElementById('adv-reported-net').textContent = metrics?.reported_net_income ? formatLargeMoney(metrics.reported_net_income) : '—';
  document.getElementById('adv-monetary-gain').textContent = metrics?.monetary_gain_loss ? formatLargeMoney(metrics.monetary_gain_loss) : '—';
  document.getElementById('adv-adjusted-net').textContent = metrics?.adjusted_net_income ? formatLargeMoney(metrics.adjusted_net_income) : '—';

  // DCF Intrinsic
  document.getElementById('adv-dcf-intrinsic').textContent = score.dcf_intrinsic_value ? `$${score.dcf_intrinsic_value.toFixed(2)}` : '—';
  document.getElementById('adv-dcf-growth').textContent = score.dcf_growth_rate_pct ? `${(score.dcf_growth_rate_pct * 100).toFixed(1)}%` : '—';
  document.getElementById('adv-dcf-discount').textContent = score.dcf_discount_rate_pct ? `${(score.dcf_discount_rate_pct * 100).toFixed(1)}%` : '—';
  document.getElementById('adv-dcf-mos').textContent = score.dcf_mos ? `${(score.dcf_mos * 100).toFixed(1)}%` : '—';

  // Quality Flags check
  const flagsContainer = document.getElementById('adv-quality-flags');
  flagsContainer.innerHTML = '';
  const flagsList = detailQualityFlags;
  
  if (flagsList.length === 0) {
    flagsContainer.innerHTML = '<span class="flag-tag pass">Denetim bayrağı yok</span>';
  } else {
    flagsList.forEach(f => {
      const tag = document.createElement('span');
      tag.className = 'flag-tag warn';
      tag.textContent = qualityFlagLabel(f);
      tag.title = qualityFlagDetail(f);
      flagsContainer.appendChild(tag);
    });
  }

  // APEXCHARTS: Price Chart (730d)
  const priceHistory = queryAll('SELECT date, close FROM price_history_730d WHERE company_id = :id ORDER BY date ASC', { ':id': score.company_id });
  renderPriceHistoryChart(priceHistory);

  // APEXCHARTS: Factor Quarterly Breakdown
  const factorHistory = queryAll('SELECT quarter_end, buffett, graham, piotroski, momentum, technical, dcf_mos FROM factor_history_quarterly WHERE company_id = :id ORDER BY quarter_end ASC', { ':id': score.company_id });
  renderFactorHistoryChart(factorHistory);

  // Open sliding sheet
  openSheet('detail-sheet');
}

// --- Sheet open/close with hardware-back support ---------------------------
// Opening a sheet pushes a history entry so the Android/browser back button
// (and swipe-back) closes the sheet instead of leaving the app. The close
// buttons route through history.back() so both paths stay in sync.
function openSheet(id) {
  const el = document.getElementById(id);
  if (!el || el.classList.contains('open')) return;
  el.classList.add('open');
  try { history.pushState({ bpSheet: id }, ''); } catch { /* sandboxed */ }
}

function closeOpenSheets() {
  let closed = false;
  document.querySelectorAll('.overlay-sheet.open').forEach(el => {
    el.classList.remove('open');
    // Clear swipe leftovers in the same tick the class goes: base state is
    // already translateX(100%), so this never causes visible motion.
    el.style.transition = '';
    el.style.transform = '';
    closed = true;
  });
  if (closed) {
    activeDetailTicker = null;
  }
  return closed;
}

// Guards the async history.back() round-trip: a second close request
// (double-ESC / double-tap) before popstate lands would otherwise queue a
// second back() and navigate the app itself away.
let sheetCloseInFlight = false;

function requestSheetClose() {
  if (sheetCloseInFlight) return;
  if (history.state?.bpSheet) {
    sheetCloseInFlight = true;
    history.back(); // popstate handler does the closing
  } else {
    closeOpenSheets();
  }
}

// Swipe-right-to-close for overlay sheets (native sheet ergonomics).
// Direction-locked: vertical scrolling wins, and drags starting on a chart
// are ignored so ApexCharts touch interactions keep working.
function enableSwipeToClose(sheetId) {
  const sheet = document.getElementById(sheetId);
  if (!sheet) return;
  let startX = 0, startY = 0, dragging = false, locked = false, width = 1;

  sheet.addEventListener('touchstart', (e) => {
    if (!sheet.classList.contains('open') || e.touches.length !== 1) return;
    if (e.target.closest('.chart-container')) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    width = sheet.offsetWidth || 1;
    dragging = true;
    locked = false;
  }, { passive: true });

  sheet.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    const dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    if (!locked) {
      if (dx > 26 && Math.abs(dx) > 2 * Math.abs(dy)) {
        locked = true;
        window.__bpSheetDragging = true;
        sheet.style.transition = 'none';
      } else if (Math.abs(dy) > 14) {
        dragging = false; // vertical intent — leave scrolling alone
        return;
      } else {
        return;
      }
    }
    e.preventDefault();
    sheet.style.transform = `translateX(${Math.max(0, dx)}px)`;
  }, { passive: false });

  const finish = (e) => {
    if (!dragging) return;
    dragging = false;
    if (!locked) return;
    locked = false;
    window.__bpSheetDragging = false;
    const dx = (e.changedTouches?.[0]?.clientX ?? startX) - startX;
    sheet.style.transition = '';
    if (dx > width * 0.32) {
      // Park the sheet offscreen inline, then close through the ONE shared
      // path (requestSheetClose → history.back → popstate → closeOpenSheets,
      // which also clears this inline transform). Closing the class here
      // directly raced a queued back() against quick re-opens.
      sheet.style.transform = 'translateX(100%)';
      requestSheetClose();
    } else {
      sheet.style.transform = ''; // spring back to the .open position
    }
  };
  sheet.addEventListener('touchend', finish);
  sheet.addEventListener('touchcancel', finish);
}

function formatLargeMoney(val) {
  const abs = Math.abs(val);
  const sign = val >= 0 ? '' : '-';
  if (abs >= 1e9) return `${sign}$${(val / 1e9).toFixed(2)} milyar`;
  if (abs >= 1e6) return `${sign}$${(val / 1e6).toFixed(2)} milyon`;
  return `${sign}$${val.toLocaleString('tr-TR')}`;
}

// Render Apex Price Chart
function renderPriceHistoryChart(history) {
  if (priceChartInstance) priceChartInstance.destroy();
  if (history.length === 0) return;

  const data = history.map(h => ({ x: new Date(h.date).getTime(), y: h.close }));

  const options = {
    series: [{ name: 'Fiyat', data: data }],
    chart: { type: 'area', height: 220, toolbar: { show: false }, background: 'transparent' },
    colors: ['#FFB48A'],
    dataLabels: { enabled: false },
    stroke: { curve: 'smooth', width: 2 },
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 1, opacityFrom: 0.35, opacityTo: 0.05, stops: [0, 90, 100] }
    },
    grid: { show: true, borderColor: '#1F1F23', strokeDashArray: 4 },
    xaxis: { type: 'datetime', labels: { style: { colors: '#B5B5B2', fontSize: '9px' } } },
    yaxis: { labels: { style: { colors: '#B5B5B2', fontSize: '9px' }, formatter: val => val.toFixed(1) } },
    theme: { mode: 'dark' }
  };

  priceChartInstance = new ApexCharts(document.getElementById('detail-price-chart'), options);
  priceChartInstance.render();
}

// Render Apex Factor Line Chart
function renderFactorHistoryChart(history) {
  if (factorChartInstance) factorChartInstance.destroy();
  if (history.length === 0) return;

  const categories = history.map(h => h.quarter_end);
  const buffettSeries = history.map(h => (h.buffett || 0.0) * 100);
  const grahamSeries = history.map(h => (h.graham || 0.0) * 100);
  const piotroskiSeries = history.map(h => ((h.piotroski || 0.0) / 9.0) * 100);
  const momentumSeries = history.map(h => (h.momentum || 0.0) * 100);
  const dcfSeries = history.map(h => (h.dcf_mos || 0.0) * 100);

  const options = {
    series: [
      { name: 'Buffett', data: buffettSeries },
      { name: 'Graham', data: grahamSeries },
      { name: 'Piotroski', data: piotroskiSeries },
      { name: 'Momentum', data: momentumSeries },
      { name: 'DCF (MOS)', data: dcfSeries }
    ],
    chart: { type: 'line', height: 220, toolbar: { show: false }, background: 'transparent' },
    colors: ['#FFB48A', '#7DD3FC', '#A78BFA', '#22C55E', '#EAB308'],
    stroke: { curve: 'smooth', width: 2 },
    grid: { borderColor: '#1F1F23', strokeDashArray: 4 },
    xaxis: { categories: categories, labels: { style: { colors: '#B5B5B2', fontSize: '9px' } } },
    yaxis: { max: 100, min: 0, labels: { style: { colors: '#B5B5B2', fontSize: '9px' }, formatter: val => `${val.toFixed(0)}%` } },
    legend: { position: 'bottom', horizontalAlign: 'center', fontSize: '10px', labels: { colors: '#F5F5F4' } },
    theme: { mode: 'dark' }
  };

  factorChartInstance = new ApexCharts(document.getElementById('detail-factor-chart'), options);
  factorChartInstance.render();
}

// ==========================================================================
// 9. Backtesting Overlay Sheet
// ==========================================================================

function summarizeBacktestFromNav(points) {
  if (!Array.isArray(points) || points.length < 2) {
    return {
      fromSnapshotFallback: true,
      cases: [],
      gates: []
    };
  }

  const navs = points
    .map(point => ({
      strategy: finiteNumber(point.strategy_return, null),
      benchmark: finiteNumber(point.benchmark_return, null)
    }))
    .filter(point => point.strategy > 0 && point.benchmark > 0);

  if (navs.length < 2) {
    return {
      fromSnapshotFallback: true,
      cases: [],
      gates: []
    };
  }

  let runningMax = navs[0].strategy;
  let maxDrawdown = 0;
  let maxWeekly = -Infinity;
  let minWeekly = Infinity;

  for (let i = 1; i < navs.length; i += 1) {
    const weeklyReturn = navs[i].strategy / navs[i - 1].strategy - 1.0;
    maxWeekly = Math.max(maxWeekly, weeklyReturn);
    minWeekly = Math.min(minWeekly, weeklyReturn);
    runningMax = Math.max(runningMax, navs[i].strategy);
    maxDrawdown = Math.min(maxDrawdown, navs[i].strategy / runningMax - 1.0);
  }

  const totalReturnPct = (navs[navs.length - 1].strategy / navs[0].strategy - 1.0) * 100;
  const benchmarkReturnPct = (navs[navs.length - 1].benchmark / navs[0].benchmark - 1.0) * 100;
  const baseCase = {
    case: 'snapshot_nav',
    total_return_pct: totalReturnPct,
    benchmark_return_pct: benchmarkReturnPct,
    alpha_pct: totalReturnPct - benchmarkReturnPct,
    max_drawdown_pct: maxDrawdown * 100,
    max_weekly_return_pct: Number.isFinite(maxWeekly) ? maxWeekly * 100 : null,
    min_weekly_return_pct: Number.isFinite(minWeekly) ? minWeekly * 100 : null
  };

  return {
    fromSnapshotFallback: true,
    execution_mode: 'unknown',
    cases: [baseCase],
    gates: [
      {
        key: 'execution',
        status: 'warn',
        detail: 'Investor-grade JSON yok; execution varsayımı snapshot NAV içinde doğrulanamadı.'
      },
      {
        key: 'slippage',
        status: 'warn',
        detail: 'Slippage stres raporu manifest içinde yok.'
      },
      {
        key: 'drawdown',
        status: baseCase.max_drawdown_pct >= -35 ? 'pass' : 'warn',
        detail: `Max drawdown: ${baseCase.max_drawdown_pct.toFixed(1)}%`
      },
      {
        key: 'price_jumps',
        status: 'warn',
        detail: 'Fiyat sıçrama audit raporu manifest içinde yok.'
      },
      {
        key: 'survivorship',
        status: 'warn',
        detail: 'Survivorship audit raporu manifest içinde yok.'
      }
    ]
  };
}

function setAuditRow(elementId, text, status = 'warn') {
  const valueEl = document.getElementById(elementId);
  if (!valueEl) return;
  valueEl.textContent = text;
  const row = valueEl.closest('.audit-check-row');
  if (row) {
    row.classList.remove('pass', 'warn');
    row.classList.add(status === 'pass' ? 'pass' : 'warn');
  }
}

function renderBacktestAuditPanel(perfPoints) {
  const audit = window.feedBacktestAudit || summarizeBacktestFromNav(perfPoints);
  const isNavFallback = Boolean(audit.fromSnapshotFallback);
  const gates = new Map((audit.gates || []).map(gate => [gate.key, gate]));
  const baseCase = (audit.cases || []).find(item => item.case === 'base') || (audit.cases || [])[0] || {};
  // The NAV-fallback summary has a single 'snapshot_nav' pseudo-case; it must
  // not be mistaken for a slippage stress case (used to render the base alpha
  // as "alpha @ 0.00%" nonsense).
  const worstStress = (audit.cases || []).slice().reverse()
    .find(item => item.case && item.case !== 'base' && item.case !== 'snapshot_nav') || null;

  const executionGate = gates.get('execution') || {};
  const executionMode = isNavFallback
    ? '—'
    : (audit.execution_mode === 'same_day_open'
      ? 'T-1 veri → T açılış'
      : (audit.execution_mode === 'next_open' ? 'T+1 next open' : (audit.execution_mode || '—')));
  setAuditRow('bt-audit-execution', executionMode, executionGate.status || 'warn');

  const slippageGate = gates.get('slippage') || {};
  const slippageText = worstStress
    ? `${formatPercent(worstStress.alpha_pct, 1)} alpha @ ${Number(worstStress.friction_round_trip_pct || 0).toFixed(2)}%`
    : (isNavFallback ? '—' : 'Stres yok');
  setAuditRow('bt-audit-slippage', slippageText, slippageGate.status || 'warn');

  const drawdownGate = gates.get('drawdown') || {};
  setAuditRow('bt-audit-drawdown', formatPercent(baseCase.max_drawdown_pct, 1), drawdownGate.status || 'warn');

  const jumpGate = gates.get('price_jumps') || {};
  const jumpCount = audit.price_jump_audit?.flag_count;
  setAuditRow(
    'bt-audit-price-jumps',
    Number.isFinite(Number(jumpCount)) ? `${jumpCount} bayrak` : '—',
    jumpGate.status || 'warn'
  );

  const survivalGate = gates.get('survivorship') || {};
  const inactiveCount = audit.survivorship_audit?.inactive_priced_company_count;
  setAuditRow(
    'bt-audit-survivorship',
    Number.isFinite(Number(inactiveCount)) ? `${inactiveCount} pasif hisse` : '—',
    survivalGate.status || 'warn'
  );

  const statusEl = document.getElementById('bt-audit-status');
  const hasWarnings = (audit.gates || []).some(gate => gate.status !== 'pass');
  statusEl.textContent = audit.fromSnapshotFallback ? 'SINIRLI' : (hasWarnings ? 'UYARI' : 'PASS');
  statusEl.className = hasWarnings || audit.fromSnapshotFallback ? 'badge badge-delayed' : 'badge badge-live';

  const noteEl = document.getElementById('bt-audit-note');
  if (audit.fromSnapshotFallback) {
    noteEl.textContent = 'Investor-grade JSON manifestte yok; bu panel yalnızca snapshot NAV eğrisinden sınırlı kontrol yapıyor.';
  } else {
    noteEl.textContent = `${audit.start_date || '—'} / ${audit.end_date || '—'} raporu. Baz alpha ${formatPercent(baseCase.alpha_pct, 1)}; en sert slippage alpha ${worstStress ? formatPercent(worstStress.alpha_pct, 1) : '—'}.`;
  }
}

function openBacktestingSheet() {
  const perfPoints = queryAll('SELECT date, strategy_return, benchmark_return FROM model_performance ORDER BY date ASC');
  if (perfPoints.length === 0) return;

  // Render Apex Area Backtest Chart
  renderBacktestAreaChart(perfPoints);

  // Cumulative numbers. Prefer the investor-grade manifest because it carries
  // the audited execution/slippage run; snapshot NAV remains the chart source.
  const latestPoint = perfPoints[perfPoints.length - 1];
  const firstPoint = perfPoints[0];
  const auditSummary = getInvestorAuditSummary();
  const auditBaseCase = auditSummary.baseCase;
  const stratRet = auditBaseCase
    ? finiteNumber(auditBaseCase.total_return_pct, 0)
    : latestPoint.strategy_return - 100.0;
  const benchRet = auditBaseCase
    ? finiteNumber(auditBaseCase.benchmark_return_pct, 0)
    : latestPoint.benchmark_return - 100.0;
  const alpha = auditBaseCase
    ? finiteNumber(auditBaseCase.alpha_pct, stratRet - benchRet)
    : stratRet - benchRet;

  document.getElementById('bt-nav-return').textContent = `${stratRet >= 0 ? '+' : ''}${stratRet.toFixed(1)}%`;
  document.getElementById('bt-index-return').textContent = `${benchRet >= 0 ? '+' : ''}${benchRet.toFixed(1)}%`;
  
  const alphaEl = document.getElementById('bt-alpha-banner');
  const periodLabel = `${formatDateToTurkish(firstPoint.date)} – ${formatDateToTurkish(latestPoint.date)}`;
  document.getElementById('bt-period-label').textContent = periodLabel;
  alphaEl.textContent = `Dönemsel Model Alphası: ${alpha >= 0 ? '+' : ''}${alpha.toFixed(1)}%`;
  alphaEl.style.backgroundColor = alpha >= 0 ? 'rgba(34, 197, 94, 0.08)' : 'rgba(239, 68, 68, 0.08)';
  alphaEl.style.color = alpha >= 0 ? 'var(--success)' : 'var(--danger)';

  renderBacktestAuditPanel(perfPoints);

  // Render completed transactions list
  const closedPositions = queryAll(`
    SELECT * FROM portfolio_history
    WHERE selection_date >= :startDate AND exit_date <= :todayDate
    ORDER BY exit_date DESC, sort_order ASC
  `, {
    ':startDate': LIVE_TRACKING_START_DATE,
    ':todayDate': localIsoDate(),
  });
  const closedListEl = document.getElementById('bt-transactions-list');
  closedListEl.innerHTML = '';

  if (closedPositions.length === 0) {
    closedListEl.innerHTML = `<p style="color:var(--text-muted); font-size:11px; font-style:italic;">Tamamlanmış işlem kaydı bulunmuyor.</p>`;
  } else {
    closedPositions.forEach(pos => {
      const pnl = finiteNumber(pos.pnl_pct);
      const pnlClass = pnl >= 0 ? 'pos-text' : 'neg-text';
      const holdingDays = pos.holding_days != null
        ? Math.max(0, Math.round(finiteNumber(pos.holding_days)))
        : daysBetweenIsoDates(pos.selection_date, pos.exit_date);

      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; font-size:11px; align-items:center;">
          <div>
            <strong style="color:var(--primary); font-size:12px;">${escapeHtml(pos.ticker)}</strong>
            <div style="font-size:9px; color:var(--text-muted); margin-top:2px;">
              ${escapeHtml(formatDateToTurkish(pos.selection_date))} - ${escapeHtml(formatDateToTurkish(pos.exit_date))} (${holdingDays} Gün)
            </div>
          </div>
          <div style="text-align:right;">
            <strong class="${pnlClass} tabular-nums" style="font-size:12px;">${pnl >= 0 ? '+' : ''}${pnl.toFixed(1)}%</strong>
            <div style="font-size:8px; color:var(--text-muted); margin-top:2px;">${escapeHtml(pos.exit_reason || 'Model Değişimi')}</div>
          </div>
        </div>
      `;
      closedListEl.appendChild(card);
    });
  }

  // Open sliding sheet
  openSheet('backtest-sheet');
}

function renderBacktestAreaChart(points) {
  if (backtestChartInstance) backtestChartInstance.destroy();
  
  const stratData = points.map(p => ({ x: new Date(p.date).getTime(), y: (p.strategy_return - 100.0) }));
  const benchData = points.map(p => ({ x: new Date(p.date).getTime(), y: (p.benchmark_return - 100.0) }));

  const options = {
    series: [
      { name: 'Main V2 Portfolio', data: stratData },
      { name: 'S&P 500 (SPY, toplam getiri)', data: benchData }
    ],
    chart: { type: 'area', height: 250, toolbar: { show: false }, background: 'transparent' },
    colors: ['#FFB48A', '#7DD3FC'],
    dataLabels: { enabled: false },
    stroke: { curve: 'smooth', width: 2 },
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 1, opacityFrom: 0.25, opacityTo: 0.02, stops: [0, 95, 100] }
    },
    grid: { borderColor: '#1F1F23', strokeDashArray: 4 },
    xaxis: { type: 'datetime', labels: { style: { colors: '#B5B5B2', fontSize: '9px' } } },
    yaxis: { labels: { style: { colors: '#B5B5B2', fontSize: '9px' }, formatter: val => `${val.toFixed(0)}%` } },
    legend: { position: 'bottom', horizontalAlign: 'center', fontSize: '10px', labels: { colors: '#F5F5F4' } },
    theme: { mode: 'dark' }
  };

  backtestChartInstance = new ApexCharts(document.getElementById('backtest-area-chart'), options);
  backtestChartInstance.render();
}

// ==========================================================================
// 10. Application Launch Sequence (Robust Load + Failsafe Timer)
// ==========================================================================

async function initApp() {
  console.log('[US Picker] Application init sequence started...');
  const statusEl = document.getElementById('splash-status');
  const progressFill = document.getElementById('splash-progress');
  const retryBtn = document.getElementById('splash-retry-btn');
  
  // First launch downloads and opens a ~10 MB compressed snapshot. Slow
  // mobile connections can legitimately take longer than 15 seconds.
  const slowLoadTimer = setTimeout(() => {
    if (document.getElementById('splash-screen').style.display !== 'none') {
      console.warn('[Launch] Initial load is taking longer than expected.');
      statusEl.textContent = 'İlk kurulum biraz uzun sürüyor; veriler indirilmeye devam ediyor...';
    }
  }, 20000);

  try {
    statusEl.textContent = '🔍 Manifest bilgileri sorgulanıyor...';
    progressFill.style.width = '10%';
    
    let manifest = null;
    try {
      const res = await fetch('./manifest.json?t=' + Date.now(), { cache: 'no-store' });
      if (res.ok) {
        manifest = validateSnapshotManifest(await res.json());
        window.feedManifest = manifest;
        window.feedBacktestAudit = manifest.backtest_audit || null;
        console.log('[DB] Live manifest found:', manifest.snapshot_version);
      } else {
        console.warn('[DB] Live manifest request failed with HTTP', res.status);
      }
    } catch(e) {
      console.warn('[DB] Could not query live manifest:', e.message);
    }

    statusEl.textContent = '🗃️ Önbellek kontrol ediliyor...';
    progressFill.style.width = '20%';
    const cached = await getCachedSnapshot();
    if (!window.feedBacktestAudit && cached?.backtest_audit) {
      window.feedBacktestAudit = cached.backtest_audit;
    }

    let rawBytes = null;
    let snapshotSource = "";
    const showDownloadProgress = (percent) => {
      progressFill.style.width = `${20 + percent * 0.4}%`;
      statusEl.textContent = `⬇️ İndiriliyor: %${percent}...`;
    };
    const persistDownloadedSnapshot = async () => {
      statusEl.textContent = '💾 Veriler önbelleğe yazılıyor...';
      progressFill.style.width = '80%';
      try {
        await setCachedSnapshot({
          db_blob: rawBytes,
          sha256: manifest.snapshot.sha256,
          exported_at: manifest.exported_at,
          snapshot_version: manifest.snapshot_version,
          backtest_audit: manifest.backtest_audit || null
        });
      } catch(cacheErr) {
        console.warn('[DB] Failed writing database snapshot to browser IndexedDB:', cacheErr);
      }
    };

    if (cached && manifest && cached.sha256 === manifest.snapshot.sha256) {
      statusEl.textContent = '📦 Önbellekteki veriler yükleniyor...';
      progressFill.style.width = '50%';
      rawBytes = cached.db_blob;
      snapshotSource = 'cache-current';
    } else if (!manifest && cached?.db_blob) {
      statusEl.textContent = '📴 Çevrimdışı Mod: Önbellekteki veriler yükleniyor...';
      progressFill.style.width = '50%';
      rawBytes = cached.db_blob;
      snapshotSource = 'cache-offline';
    } else {
      if (!manifest) {
        throw new Error('Canlı manifest alınamadı ve önbellekte kayıtlı veri bulunmuyor!');
      }

      statusEl.textContent = '⬇️ Güncel veritabanı indiriliyor (~10MB)...';
      try {
        rawBytes = await downloadSnapshot(manifest, showDownloadProgress);
        snapshotSource = 'download';
        await persistDownloadedSnapshot();
      } catch (downloadError) {
        if (!cached?.db_blob) throw downloadError;
        console.warn('[DB] Latest snapshot download failed; using last valid cache:', downloadError);
        statusEl.textContent = '📴 Güncel veri alınamadı; son kayıtlı veriler açılıyor...';
        progressFill.style.width = '60%';
        rawBytes = cached.db_blob;
        snapshotSource = 'cache-stale';
      }
    }

    statusEl.textContent = '⚙️ SQL veritabanı motoru başlatılıyor...';
    progressFill.style.width = '90%';
    
    const SQL = await initSqlJs({
      locateFile: filename => `./vendor/${filename}?v=us1`
    });

    try {
      dbInstance = new SQL.Database(rawBytes);
    } catch (databaseError) {
      const canRecoverFromNetwork = manifest && snapshotSource.startsWith('cache');
      if (!canRecoverFromNetwork) throw databaseError;

      console.warn('[DB] Cached snapshot is invalid; downloading a clean copy.', databaseError);
      await deleteCachedSnapshot();
      statusEl.textContent = '🔄 Önbellek bozuk; temiz snapshot indiriliyor...';
      rawBytes = await downloadSnapshot(manifest, showDownloadProgress);
      snapshotSource = 'download-recovery';
      await persistDownloadedSnapshot();
      dbInstance = new SQL.Database(rawBytes);
    }
    progressFill.style.width = '100%';
    
    clearTimeout(slowLoadTimer);
    console.log('[DB] SQL.Database initialization completed successfully!');

    returnCostSettings = loadReturnCostSettings();
    refreshReturnCostUi();

    // Initialize offline-safe snapshot prices, then update from the public
    // near-live feed without blocking application startup.
    const tickers = queryAll('SELECT ticker FROM open_positions').map(p => p.ticker);
    loadSnapshotPrices(tickers);

    // Populate advanced dropdowns
    populateSectorDropdown();

    // Wire up dynamic events
    setupBrowseFilters();
    setupNavigation();

    // Render Home tab initially
    renderPage('picks');
    startLivePriceRefresh();

    // Dismiss splash screen with minor timeout to ensure UI elements settled
    setTimeout(() => {
      document.getElementById('splash-screen').style.display = 'none';
      document.getElementById('app-content').style.display = 'flex';
    }, 400);

  } catch(err) {
    clearTimeout(slowLoadTimer);
    console.error('[Launch] App start failed:', err);
    statusEl.textContent = `❌ Hata: ${err?.message || 'Uygulama başlatılamadı.'}`;
    statusEl.style.color = 'var(--danger)';
    retryBtn.style.display = 'block';
  }
}

// Document Load Listener
window.addEventListener('load', () => {
  // Wire up close buttons (history-aware: hardware back also closes)
  document.getElementById('close-detail-btn').addEventListener('click', requestSheetClose);
  document.getElementById('close-backtest-btn').addEventListener('click', requestSheetClose);
  document.getElementById('close-cost-profile-btn').addEventListener('click', requestSheetClose);

  // A reload while a sheet was open leaves its {bpSheet} entry in history;
  // without this cleanup the next hardware-back press is silently swallowed.
  if (history.state?.bpSheet) {
    try { history.replaceState(null, ''); } catch { /* sandboxed */ }
  }

  window.addEventListener('popstate', () => {
    sheetCloseInFlight = false;
    closeOpenSheets();
    // Forward-traversal can land back ON a {bpSheet} entry with no sheet
    // visible — neutralize it so back keeps working one-press-one-action.
    if (history.state?.bpSheet) {
      try { history.replaceState(null, ''); } catch { /* sandboxed */ }
    }
  });

  enableSwipeToClose('detail-sheet');
  enableSwipeToClose('backtest-sheet');
  enableSwipeToClose('cost-profile-sheet');

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.querySelector('.overlay-sheet.open')) {
      requestSheetClose();
    }
  });

  document.getElementById('open-backtest-btn').addEventListener('click', openBacktestingSheet);
  document.getElementById('decision-open-backtest')?.addEventListener('click', openBacktestingSheet);
  document.getElementById('open-cost-profile-btn').addEventListener('click', openCostProfileEditor);
  document.querySelectorAll('[data-return-view]').forEach(button => {
    button.addEventListener('click', () => setReturnView(button.dataset.returnView));
  });
  document.getElementById('cost-profile-form').addEventListener('submit', saveCostProfile);
  
  document.getElementById('refresh-db-btn').addEventListener('click', () => {
    window.location.reload();
  });
  
  document.getElementById('splash-retry-btn').addEventListener('click', () => {
    window.location.reload();
  });

  // Collapsible toggle on details
  const advToggle = document.getElementById('detail-advanced-toggle');
  const advBody = document.getElementById('detail-advanced-body');
  const collapsibleSection = advToggle.closest('.collapsible-section');
  
  advToggle.addEventListener('click', () => {
    const isOpen = advBody.style.display !== 'none';
    if (isOpen) {
      advBody.style.display = 'none';
      collapsibleSection.classList.remove('open');
    } else {
      advBody.style.display = 'block';
      collapsibleSection.classList.add('open');
    }
  });

  // Run app
  initApp();
});
