/**
 * Phase 0 Test Harness for Options Dashboard Mock Server
 * Uses Node.js built-in test runner (node:test) - no external dependencies
 *
 * Run from project root:
 *   node --test tests/test_mock.js
 */

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { request } from 'node:http';

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------

const SERVER_HOST = '127.0.0.1';
const SERVER_PORT = 8765;
const BASE_URL    = `http://${SERVER_HOST}:${SERVER_PORT}`;

let serverProcess = null;

/**
 * Wait for the server to be accepting connections by polling the health
 * endpoint (any 2xx or 4xx is fine — it means the process is up).
 */
function waitForServer(timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    function attempt() {
      const req = request(
        { hostname: SERVER_HOST, port: SERVER_PORT, path: '/api/dashboard', method: 'GET' },
        (res) => {
          res.resume(); // drain
          resolve();
        }
      );
      req.on('error', () => {
        if (Date.now() > deadline) {
          reject(new Error('Mock server did not start in time'));
        } else {
          setTimeout(attempt, 200);
        }
      });
      req.end();
    }
    attempt();
  });
}

/** Perform a GET request and return { status, body } */
function get(path) {
  return new Promise((resolve, reject) => {
    const req = request(
      { hostname: SERVER_HOST, port: SERVER_PORT, path, method: 'GET' },
      (res) => {
        let raw = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { raw += chunk; });
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(raw), headers: res.headers });
          } catch (e) {
            resolve({ status: res.statusCode, body: raw, headers: res.headers });
          }
        });
      }
    );
    req.on('error', reject);
    req.end();
  });
}

before(async () => {
  // Resolve path relative to this file (tests/ directory is a sibling of mock/)
  const serverPath = new URL('../mock/server.py', import.meta.url).pathname;

  serverProcess = spawn('python3', [serverPath], {
    stdio: ['ignore', 'pipe', 'pipe']
  });

  serverProcess.stderr.on('data', (d) => process.stderr.write('[server] ' + d));
  serverProcess.stdout.on('data', (d) => process.stdout.write('[server] ' + d));

  serverProcess.on('error', (err) => {
    throw new Error('Failed to start mock server: ' + err.message);
  });

  await waitForServer(10000);
  console.log('[test] Mock server is ready on port', SERVER_PORT);
});

after(() => {
  if (serverProcess) {
    serverProcess.kill('SIGTERM');
    console.log('[test] Mock server killed');
  }
});

// ---------------------------------------------------------------------------
// Helper assertions
// ---------------------------------------------------------------------------

function assertString(val, label) {
  assert.equal(typeof val, 'string', `${label} should be a string`);
}
function assertNumber(val, label) {
  assert.equal(typeof val, 'number', `${label} should be a number`);
}
function assertBool(val, label) {
  assert.equal(typeof val, 'boolean', `${label} should be a boolean`);
}
function assertArray(val, label) {
  assert.ok(Array.isArray(val), `${label} should be an array`);
}
function assertObject(val, label) {
  assert.equal(typeof val, 'object', `${label} should be an object`);
  assert.ok(!Array.isArray(val), `${label} should be a plain object`);
}

// ---------------------------------------------------------------------------
// CORS helper
// ---------------------------------------------------------------------------

function assertCORS(headers, endpoint) {
  assert.equal(
    headers['access-control-allow-origin'], '*',
    `${endpoint} must return CORS header Access-Control-Allow-Origin: *`
  );
}

// ---------------------------------------------------------------------------
// Tests: individual endpoints — structure
// ---------------------------------------------------------------------------

test('GET /api/market_decision — structure', async () => {
  const { status, body, headers } = await get('/api/market_decision');
  assert.equal(status, 200);
  assertCORS(headers, '/api/market_decision');
  assertString(body.timestamp,       'timestamp');
  assertNumber(body.spot,            'spot');
  assertString(body.decision,        'decision');
  assertNumber(body.confidence,      'confidence');
  assertString(body.action,          'action');
  assertArray(body.supporting_signals, 'supporting_signals');
  assertArray(body.opposing_signals,   'opposing_signals');
  assert.ok(body.spot > 100000, 'spot should be a realistic BTC price');
});

