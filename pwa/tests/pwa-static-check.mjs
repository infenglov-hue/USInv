import assert from "node:assert/strict";
import { createCipheriv, createHash, webcrypto } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { createContext, runInContext } from "node:vm";
import { gzipSync, gunzipSync } from "node:zlib";

const root = resolve(import.meta.dirname, "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const indexHtml = read("index.html");
const serviceWorker = read("sw.js");
const appJs = read("app.js");
const indexCss = read("index.css");
const manifest = JSON.parse(read("manifest.webmanifest"));

const requiredFiles = [
  "app.js",
  "portfolio-costs.js",
  "index.css",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "vendor/pako.min.js",
  "vendor/sql-wasm.js",
  "vendor/sql-wasm.wasm",
  "vendor/apexcharts.min.js",
];

for (const file of requiredFiles) {
  if (!existsSync(resolve(root, file))) {
    throw new Error(`Missing required PWA asset: ${file}`);
  }
}

for (const localRuntime of [
  "./vendor/pako.min.js?v=us4",
  "./vendor/sql-wasm.js?v=us4",
  "./vendor/apexcharts.min.js?v=us4",
]) {
  if (!indexHtml.includes(localRuntime)) {
    throw new Error(`index.html does not reference local runtime: ${localRuntime}`);
  }
}

if (/cdnjs\.cloudflare\.com\/ajax\/libs\/(?:pako|sql\.js)|cdn\.jsdelivr\.net\/npm\/apexcharts/.test(indexHtml)) {
  throw new Error("Core PWA runtime still depends on an external CDN.");
}

if (!serviceWorker.includes("us-picker-shell-v4")) {
  throw new Error("Service worker cache version was not bumped to us4.");
}

if (!appJs.includes("Snapshot bütünlük kontrolü başarısız")) {
  throw new Error("Snapshot SHA-256 validation is missing.");
}

if (!appJs.includes("mixedScalePercent(company?.free_float_pct)")) {
  throw new Error("Stock detail free-float percentage scale normalization is missing.");
}

if (!appJs.includes("infenglov-hue/USInv/live-data/live_prices.json")) {
  throw new Error("PWA near-live price feed URL is missing.");
}

if (!appJs.includes("./vendor/${filename}?v=us4")) {
  throw new Error("sql.js WASM locateFile cache version was not bumped to us4.");
}

if (!indexHtml.includes("./portfolio-costs.js?v=us4")) {
  throw new Error("Portfolio cost profile runtime is missing.");
}

if (!indexHtml.includes("Model Audit") || indexHtml.includes("4Y Model")) {
  throw new Error("Backtest summary still presents a stale fixed-year label.");
}

if (!indexHtml.includes("Portföy Tarihi")
  || !indexHtml.includes("health-portfolio-date")
  || !indexHtml.includes("Sinyal Verisi")
  || !indexHtml.includes("health-signal-date")
  || !appJs.includes("Portföy: ${priceState.portfolioDate")
  || !appJs.includes("Sinyal: ${priceState.stockPriceDate")) {
  throw new Error("Portfolio effective date and T-1 signal date labels are not separated.");
}

if (!appJs.includes("pos.signal_date")
  || !appJs.includes("same_day_open")
  || !appJs.includes("T-1 veri")
  || !appJs.includes("Yarın İşlem Günü")
  || !appJs.includes("cycle_date <= :todayDate")
  || !appJs.includes("exit_date <= :todayDate")) {
  throw new Error("Pre-open T-1 signal / T execution contract is missing from the PWA.");
}

if (!appJs.includes("PortfolioCosts.normalizeSettings")
  || !appJs.includes("PortfolioCosts.calculateTradeReturn")
  || !appJs.includes("returnCostSettings")
  || !indexHtml.includes("Aracı kurum · maliyet %0")
  || !indexHtml.includes("cost-profile-label")
  || !indexHtml.includes("işlem kaydı tutulmaz")) {
  throw new Error("Broker-cost profile (gross/net) wiring is incomplete.");
}

if (!appJs.includes("sonraki rotasyona kadar")
  || !indexHtml.includes("data-rotation-weeks")
  || !indexHtml.includes("data-portfolio-target")) {
  throw new Error("Manifest-driven portfolio target and rotation copy are missing from the PWA.");
}

// B1 continuity (v16): rotation detection must prefer cycle_ref_date and the
// history must be able to build from portfolio_cycle_marks with a legacy
// cohort fallback for pre-B1 snapshots. Rotation date is derived in JS (not
// SQL) so a snapshot missing the column degrades cleanly instead of throwing.
if (!appJs.includes("pos.cycle_ref_date || pos.selection_date")
  || !appJs.includes("portfolio_cycle_marks")
  || !appJs.includes("buildLegacyCohortRecords")) {
  throw new Error("B1 continuity support is missing from the PWA.");
}

if (!appJs.includes("getMarketWidePriceDate")
  || !appJs.includes("getBenchmarkEndPrice")
  || !indexHtml.includes("data-health-card")) {
  throw new Error("PWA data health and market-wide price-date safeguards are missing.");
}

if (!appJs.includes("summarizeQuoteCoverage")
  || !appJs.includes("formatQuoteCoverage")
  || appJs.includes("canlıya yakın`")) {
  throw new Error("PWA quote coverage must separate fresh, delayed, and snapshot prices.");
}

if (!appJs.includes("Ham stres sinyali")
  || !appJs.includes("en az 2/4 seviyesinde 5 iş günü")
  || !appJs.includes("Hedef dağılım")
  || !indexHtml.includes("Nakit hedefi")
  || !indexHtml.includes("Pozisyon ağırlıkları rotasyonda belirlenir.")) {
  throw new Error("PWA cash-allocation explanation is missing or misleading.");
}

if (!appJs.includes("QUALITY_FLAG_META")
  || !appJs.includes("DCF pahalı")
  || !appJs.includes("Model hedef")
  || !appJs.includes("TARGET_SOURCE_META")
  || !appJs.includes("targetSourceMeta")
  || !appJs.includes("inferTargetSource")
  || !appJs.includes("200 günlük ortalamanın")
  || !appJs.includes("parseQualityFlags")
  || !indexCss.includes("mini-risk-chip")) {
  throw new Error("PWA quality-flag risk chips are missing.");
}

if (!indexCss.includes("height: min(900px, 100vh)")) {
  throw new Error("Desktop mobile frame must fit inside the viewport height.");
}

if (appJs.includes("loadSimulatedLivePrices")) {
  throw new Error("PWA still labels snapshot prices as simulated live prices.");
}

if (manifest.start_url !== "./" || manifest.scope !== "./" || manifest.display !== "standalone") {
  throw new Error("PWA manifest install scope/display configuration is invalid.");
}

const window = { addEventListener() {}, location: { hostname: 'localhost' } };
const PortfolioCosts = {
  normalizeSettings: () => ({}),
  calculateTradeReturn: ({ entryPrice, exitPrice }) => exitPrice / entryPrice - 1,
};
const context = createContext({
  window, navigator: {}, PortfolioCosts,
  crypto: webcrypto, atob, TextDecoder, URLSearchParams, DataView, Uint8Array,
  pako: { ungzip: gunzipSync },
});
runInContext(appJs, context, { filename: 'app.js' });
assert.equal(context.getPortfolioSettings().target, 5);
assert.equal(context.getPortfolioSettings().weeks, 2);
window.feedManifest = { rotation: { target_portfolio_size: 8, rotation_weeks: 4 } };
assert.equal(context.getPortfolioSettings().target, 8);
assert.equal(context.getRotationInfo().weeks, 4);
assert.equal(context.marketIsoDate(new Date('2026-11-10T02:00:00Z')), '2026-11-09');
assert.equal(context.daysBetweenIsoDates('2026-11-10', '2026-11-09'), -1);
assert.equal(context.daysBetweenIsoDates('2026-11-10', null), null);
window.feedBacktestAudit = { cases: [], gates: [] };
assert.equal(context.getInvestorAuditSummary().hasWarnings, true);

const stagedOpen = [{ ticker: 'NEW', selection_date: '2026-11-09', entry_price: 120 }];
const scheduledExit = { ticker: 'HELD', selection_date: '2026-10-26', exit_date: '2026-11-09', entry_price: 100, exit_price: 160 };
const cycleMarks = [
  { ticker: 'HELD', cycle_date: '2026-10-26', ref_price: 100 },
  { ticker: 'NEW', cycle_date: '2026-11-09', ref_price: 120 },
];
const beforeOpen = new Date('2026-11-09T14:29:00Z');
const atOpen = new Date('2026-11-09T14:30:00Z');
context.getPriceOnOrBefore = (ticker, date) => ticker === 'SPY' && date === '2026-10-26' ? 100 : ticker === 'SPY' ? 105 : 110;
context.getPriceDataState = () => ({ stockPriceDate: '2026-11-06' });
window.livePriceDetails = { HELD: { quote_time: '2026-11-09T14:25:00Z' } };
for (const withMarks of [false, true]) {
  context.queryAll = (sql, params) => {
    const cutoff = params[':todayDate'];
    if (sql.includes('FROM portfolio_cycle_marks')) return withMarks ? cycleMarks.filter(mark => mark.cycle_date <= cutoff) : [];
    return sql.includes('exit_date > :todayDate') && scheduledExit.exit_date > cutoff ? [scheduledExit] : [];
  };
  const records = context.buildWeeklyPerformanceRecords(stagedOpen, { HELD: 190 }, beforeOpen);
  assert.equal(records.length, 1);
  assert.equal(records[0].weekStartDate, '2026-10-26');
  assert.equal(records[0].isCompleted, false);
  assert.equal(records[0].positions[0].ticker, 'HELD');
  assert.equal(records[0].positions[0].exitPrice, 110);
}
assert.equal(context.latestEffectiveHistoryDate(atOpen), '2026-11-09');

const privateKey = Buffer.alloc(32, 1);
const nonce = Buffer.alloc(12, 2);
const snapshotBytes = gzipSync('synthetic snapshot payload');
const privateManifest = {
  snapshot: {
    filename: 'mobile_snapshot.db.gz',
    sha256: createHash('sha256').update(snapshotBytes).digest('hex'),
    size_bytes: snapshotBytes.length,
  },
};
const encodedManifest = Buffer.from(JSON.stringify(privateManifest));
const header = Buffer.alloc(4);
header.writeUInt32BE(encodedManifest.length);
const cipher = createCipheriv('aes-256-gcm', privateKey, nonce);
const encryptedBytes = Buffer.concat([
  cipher.update(Buffer.concat([header, encodedManifest, snapshotBytes])), cipher.final(), cipher.getAuthTag(),
]);
const envelope = {
  version: 1, filename: 'mobile_snapshot.db.gz.enc', nonce: nonce.toString('base64url'),
  size_bytes: encryptedBytes.length, sha256: createHash('sha256').update(encryptedBytes).digest('hex'),
};
const cryptoKey = await webcrypto.subtle.importKey('raw', privateKey, 'AES-GCM', false, ['decrypt']);
const opened = await context.decryptPrivateSnapshot(envelope, encryptedBytes, cryptoKey);
assert.equal(opened.rawBytes.toString(), 'synthetic snapshot payload');
assert.equal(opened.manifest.snapshot.sha256, privateManifest.snapshot.sha256);
const wrongKey = await webcrypto.subtle.importKey('raw', Buffer.alloc(32, 3), 'AES-GCM', false, ['decrypt']);
await assert.rejects(context.decryptPrivateSnapshot(envelope, encryptedBytes, wrongKey), /Erişim anahtarı bu paketi açamadı/);
const corrupted = Buffer.from(encryptedBytes);
corrupted[0] ^= 1;
await assert.rejects(context.decryptPrivateSnapshot(envelope, corrupted, cryptoKey), /bütünlük kontrolü başarısız/);
assert.throws(() => context.validateEncryptedFeed({ ...envelope, filename: '../private.enc' }));
window.location = { pathname: '/USInv/index.html', hash: `#feed=${privateKey.toString('base64url')}`, search: '' };
context.history = { replaceState: (_state, _title, url) => assert.equal(url, '/USInv/index.html') };
assert.equal(context.takePrivateFeedKeyFromFragment(), privateKey.toString('base64url'));
assert.equal(context.privateFeedStorageId(), 'us-picker-private-feed:/USInv/');

if (!process.argv.includes('--shell-only')) {
  const publication = JSON.parse(read("manifest.json"));
  const snapshotManifest = Object.hasOwn(publication, 'encrypted_feed')
    ? context.validateEncryptedFeed(publication.encrypted_feed)
    : context.validateSnapshotManifest(publication).snapshot;
  const snapshotPath = resolve(root, snapshotManifest.filename);
  const snapshotBytes = readFileSync(snapshotPath);
  const actualHash = createHash("sha256").update(snapshotBytes).digest("hex");
  if (actualHash !== snapshotManifest.sha256) {
    throw new Error("Published snapshot SHA-256 does not match manifest.json.");
  }
  if (statSync(snapshotPath).size !== snapshotManifest.size_bytes) {
    throw new Error("Published snapshot size does not match manifest.json.");
  }
}

console.log("PWA static checks passed.");
