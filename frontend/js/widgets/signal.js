// js/widgets/signal.js — Module 1: Signal Principal (Phase 5)
import { apiFetch } from '../api.js';
import { esc, fmtPct } from '../lib/fmt.js';

export async function loadSignal(signal) {
  const el = document.getElementById('m1-content');
  try {
    const data = await apiFetch('/api/market_decision', signal);
    if (!data) {
      el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>`;
      return;
    }

    const dir = (data.directional?.direction || data.direction || 'NEUTRE').toUpperCase();
    const conf = data.directional?.confidence || data.confidence || 0;
    const watch = data.watch_message || data.message || '';
    const warnings = data.warnings || [];

    let dirClass = 'neutre', dirLabel = 'NEUTRE';
    if (dir.includes('BULL') || dir.includes('HAUSS')) { dirClass = 'haussier'; dirLabel = 'HAUSSIER'; }
    else if (dir.includes('BEAR') || dir.includes('BAISS')) { dirClass = 'baissier'; dirLabel = 'BAISSIER'; }
    else if (dir === 'NEUTRE' || dir === 'NEUTRAL') { dirClass = 'neutre'; dirLabel = 'NEUTRE'; }

    const convColor = dirClass === 'haussier' ? 'var(--green)' : dirClass === 'baissier' ? 'var(--red)' : 'var(--yellow)';

    let warningHTML = '';
    if (warnings.length > 0) {
      warningHTML = '<div class="warnings-row">' +
        warnings.map(w => `<span class="warning-pill">${esc(w)}</span>`).join('') +
        '</div>';
    }

    el.innerHTML = `
      <div class="signal-hero">
        <div class="signal-direction ${dirClass}">${dirLabel}</div>
        <div class="signal-conviction">
          <div class="conviction-pct" style="color:${convColor}">${fmtPct(conf)}</div>
          <div class="conviction-label">conviction</div>
          <div class="conviction-bar-wrap">
            <div class="conviction-bar" id="conv-bar" style="background:${convColor};width:0%"></div>
          </div>
        </div>
        ${watch ? `<div class="signal-watch">${esc(watch)}</div>` : ''}
        ${warningHTML}
      </div>
    `;

    setTimeout(() => {
      const bar = document.getElementById('conv-bar');
      if (bar) bar.style.width = Math.min(conf, 100) + '%';
    }, 100);

    return data;
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