test('GET /api/options_walls — structure', async () => {
  const { status, body, headers } = await get('/api/options_walls');
  assert.equal(status, 200);
  assertCORS(headers, '/api/options_walls');
  assertNumber(body.spot,            'spot');
  assertArray(body.walls,            'walls');
  assert.ok(body.walls.length > 0,   'walls should not be empty');
  const wall = body.walls[0];
  assertNumber(wall.strike, 'wall.strike');
  assertString(wall.type,   'wall.type');
  assertNumber(wall.gex,    'wall.gex');
  assertNumber(wall.oi,     'wall.oi');
  assertString(wall.label,  'wall.label');
  assertNumber(body.gamma_flip_level, 'gamma_flip_level');
  assertNumber(body.net_gex,          'net_gex');
});

test('GET /api/probability_engine — structure', async () => {
  const { status, body, headers } = await get('/api/probability_engine');
  assert.equal(status, 200);
  assertCORS(headers, '/api/probability_engine');
  assertNumber(body.spot, 'spot');
  assertArray(body.scenarios, 'scenarios');
  assert.equal(body.scenarios.length, 3, 'should have 3 scenarios');
  const sc = body.scenarios[0];
  assertString(sc.label,       'scenario.label');
  assertNumber(sc.probability, 'scenario.probability');
  assertNumber(sc.target,      'scenario.target');
  assertNumber(body.expected_move_1sd, 'expected_move_1sd');
  assertNumber(body.skew,              'skew');
});

test('GET /api/dashboard — nominal structure', async () => {
  const { status, body, headers } = await get('/api/dashboard');
  assert.equal(status, 200);
  assertCORS(headers, '/api/dashboard');
  assertNumber(body.spot,                   'spot');
  assertNumber(body.gex_total,              'gex_total');
  assertNumber(body.mopi_squeeze_heuristic, 'mopi_squeeze_heuristic');
  assertNumber(body.funding_rate,           'funding_rate');
  assertString(body.regime,                 'regime');
  assertNumber(body.gamma_flip,             'gamma_flip');
  assert.ok(body.gex_total > 0, 'gex_total should be positive in nominal case');
  assert.ok(body.mopi_squeeze_heuristic > 0, 'mopi_squeeze_heuristic should be > 0 in nominal');
});

test('GET /api/dashboard?variant=gex_zero — gex_total is 0', async () => {
  const { status, body } = await get('/api/dashboard?variant=gex_zero');
  assert.equal(status, 200);
  assert.equal(body.gex_total, 0, 'gex_total must be 0 for gex_zero variant');
});

test('GET /api/dashboard?variant=squeeze_zero — mopi_squeeze_heuristic is 0', async () => {
  const { status, body } = await get('/api/dashboard?variant=squeeze_zero');
  assert.equal(status, 200);
  assert.equal(body.mopi_squeeze_heuristic, 0, 'mopi_squeeze_heuristic must be 0 for squeeze_zero variant');
});

test('GET /api/dealer_pressure — structure', async () => {
  const { status, body, headers } = await get('/api/dealer_pressure');
  assert.equal(status, 200);
  assertCORS(headers, '/api/dealer_pressure');
  assertNumber(body.net_delta_pressure,  'net_delta_pressure');
  assertString(body.pressure_direction,  'pressure_direction');
  assertNumber(body.pressure_magnitude,  'pressure_magnitude');
  assertArray(body.flows,                'flows');
  assert.ok(body.flows.length > 0, 'flows should not be empty');
  const flow = body.flows[0];
  assertString(flow.time,      'flow.time');
  assertNumber(flow.delta,     'flow.delta');
  assertString(flow.direction, 'flow.direction');
});

test('GET /api/narrative — nominal structure and banner', async () => {
  const { status, body, headers } = await get('/api/narrative');
  assert.equal(status, 200);
  assertCORS(headers, '/api/narrative');
  assertString(body.phrase_synthese,  'phrase_synthese');
  assertString(body.sentiment,        'sentiment');
  assertArray(body.key_themes,        'key_themes');
  assertArray(body.risk_factors,      'risk_factors');
  assert.ok('banner_message' in body, 'nominal narrative should have banner_message');
  assert.equal(body.banner_message, 'Test banner', 'banner_message should equal "Test banner"');
});

test('GET /api/narrative?variant=xss — phrase_synthese contains raw < characters (unescaped)', async () => {
  const { status, body } = await get('/api/narrative?variant=xss');
  assert.equal(status, 200);
  assertString(body.phrase_synthese, 'phrase_synthese');
  assert.ok(
    body.phrase_synthese.includes('<'),
    'phrase_synthese from xss variant must contain literal < (server must NOT pre-escape HTML)'
  );
  assert.ok(
    body.phrase_synthese.includes('<b>test XSS</b>'),
    'phrase_synthese must contain <b>test XSS</b>'
  );
});

