// js/widgets/mopi_btc.js — Module 9: MOPI vs BTC (Phase 5)
import { apiFetch } from '../api.js';
import { fmtPrice } from '../lib/fmt.js';
import { drawDualAxis } from '../lib/canvas.js';
import { CFG } from '../config.js';
import { currentPeriod } from '../store.js';

export async function loadMopiVsBtc(signal) {
  const el = document.getElementById('m9-content');
  try {
    const { currentPeriod: period } = await import('../store.js');
    const data = await apiFetch('/api/mopi_vs_btc?period=' + period, signal);
    if (!data?.mopi?.length) { el.innerHTML = '<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>'; return; }

    const mopi = data.mopi;
    const btc = data.btc_price;
    const ts = data.timestamps;
    const lastMopi = mopi[mopi.length - 1];
    const corr = data.correlations?.corr_now?.toFixed(2) ?? '\u2014';
    const mopiColor = lastMopi > CFG.MOPI_HIGH ? 'var(--green)' : lastMopi < CFG.MOPI_LOW ? 'var(--red)' : 'var(--yellow)';
    // F9.4 — labels corrects : pas de 'Suracheté' si signal est Long
    const mopiLabel = lastMopi > CFG.MOPI_HIGH ? 'Signal Long — MOPI haussier' : lastMopi < CFG.MOPI_LOW ? 'Signal Short — MOPI baissier' : 'Zone neutre';

    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <span style="font-size:11px;color:var(--muted)">7 jours \u00b7 r\u00e9solution 1h</span>
        <span style="font-size:13px;font-weight:800;color:${mopiColor}">MOPI ${lastMopi?.toFixed(1)} \u2014 ${mopiLabel}</span>
      </div>
      <div class="mopi-chart-wrap"><canvas id="canvas-mopi-btc"></canvas></div>
      <div class="mopi-legend">
        <div class="mopi-legend-item"><div class="mopi-legend-dot" style="background:#00d47e"></div>MOPI (axe 0\u2013100)</div>
        <div class="mopi-legend-item"><div class="mopi-legend-dot" style="background:#3d8eff"></div>BTC Price</div>
      </div>
      <div class="mopi-stats-row">
        <div class="mopi-stat-box">
          <div class="mopi-stat-lbl">MOPI actuel</div>
          <div class="mopi-stat-val" style="color:${mopiColor}">${lastMopi?.toFixed(1)}</div>
        </div>
        <div class="mopi-stat-box">
          <div class="mopi-stat-lbl">Corr\u00e9lation BTC</div>
          <div class="mopi-stat-val">${corr}</div>
        </div>
        <div class="mopi-stat-box">
          <div class="mopi-stat-lbl">Signaux &gt;70</div>
          <div class="mopi-stat-val" style="color:var(--green)">${(() => {
            const xs = data.crossovers_above_70;
            if (!xs || !xs.length) return '\u2014';
            const count = xs.length;
            const last = xs[xs.length - 1];
            const d = new Date(last * 1000);
            const fmt = d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
            return count + ' <span style="font-size:9px;font-weight:400;color:var(--muted)">dernier : ' + fmt + '</span>';
          })()}</div>
        </div>
      </div>
    `;

    requestAnimationFrame(() => {
      drawDualAxis('canvas-mopi-btc', ts, mopi, btc);
    });
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
