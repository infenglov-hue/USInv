import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const indexHtml = read("index.html");
const serviceWorker = read("sw.js");
const appJs = read("app.js");
const indexCss = read("index.css");
const manifest = JSON.parse(read("manifest.webmanifest"));
const snapshotManifest = JSON.parse(read("manifest.json"));

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
  "mobile_snapshot.db.gz",
];

for (const file of requiredFiles) {
  if (!existsSync(resolve(root, file))) {
    throw new Error(`Missing required PWA asset: ${file}`);
  }
}

for (const localRuntime of [
  "./vendor/pako.min.js?v=us1",
  "./vendor/sql-wasm.js?v=us1",
  "./vendor/apexcharts.min.js?v=us1",
]) {
  if (!indexHtml.includes(localRuntime)) {
    throw new Error(`index.html does not reference local runtime: ${localRuntime}`);
  }
}

if (/cdnjs\.cloudflare\.com\/ajax\/libs\/(?:pako|sql\.js)|cdn\.jsdelivr\.net\/npm\/apexcharts/.test(indexHtml)) {
  throw new Error("Core PWA runtime still depends on an external CDN.");
}

if (!serviceWorker.includes("us-picker-shell-v1")) {
  throw new Error("Service worker cache version was not bumped to us1.");
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

if (!appJs.includes("./vendor/${filename}?v=us1")) {
  throw new Error("sql.js WASM locateFile cache version was not bumped to us1.");
}

if (!indexHtml.includes("./portfolio-costs.js?v=us1")) {
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

if (!appJs.includes("sonraki rotasyona kadar") || !indexHtml.includes("2 haftada bir Pazartesi")) {
  throw new Error("Bi-weekly rotation hold-rule copy is missing from the PWA.");
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
  || !appJs.includes("RİSK DIŞI (%75 Nakit)")) {
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

const snapshotPath = resolve(root, snapshotManifest.snapshot.filename);
const snapshotBytes = readFileSync(snapshotPath);
const actualHash = createHash("sha256").update(snapshotBytes).digest("hex");
if (actualHash !== snapshotManifest.snapshot.sha256) {
  throw new Error("Published snapshot SHA-256 does not match manifest.json.");
}
if (statSync(snapshotPath).size !== snapshotManifest.snapshot.size_bytes) {
  throw new Error("Published snapshot size does not match manifest.json.");
}

console.log("PWA static checks passed.");