test('GET /api/narrative?variant=no_banner — no banner_message field', async () => {
  const { status, body } = await get('/api/narrative?variant=no_banner');
  assert.equal(status, 200);
  assert.ok(!('banner_message' in body), 'no_banner variant must NOT have banner_message field');
});

test('GET /api/model_arena/leaderboard — structure', async () => {
  const { status, body, headers } = await get('/api/model_arena/leaderboard');
  assert.equal(status, 200);
  assertCORS(headers, '/api/model_arena/leaderboard');
  assertArray(body.models, 'models');
  assert.ok(body.models.length > 0, 'models list should not be empty');
  const m = body.models[0];
  assertNumber(m.rank,            'model.rank');
  assertString(m.name,            'model.name');
  assertNumber(m.accuracy,        'model.accuracy');
  assertNumber(m.sharpe,          'model.sharpe');
  assertNumber(m.total_signals,   'model.total_signals');
  assertNumber(m.correct_signals, 'model.correct_signals');
});

test('GET /api/vol_structure — structure', async () => {
  const { status, body, headers } = await get('/api/vol_structure');
  assert.equal(status, 200);
  assertCORS(headers, '/api/vol_structure');
  assertArray(body.term_structure, 'term_structure');
  assert.ok(body.term_structure.length >= 4, 'term_structure should have at least 4 entries');
  const ts = body.term_structure[0];
  assertString(ts.expiry, 'ts.expiry');
  assertNumber(ts.iv,     'ts.iv');
  assertNumber(ts.rv,     'ts.rv');
  assertNumber(body.skew_25d, 'skew_25d');
  assertNumber(body.atm_vol,  'atm_vol');
});

test('GET /api/mopi_vs_btc?period=7d — structure and data length', async () => {
  const { status, body, headers } = await get('/api/mopi_vs_btc?period=7d');
  assert.equal(status, 200);
  assertCORS(headers, '/api/mopi_vs_btc');
  assert.equal(body.period, '7d');
  assertArray(body.data, 'data');
  assert.equal(body.data.length, 7, '7d period should return 7 data points');
  const d = body.data[0];
  assertNumber(d.day,         'd.day');
  assertNumber(d.spot,        'd.spot');
  assertNumber(d.mopi_score,  'd.mopi_score');
  assertNumber(d.mopi_squeeze,'d.mopi_squeeze');
});

test('GET /api/mopi_vs_btc?period=14d — 14 data points', async () => {
  const { status, body } = await get('/api/mopi_vs_btc?period=14d');
  assert.equal(status, 200);
  assert.equal(body.data.length, 14, '14d period should return 14 data points');
});

test('GET /api/mopi_vs_btc?period=30d — 30 data points', async () => {
  const { status, body } = await get('/api/mopi_vs_btc?period=30d');
  assert.equal(status, 200);
  assert.equal(body.data.length, 30, '30d period should return 30 data points');
});

test('GET /api/vex_cex — structure', async () => {
  const { status, body, headers } = await get('/api/vex_cex');
  assert.equal(status, 200);
  assertCORS(headers, '/api/vex_cex');
  assertNumber(body.vex_score,    'vex_score');
  assertNumber(body.cex_score,    'cex_score');
  assertNumber(body.vex_cex_ratio,'vex_cex_ratio');
  assertString(body.combined_signal, 'combined_signal');
  assertObject(body.breakdown,    'breakdown');
});

test('GET /api/vex_cex_history?period=7d — structure', async () => {
  const { status, body, headers } = await get('/api/vex_cex_history?period=7d');
  assert.equal(status, 200);
  assertCORS(headers, '/api/vex_cex_history');
  assert.equal(body.period, '7d');
  assertArray(body.data, 'data');
  assert.equal(body.data.length, 7, '7d history should have 7 entries');
});

