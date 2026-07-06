// js/widgets/vol_weather.js — Module 7: Météo Volatilité (Phase 5)
import { apiFetch } from '../api.js';
import { esc } from '../lib/fmt.js';
import { CFG } from '../config.js';

export async function loadVolWeather(signal) {
  const el = document.getElementById('m7-content');
  try {
    const data = await apiFetch('/api/vol_structure', signal);
    if (!data?.data?.length) { el.innerHTML = '<div class="error-state"><div class="error-icon">\u26a0</div>Donn\u00e9es indisponibles</div>'; return; }

    const terms = data.data.slice(0, 8);
    const nearIV = terms[0]?.iv || 0;
    const icon = nearIV > CFG.IV_EXTREME ? '\u26c8' : nearIV > CFG.IV_HIGH ? '\uD83C\uDF29' : nearIV > CFG.IV_ELEVATED ? '\uD83C\uDF27' : nearIV > CFG.IV_MODERATE ? '\u26c5' : '\u2600\uFE0F';
    const label = nearIV > CFG.IV_EXTREME ? 'Volatilit\u00e9 Extr\u00eame' : nearIV > CFG.IV_HIGH ? 'Volatilit\u00e9 Haute' : nearIV > CFG.IV_ELEVATED ? 'Volatilit\u00e9 \u00c9lev\u00e9e' : nearIV > CFG.IV_MODERATE ? 'Volatilit\u00e9 Mod\u00e9r\u00e9e' : 'Volatilit\u00e9 Basse';
    const desc = nearIV > CFG.IV_HIGH ? 'March\u00e9 sous tension \u2014 spreads \u00e9largis, prudence sur les positions directionnelles.'
               : nearIV > 40 ? 'Opportunit\u00e9s options pr\u00e9sentes, gestion du risque requise.'
               : 'March\u00e9 calme \u2014 co\u00fbt des options faible, favorable aux achats de protection.';

    const termCards = terms.map(t => `
      <div class="vol-term-item">
        <div class="vol-term-expiry">${t.expiry}</div>
        <div class="vol-term-iv" style="color:${t.iv > CFG.IV_HIGH ? 'var(--red)' : t.iv > 40 ? 'var(--yellow)' : 'var(--green)'}">${t.iv?.toFixed(1)}%</div>
        <div class="vol-term-oi">${t.oi_pct?.toFixed(1)}% OI</div>
      </div>`).join('');

    el.innerHTML = `
      <div class="vol-weather">
        <div class="vol-icon">${icon}</div>
        <div class="vol-info">
          <div class="vol-label">IV Term Structure</div>
          <div class="vol-value" style="color:${nearIV > CFG.IV_HIGH ? 'var(--red)' : nearIV > 40 ? 'var(--yellow)' : 'var(--green)'}">${nearIV?.toFixed(1)}%</div>
          <div class="vol-desc">${desc}</div>
        </div>
      </div>
      <div class="vol-term-grid">${termCards}</div>
    `;
  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">\u26a0</div>Erreur inattendue</div>`;
  }
}
