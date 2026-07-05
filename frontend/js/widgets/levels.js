// js/widgets/levels.js — Module 2: Niveaux Clés (Phase 5)
import { apiFetch } from '../api.js';
import { esc, fmtPrice, tagBadge } from '../lib/fmt.js';

export async function loadLevels(decisionData, signal) {
  const el = document.getElementById('m2-content');
  try {
    const wallsData = await apiFetch('/api/options_walls', signal);
    const walls = wallsData || {};

    const price = walls.btc_price || 0;
    const allWalls = (walls.walls || []).slice().sort((a,b) => a.strike - b.strike);
    const supports = allWalls.filter(w => w.strike < price).slice(-3).reverse();
    const resistances = allWalls.filter(w => w.strike > price).slice(0, 3);

    const wallCard = (w, side) => {
      const cls = side === 'support' ? 'support' : 'resistance';
      return `<div class="level-card-sm ${cls}">
        <div class="level-type-sm">${side === 'support' ? 'Support' : 'R\u00e9sistance'}${tagBadge(w.tag)}</div>
        <div class="level-price-sm ${cls}">${fmtPrice(w.strike)}</div>
        <div class="level-tag-sm">${esc(w.type?.replace('_',' ')||'')} \u00b7 ${Math.round(w.total_oi||0).toLocaleString()} BTC</div>
      </div>`;
    };

    el.innerHTML = `
      <div class="lvl-section-title">Murs Options (actifs)</div>
      <div class="levels-grid-2">
        ${resistances.map(w => wallCard(w, 'resistance')).join('')}
        ${supports.map(w => wallCard(w, 'support')).join('')}
      </div>
    `;
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
