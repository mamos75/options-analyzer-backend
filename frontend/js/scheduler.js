// js/scheduler.js -- Data loading and refresh loop (Phase 5)
import { REFRESH_INTERVAL } from './config.js';
import {
  countdownInterval, setCountdownInterval,
  secondsLeft, setSecondsLeft,
  nextLoadSeq, getLoadSeq,
  loadController, setLoadController,
  vcPeriod
} from './store.js';
import { fmtPrice } from './lib/fmt.js';
import { loadLevels } from './widgets/levels.js';
import { loadProbabilities } from './widgets/probabilities.js';
import { loadContext } from './widgets/context.js';
import { loadNarrative } from './widgets/narrative.js';
import { loadModel } from './widgets/model.js';
import { loadVolWeather } from './widgets/vol_weather.js';
import { loadGexDex } from './widgets/gex_dex.js';
import { loadMopiVsBtc } from './widgets/mopi_btc.js';
import { loadRegimeSummary } from './widgets/regime.js';
import { loadArbiterQuick } from './widgets/arbiter_quick.js';
import { loadVexCex, drawVcCharts } from './widgets/vex_cex.js';

let _nextRefreshAt = 0;

export async function loadBtcPrice() {
  try {
    const resp = await fetch('https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT');
    const data = await resp.json();
    const price = parseFloat(data.price);
    const el = document.getElementById('btc-price');
    el.textContent = fmtPrice(price);
    const storeModule = await import('./store.js');
    if (storeModule.lastBtcPrice !== null) {
      el.className = 'header-price ' + (price > storeModule.lastBtcPrice ? 'up' : price < storeModule.lastBtcPrice ? 'down' : 'neutral');
    }
    storeModule.setLastBtcPrice(price);
  } catch(e) {
    const el = document.getElementById('btc-price');
    if (el) el.textContent = '--';
  }
}

export async function loadAllData() {
  // Cancel any in-flight request set
  if (loadController) loadController.abort();
  const seq = nextLoadSeq();
  const newController = new AbortController();
  setLoadController(newController);
  const { signal } = newController;

  await loadBtcPrice();
  if (seq !== getLoadSeq()) return;

  // Load F5 quick block first (top of page)
  await loadArbiterQuick(signal);
  if (seq !== getLoadSeq()) return;

  // Load regime summary
  await loadRegimeSummary(signal);
  if (seq !== getLoadSeq()) return;

  // Load rest in parallel
  const results = await Promise.allSettled([
    loadLevels(signal),
    loadProbabilities(signal),
    loadContext(signal),
    loadVolWeather(signal),
    loadGexDex(signal),
    loadMopiVsBtc(signal),
    loadNarrative(signal),
    loadModel(signal),
    loadVexCex(signal),
  ]);

  if (seq !== getLoadSeq()) return;

  // Draw VEX/CEX history charts after card content is rendered
  const storeModule = await import('./store.js');
  drawVcCharts(storeModule.vcPeriod);

  // Show stale indicator for any failed modules
  const moduleIds = ['m2-content','m3-content','m4-content','m7-content','m8-content','m9-content','m5-content','m6-content','vex-cex-content'];
  results.forEach((r, i) => {
    const el = document.getElementById(moduleIds[i]);
    if (!el) return;
    if (r.status === 'rejected') {
      el.dataset.stale = '1';
    } else {
      delete el.dataset.stale;
    }
  });
}

export function startRefreshLoop() {
  _nextRefreshAt = Date.now() + REFRESH_INTERVAL * 1000;

  if (countdownInterval) clearInterval(countdownInterval);
  setCountdownInterval(setInterval(() => {
    const remaining = Math.max(0, Math.ceil((_nextRefreshAt - Date.now()) / 1000));
    setSecondsLeft(remaining);
    updateCountdown();
    if (Date.now() >= _nextRefreshAt) {
      _nextRefreshAt = Date.now() + REFRESH_INTERVAL * 1000;
      loadAllData();
    }
  }, 1000));

  // Refresh immediately when tab becomes visible again after being hidden
  document.removeEventListener('visibilitychange', _onVisibilityChange);
  document.addEventListener('visibilitychange', _onVisibilityChange);
}

export function _onVisibilityChange() {
  if (document.visibilityState === 'visible' && Date.now() >= _nextRefreshAt) {
    _nextRefreshAt = Date.now() + REFRESH_INTERVAL * 1000;
    loadAllData();
  }
}

export function updateCountdown() {
  const el = document.getElementById('refresh-countdown');
  if (el) el.textContent = 'Actualisation dans ' + secondsLeft + 's';
}
