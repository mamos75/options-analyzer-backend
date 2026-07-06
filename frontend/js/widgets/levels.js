// js/widgets/levels.js — F3: type réel, tags PIN/ATM, pas de rôle hardcodé
import { apiFetch } from '../api.js';
import { esc, fmtPrice, tagBadge } from '../lib/fmt.js';

// Résout le rôle affiché d'un mur selon son type et sa position vs spot
function wallRole(w, price) {
  const type = w.type || '';
  const side = w.side || '';
  const strike = w.strike;

  if (side === 'AT_MONEY') return { label: 'ATM', cls: 'support', tag: 'ATM', tooltip: 'Mur au niveau du spot — agit comme pivot' };

  // Contradiction : CALL WALL classé SUPPORT ou PUT WALL classé RESISTANCE
  // = mur traversé par le prix, rôle d'aimant (PIN)
  const contradiction = (type === 'CALL_WALL' && side === 'SUPPORT') ||
                        (type === 'PUT_WALL'  && side === 'RESISTANCE');
  if (contradiction) {
    return {
      label:   type.replace('_', ' '),
      cls:     side === 'SUPPORT' ? 'support' : 'resistance',
      tag:     'PIN',
      tooltip: 'Mur traversé par le prix — agit comme aimant / support technique',
    };
  }

  // Cas normal
  if (side === 'SUPPORT')    return { label: type.replace('_', ' '), cls: 'support',    tag: null, tooltip: null };
  if (side === 'RESISTANCE') return { label: type.replace('_', ' '), cls: 'resistance', tag: null, tooltip: null };

  // Fallback position relative
  return strike < price
    ? { label: type.replace('_', ' '), cls: 'support',    tag: null, tooltip: null }
    : { label: type.replace('_', ' '), cls: 'resistance', tag: null, tooltip: null };
}

export async function loadLevels(signal) {
  const el = document.getElementById('m2-content');
  try {
    const wallsData = await apiFetch('/api/options_walls', signal);
    const walls = wallsData || {};

    const price = walls.btc_price || 0;
    const allWalls = (walls.walls || []).slice().sort((a, b) => a.strike - b.strike);
    const supports    = allWalls.filter(w => w.side === 'SUPPORT' || w.side === 'AT_MONEY' || (!w.side && w.strike < price)).slice(-3).reverse();
    const resistances = allWalls.filter(w => (w.side === 'RESISTANCE' || (!w.side && w.strike > price)) && w.side !== 'SUPPORT').slice(0, 3);

    const wallCard = (w) => {
      const role = wallRole(w, price);
      const tooltipAttr = role.tooltip ? ` title="${role.tooltip}"` : '';
      const extraBadge = role.tag === 'PIN'
        ? `<span style="font-size:9px;background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44;border-radius:4px;padding:1px 5px;margin-left:4px" title="${role.tooltip}">PIN</span>`
        : role.tag === 'ATM'
        ? `<span style="font-size:9px;background:#8b5cf622;color:#8b5cf6;border:1px solid #8b5cf644;border-radius:4px;padding:1px 5px;margin-left:4px">ATM</span>`
        : tagBadge(w.tag);
      return `<div class="level-card-sm ${role.cls}"${tooltipAttr}>
        <div class="level-type-sm">${esc(role.label)}${extraBadge}</div>
        <div class="level-price-sm ${role.cls}">${fmtPrice(w.strike)}</div>
        <div class="level-tag-sm">${Math.round(w.total_oi || 0).toLocaleString()} BTC</div>
      </div>`;
    };

    el.innerHTML = `
      <div class="lvl-section-title">Murs Options (actifs)</div>
      <div class="levels-grid-2">
        ${resistances.map(w => wallCard(w)).join('')}
        ${supports.map(w => wallCard(w)).join('')}
      </div>
    `;
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
