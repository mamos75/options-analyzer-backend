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
      <div id="mopi-track-record" style="margin-top:10px;padding:8px 10px;background:var(--card2);border-radius:8px;border:1px solid var(--border);font-size:11px;color:var(--txt3)">
        <span style="color:var(--muted)">Track record MOPI chargement...</span>
      </div>
    `;

    requestAnimationFrame(() => {
      drawDualAxis('canvas-mopi-btc', ts, mopi, btc);
    });

    // F12.4 — Load MOPI track record async (non-blocking)
    _loadMopiTrackRecord();
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}

// F12.4 — Track record panel : résultats de validation MOPI
async function _loadMopiTrackRecord() {
  const el = document.getElementById('mopi-track-record');
  if (!el) return;
  try {
    const resp = await fetch('/api/mopi_validation');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const v = await resp.json();
    const r = v.results || {};
    const verdict = v.verdict || {};
    const status = verdict.signal_status || '';
    const n = v.n_snapshots_total || 0;

    const statusColor = status === 'validé_24h' ? 'var(--green)' :
                        status === 'partiel_24h' ? 'var(--yellow)' : '#ef4444';
    const statusLabel = status === 'validé_24h' ? 'Validé horizon 24h' :
                        status === 'partiel_24h' ? 'Partiel 24h' :
                        status === 'recalibrer' ? 'À recalibrer' : 'Pas d\'edge';

    function fmtBucket(b, dirLabel) {
      if (!b || b.n === 0) return 'n=0';
      const lb = b.wilson_lb !== null ? (b.wilson_lb * 100).toFixed(0) + '%' : '-';
      const wr = b.wr !== null ? (b.wr * 100).toFixed(0) + '%' : '-';
      const edge = b.has_edge ? ' ✓' : '';
      return `${dirLabel} WR ${wr} (LB ${lb}, n=${b.n})${edge}`;
    }

    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-weight:700;color:var(--txt2)">Track record MOPI</span>
        <span style="background:${statusColor}22;color:${statusColor};padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600">${statusLabel}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px">
        <div style="color:var(--muted)">Signal >70 @4h</div><div>${fmtBucket(r.high_4h, 'UP')}</div>
        <div style="color:var(--muted)">Signal >70 @24h</div><div style="color:${r.high_24h?.has_edge ? 'var(--green)' : 'var(--txt3)'}">${fmtBucket(r.high_24h, 'UP')}</div>
        <div style="color:var(--muted)">Signal <30 @4h</div><div>${fmtBucket(r.low_4h, 'DOWN')}</div>
        <div style="color:var(--muted)">Signal <30 @24h</div><div style="color:${r.low_24h?.has_edge ? 'var(--green)' : 'var(--txt3)'}">${fmtBucket(r.low_24h, 'DOWN')}</div>
      </div>
      <div style="margin-top:6px;font-size:10px;color:var(--muted)">${n} snapshots · Wilson LB 95% IC unilat. · seuils ${v.thresholds?.high}/${v.thresholds?.low}</div>
    `;
  } catch(e) {
    if (el) el.innerHTML = '<span style="color:var(--muted);font-size:10px">Track record indisponible</span>';
  }
}
