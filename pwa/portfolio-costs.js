/**
 * Model portfolio transaction-cost settings and return math.
 *
 * Prices always come from the public model ledger. The browser stores only the
 * selected broker rates and gross/net view; no personal fills or trades exist.
 */
(function exposePortfolioCosts(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.PortfolioCosts = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function buildApi() {
  'use strict';

  const SETTINGS_VERSION = 1;
  const DEFAULT_SETTINGS = Object.freeze({
    version: SETTINGS_VERSION,
    broker: 'Aracı kurum',
    buyCostPct: 0,
    sellCostPct: 0,
    returnView: 'net',
  });

  function finite(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function normalizeCost(value) {
    return Math.min(100, Math.max(0, finite(value, 0)));
  }

  function normalizeSettings(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    return {
      version: SETTINGS_VERSION,
      broker: String(source.broker || DEFAULT_SETTINGS.broker).trim() || DEFAULT_SETTINGS.broker,
      buyCostPct: normalizeCost(source.buyCostPct),
      sellCostPct: normalizeCost(source.sellCostPct),
      returnView: source.returnView === 'gross' ? 'gross' : 'net',
    };
  }

  function calculateTradeReturn({
    entryPrice,
    exitPrice,
    buyCostPct = 0,
    sellCostPct = 0,
    view = 'net',
    includeBuyCost = true,
    includeSellCost = true,
  }) {
    const entry = finite(entryPrice, null);
    const exit = finite(exitPrice, null);
    if (!(entry > 0) || !(exit > 0)) return null;
    if (view === 'gross') return exit / entry - 1;

    const buyRate = includeBuyCost ? normalizeCost(buyCostPct) / 100 : 0;
    const sellRate = includeSellCost ? normalizeCost(sellCostPct) / 100 : 0;
    return (exit * (1 - sellRate)) / (entry * (1 + buyRate)) - 1;
  }

  return {
    SETTINGS_VERSION,
    DEFAULT_SETTINGS,
    normalizeSettings,
    calculateTradeReturn,
  };
}));