test('GET /api/model_arena/bme_status — nominal: has_edge=true, wilson_lb is float', async () => {
  const { status, body, headers } = await get('/api/model_arena/bme_status');
  assert.equal(status, 200);
  assertCORS(headers, '/api/model_arena/bme_status');
  assertString(body.model,           'model');
  assertNumber(body.dir_winrate,     'dir_winrate');
  assertNumber(body.n_dir_attempted, 'n_dir_attempted');
  assertBool(body.has_edge,          'has_edge');
  assertNumber(body.wilson_lb,       'wilson_lb');
  assert.equal(body.has_edge, true,  'nominal bme_status should have has_edge=true');
  assert.equal(body.dir_winrate, 0.70, 'nominal dir_winrate should be 0.70');
  assert.equal(body.n_dir_attempted, 60, 'nominal n_dir_attempted should be 60');
  assert.ok(Number.isFinite(body.wilson_lb), 'wilson_lb must be a finite float');
  assert.ok(body.wilson_lb === 0.58, 'nominal wilson_lb should be 0.58');
});

test('GET /api/model_arena/bme_status?variant=no_edge — has_edge=false', async () => {
  const { status, body } = await get('/api/model_arena/bme_status?variant=no_edge');
  assert.equal(status, 200);
  assertBool(body.has_edge, 'has_edge');
  assert.equal(body.has_edge, false,  'no_edge variant should have has_edge=false');
  assert.equal(body.dir_winrate, 0.53, 'no_edge dir_winrate should be 0.53');
  assert.equal(body.n_dir_attempted, 30, 'no_edge n_dir_attempted should be 30');
  assert.ok(body.wilson_lb < 0.50, 'no_edge wilson_lb should be < 0.50 (no edge)');
});

test('GET /api/snapshot — nominal returns all 8 required keys', async () => {
  const { status, body, headers } = await get('/api/snapshot');
  assert.equal(status, 200);
  assertCORS(headers, '/api/snapshot');

  const requiredKeys = [
    'snapshot_ts', 'spot', 'dashboard', 'walls',
    'dealer', 'squeeze', 'narrative', 'gravity', 'bme_status'
  ];
  for (const key of requiredKeys) {
    assert.ok(key in body, `snapshot must have key: ${key}`);
  }

  assertString(body.snapshot_ts, 'snapshot_ts');
  assertNumber(body.spot,        'spot');
  assertObject(body.dashboard,   'dashboard');
  assertObject(body.walls,       'walls');
  assertObject(body.dealer,      'dealer');
  assertObject(body.squeeze,     'squeeze');
  assertObject(body.narrative,   'narrative');
  assertObject(body.gravity,     'gravity');
  assertObject(body.bme_status,  'bme_status');
});

test('GET /api/snapshot?variant=down — returns HTTP 500', async () => {
  const { status, body } = await get('/api/snapshot?variant=down');
  assert.equal(status, 500, 'snapshot?variant=down must return HTTP 500');
  assertString(body.error, 'error field in 500 response');
});

test('GET /api/unknown — returns HTTP 404', async () => {
  const { status, body } = await get('/api/unknown_nonexistent');
  assert.equal(status, 404, 'unknown endpoint must return 404');
  assertString(body.error, 'error field in 404 response');
});

// ---------------------------------------------------------------------------
// Phase 1 Frontend Fix Verification Tests
// ---------------------------------------------------------------------------

test('fmtPrice has adaptive precision (M7)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/fmt.js', 'utf8');
  assert.ok(
    src.includes('minimumFractionDigits'),
    'fmtPrice must use minimumFractionDigits for adaptive precision (M7 fix)'
  );
});

test('fmtPrice accepts optional decimals param (M7)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/fmt.js', 'utf8');
  assert.ok(
    src.includes('export function fmtPrice(val, decimals)'),
    'fmtPrice must accept optional decimals parameter (M7 fix)'
  );
});

test('fmtPrice adaptive logic covers three tiers (M7)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/fmt.js', 'utf8');
  assert.ok(
    src.includes('n >= 10000 ? 0 : n >= 1000 ? 1 : 2'),
    'fmtPrice must have three-tier adaptive decimal logic: >=10000→0, >=1000→1, else→2 (M7 fix)'
  );
});

test('C1 — no ECharts tooltip formatters with p.name (audit note)', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  // Confirm no ECharts is used — all charts are raw Canvas2D, so tooltip NaN bug cannot exist
  const hasEcharts = html.includes('echarts') || html.includes('ECharts');
  assert.ok(!hasEcharts, 'C1 audit: file must not use ECharts (all charts are Canvas2D; no tooltip NaN possible)');
});

test('C2 — no markLine usage (audit note)', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  const hasMarkLine = html.includes('markLine');
  assert.ok(!hasMarkLine, 'C2 audit: file must not contain stray markLine (no ECharts series present)');
});

// ---------------------------------------------------------------------------
// Phase 2 Frontend Fix Verification Tests
// ---------------------------------------------------------------------------

