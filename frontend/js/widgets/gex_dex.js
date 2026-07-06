// js/widgets/gex_dex.js — Module 8: GEX & DEX Evolution (Phase 5)
import { apiFetch } from '../api.js';
import { fmtBig } from '../lib/fmt.js';
import { drawSparkline } from '../lib/canvas.js';
import { currentPeriod, setCurrentPeriod } from '../store.js';

export function setPeriod(p) {
  setCurrentPeriod(p);
  document.querySelectorAll('.period-btn').forEach(btn => {
    const txt = btn.textContent.toLowerCase().replace('j', 'd');
    btn.classList.toggle('active', txt === p);
  });
  // Import and call sibling widget to avoid circular dep at top level
  import('./mopi_btc.js').then(m => m.loadMopiVsBtc());
  loadGexDex();
}

export async function loadGexDex(signal) {
  const el = document.getElementById('m8-content');
  try {
    // Re-read currentPeriod from store at call time
    const { currentPeriod: period } = await import('../store.js');
    const data = await apiFetch('/api/mopi_vs_btc?period=' + period, signal);
    if (!data?.gex?.length) { el.innerHTML = '<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>'; return; }

    const gexVals = data.gex.filter(v => v != null);
    const dexVals = data.dex.filter(v => v != null);
    const lastGex = gexVals.length > 0 ? gexVals[gexVals.length - 1] : null;
    const lastDex = dexVals[dexVals.length - 1] || 0;
    const prevGex = gexVals.length > 1 ? gexVals[gexVals.length - 2] : lastGex;
    const prevDex = dexVals[dexVals.length - 2] || lastDex;
    const dexDir = lastDex > prevDex ? '\u25b2' : '\u25bc';
    const gexValid = lastGex != null && !isNaN(lastGex);
    const gexDir = !gexValid ? '\u2014' : lastGex > prevGex ? '\u25b2' : '\u25bc';
    const gexColor = !gexValid ? 'var(--muted)' : lastGex < 0 ? 'var(--red)' : lastGex === 0 ? 'var(--yellow)' : 'var(--green)';
    const dexColor = lastDex > 0 ? 'var(--red)' : 'var(--green)';

    // F7.2 — dynamic legend driven by regime_meca (not raw GEX sign)
    let gexRegimeMeca = null;
    try {
      const snap = await apiFetch('/api/snapshot');
      gexRegimeMeca = snap?.dashboard?.gex_regime || null;
    } catch(_) {}

    let gexImpact;
    if (!gexValid) {
      gexImpact = 'Donn\u00e9es GEX indisponibles';
    } else if (gexRegimeMeca === 'AMPLIFICATEUR') {
      const absGex = lastGex != null ? (Math.abs(lastGex)/1e9).toFixed(2) : '?';
      gexImpact = `Spot sous le Gamma Flip — les dealers amplifient les mouvements. Intensit\u00e9 globale (${absGex}B) mesure la force du pin au-dessus.`;
    } else if (gexRegimeMeca === 'STABILISANT') {
      gexImpact = 'Spot au-dessus du Gamma Flip — les dealers absorbent les mouvements (pin effect).';
    } else if (gexRegimeMeca === 'ZONE_DE_FLIP') {
      gexImpact = 'Sur la ligne de bascule — r\u00e9gime ind\u00e9termin\u00e9, chaque mouvement peut inverser la dynamique.';
    } else {
      // fallback signe GEX si regime_meca indisponible
      gexImpact = lastGex === 0 ? 'GEX = 0 \u2192 Gamma Flip : point de basculement du r\u00e9gime dealers'
        : lastGex < 0 ? 'GEX n\u00e9gatif \u2192 dealers amplifient les mouvements (amplificateur)'
        : 'GEX positif \u2192 dealers absorbent les mouvements (stabilisateur / pin effect)';
    }
    const dexImpact = lastDex > 0
      ? 'DEX positif \u2192 dealers couvrent en vendant BTC si le prix monte (r\u00e9sistance)'
      : 'DEX n\u00e9gatif \u2192 dealers ach\u00e8tent BTC si le prix baisse (support)';

    el.innerHTML = `
      <div class="gex-dex-grid">
        <div class="gex-dex-card">
          <div class="gex-dex-title">GEX (Gamma)</div>
          <div class="gex-dex-value" style="color:${gexColor}">${gexDir} ${fmtBig(lastGex)}</div>
          <div class="spark-wrap"><canvas id="spark-gex"></canvas></div>
          <div class="gex-dex-impact">${gexImpact}</div>
        </div>
        <div class="gex-dex-card">
          <div class="gex-dex-title">DEX (Delta)</div>
          <div class="gex-dex-value" style="color:${dexColor}">${dexDir} ${fmtBig(lastDex)}</div>
          <div class="spark-wrap"><canvas id="spark-dex"></canvas></div>
          <div class="gex-dex-impact">${dexImpact}</div>
        </div>
      </div>
      <div class="gex-dex-legend">
        <span style="color:var(--green)">&#9632;</span> Stabilisant &nbsp;
        <span style="color:var(--red)">&#9632;</span> Amplifiant &nbsp;
        <span style="color:var(--yellow)">&#9632;</span> Flip (GEX=0)
      </div>
    `;

    requestAnimationFrame(() => {
      const sparkGexColor = lastGex < 0 ? '#ff3c6b' : lastGex === 0 ? '#eab308' : '#00d47e';
      const sparkGexFill = lastGex < 0 ? 'rgba(255,60,107,0.1)' : lastGex === 0 ? 'rgba(234,179,8,0.1)' : 'rgba(0,212,126,0.1)';
      drawSparkline('spark-gex', gexVals, sparkGexColor, sparkGexFill);
      drawSparkline('spark-dex', dexVals, lastDex > 0 ? '#ff3c6b' : '#00d47e', lastDex > 0 ? 'rgba(255,60,107,0.1)' : 'rgba(0,212,126,0.1)');
    });
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
