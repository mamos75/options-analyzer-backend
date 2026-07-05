// js/widgets/model.js — Module 6: Modèle Actif (Phase 5)
import { apiFetch } from '../api.js';
import { esc, fmtPct, formatModelName } from '../lib/fmt.js';

export async function loadModel(signal) {
  const data = await apiFetch('/api/model_arena/leaderboard', signal);
  const el = document.getElementById('m6-content');

  if (!data) {
    el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>`;
    return;
  }

  let model = null;
  if (Array.isArray(data)) {
    model = data[0];
  } else if (data.principal) {
    model = data.principal;
  } else if (data.leaderboard && Array.isArray(data.leaderboard)) {
    model = data.leaderboard[0];
  } else {
    model = data;
  }

  const name = formatModelName(model?.model_name || model?.name || model?.engine || 'Unknown');
  const winRate = model?.win_rate ?? model?.winrate ?? null;
  const predictions = model?.total_predictions ?? model?.predictions ?? null;
  const days = model?.days_of_data ?? model?.days ?? null;

  el.innerHTML = `
    <div class="model-row">
      <div class="model-name">${esc(name)}</div>
      <div class="model-stats">
        ${winRate !== null ? `<div class="model-stat"><span>Win Rate</span><span style="color:var(--green)">${fmtPct(winRate)}</span></div>` : ''}
        ${predictions !== null ? `<div class="model-stat"><span>Pr\u00e9dictions</span><span>${Number(predictions).toLocaleString()}</span></div>` : ''}
        ${days !== null ? `<div class="model-stat"><span>Donn\u00e9es</span><span>${days}j</span></div>` : ''}
      </div>
    </div>
  `;
}