test('C4 — HTML contains AbortController (race condition fix)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/scheduler.js', 'utf8');
  assert.ok(
    src.includes('AbortController'),
    'C4 fix: scheduler.js must contain AbortController for race condition prevention'
  );
});

test('E1 — HTML contains Promise.allSettled (parallel error isolation)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/scheduler.js', 'utf8');
  assert.ok(
    src.includes('Promise.allSettled'),
    'E1 fix: scheduler.js must use Promise.allSettled so one module failure does not block others'
  );
});

test('M4 — HTML contains visibilitychange event (tab visibility scheduler)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/scheduler.js', 'utf8');
  assert.ok(
    src.includes('visibilitychange'),
    'M4 fix: scheduler.js must listen for visibilitychange to refresh stale data on tab focus'
  );
});

test('M4 — HTML contains _onVisibilityChange handler function', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/scheduler.js', 'utf8');
  assert.ok(
    src.includes('_onVisibilityChange'),
    'M4 fix: scheduler.js must define _onVisibilityChange function for tab visibility handling'
  );
});

test('E2 — HTML contains data-stale attribute (stale data indicator)', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  assert.ok(
    html.includes('data-stale'),
    'E2 fix: HTML must use data-stale attribute to visually indicate failed/stale modules'
  );
});

test('E2 — HTML contains api-error-banner element (error banner)', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  assert.ok(
    html.includes('api-error-banner'),
    'E2 fix: HTML must contain api-error-banner element for displaying API errors'
  );
});

// ---------------------------------------------------------------------------
// Phase 3 Frontend Fix Verification Tests
// ---------------------------------------------------------------------------

test('E3 — esc() helper defined in HTML', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/fmt.js', 'utf8');
  assert.ok(
    src.includes('export function esc(s)'),
    'E3 fix: js/lib/fmt.js must export esc(s) XSS escape helper function'
  );
  assert.ok(
    src.includes(".replace(/&/g, '&amp;')"),
    'E3 fix: esc() must escape ampersands'
  );
  assert.ok(
    src.includes(".replace(/</g, '&lt;')"),
    'E3 fix: esc() must escape < characters'
  );
});

test('E3 — narrative phrase uses esc() wrapper', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/narrative.js', 'utf8');
  assert.ok(
    src.includes('${esc(phrase)}'),
    'E3 fix: loadNarrative must wrap phrase in esc() to prevent XSS'
  );
  assert.ok(
    src.includes('${esc(banner)}'),
    'E3 fix: loadNarrative must wrap banner in esc() to prevent XSS'
  );
});

test('E3 — loadSignal watch uses esc() wrapper', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/signal.js', 'utf8');
  assert.ok(
    src.includes('${esc(watch)}'),
    'E3 fix: loadSignal must wrap watch in esc() to prevent XSS'
  );
  assert.ok(
    src.includes('${esc(w)}'),
    'E3 fix: loadSignal warnings must use esc(w) for each warning pill'
  );
});

test('E6 — GEX=0 gamma flip text present', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/gex_dex.js', 'utf8');
  assert.ok(
    src.includes('GEX = 0') && src.includes('Gamma Flip'),
    'E6 fix: loadGexDex must handle GEX=0 as gamma flip point with specific text'
  );
  assert.ok(
    src.includes('gexValid'),
    'E6 fix: loadGexDex must define gexValid to handle null/NaN GEX'
  );
  assert.ok(
    src.includes("lastGex === 0 ? 'var(--yellow)'"),
    'E6 fix: GEX=0 must use yellow color (gamma flip / neutral)'
  );
});

test('M3 — loadGexDex has try/catch', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/gex_dex.js', 'utf8');
  assert.ok(src.includes('export async function loadGexDex'), 'loadGexDex must exist');
  assert.ok(
    src.includes('try {') && src.includes('} catch(e)'),
    'M3 fix: loadGexDex must have try/catch for error isolation'
  );
});

test('M3 — loadContext has try/catch', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/context.js', 'utf8');
  assert.ok(src.includes('export async function loadContext'), 'loadContext must exist');
  assert.ok(
    src.includes('try {') && src.includes('} catch(e)'),
    'M3 fix: loadContext must have try/catch for error isolation'
  );
});

test('M3 — loadSignal has try/catch', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/signal.js', 'utf8');
  assert.ok(src.includes('export async function loadSignal'), 'loadSignal must exist');
  assert.ok(
    src.includes('try {') && src.includes('} catch(e)'),
    'M3 fix: loadSignal must have try/catch for error isolation'
  );
});

