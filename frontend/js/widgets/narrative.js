// js/widgets/narrative.js — Module 5: Narrative IA (Phase 5)
import { apiFetch } from '../api.js';
import { esc, fmtPrice } from '../lib/fmt.js';

export async function loadNarrative(signal) {
  const data = await apiFetch('/api/narrative', signal);
  const el = document.getElementById('m5-content');

  if (!data) {
    el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>`;
    return;
  }

  const phrase = data.phrase_synthese || data.synthesis || data.text || '\u2014';
  const banner = data.banner_message || data.banner || null;
  const niveauHaut = data.niveau_haut || data.high_level || null;
  const niveauBas = data.niveau_bas || data.low_level || null;

  el.innerHTML = `
    ${phrase ? `<div class="narrative-phrase">${esc(phrase)}</div>` : ''}
    ${banner ? `<div class="narrative-banner">${esc(banner)}</div>` : ''}
    ${(niveauHaut || niveauBas) ? `
    <div class="narrative-levels">
      ${niveauHaut ? `<span class="level-chip up">\u2191 ${fmtPrice(niveauHaut)}</span>` : ''}
      ${niveauBas ? `<span class="level-chip down">\u2193 ${fmtPrice(niveauBas)}</span>` : ''}
    </div>` : ''}
  `;
}
