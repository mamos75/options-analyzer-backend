// js/widgets/probabilities.js — F2: dominant only, score de règles, équilibré
import { apiFetch } from '../api.js';
import { esc } from '../lib/fmt.js';

// F14.3 — EQUILIBRE_DELTA supprimé : on lit horizon_verdict_* depuis le backend

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

    const _conf = (key) => {
      const v = data[key];
      if (v == null || typeof v !== 'object') return null;
      const n = Number(v.confidence);
      return Number.isFinite(n) ? n : null;
    };

    const _cov = (key) => {
      const v = data[key];
      if (v == null || typeof v !== 'object') return null;
      const n = Number(v.data_coverage_pct);
      return Number.isFinite(n) ? n : null;
    };

    const _hist = (key) => {
      const v = data[key];
      if (v == null || typeof v !== 'object') return null;
      return v.historical_validation || null;
    };

    const horizons = [
      { label: '24h', bull: _p('bull_24h'), bear: _p('bear_24h'),
        conf_bull: _conf('bull_24h'), conf_bear: _conf('bear_24h'),
        cov_bull: _cov('bull_24h'), hist_bull: _hist('bull_24h'), hist_bear: _hist('bear_24h') },
      { label: '72h', bull: _p('bull_72h'), bear: _p('bear_72h'),
        conf_bull: _conf('bull_72h'), conf_bear: _conf('bear_72h'),
        cov_bull: _cov('bull_72h'), hist_bull: _hist('bull_72h'), hist_bear: _hist('bear_72h') },
    ];

    // Crash warning
    const crashWarn = data.crash_regime_warning;
    let html = '';
    if (crashWarn) {
      html += `<div class="stale-banner" style="margin-bottom:10px;padding:7px 10px;background:rgba(239,68,68,0.15);border-left:3px solid var(--red);border-radius:4px;font-size:0.8rem;color:var(--red)">⚠ ${esc(crashWarn)}</div>`;
    }

    html += '<div class="prob-horizons">';

    horizons.forEach((h, hi) => {
      const bull = h.bull ?? 0;
      const bear = h.bear ?? 0;
      const delta = Math.abs(bull - bear);
      // F14.3 — verdict depuis le backend (source unique)
      const verdictKey = hi === 0 ? 'horizon_verdict_24h' : 'horizon_verdict_72h';
      const horizonVerdict = data[verdictKey] || 'EQUILIBRE';
      const equilibre = (horizonVerdict === 'EQUILIBRE');

      if (equilibre) {
        // Scores trop proches — pas de biais exploitable
        const betterScore = Math.max(bull, bear);
        const conf = h.conf_bull ?? h.conf_bear ?? null;
        const cov = h.cov_bull ?? null;
        html += `
          <div class="prob-horizon">
            <div class="prob-horizon-label">${esc(h.label)}</div>
            <div class="prob-scenario dominant">
              <div class="prob-header">
                <span class="prob-label" style="color:var(--yellow)">ÉQUILIBRÉ</span>
                <span class="prob-pct" style="color:var(--muted)">${Math.round(betterScore)}/100</span>
              </div>
              <div class="prob-bar-wrap">
                <div class="prob-bar" id="pb-${hi}-dom" style="width:0%;background:var(--yellow)"></div>
              </div>
              <div class="prob-sub">Pas de biais exploitable — scores trop proches</div>
              ${conf != null ? `<div class="prob-meta">Complétude : ${Math.round(conf)}%</div>` : ''}
            </div>
          </div>
        `;
        return;
      }

      const dominant = bull >= bear ? 'bull' : 'bear';
      const domScore = dominant === 'bull' ? bull : bear;
      const domConf  = dominant === 'bull' ? h.conf_bull : h.conf_bear;
      const domCov   = h.cov_bull;
      const domHist  = dominant === 'bull' ? h.hist_bull : h.hist_bear;
      const domLabel = dominant === 'bull' ? 'Biais HAUSSIER' : 'Biais BAISSIER';
      const domColor = dominant === 'bull' ? 'var(--green)' : 'var(--red)';

      html += `
        <div class="prob-horizon">
          <div class="prob-horizon-label">${esc(h.label)}</div>
          <div class="prob-scenario dominant">
            <div class="prob-header">
              <span class="prob-label" style="color:${domColor}">${domLabel}</span>
              <span class="prob-pct" style="color:${domColor}">${Math.round(domScore)}/100</span>
            </div>
            <div class="prob-bar-wrap">
              <div class="prob-bar ${dominant}" id="pb-${hi}-dom" style="width:0%"></div>
            </div>
            <div class="prob-meta">
              ${domConf != null ? `Complétude : ${Math.round(domConf)}%` : ''}
              ${domHist ? ` · Validation : ${esc(domHist)}` : ''}
            </div>
          </div>
        </div>
      `;
    });

    // Interprétation globale du backend (1 phrase)
    const interp = data.interpretation || data.conclusion_line || '';
    if (interp) {
      html += `<div class="prob-conclusion">${esc(interp)}</div>`;
    }

    html += '</div>';

    // Note terminologique
    html += `<div class="prob-disclaimer" style="margin-top:8px;font-size:0.72rem;color:var(--muted);line-height:1.4">
      Score de règles (0–100) : somme des règles options déclenchées, pondérées par fiabilité.
      Ce n'est pas une probabilité statistique — pas de calibration historique suffisante.
    </div>`;

    el.innerHTML = html;

    // Animate bars
    setTimeout(() => {
      horizons.forEach((h, hi) => {
        const bull = h.bull ?? 0;
        const bear = h.bear ?? 0;
        // F14.3 — verdict depuis le backend (source unique)
        const vKey = hi === 0 ? 'horizon_verdict_24h' : 'horizon_verdict_72h';
        const equilibre = (data[vKey] || 'EQUILIBRE') === 'EQUILIBRE';
        const domScore = equilibre ? Math.max(bull, bear) : (bull >= bear ? bull : bear);
        const bar = document.getElementById(`pb-${hi}-dom`);
        if (bar) bar.style.width = Math.min(domScore, 100) + '%';
      });
    }, 150);

  } catch(e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Erreur inattendue</div>`;
  }
}
