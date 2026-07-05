// js/widgets/probabilities.js — Module 3: Probabilités (Phase 5)
import { apiFetch } from '../api.js';
import { fmtPct } from '../lib/fmt.js';

export async function loadProbabilities(signal) {
  const el = document.getElementById('m3-content');
  try {
    const data = await apiFetch('/api/probability_engine', signal);

    if (!data) {
      el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>`;
      return;
    }

    const scenarios = [
      { label: 'Hausse 24h', key: 'bull_24h', type: 'bull' },
      { label: 'Baisse 24h', key: 'bear_24h', type: 'bear' },
      { label: 'Hausse 72h', key: 'bull_72h', type: 'bull' },
      { label: 'Baisse 72h', key: 'bear_72h', type: 'bear' },
    ];

    const pctColor = (type) => type === 'bull' ? 'var(--green)' : 'var(--red)';

    let html = '<div class="prob-grid">';
    scenarios.forEach((s, i) => {
      const val = data[s.key]?.probability ?? data[s.key] ?? 0;
      html += `
        <div class="prob-item">
          <div class="prob-header">
            <span class="prob-label">${s.label}</span>
            <span class="prob-pct" style="color:${pctColor(s.type)}">${fmtPct(val)}</span>
          </div>
          <div class="prob-bar-wrap">
            <div class="prob-bar ${s.type}" id="pb-${i}" style="width:0%"></div>
          </div>
        </div>
      `;
    });
    html += '</div>';
    el.innerHTML = html;

    setTimeout(() => {
      scenarios.forEach((s, i) => {
        const val = data[s.key]?.probability ?? data[s.key] ?? 0;
        const bar = document.getElementById('pb-' + i);
        if (bar) bar.style.width = Math.min(val, 100) + '%';
      });
    }, 150);
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
