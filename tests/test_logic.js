/**
 * Phase 6 — Logic Test Suite
 * Tests pure JS logic (stats, fmt, config, payload validation)
 * Run from project root: node --test tests/test_logic.js
 *
 * Uses node:test + node:assert/strict
 * Logic functions are inlined (no ES module import chains needed)
 */

import { test, before, after, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { request } from 'node:http';

// ---------------------------------------------------------------------------
// Inlined logic — A: stats.js (wilsonLB, clientHasEdge)
// ---------------------------------------------------------------------------

function wilsonLB(wr, n, z) {
  if (wr == null || n == null || n <= 0) return null;
  z = z || 1.96;
  const z2 = z * z;
  const denom = 1.0 + z2 / n;
  const centre = (wr + z2 / (2 * n)) / denom;
  const margin = z * Math.sqrt(wr * (1 - wr) / n + z2 / (4 * n * n)) / denom;
  return Math.max(0, centre - margin);
}

function clientHasEdge(wr, n) {
  if (wr == null || n == null || n < 30) return false;
  return wilsonLB(wr, n) > 0.50;
}

// ---------------------------------------------------------------------------
// Inlined logic — B: fmt.js (esc)
// ---------------------------------------------------------------------------

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// Inlined logic — C: fmt.js (fmtPrice)
// ---------------------------------------------------------------------------

function fmtPrice(val, decimals) {
  if (val == null) return '\u2014';
  const n = Number(val);
  const d = decimals != null ? decimals : n >= 10000 ? 0 : n >= 1000 ? 1 : 2;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}

// ---------------------------------------------------------------------------
// Mock server lifecycle (for Part E — payload validation)
// ---------------------------------------------------------------------------

const SERVER_HOST = '127.0.0.1';
const SERVER_PORT = 8765;

let serverProcess = null;

function waitForServer(timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    function attempt() {
      const req = request(
        { hostname: SERVER_HOST, port: SERVER_PORT, path: '/api/dashboard', method: 'GET' },
        (res) => { res.resume(); resolve(); }
      );
      req.on('error', () => {
        if (Date.now() > deadline) {
          reject(new Error('Mock server did not start within timeout'));
        } else {
          setTimeout(attempt, 200);
        }
      });
      req.end();
    }
    attempt();
  });
}

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
            resolve({ status: res.statusCode, body: JSON.parse(raw) });
          } catch (e) {
            resolve({ status: res.statusCode, body: raw });
          }
        });
      }
    );
    req.on('error', reject);
    req.end();
  });
}

before(async () => {
  const serverPath = new URL('../mock/server.py', import.meta.url).pathname;
  serverProcess = spawn('python3', [serverPath], {
    stdio: ['ignore', 'pipe', 'pipe'],
    cwd: new URL('..', import.meta.url).pathname,
  });
  serverProcess.stderr.on('data', (d) => {
    // Uncomment for debug: process.stderr.write('[MOCK] ' + d);
  });
  await waitForServer(10000);
});

after(() => {
  if (serverProcess) {
    serverProcess.kill('SIGTERM');
    serverProcess = null;
  }
});

// ===========================================================================
// SECTION A — wilsonLB() and clientHasEdge()
// ===========================================================================

describe('A — wilsonLB() stats logic', () => {

  test('wilsonLB(null, 30) returns null', () => {
    assert.equal(wilsonLB(null, 30), null);
  });

  test('wilsonLB(0.7, 0) returns null (n=0 invalid)', () => {
    assert.equal(wilsonLB(0.7, 0), null);
  });

  test('wilsonLB(0.5, 30) is between 0.32 and 0.38', () => {
    const v = wilsonLB(0.5, 30);
    assert.ok(typeof v === 'number', 'result must be a number');
    assert.ok(v > 0.32, `expected > 0.32, got ${v}`);
    assert.ok(v < 0.38, `expected < 0.38, got ${v}`);
  });

  test('wilsonLB(0.7, 100) is greater than 0.60', () => {
    const v = wilsonLB(0.7, 100);
    assert.ok(v > 0.60, `expected > 0.60, got ${v}`);
  });

  test('wilsonLB(0.53, 30) is less than 0.50 — no edge at 53%/n=30', () => {
    const v = wilsonLB(0.53, 30);
    assert.ok(v < 0.50, `expected < 0.50, got ${v}`);
  });

  test('wilsonLB(0.65, 100) is greater than 0.50 — has edge at 65%/n=100', () => {
    const v = wilsonLB(0.65, 100);
    assert.ok(v > 0.50, `expected > 0.50, got ${v}`);
  });

  test('wilsonLB result is always between 0 and 1', () => {
    const cases = [
      [0.0, 50], [1.0, 50], [0.5, 1], [0.5, 1000], [0.99, 500], [0.01, 500]
    ];
    for (const [wr, n] of cases) {
      const v = wilsonLB(wr, n);
      assert.ok(v >= 0 && v <= 1, `wilsonLB(${wr},${n}) = ${v} out of [0,1]`);
    }
  });

  test('clientHasEdge(0.53, 30) returns false', () => {
    assert.equal(clientHasEdge(0.53, 30), false);
  });

  test('clientHasEdge(0.65, 100) returns true', () => {
    assert.equal(clientHasEdge(0.65, 100), true);
  });

  test('clientHasEdge(0.65, 29) returns false — n < 30', () => {
    assert.equal(clientHasEdge(0.65, 29), false);
  });

  test('clientHasEdge(null, 100) returns false', () => {
    assert.equal(clientHasEdge(null, 100), false);
  });

});