test('M3 — loadVolWeather has try/catch', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/vol_weather.js', 'utf8');
  assert.ok(src.includes('export async function loadVolWeather'), 'loadVolWeather must exist');
  assert.ok(
    src.includes('try {') && src.includes('} catch(e)'),
    'M3 fix: loadVolWeather must have try/catch for error isolation'
  );
});

// ---------------------------------------------------------------------------
// Phase 4 Frontend Fix Verification Tests
// ---------------------------------------------------------------------------

test('E4 — wilsonLB() function defined', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/stats.js', 'utf8');
  assert.ok(
    src.includes('export function wilsonLB(wr, n, z)'),
    'E4 fix: js/lib/stats.js must export wilsonLB(wr, n, z) function'
  );
  assert.ok(
    src.includes('centre - margin'),
    'E4 fix: wilsonLB must compute centre - margin (Wilson lower bound formula)'
  );
  assert.ok(
    src.includes('Math.max(0, centre - margin)'),
    'E4 fix: wilsonLB must return Math.max(0, centre - margin)'
  );
});

test('E4 — clientHasEdge() function defined', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/stats.js', 'utf8');
  assert.ok(
    src.includes('export function clientHasEdge(wr, n)'),
    'E4 fix: js/lib/stats.js must export clientHasEdge(wr, n) function'
  );
  assert.ok(
    src.includes('return wilsonLB(wr, n) > 0.50'),
    'E4 fix: clientHasEdge must compare wilsonLB result to 0.50 threshold'
  );
});

test('M8 — CFG config object defined', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/config.js', 'utf8');
  assert.ok(
    src.includes('export const CFG = {'),
    'M8 fix: js/config.js must export CFG config object'
  );
  assert.ok(
    src.includes('FLIP_NEAR_PCT:  2.0'),
    'M8 fix: CFG must contain FLIP_NEAR_PCT threshold'
  );
  assert.ok(
    src.includes('FLIP_WARN_PCT:  5.0'),
    'M8 fix: CFG must contain FLIP_WARN_PCT threshold'
  );
  assert.ok(
    src.includes('GEX_BIG_VEX:           20e6'),
    'M8 fix: CFG must contain GEX_BIG_VEX threshold'
  );
  assert.ok(
    src.includes('GEX_BIG_CEX:           10e6'),
    'M8 fix: CFG must contain GEX_BIG_CEX threshold'
  );
  assert.ok(
    src.includes('LEVELS_NEAR_THRESH: 0.015'),
    'M8 fix: CFG must contain LEVELS_NEAR_THRESH'
  );
});

test('M8 — CFG.FLIP_NEAR_PCT replaces magic 2.0', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/regime.js', 'utf8');
  assert.ok(
    src.includes('CFG.FLIP_NEAR_PCT'),
    'M8 fix: CFG.FLIP_NEAR_PCT must be used in regime.js (not magic 2.0)'
  );
  assert.ok(
    src.includes('CFG.FLIP_WARN_PCT'),
    'M8 fix: CFG.FLIP_WARN_PCT must be used in regime.js (not magic 5.0)'
  );
  assert.ok(
    !src.includes('Math.abs(flipDistPct) <= 2.0'),
    'M8 fix: magic 2.0 for flipDistPct must be replaced with CFG.FLIP_NEAR_PCT'
  );
  assert.ok(
    !src.includes('Math.abs(flipDistPct) <= 5.0'),
    'M8 fix: magic 5.0 for flipDistPct must be replaced with CFG.FLIP_WARN_PCT'
  );
});

test('M2 — tagBadge() function defined', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/fmt.js', 'utf8');
  assert.ok(
    src.includes('export function tagBadge(tag)'),
    'M2 fix: js/lib/fmt.js must export tagBadge(tag) function'
  );
  assert.ok(
    src.includes("ACTIONABLE: 'var(--green)'"),
    'M2 fix: tagBadge must map ACTIONABLE->green'
  );
  assert.ok(
    src.includes('tag-badge'),
    'M2 fix: tagBadge must use tag-badge CSS class'
  );
});

test('M1 — tag-badge CSS class defined', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  assert.ok(
    html.includes('.tag-badge {'),
    'M1 fix: .tag-badge CSS class must be defined in style block'
  );
  assert.ok(
    html.includes('border-radius: 5px'),
    'M1 fix: .tag-badge must have border-radius'
  );
  assert.ok(
    html.includes('text-transform: uppercase'),
    'M1 fix: .tag-badge must use uppercase text-transform'
  );
});

