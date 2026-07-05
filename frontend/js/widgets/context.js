// js/widgets/context.js — Module 4: Contexte (Phase 5)
import { apiFetch } from '../api.js';
import { esc, fmtPct } from '../lib/fmt.js';
import { CFG } from '../config.js';

export async function loadContext(signal) {
  const el = document.getElementById('m4-content');
  try {
    const [dashData, dealerData] = await Promise.all([
      apiFetch('/api/dashboard', signal),
      apiFetch('/api/dealer_pressure', signal)
    ]);

    const dash = dashData || {};
    const dealer = dealerData || {};

    const volState = dash.weather_state || dash.volatility_state || '\u2014';
    const dealerDir = dealer.direction || '\u2014';
    const mopi = dash.mopi_score ?? dealer.mopi_score ?? null;
    const gex = dash.gex_regime || dealer.gex_regime || '\u2014';

    const volColors = {
      'CALME': 'var(--green)',
      'TRANSITION': 'var(--yellow)',
      'EXPLOSIVE': 'var(--red)',
      'CHAOS': 'var(--red)',
    };
    const volColor = volColors[volState] || 'var(--text)';

    let dealerLabel = dealerDir;
    let dealerColor = 'var(--text)';
    if (dealerDir.includes('BEARISH') || dealerDir.includes('BEAR')) {
      dealerLabel = 'R\u00e9sistance'; dealerColor = 'var(--red)';
    } else if (dealerDir.includes('BULLISH') || dealerDir.includes('BULL')) {
      dealerLabel = 'Support'; dealerColor = 'var(--green)';
    }

    let mopiColor = 'var(--text)';
    if (mopi !== null) {
      if (mopi >= CFG.MOPI_COLOR_HIGH) mopiColor = 'var(--green)';
      else if (mopi <= CFG.MOPI_COLOR_LOW) mopiColor = 'var(--red)';
      else mopiColor = 'var(--yellow)';
    }

    const gexColors = { 'AMPLIFICATEUR': 'var(--red)', 'ABSORBEUR': 'var(--green)' };
    const gexColor = gexColors[gex] || 'var(--text)';

    el.innerHTML = `
      <div class="context-grid">
        <div class="context-pill">
          <div class="context-pill-label">Volatilit\u00e9</div>
          <div class="context-pill-value" style="color:${volColor}">${volState}</div>
          <div class="context-pill-sub">R\u00e9gime de march\u00e9</div>
        </div>
        <div class="context-pill">
          <div class="context-pill-label">Dealer</div>
          <div class="context-pill-value" style="color:${dealerColor}">${dealerLabel}</div>
          <div class="context-pill-sub">Flux dealers</div>
        </div>
        <div class="context-pill">
          <div class="context-pill-label">MOPI</div>
          <div class="context-pill-value" style="color:${mopiColor}">${mopi !== null ? Math.round(mopi) + '/100' : '\u2014'}</div>
          <div class="context-pill-sub">Market Options Pressure</div>
        </div>
        <div class="context-pill">
          <div class="context-pill-label">GEX</div>
          <div class="context-pill-value" style="color:${gexColor}">${gex}</div>
          <div class="context-pill-sub">Gamma Exposure</div>
        </div>
      </div>
    `;
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
