import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PortfolioCosts = require('../portfolio-costs.js');

const defaults = PortfolioCosts.normalizeSettings();
assert.equal(defaults.broker, 'Aracı kurum');
assert.equal(defaults.buyCostPct, 0);
assert.equal(defaults.sellCostPct, 0);
assert.equal(defaults.returnView, 'net');
assert.equal('fills' in defaults, false);

const gross = PortfolioCosts.calculateTradeReturn({
  entryPrice: 100,
  exitPrice: 110,
  buyCostPct: 0.2,
  sellCostPct: 0.2,
  view: 'gross',
});
const net = PortfolioCosts.calculateTradeReturn({
  entryPrice: 100,
  exitPrice: 110,
  buyCostPct: 0.2,
  sellCostPct: 0.2,
  view: 'net',
});
assert.ok(Math.abs(gross - 0.1) < 1e-12);
assert.ok(net < gross);

const buyOnly = PortfolioCosts.calculateTradeReturn({
  entryPrice: 100,
  exitPrice: 110,
  buyCostPct: 0.2,
  sellCostPct: 0.2,
  view: 'net',
  includeSellCost: false,
});
const sellOnly = PortfolioCosts.calculateTradeReturn({
  entryPrice: 100,
  exitPrice: 110,
  buyCostPct: 0.2,
  sellCostPct: 0.2,
  view: 'net',
  includeBuyCost: false,
});
assert.ok(buyOnly < gross);
assert.ok(sellOnly < gross);
assert.ok(net < buyOnly);
assert.ok(net < sellOnly);

const sanitized = PortfolioCosts.normalizeSettings({
  broker: ' ',
  buyCostPct: -1,
  sellCostPct: 'not-a-number',
  returnView: 'gross',
  fills: { ignored: true },
});
assert.equal(sanitized.broker, 'Aracı kurum');
assert.equal(sanitized.buyCostPct, 0);
assert.equal(sanitized.sellCostPct, 0);
assert.equal(sanitized.returnView, 'gross');
assert.equal('fills' in sanitized, false);

console.log('Portfolio cost checks passed.');
