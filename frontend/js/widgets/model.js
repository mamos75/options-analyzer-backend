// js/widgets/model.js — F4: fix payload key ranking[].model, fallback FR propre
import { apiFetch } from '../api.js';
import { esc, fmtPct, formatModelName } from '../lib/fmt.js';

export async function loadModel(signal) {
  const data = await apiFetch('/api/model_arena/leaderboard', signal);
  const el = document.getElementById('m6-content');

  if (!data) {
    el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Données indisponibles</div>`;
    return;
  }

  // API retourne { ranking: [...] } — cherche le modèle principal (is_principal=true)
  const ranking = data.ranking || (Array.isArray(data) ? data : []);
  const principal = ranking.find(m => m.is_principal) || ranking.find(m => m.is_best) || ranking[0] || null;

  if (!principal) {
    console.warn('[model] Aucun modèle principal trouvé dans le payload', data);
    el.innerHTML = `<div class="model-row"><div class="model-name">Modèle non identifié</div></div>`;
    return;
  }

  // Clé correcte : m.model (pas model_name/name/engine)
  const rawName = principal.model || principal.model_name || principal.name || principal.engine || null;
  if (!rawName) console.warn('[model] Champ nom manquant dans', principal);

  const name = rawName ? formatModelName(rawName) : 'Modèle non identifié';
  const version = principal.version || '';
  const status  = principal.status  || '';
  const winRate = principal.avg_winrate ?? principal.win_rate ?? null;
  const n       = principal.n_evaluated ?? principal.total_predictions ?? null;

  // F7.4 — baseline context : 3 classes par défaut (BEAR/BULL/FLAT), hasard = 33%
  const nClasses  = data.n_classes || principal.n_classes || 3;
  const baseline  = 100 / nClasses;  // hasard = 33.3% pour 3 classes
  const wrPct     = winRate !== null ? winRate * 100 : null;
  const aboveBase = wrPct !== null && wrPct >= baseline;
  const wrColor   = wrPct === null ? 'var(--muted)' : aboveBase ? 'var(--green)' : '#ef4444';
  const wrBadge   = !aboveBase && wrPct !== null
    ? `<span style="font-size:9px;background:#ef444422;color:#ef4444;border-radius:4px;padding:1px 5px;margin-left:6px">⚠ sous hasard</span>`
    : '';

  el.innerHTML = `
    <div class="model-row">
      <div class="model-name">${esc(name)}${version ? `<span style="font-size:10px;color:var(--muted);margin-left:6px">${esc(version)}</span>` : ''}</div>
      <div class="model-stats">
        ${wrPct !== null ? `<div class="model-stat"><span>Win Rate</span><span style="color:${wrColor}">${fmtPct(wrPct)}${wrBadge}</span><span style="font-size:9px;color:var(--muted);margin-left:4px">(hasard = ${fmtPct(baseline)}, ${nClasses} classes)</span></div>` : ''}
        ${n !== null ? `<div class="model-stat"><span>Évaluations</span><span>${Number(n).toLocaleString()}</span></div>` : ''}
        ${status ? `<div class="model-stat"><span>Statut</span><span style="color:var(--yellow)">${esc(status)}</span></div>` : ''}
      </div>
    </div>
  `;
}