test('M1 — wallCard uses tagBadge() instead of inline tagColor', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/levels.js', 'utf8');
  const wallCardIdx = src.indexOf('const wallCard = (w, side) =>');
  assert.ok(wallCardIdx !== -1, 'wallCard must exist in levels.js');
  const wallCardEnd = src.indexOf('};', wallCardIdx) + 2;
  const wallCardBody = src.slice(wallCardIdx, wallCardEnd);
  assert.ok(
    wallCardBody.includes('tagBadge(w.tag)'),
    'M1 fix: wallCard must use tagBadge(w.tag)'
  );
  assert.ok(
    !wallCardBody.includes('tagColor'),
    'M1 fix: wallCard must not use old tagColor variable'
  );
});

test('LEGEND — gex-dex-legend CSS class defined', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  assert.ok(
    html.includes('.gex-dex-legend {'),
    'LEGEND fix: .gex-dex-legend CSS class must be defined'
  );
});

test('LEGEND — dealer direction legend HTML rendered in loadGexDex', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/gex_dex.js', 'utf8');
  assert.ok(
    src.includes('gex-dex-legend'),
    'LEGEND fix: gex-dex-legend div must appear in loadGexDex output'
  );
  assert.ok(
    src.includes('Stabilisant'),
    'LEGEND fix: legend must include Stabilisant label (GEX+)'
  );
  assert.ok(
    src.includes('Amplifiant'),
    'LEGEND fix: legend must include Amplifiant label (GEX-)'
  );
  assert.ok(
    src.includes('Flip (GEX=0)'),
    'LEGEND fix: legend must include Flip (GEX=0) label'
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// Phase 5 — ES Modules Tests
// ─────────────────────────────────────────────────────────────────────────────

test('Phase 5 -- js/main.js exists', async () => {
  const fs = await import('node:fs');
  assert.ok(fs.existsSync('./frontend/js/main.js'), 'js/main.js must exist');
});

test('Phase 5 -- js/config.js exports CFG', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/config.js', 'utf8');
  assert.ok(src.includes('export const CFG'), 'config.js must export CFG');
  assert.ok(src.includes('export const API_BASE'), 'config.js must export API_BASE');
  assert.ok(src.includes('export const REFRESH_INTERVAL'), 'config.js must export REFRESH_INTERVAL');
  assert.ok(src.includes('export const ELITE_PLAN'), 'config.js must export ELITE_PLAN');
});

test('Phase 5 -- js/lib/stats.js exports wilsonLB', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/stats.js', 'utf8');
  assert.ok(src.includes('export function wilsonLB'), 'stats.js must export wilsonLB');
  assert.ok(src.includes('export function clientHasEdge'), 'stats.js must export clientHasEdge');
});

test('Phase 5 -- js/lib/fmt.js exports formatters', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/fmt.js', 'utf8');
  assert.ok(src.includes('export function esc'), 'fmt.js must export esc');
  assert.ok(src.includes('export function fmtPrice'), 'fmt.js must export fmtPrice');
  assert.ok(src.includes('export function fmtPct'), 'fmt.js must export fmtPct');
  assert.ok(src.includes('export function formatModelName'), 'fmt.js must export formatModelName');
  assert.ok(src.includes('export function tagBadge'), 'fmt.js must export tagBadge');
  assert.ok(src.includes('export function fmtBig'), 'fmt.js must export fmtBig');
});

test('Phase 5 -- js/lib/canvas.js exports draw functions', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/lib/canvas.js', 'utf8');
  assert.ok(src.includes('export function drawSparkline'), 'canvas.js must export drawSparkline');
  assert.ok(src.includes('export function drawDualAxis'), 'canvas.js must export drawDualAxis');
  assert.ok(src.includes('export function drawVcLine'), 'canvas.js must export drawVcLine');
});

test('Phase 5 -- js/api.js exports apiFetch', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/api.js', 'utf8');
  assert.ok(src.includes('export async function apiFetch'), 'api.js must export apiFetch');
  assert.ok(src.includes('export function getAuthHeaders'), 'api.js must export getAuthHeaders');
});