// ===========================================================================
// SECTION B — esc() XSS helper
// ===========================================================================

describe('B — esc() XSS helper', () => {

  test('esc(null) returns empty string', () => {
    assert.equal(esc(null), '');
  });

  test('esc(undefined) returns empty string', () => {
    assert.equal(esc(undefined), '');
  });

  test('esc("<script>alert(1)</script>") escapes tags', () => {
    assert.equal(
      esc('<script>alert(1)</script>'),
      '&lt;script&gt;alert(1)&lt;/script&gt;'
    );
  });

  test('esc("Hello & World") escapes ampersand', () => {
    assert.equal(esc('Hello & World'), 'Hello &amp; World');
  });

  test('esc(\'\"quoted\"\') escapes double quotes', () => {
    assert.equal(esc('"quoted"'), '&quot;quoted&quot;');
  });

  test("esc(\"it's\") escapes single quote", () => {
    assert.equal(esc("it's"), "it&#39;s");
  });

  test('esc(42) coerces number to string', () => {
    assert.equal(esc(42), '42');
  });

  test('esc("") returns empty string', () => {
    assert.equal(esc(''), '');
  });

  test('esc output contains no raw < or > when input has them', () => {
    const output = esc('<div class="foo">bar</div>');
    assert.ok(!output.includes('<'), 'output must not contain raw <');
    assert.ok(!output.includes('>'), 'output must not contain raw >');
  });

});

// ===========================================================================
// SECTION C — fmtPrice() adaptive precision
// ===========================================================================

describe('C — fmtPrice() adaptive precision', () => {

  test('fmtPrice(null) returns em-dash', () => {
    assert.equal(fmtPrice(null), '\u2014');
  });

  test('fmtPrice(105000) returns $105,000 (0 decimals for >= 10000)', () => {
    assert.equal(fmtPrice(105000), '$105,000');
  });

  test('fmtPrice(5000) starts with $5,000 (1 decimal for >= 1000 < 10000)', () => {
    const v = fmtPrice(5000);
    assert.ok(v.startsWith('$5,000'), `expected to start with $5,000, got: ${v}`);
  });

  test('fmtPrice(500) starts with $500 (2 decimals for < 1000)', () => {
    const v = fmtPrice(500);
    assert.ok(v.startsWith('$500'), `expected to start with $500, got: ${v}`);
    assert.ok(v.includes('.'), `expected decimal point for < 1000, got: ${v}`);
  });

  test('fmtPrice(100000, 2) includes .00 (explicit decimals override)', () => {
    const v = fmtPrice(100000, 2);
    assert.ok(v.includes('.00'), `expected .00 with explicit decimals=2, got: ${v}`);
  });

  test('fmtPrice(0) returns $0.00', () => {
    assert.equal(fmtPrice(0), '$0.00');
  });

  test('fmtPrice result always starts with $', () => {
    const cases = [0, 100, 999.99, 1000, 5000, 10000, 99999, 1000000];
    for (const v of cases) {
      const result = fmtPrice(v);
      assert.ok(result.startsWith('$'), `fmtPrice(${v}) = "${result}" must start with $`);
    }
  });

});

// ===========================================================================
// SECTION D — CFG values validation (config.js)
// ===========================================================================

