// js/widgets/levels.js — F3: type réel, tags PIN/ATM, pas de rôle hardcodé
// F9.6 — Garantit ≥1 mur au-dessus ET ≥1 mur en-dessous du spot dans le top-N affiché
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

// F9.6 — Sélectionne top-N murs en garantissant ≥1 au-dessus ET ≥1 en-dessous du spot.
// Algorithme :
//   1. Sépare les murs en deux groupes : above_spot et below_spot (par strike vs price)
//   2. Trie chaque groupe par total_oi décroissant (plus gros mur en premier)
//   3. Prend le meilleur above + meilleur below (slots garantis)
//   4. Remplit les slots restants (N-2) avec les prochains murs par OI, tous côtés confondus
//   5. Re-trie le résultat final par strike décroissant pour l'affichage (résistances en haut)
function selectTopWallsBalanced(allWalls, price, N) {
  if (!allWalls || allWalls.length === 0) return [];

  // Sépare above (résistances) et below (supports) par position du strike vs spot
  // AT_MONEY est inclus dans below pour éviter un slot vide côté support
  const above = allWalls.filter(w => w.strike > price).slice().sort((a, b) => (b.total_oi || 0) - (a.total_oi || 0));
  const below  = allWalls.filter(w => w.strike <= price).slice().sort((a, b) => (b.total_oi || 0) - (a.total_oi || 0));

  const selected = new Set();
  const result = [];

  // Slots garantis : meilleur above + meilleur below (si ils existent)
  if (above.length > 0) { result.push(above[0]); selected.add(above[0].strike + '|' + above[0].type); }
  if (below.length > 0) { result.push(below[0]); selected.add(below[0].strike + '|' + below[0].type); }

  // Slots restants : parcours les murs par OI décroissant (tous côtés), sans doublon
  const remaining = allWalls
    .slice()
    .sort((a, b) => (b.total_oi || 0) - (a.total_oi || 0));

  for (const w of remaining) {
    if (result.length >= N) break;
    const key = w.strike + '|' + w.type;
    if (!selected.has(key)) {
      result.push(w);
      selected.add(key);
    }
  }

  // Re-tri par strike décroissant : résistances (plus haut strike) en premier
  return result.sort((a, b) => b.strike - a.strike);
}

export async function loadLevels(signal) {
  const el = document.getElementById('m2-content');
  try {
    const wallsData = await apiFetch('/api/options_walls', signal);
    const walls = wallsData || {};

    const price = walls.btc_price || 0;
    const allWalls = (walls.walls || []).slice().sort((a, b) => a.strike - b.strike);

    // F9.6 — sélection garantissant ≥1 above + ≥1 below spot (au lieu de slice brut)
    const TOP_N = 6;
    const topWalls = selectTopWallsBalanced(allWalls, price, TOP_N);

    // Sépare pour l'affichage (résistances en haut, supports en bas)
    const resistances = topWalls.filter(w => w.strike > price);
    const supports    = topWalls.filter(w => w.strike <= price);

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
