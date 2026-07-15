// js/widgets/gex_dex.js — GEX & DEX Évolution (refonte chart dual-axis)
import { apiFetch } from '../api.js';
import { fmtBig } from '../lib/fmt.js';
import { currentPeriod, setCurrentPeriod } from '../store.js';

export function setPeriod(p) {
  setCurrentPeriod(p);
  document.querySelectorAll('.period-btn').forEach(btn => {
    const txt = btn.textContent.toLowerCase().replace('j', 'd');
    btn.classList.toggle('active', txt === p);
  });
  loadGexDex();
}

/* ── Dessin du chart dual-axe ───────────────────────────────────────────── */
function drawGexDexChart(canvasId, timestamps, gexVals, dexVals) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !gexVals.length) return;

  const dpr = window.devicePixelRatio || 1;
  const W   = canvas.parentElement.getBoundingClientRect().width || 600;
  const H   = 200;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const pad = { t: 20, b: 36, l: 62, r: 62 };
  const cW  = W - pad.l - pad.r;
  const cH  = H - pad.t - pad.b;
  const n   = gexVals.length;

  const xAt = i => pad.l + (i / (n - 1)) * cW;

  /* ── Échelles ── */
  const gexMin  = Math.min(...gexVals);
  const gexMax  = Math.max(...gexVals);
  const gexRange = (gexMax - gexMin) || 1;
  const yGex = v => pad.t + cH - ((v - gexMin) / gexRange) * cH;

  const dexMin  = Math.min(...dexVals);
  const dexMax  = Math.max(...dexVals);
  const dexRange = (dexMax - dexMin) || 1;
  const yDex = v => pad.t + cH - ((v - dexMin) / dexRange) * cH;

  /* ── Fond + grille ── */
  ctx.clearRect(0, 0, W, H);

  ctx.strokeStyle = 'rgba(26,37,64,.6)';
  ctx.lineWidth = 1;
  const nGridY = 4;
  for (let i = 0; i <= nGridY; i++) {
    const y = pad.t + (i / nGridY) * cH;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cW, y); ctx.stroke();
  }

  /* ── Ligne GEX = 0 ── */
  if (gexMin <= 0 && gexMax >= 0) {
    const y0 = yGex(0);
    ctx.save();
    ctx.strokeStyle = 'rgba(0,232,135,.45)';
    ctx.lineWidth   = 1;
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, y0); ctx.lineTo(pad.l + cW, y0); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(0,232,135,.55)';
    ctx.font = `${9 / dpr * dpr}px Inter,sans-serif`;
    ctx.textAlign = 'right';
    ctx.fillText('Flip', pad.l - 4, y0 + 3);
    ctx.restore();
  }

  /* ── Aire DEX ── */
  const dexColor  = 'rgba(124,92,255,1)';
  const dexFill0  = 'rgba(124,92,255,.20)';
  const dexFill1  = 'rgba(124,92,255,.02)';
  const gradDex   = ctx.createLinearGradient(0, pad.t, 0, pad.t + cH);
  gradDex.addColorStop(0, dexFill0);
  gradDex.addColorStop(1, dexFill1);
  ctx.beginPath();
  ctx.moveTo(xAt(0), yDex(dexVals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(xAt(i), yDex(dexVals[i]));
  ctx.lineTo(xAt(n - 1), pad.t + cH);
  ctx.lineTo(xAt(0), pad.t + cH);
  ctx.closePath();
  ctx.fillStyle = gradDex;
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(xAt(0), yDex(dexVals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(xAt(i), yDex(dexVals[i]));
  ctx.strokeStyle = dexColor;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  /* ── Aire GEX ── */
  const gexColor = 'rgba(0,207,255,1)';
  const gexFill0 = 'rgba(0,207,255,.22)';
  const gexFill1 = 'rgba(0,207,255,.02)';
  const gradGex  = ctx.createLinearGradient(0, pad.t, 0, pad.t + cH);
  gradGex.addColorStop(0, gexFill0);
  gradGex.addColorStop(1, gexFill1);
  ctx.beginPath();
  ctx.moveTo(xAt(0), yGex(gexVals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(xAt(i), yGex(gexVals[i]));
  ctx.lineTo(xAt(n - 1), pad.t + cH);
  ctx.lineTo(xAt(0), pad.t + cH);
  ctx.closePath();
  ctx.fillStyle = gradGex;
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(xAt(0), yGex(gexVals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(xAt(i), yGex(gexVals[i]));
  ctx.strokeStyle = gexColor;
  ctx.lineWidth = 2;
  ctx.stroke();

  /* ── Axes labels gauche (GEX) ── */
  ctx.fillStyle = 'rgba(0,207,255,.8)';
  ctx.font = '9px Inter,sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= nGridY; i++) {
    const frac = i / nGridY;
    const val  = gexMin + frac * gexRange;
    const y    = pad.t + cH - frac * cH;
    ctx.fillText((val / 1e9).toFixed(1) + 'B', pad.l - 5, y + 3);
  }

  /* ── Axes labels droite (DEX) ── */
  ctx.fillStyle = 'rgba(124,92,255,.8)';
  ctx.textAlign = 'left';
  for (let i = 0; i <= nGridY; i++) {
    const frac = i / nGridY;
    const val  = dexMin + frac * dexRange;
    const y    = pad.t + cH - frac * cH;
    const abs  = Math.abs(val);
    const lbl  = (val < 0 ? '-' : '') + (abs >= 1000 ? (abs / 1000).toFixed(1) + 'k' : Math.round(abs).toString());
    ctx.fillText(lbl, pad.l + cW + 5, y + 3);
  }

  /* ── Labels axe X ── */
  const nLabels = Math.min(6, n);
  ctx.fillStyle = 'rgba(75,90,116,.9)';
  ctx.textAlign = 'center';
  ctx.font = '9px Inter,sans-serif';
  for (let j = 0; j < nLabels; j++) {
    const i   = Math.round((j / (nLabels - 1)) * (n - 1));
    const ts  = timestamps[i];
    const d   = new Date(ts * 1000);
    const lbl = d.getDate() + '/' + (d.getMonth() + 1) + ' ' +
                String(d.getHours()).padStart(2, '0') + 'h';
    ctx.fillText(lbl, xAt(i), pad.t + cH + 14);
  }

  /* ── Légende inline haut ── */
  ctx.textAlign = 'left';
  ctx.font = 'bold 9px Inter,sans-serif';
  ctx.fillStyle = 'rgba(0,207,255,.9)';
  ctx.fillText('■ GEX ($B)', pad.l, 13);
  ctx.fillStyle = 'rgba(124,92,255,.9)';
  ctx.fillText('■ DEX (BTC)', pad.l + 70, 13);

  /* ── Tooltip interactif ── */
  // Stocker les données pour le tooltip (sur le canvas)
  canvas._chartData = { timestamps, gexVals, dexVals, xAt, yGex, yDex, pad, cW, cH, W, H, n };
}

function attachTooltip(canvas) {
  if (canvas._tooltipAttached) return;
  canvas._tooltipAttached = true;

  const tip = document.createElement('div');
  tip.id = 'gex-dex-tooltip';
  tip.style.cssText = `
    position:fixed; pointer-events:none; z-index:9999;
    background:#111a2e; border:1px solid #1c2d4a; border-radius:8px;
    padding:8px 12px; font-size:11px; color:#e2e8f5;
    box-shadow:0 4px 20px rgba(0,0,0,.5); display:none;
    font-family:Inter,sans-serif; line-height:1.6; min-width:150px;
  `;
  document.body.appendChild(tip);

  canvas.addEventListener('mousemove', e => {
    const d = canvas._chartData;
    if (!d) return;
    const rect = canvas.getBoundingClientRect();
    const mx   = e.clientX - rect.left;
    const my   = e.clientY - rect.top;
    if (mx < d.pad.l || mx > d.pad.l + d.cW) { tip.style.display = 'none'; return; }

    const frac = (mx - d.pad.l) / d.cW;
    const idx  = Math.round(frac * (d.n - 1));
    if (idx < 0 || idx >= d.n) { tip.style.display = 'none'; return; }

    const ts   = d.timestamps[idx];
    const gex  = d.gexVals[idx];
    const dex  = d.dexVals[idx];
    const date = new Date(ts * 1000);
    const lbl  = date.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' }) +
                 ' ' + String(date.getHours()).padStart(2, '0') + ':' +
                 String(date.getMinutes()).padStart(2, '0');

    const gexSign = gex >= 0 ? '+' : '';
    const dexSign = dex >= 0 ? '+' : '';
    const gexCol  = gex < 0 ? '#ff4d4d' : gex === 0 ? '#ffb830' : '#00cfff';
    const dexCol  = '#7c5cff';

    tip.innerHTML = `
      <div style="color:#4a5d80;margin-bottom:4px;font-size:10px;">${lbl}</div>
      <div><span style="color:${gexCol}">■ GEX</span> : <b>${gexSign}${(gex / 1e9).toFixed(2)}B$</b></div>
      <div><span style="color:${dexCol}">■ DEX</span> : <b>${dexSign}${Math.round(dex).toLocaleString('fr-FR')} BTC</b></div>
    `;
    tip.style.display = 'block';
    tip.style.left    = (e.clientX + 14) + 'px';
    tip.style.top     = (e.clientY - 40) + 'px';
  });

  canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
}

/* ── Widget principal ───────────────────────────────────────────────────── */
export async function loadGexDex(signal) {
  const el = document.getElementById('m8-content');
  if (!el) return;

  try {
    const { currentPeriod: period } = await import('../store.js');
    const data = await apiFetch('/api/gex_dex_history?period=' + period, signal);

    if (!data?.gex?.length) {
      el.innerHTML = '<div class="error-state"><div class="error-icon">⚠</div>Données indisponibles</div>';
      return;
    }

    const gexVals = data.gex.map(v => v ?? 0);
    const dexVals = data.dex.map(v => v ?? 0);
    const timestamps = data.timestamps ?? [];
    const lastGex = gexVals[gexVals.length - 1];
    const lastDex = dexVals[dexVals.length - 1];
    const prevGex = gexVals.length > 1 ? gexVals[gexVals.length - 2] : lastGex;
    const prevDex = dexVals.length > 1 ? dexVals[dexVals.length - 2] : lastDex;

    /* Variation sur la période (first → last) — source de vérité pour flèches ET texte */
    const gexDelta = lastGex - gexVals[0];
    const dexDelta = lastDex - dexVals[0];
    const gexDeltaSign = gexDelta >= 0 ? '+' : '';
    const dexDeltaSign = dexDelta >= 0 ? '+' : '';

    /* Flèche = signe du delta de période (cohérent avec le texte "+X.XXB$") */
    const gexDir   = gexDelta >= 0 ? '▲' : '▼';
    const gexColor = lastGex < 0 ? 'var(--red)' : lastGex === 0 ? 'var(--yellow)' : '#00cfff';

    /* DEX : flèche = signe du delta de période, couleur = signe de la valeur courante */
    const dexDir   = dexDelta >= 0 ? '▲' : '▼';
    const dexColor = lastDex > 0 ? 'var(--red)' : '#00d47e';

    /* Narrative contexte */
    let gexImpact = '';
    try {
      const snap = await apiFetch('/api/snapshot', signal);
      const regime = snap?.dashboard?.gex_regime || '';
      if (regime === 'AMPLIFICATEUR') {
        gexImpact = `Dealers amplifient les mouvements — GEX négatif. Intensité : ${(Math.abs(lastGex)/1e9).toFixed(2)}B$`;
      } else if (regime === 'STABILISANT') {
        gexImpact = 'Dealers absorbent les mouvements — pin effect actif.';
      } else if (regime === 'ZONE_DE_FLIP') {
        gexImpact = 'Zone de bascule — régime indéterminé.';
      } else {
        gexImpact = lastGex === 0 ? 'GEX = 0 → Gamma Flip : point de basculement'
          : lastGex < 0 ? 'GEX négatif → amplificateur'
          : 'GEX positif → stabilisateur (pin effect)';
      }
    } catch(_) {
      gexImpact = lastGex < 0 ? 'GEX négatif → amplificateur' : 'GEX positif → stabilisateur';
    }

    const dexImpact = lastDex > 0
      ? `Dealers long delta — résistance dynamique si prix monte`
      : `Dealers short delta — soutien dynamique si prix monte`;

    el.innerHTML = `
      <div class="gex-dex-summary">
        <div class="gex-dex-card">
          <div class="gex-dex-title">GEX (Gamma)</div>
          <div class="gex-dex-value" style="color:${gexColor}">${gexDir} ${fmtBig(lastGex)}</div>
          <div class="gex-dex-delta">${gexDeltaSign}${(gexDelta/1e9).toFixed(2)}B$ sur la période</div>
          <div class="gex-dex-impact">${gexImpact}</div>
        </div>
        <div class="gex-dex-card">
          <div class="gex-dex-title">DEX (Delta)</div>
          <div class="gex-dex-value" style="color:${dexColor}">${dexDir} ${fmtBig(lastDex)}</div>
          <div class="gex-dex-delta">${dexDeltaSign}${Math.round(dexDelta).toLocaleString('fr-FR')} BTC sur la période</div>
          <div class="gex-dex-impact">${dexImpact}</div>
        </div>
      </div>

      <div class="gex-dex-chart-wrap">
        <canvas id="canvas-gex-dex" style="width:100%;cursor:crosshair;display:block;"></canvas>
      </div>

      <div class="gex-dex-legend">
        <span style="color:#00cfff">■</span> GEX ($B, axe gauche) &nbsp;·&nbsp;
        <span style="color:#7c5cff">■</span> DEX net (BTC, axe droit) &nbsp;·&nbsp;
        <span style="color:rgba(0,232,135,.7)">▬</span> GEX = 0 (Flip)
      </div>
    `;

    requestAnimationFrame(() => {
      const c = document.getElementById('canvas-gex-dex');
      if (c) {
        drawGexDexChart('canvas-gex-dex', timestamps, gexVals, dexVals);
        attachTooltip(c);
      }
    });

  } catch(e) {
    if (e?.name !== 'AbortError' && el) {
      el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Erreur inattendue</div>`;
    }
  }
}