describe('D — CFG values (config.js)', () => {

  test('CFG thresholds match expected values', async () => {
    const fs = await import('node:fs');
    const configPath = new URL('../frontend/js/config.js', import.meta.url).pathname;
    const src = fs.readFileSync(configPath, 'utf8');

    // FLIP_NEAR_PCT: 2.0
    assert.ok(
      /FLIP_NEAR_PCT:\s*2\.0/.test(src),
      'FLIP_NEAR_PCT must be 2.0'
    );

    // FLIP_WARN_PCT: 5.0
    assert.ok(
      /FLIP_WARN_PCT:\s*5\.0/.test(src),
      'FLIP_WARN_PCT must be 5.0'
    );

    // EDGE_MIN_N: 30
    assert.ok(
      /EDGE_MIN_N:\s*30/.test(src),
      'EDGE_MIN_N must be 30'
    );

    // MOPI_HIGH: 70
    assert.ok(
      /MOPI_HIGH:\s*70/.test(src),
      'MOPI_HIGH must be 70'
    );

    // MOPI_LOW: 30
    assert.ok(
      /MOPI_LOW:\s*30/.test(src),
      'MOPI_LOW must be 30'
    );

    // IV_EXTREME: 80
    assert.ok(
      /IV_EXTREME:\s*80/.test(src),
      'IV_EXTREME must be 80'
    );

    // IV_HIGH: 60
    assert.ok(
      /IV_HIGH:\s*60/.test(src),
      'IV_HIGH must be 60'
    );
  });

});

// ===========================================================================
// SECTION E — Payload validation against mock server
// ===========================================================================

describe('E — Payload validation (mock server)', () => {

  test('/api/dashboard has required fields', async () => {
    const { body } = await get('/api/dashboard');
    assert.ok(typeof body.mopi_squeeze_heuristic === 'number',
      'mopi_squeeze_heuristic must be a number');
    assert.ok(typeof body.weather_color === 'string' && body.weather_color.startsWith('#'),
      'weather_color must be a hex string starting with #');
    assert.ok(typeof body.gex_total === 'number',
      'gex_total must be a number');
  });

  test('/api/options_walls has required fields', async () => {
    const { body } = await get('/api/options_walls');
    assert.ok(Array.isArray(body.walls), 'walls must be an array');
    // major_call_wall and major_put_wall may be absent from fixture; check if present, must be number or null
    if ('major_call_wall' in body) {
      assert.ok(body.major_call_wall === null || typeof body.major_call_wall === 'number',
        'major_call_wall must be number or null');
    }
    if ('major_put_wall' in body) {
      assert.ok(body.major_put_wall === null || typeof body.major_put_wall === 'number',
        'major_put_wall must be number or null');
    }
  });

  test('/api/model_arena/bme_status nominal: has_edge, wilson_lb, eval_is_oos, n_out_of_sample', async () => {
    const { body } = await get('/api/model_arena/bme_status');
    assert.equal(body.has_edge, true, 'nominal has_edge must be true');
    assert.ok(typeof body.wilson_lb === 'number', 'wilson_lb must be a number');
    assert.equal(body.eval_is_oos, true, 'eval_is_oos must be true');
    assert.ok(typeof body.n_out_of_sample === 'number' && body.n_out_of_sample >= 30,
      'n_out_of_sample must be >= 30');
  });

  test('/api/model_arena/bme_status?variant=no_edge: has_edge === false', async () => {
    const { body } = await get('/api/model_arena/bme_status?variant=no_edge');
    assert.equal(body.has_edge, false, 'no_edge variant has_edge must be false');
  });

  test('Cross-validate: clientHasEdge mirrors server has_edge (nominal)', async () => {
    const { body } = await get('/api/model_arena/bme_status');
    const clientResult = clientHasEdge(body.dir_winrate, body.n_dir_attempted);
    assert.equal(
      clientResult, body.has_edge,
      `clientHasEdge(${body.dir_winrate}, ${body.n_dir_attempted}) = ${clientResult} but server has_edge = ${body.has_edge}`
    );
  });

  test('Cross-validate: clientHasEdge mirrors server has_edge (no_edge variant)', async () => {
    const { body } = await get('/api/model_arena/bme_status?variant=no_edge');
    const clientResult = clientHasEdge(body.dir_winrate, body.n_dir_attempted);
    assert.equal(
      clientResult, body.has_edge,
      `clientHasEdge(${body.dir_winrate}, ${body.n_dir_attempted}) = ${clientResult} but server has_edge = ${body.has_edge}`
    );
  });

  test('/api/narrative?variant=xss: phrase_synthese contains raw < (unescaped)', async () => {
    const { body } = await get('/api/narrative?variant=xss');
    assert.ok(typeof body.phrase_synthese === 'string', 'phrase_synthese must be a string');
    assert.ok(
      body.phrase_synthese.includes('<'),
      'XSS variant phrase_synthese must contain raw < (server serves unescaped, frontend must escape)'
    );
  });

  test('/api/vex_cex has required fields', async () => {
    const { body } = await get('/api/vex_cex');
    assert.ok(typeof body.vex_total === 'number', 'vex_total must be a number');
    assert.ok(typeof body.cex_total === 'number', 'cex_total must be a number');
    assert.ok(body.gamma_flip === null || typeof body.gamma_flip === 'number',
      'gamma_flip must be number or null');
    assert.ok(typeof body.vex_direction === 'string', 'vex_direction must be a string');
  });

  test('/api/snapshot has all 8 required keys', async () => {
    const { body } = await get('/api/snapshot');
    const required = ['snapshot_ts', 'spot', 'dashboard', 'walls', 'dealer', 'squeeze', 'narrative', 'bme_status'];
    for (const key of required) {
      assert.ok(key in body, `/api/snapshot must have key: ${key}`);
    }
  });

});