test('Phase 5 -- js/store.js exports state', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/store.js', 'utf8');
  assert.ok(src.includes('export let authState'), 'store.js must export authState');
  assert.ok(src.includes('export let lastBtcPrice'), 'store.js must export lastBtcPrice');
  assert.ok(src.includes('export let currentPeriod'), 'store.js must export currentPeriod');
  assert.ok(src.includes('export let vcPeriod'), 'store.js must export vcPeriod');
  assert.ok(src.includes('export let lastRegime'), 'store.js must export lastRegime');
});

test('Phase 5 -- js/auth.js exports auth functions', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/auth.js', 'utf8');
  assert.ok(src.includes('export async function checkAuth'), 'auth.js must export checkAuth');
  assert.ok(src.includes('export function loginWithPatreon'), 'auth.js must export loginWithPatreon');
  assert.ok(src.includes('export async function handleLegacyLogin'), 'auth.js must export handleLegacyLogin');
  assert.ok(src.includes('export function logout'), 'auth.js must export logout');
  assert.ok(src.includes('export function showScreen'), 'auth.js must export showScreen');
  assert.ok(src.includes('export async function initApp'), 'auth.js must export initApp');
});

test('Phase 5 -- js/scheduler.js exports loop functions', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/scheduler.js', 'utf8');
  assert.ok(src.includes('export async function loadAllData'), 'scheduler.js must export loadAllData');
  assert.ok(src.includes('export function startRefreshLoop'), 'scheduler.js must export startRefreshLoop');
  assert.ok(src.includes('export function updateCountdown'), 'scheduler.js must export updateCountdown');
  assert.ok(src.includes('export async function loadBtcPrice'), 'scheduler.js must export loadBtcPrice');
});

test('Phase 5 -- HTML uses type=module script', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  assert.ok(html.includes('type="module"'), 'HTML must use ES module script tag');
  assert.ok(html.includes('src="js/main.js"'), 'HTML must reference js/main.js');
  assert.ok(!html.includes('function loadSignal'), 'loadSignal must be removed from HTML inline script');
  assert.ok(!html.includes('function initApp'), 'initApp must be removed from HTML inline script');
  assert.ok(!html.includes('function loadAllData'), 'loadAllData must be removed from HTML inline script');
});

test('Phase 5 -- no window._lastRegime in HTML', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  assert.ok(!html.includes('window._lastRegime'), 'window._lastRegime global must be removed from HTML');
});

test('Phase 5 -- all widget modules exist', async () => {
  const fs = await import('node:fs');
  const widgets = ['signal','levels','probabilities','context','narrative','model','vol_weather','gex_dex','mopi_btc','regime','vex_cex'];
  for (const w of widgets) {
    assert.ok(fs.existsSync('./frontend/js/widgets/' + w + '.js'), 'widgets/' + w + '.js must exist');
  }
});

test('Phase 5 -- regime.js exports classifyRegimeFull', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/widgets/regime.js', 'utf8');
  assert.ok(src.includes('export function classifyRegimeFull'), 'regime.js must export classifyRegimeFull');
  assert.ok(src.includes('export function classifyRegime'), 'regime.js must export classifyRegime');
  assert.ok(src.includes('export function buildLevelsContext'), 'regime.js must export buildLevelsContext');
  assert.ok(src.includes('export async function loadRegimeSummary'), 'regime.js must export loadRegimeSummary');
});

test('Phase 5 -- main.js exposes window handlers', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./frontend/js/main.js', 'utf8');
  assert.ok(src.includes('window.setPeriod'), 'main.js must expose window.setPeriod');
  assert.ok(src.includes('window.setVcPeriod'), 'main.js must expose window.setVcPeriod');
  assert.ok(src.includes('window.loginWithPatreon'), 'main.js must expose window.loginWithPatreon');
  assert.ok(src.includes('window.handleLegacyLogin'), 'main.js must expose window.handleLegacyLogin');
  assert.ok(src.includes('window.logout'), 'main.js must expose window.logout');
});

test('Phase 5 -- HTML line count reduced significantly', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  const lineCount = html.split('\n').length;
  assert.ok(lineCount < 1100, 'HTML must be under 1100 lines (was 3390), got: ' + lineCount);
});

test('Phase 5 -- no inline var declarations in HTML script', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync('./frontend/options_analyzer.html', 'utf8');
  // The only script tag should be type=module
  const scriptMatch = html.match(/<script[^>]*>/g) || [];
  assert.ok(scriptMatch.length === 1, 'HTML must have exactly 1 script tag');
  assert.ok(scriptMatch[0].includes('type="module"'), 'The only script tag must be type=module');
});
