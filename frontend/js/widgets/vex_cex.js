// js/widgets/vex_cex.js
import { apiFetch } from '../api.js';
import { esc } from '../lib/fmt.js';
import { drawVcLine } from '../lib/canvas.js';
import { lastRegime, vcPeriod, setVcPeriodState } from '../store.js';
import { classifyRegime } from './regime.js';

export async function loadVexCex(signal) {
  const data = await apiFetch('/api/vex_cex', signal);
  const el = document.getElementById('vex-cex-content');
  if (!data || data.error) {
    el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Données VEX/CEX indisponibles</div>`;
    return;
  }

  const vexColor = data.vex_total >= 0 ? 'var(--green)' : 'var(--red)';
  const cexColor = data.cex_total >= 0 ? 'var(--green)' : 'var(--red)';
  const flipColor = data.gamma_flip_side === 'below' ? 'var(--red)' : 'var(--green)';
  const flipDist = data.gamma_flip_dist_pct !== null
    ? (data.gamma_flip_dist_pct > 0 ? '+' : '') + data.gamma_flip_dist_pct.toFixed(1) + '%'
    : '—';

  const _fmtVex = v => {
    const a = Math.abs(v), s = v >= 0 ? '+' : '-';
    return a >= 1e9 ? s+(a/1e9).toFixed(2)+'B' : a >= 1e6 ? s+(a/1e6).toFixed(1)+'M' : s+Math.round(a).toLocaleString();
  };
  // CEX is in BTC/day — not dollars, no /1e6
  const _fmtCex = v => {
    const a = Math.abs(v), s = v >= 0 ? '+' : '-';
    return a >= 1000 ? s+(a/1000).toFixed(1)+'K Δ/j' : s+a.toFixed(1)+' Δ/j';
  };

  const vexStrikes = (data.vex_by_strike || []).map(s =>
    `<span class="vex-cex-chip" title="VEX: ${_fmtVex(s.vex)}">$${Math.round(s.strike).toLocaleString()}</span>`
  ).join('');
  const cexStrikes = (data.cex_by_strike || []).map(s =>
    `<span class="vex-cex-chip" title="CEX: ${_fmtCex(s.cex)}">$${Math.round(s.strike).toLocaleString()}</span>`
  ).join('');

  // Get GEX/DEX from the already-loaded mopi data if available, else from context
  // We use stored values from MODULE 8/9 or fallback to 0 for regime (we classify from vex/cex primarily)
  // For a better regime we'll use gex from the context module which is already in the DOM
  const gexEl = document.getElementById('ctx-gex-val');
  const dexEl = document.getElementById('ctx-dex-val');
  const gexRaw = gexEl ? parseFloat(gexEl.dataset.raw || '0') : 0;
  const dexRaw = dexEl ? parseFloat(dexEl.dataset.raw || '0') : 0;

  // Reuse regime computed by loadRegimeSummary() if available (avoids double fetch)
  const regime = lastRegime || classifyRegime(data.vex_total, data.cex_total, gexRaw, dexRaw, data.gamma_flip_dist_pct);

  const signalRows = [
    { name: 'VEX', val: esc(data.vex_total_fmt), bull: data.vex_total >= 0 },
    { name: 'CEX', val: esc(data.cex_total_fmt), bull: data.cex_total >= 0 },
    { name: 'Gamma Flip', val: data.gamma_flip ? `$${Math.round(data.gamma_flip).toLocaleString()}` : '—', bull: data.gamma_flip_side !== 'below' },
    { name: 'Régime GEX', val: esc(data.gamma_flip_regime || '—'), bull: data.gamma_flip_regime !== 'AMPLIFICATEUR' },
  ].map(s => `
    <div class="regime-signal-item">
      <span class="regime-signal-name">${s.name}</span>
      <span class="regime-signal-val" style="color:${s.bull ? 'var(--green)' : 'var(--red)'}">${s.val}</span>
    </div>`).join('');

  el.innerHTML = `
    <div class="vex-cex-grid">
      <div class="vex-cex-box">
        <div class="vex-cex-box-label">VEX — Vanna Exposure</div>
        <div class="vex-cex-value" style="color:${vexColor}">${esc(data.vex_total_fmt)}</div>
        <div class="vex-cex-direction" style="color:${vexColor}">${esc(data.vex_direction?.replace(/_/g, ' ') || '—')}</div>
        <div class="vex-cex-interp">${esc(data.vex_interpretation || '')}</div>
        <div class="vex-cex-strikes">${vexStrikes}</div>
      </div>
      <div class="vex-cex-box">
        <div class="vex-cex-box-label">CEX — Charm Exposure</div>
        <div class="vex-cex-value" style="color:${cexColor}">${esc(data.cex_total_fmt)}</div>
        <div class="vex-cex-direction" style="color:${cexColor}">${esc(data.cex_direction?.replace(/_/g, ' ') || '—')}</div>
        <div class="vex-cex-interp">${esc(data.cex_interpretation || '')}</div>
        <div class="vex-cex-strikes">${cexStrikes}</div>
      </div>
    </div>

    <div class="gamma-flip-box">
      <div class="gamma-flip-label">Gamma Flip</div>
      ${data.gamma_flip ? `
        <div class="gamma-flip-price" style="color:${flipColor}">
          $${Math.round(data.gamma_flip).toLocaleString()}
          <span style="font-size:13px;font-weight:600;margin-left:8px;color:${flipColor}">${flipDist} du spot</span>
        </div>
        <div class="gamma-flip-dist">
          Régime GEX : <span style="color:${flipColor};font-weight:700">${esc(data.gamma_flip_regime || '—')}</span>
          &nbsp;·&nbsp; <span style="font-weight:600">${data.gamma_flip_side === 'below' ? '▼ En-dessous du spot' : '▲ Au-dessus du spot'}</span>
        </div>
        <div class="gamma-flip-interp">${esc(data.gamma_flip_interpretation || '')}</div>
      ` : `<div style="color:var(--muted);font-size:13px;">Non détecté dans la zone actuelle</div>`}
    </div>

    <div class="vc-history-wrap">
      <div class="vc-period-row">
        <button class="vc-period-btn active" onclick="setVcPeriod('7d',this)">7J</button>
        <button class="vc-period-btn" onclick="setVcPeriod('14d',this)">14J</button>
        <button class="vc-period-btn" onclick="setVcPeriod('30d',this)">30J</button>
      </div>
      <div class="vc-chart-block">
        <div class="vc-chart-header">
          <span class="vc-chart-label">VEX — Historique</span>
          <span class="vc-chart-current" id="vc-vex-cur" style="color:${vexColor}">${esc(data.vex_total_fmt)}</span>
        </div>
        <div class="vc-canvas-wrap"><canvas id="vc-vex-canvas"></canvas></div>
      </div>
      <div class="vc-chart-block">
        <div class="vc-chart-header">
          <span class="vc-chart-label">CEX — Historique</span>
          <span class="vc-chart-current" id="vc-cex-cur" style="color:${cexColor}">${esc(data.cex_total_fmt)}</span>
        </div>
        <div class="vc-canvas-wrap"><canvas id="vc-cex-canvas"></canvas></div>
      </div>
    </div>

    <div class="regime-block">
      <div class="regime-header">
        <div class="regime-title">Diagnostic de régime</div>
        <div class="regime-badge" style="color:${regime.color};border-color:${regime.color}33">
          <span class="regime-dot" style="background:${regime.dot}"></span>
          ${regime.label}
        </div>
      </div>
      <div class="regime-plain">${regime.plain}</div>
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px;">Signaux composites</div>
      <div class="regime-signals">${signalRows}</div>
      ${regime.advice ? `<div style="margin-top:12px;padding:10px 14px;background:rgba(255,255,255,0.03);border-radius:10px;border-left:3px solid ${regime.color}40">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:${regime.color};margin-bottom:5px;">Implication pratique</div>
        <div style="font-size:12px;color:#b0bec5;line-height:1.6;">${regime.advice}</div>
      </div>` : ''}
    </div>
  `;
}
export function setVcPeriod(p, btn) {
  setVcPeriodState(p);
  document.querySelectorAll('.vc-period-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  drawVcCharts(p);
}

export async function drawVcCharts(period, signal) {
  const data = await apiFetch('/api/vex_cex_history?period=' + period, signal);
  const pts = data?.points || [];
  // V4: convention v2 (short-all) launched 2026-07-06 — fewer than 10 points = recalibration
  const recalibrating = pts.length > 0 && pts.length < 10;
  if (!data || pts.length < 2) {
    const msg = recalibrating
      ? 'Trends en recalibration — convention v2 (short-all, 2026-07-06)…'
      : 'Historique en cours de collecte…';
    ['vc-vex-canvas', 'vc-cex-canvas'].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      const dpr = window.devicePixelRatio || 1;
      const rect = el.parentElement.getBoundingClientRect();
      el.width = (rect.width || 300) * dpr;
      el.height = (rect.height || 72) * dpr;
      el.style.width = (rect.width || 300) + 'px';
      el.style.height = (rect.height || 72) + 'px';
      const ctx = el.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.fillStyle = 'rgba(148,163,184,0.4)';
      ctx.font = '11px system-ui';
      ctx.textAlign = 'center';
      ctx.fillText(msg, (rect.width || 300) / 2, (rect.height || 72) / 2 + 4);
    });
    return;
  }

  const vexPts = data.points.map(p => ({ ts: p.ts, v: p.vex }));
  const cexPts = data.points.map(p => ({ ts: p.ts, v: p.cex }));

  const lastVex = vexPts[vexPts.length - 1]?.v || 0;
  const lastCex = cexPts[cexPts.length - 1]?.v || 0;
  const vexColor = lastVex >= 0 ? '#22c55e' : '#ef4444';
  const cexColor = lastCex >= 0 ? '#22c55e' : '#ef4444';

  const fmt = v => {
    const a = Math.abs(v);
    return (v < 0 ? '-' : '+') + (a >= 1e9 ? (a/1e9).toFixed(2)+'B' : a >= 1e6 ? (a/1e6).toFixed(1)+'M' : Math.round(a).toLocaleString());
  };

  const vexCurEl = document.getElementById('vc-vex-cur');
  const cexCurEl = document.getElementById('vc-cex-cur');
  if (vexCurEl) { vexCurEl.textContent = fmt(lastVex); vexCurEl.style.color = vexColor; }
  if (cexCurEl) { cexCurEl.textContent = fmt(lastCex); cexCurEl.style.color = cexColor; }

  requestAnimationFrame(() => {
    drawVcLine('vc-vex-canvas', vexPts, vexColor);
    drawVcLine('vc-cex-canvas', cexPts, cexColor);
  });
}