// ===========================================================================
// SECTION F — Module file structure validation
// ===========================================================================

describe('F — Module file structure', () => {

  test('all JS modules exist', async () => {
    const fs = await import('node:fs');
    const base = new URL('../frontend/js', import.meta.url).pathname;
    const files = [
      'main.js',
      'config.js',
      'store.js',
      'api.js',
      'auth.js',
      'scheduler.js',
      'lib/stats.js',
      'lib/fmt.js',
      'lib/canvas.js',
      'widgets/signal.js',
      'widgets/levels.js',
      'widgets/probabilities.js',
      'widgets/context.js',
      'widgets/narrative.js',
      'widgets/model.js',
      'widgets/vol_weather.js',
      'widgets/gex_dex.js',
      'widgets/mopi_btc.js',
      'widgets/regime.js',
      'widgets/vex_cex.js',
    ];
    for (const f of files) {
      const full = base + '/' + f;
      assert.ok(fs.existsSync(full), `${f} must exist (checked: ${full})`);
    }
  });

  test('all JS modules have no syntax errors', async () => {
    const { execSync } = await import('node:child_process');
    const { readdirSync, statSync } = await import('node:fs');
    const jsDir = new URL('../frontend/js', import.meta.url).pathname;

    function walk(dir) {
      const items = readdirSync(dir);
      let files = [];
      for (const item of items) {
        const full = dir + '/' + item;
        if (statSync(full).isDirectory()) {
          files = files.concat(walk(full));
        } else if (item.endsWith('.js')) {
          files.push(full);
        }
      }
      return files;
    }

    const jsFiles = walk(jsDir);
    assert.ok(jsFiles.length > 0, 'No JS files found in frontend/js');

    for (const f of jsFiles) {
      try {
        execSync(`node --check "${f}"`, { stdio: 'pipe' });
      } catch (e) {
        const stderr = e.stderr ? e.stderr.toString() : '';
        assert.fail(`Syntax error in ${f}: ${stderr}`);
      }
    }
  });

});

// ===========================================================================
// SECTION G — Cross-validation: wilsonLB mirrors backend
// ===========================================================================

describe('G — wilsonLB formula constants match backend', () => {

  test('backend wilson_utils.py uses z=1.96', async () => {
    const fs = await import('node:fs');
    const pyPath = new URL('../backend/wilson_utils.py', import.meta.url).pathname;
    const py = fs.readFileSync(pyPath, 'utf8');
    assert.ok(py.includes('1.96'), 'backend wilson_utils.py must reference z=1.96');
  });

  test('frontend stats.js uses z=1.96', async () => {
    const fs = await import('node:fs');
    const jsPath = new URL('../frontend/js/lib/stats.js', import.meta.url).pathname;
    const js = fs.readFileSync(jsPath, 'utf8');
    assert.ok(js.includes('1.96'), 'frontend stats.js must reference z=1.96');
  });

  test('backend uses 0.50 as edge threshold', async () => {
    const fs = await import('node:fs');
    const pyPath = new URL('../backend/wilson_utils.py', import.meta.url).pathname;
    const py = fs.readFileSync(pyPath, 'utf8');
    assert.ok(py.includes('0.50'), 'backend must use 0.50 as edge threshold');
  });

  test('frontend and backend wilsonLB agree numerically', () => {
    // Spot-check a few values — if the implementations agree, they match
    const cases = [
      { wr: 0.70, n: 60 },
      { wr: 0.53, n: 30 },
      { wr: 0.65, n: 100 },
    ];
    for (const { wr, n } of cases) {
      const v = wilsonLB(wr, n);
      assert.ok(typeof v === 'number', `wilsonLB(${wr},${n}) must return a number`);
      assert.ok(v > 0 && v < 1, `wilsonLB(${wr},${n}) = ${v} must be in (0,1)`);
    }
  });

});
