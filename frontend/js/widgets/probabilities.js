// js/widgets/probabilities.js — Module 3: Probabilités (Phase 5, fix V1)
import { apiFetch } from '../api.js';
import { esc, fmtPct } from '../lib/fmt.js';

export async function loadProbabilities(signal) {
  const el = document.getElementById('m3-content');
  try {
    const data = await apiFetch('/api/probability_engine', signal);

    if (!data) {
      el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Données indisponibles</div>`;
      return;
    }

    const _p = (key) => {
      const v = data[key];
      if (v == null) return null;
      const n = typeof v === 'object' ? v.probability : v;
      const parsed = Number(n);
      return Number.isFinite(parsed) ? parsed : null;
    };

    const horizons = [
      { label: '24h', bull: _p('bull_24h'), bear: _p('bear_24h') },
      { label: '72h', bull: _p('bull_72h'), bear: _p('bear_72h') },
    ];

    let html = '<div class="prob-horizons">';

    horizons.forEach((h, hi) => {
      const bull = h.bull ?? 0;
      const bear = h.bear ?? 0;
      const dominant = bull >= bear ? 'bull' : 'bear';
      const domPct  = dominant === 'bull' ? bull : bear;
      const domLabel = dominant === 'bull' ? 'Haussier' : 'Baissier';
      const domColor = dominant === 'bull' ? 'var(--green)' : 'var(--red)';
      const secPct  = dominant === 'bull' ? bear : bull;
      const secLabel = dominant === 'bull' ? 'Baissier' : 'Haussier';
      const secColor = dominant === 'bull' ? 'var(--red)' : 'var(--green)';

      // Cap bar widths independently — never sum to >100 visually
      const domWidth = Math.min(domPct, 100);
      const secWidth = Math.min(secPct, 100);

      html += `
        <div class="prob-horizon">
          <div class="prob-horizon-label">${esc(h.label)}</div>
          <div class="prob-scenario dominant">
            <div class="prob-header">
              <span class="prob-label">${domLabel}</span>
              <span class="prob-pct" style="color:${domColor}">${fmtPct(domPct)}</span>
            </div>
            <div class="prob-bar-wrap">
              <div class="prob-bar ${dominant}" id="pb-${hi}-dom" style="width:0%"></div>
            </div>
          </div>
          <div class="prob-scenario secondary">
            <div class="prob-header">
              <span class="prob-label" style="color:var(--muted)">${secLabel}</span>
              <span class="prob-pct" style="color:${secColor};opacity:.7">${fmtPct(secPct)}</span>
            </div>
            <div class="prob-bar-wrap">
              <div class="prob-bar ${dominant === 'bull' ? 'bear' : 'bull'}" id="pb-${hi}-sec" style="width:0%;opacity:.5"></div>
            </div>
          </div>
        </div>
      `;
    });

    // Optional: conclusion line from backend
    const conclusion = data.conclusion_line || data.signal_label || '';
    if (conclusion) {
      html += `<div class="prob-conclusion">${esc(conclusion)}</div>`;
    }

    html += '</div>';
    el.innerHTML = html;

    setTimeout(() => {
      horizons.forEach((h, hi) => {
        const bull = h.bull ?? 0;
        const bear = h.bear ?? 0;
        const dominant = bull >= bear ? 'bull' : 'bear';
        const domPct = dominant === 'bull' ? bull : bear;
        const secPct = dominant === 'bull' ? bear : bull;
        const domBar = document.getElementById(`pb-${hi}-dom`);
        const secBar = document.getElementById(`pb-${hi}-sec`);
        if (domBar) domBar.style.width = Math.min(domPct, 100) + '%';
        if (secBar) secBar.style.width = Math.min(secPct, 100) + '%';
      });
    }, 150);
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Erreur inattendue</div>`;
  }
}
