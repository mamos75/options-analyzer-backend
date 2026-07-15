// js/widgets/pro_decision.js — Moteur de Décision Pro Options
import { apiFetch } from '../api.js';
import { esc, fmtPrice } from '../lib/fmt.js';

// ── Config verdict ─────────────────────────────────────────────────────────
const VERDICT_CFG = {
  'BULL_FORT':           { icon: '▲▲', label: 'BULL FORT',        color: '#22c55e', bg: 'rgba(34,197,94,0.08)',  border: 'rgba(34,197,94,0.25)' },
  'BULL_MODERE':         { icon: '▲',  label: 'BULL MODÉRÉ',      color: '#4ade80', bg: 'rgba(74,222,128,0.06)', border: 'rgba(74,222,128,0.20)' },
  'BEAR_FORT':           { icon: '▼▼', label: 'BEAR FORT',        color: '#ef4444', bg: 'rgba(239,68,68,0.08)',  border: 'rgba(239,68,68,0.25)' },
  'BEAR_MODERE':         { icon: '▼',  label: 'BEAR MODÉRÉ',      color: '#f87171', bg: 'rgba(248,113,113,0.06)', border: 'rgba(248,113,113,0.20)' },
  'ZONE_DE_FLIP_BINAIRE':{ icon: '⚖', label: 'ZONE FLIP BINAIRE', color: '#f59e0b', bg: 'rgba(245,158,11,0.08)', border: 'rgba(245,158,11,0.25)' },
  'NEUTRE_RANGE':        { icon: '↔',  label: 'NEUTRE / RANGE',   color: '#94a3b8', bg: 'rgba(148,163,184,0.06)', border: 'rgba(148,163,184,0.18)' },
  'ATTENDRE':            { icon: '⊘',  label: 'ATTENDRE',          color: '#64748b', bg: 'rgba(100,116,139,0.06)', border: 'rgba(100,116,139,0.18)' },
};

const CONVICTION_COLOR = (c) =>
  c >= 8 ? '#22c55e' : c >= 6 ? '#f59e0b' : '#ef4444';

const WARN_COLOR = { 'ELEVE': '#ef4444', 'MODERE': '#f59e0b', 'INFO': '#64748b' };

const TRADE_TYPE_CFG = {
  'CALL':         { icon: '▲', color: '#22c55e' },
  'PUT':          { icon: '▼', color: '#ef4444' },
  'CALL_SPREAD':  { icon: '↑↓', color: '#4ade80' },
  'PUT_SPREAD':   { icon: '↓↑', color: '#f87171' },
  'STRADDLE':     { icon: '⟺', color: '#a78bfa' },
  'STRANGLE':     { icon: '⟺', color: '#c4b5fd' },
  'WAIT':         { icon: '◌', color: '#64748b' },
};

const IV_REGIME_COLOR = {
  'EXTREME':   '#ef4444',
  'ELEVEE':    '#f59e0b',
  'NORMALE':   '#94a3b8',
  'COMPRIMEE': '#22c55e',
};

function fmtPct(v) {
  if (v == null) return '--';
  return (v > 0 ? '+' : '') + v.toFixed(1) + '%';
}

function fmtK(v) {
  if (!v) return '--';
  return '$' + Math.round(v).toLocaleString();
}

function convictionBar(conviction) {
  const pct = Math.round((conviction / 10) * 100);
  const col = CONVICTION_COLOR(conviction);
  return `
    <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
      <div style="flex:1;height:5px;background:rgba(255,255,255,0.08);border-radius:3px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${col};border-radius:3px;transition:width .4s"></div>
      </div>
    </div>`;
}

function renderLadder(items, dir) {
  if (!items || !items.length) return '<span style="color:var(--muted);font-size:10px">—</span>';
  const arrow = dir === 'up' ? '▲' : '▼';
  const col   = dir === 'up' ? '#4ade80' : '#f87171';
  return items.slice(0, 3).map(it => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
      <span style="font-size:11px;font-weight:700;color:${col}">${arrow} ${fmtK(it.price)}</span>
      <span style="font-size:9px;color:var(--muted)">${esc(it.tag||'')} ${it.dist_pct != null ? '(' + fmtPct(it.dist_pct) + ')' : ''}</span>
    </div>`).join('');
}

export async function loadProDecision(signal) {
  const el = document.getElementById('pro-decision-content');
  if (!el) return;

  const data = await apiFetch('/api/pro_decision', signal);

  if (!data || data.error) {
    el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>${esc(data?.error || 'Données indisponibles')}</div>`;
    return;
  }

  const vc    = VERDICT_CFG[data.verdict] || VERDICT_CFG['ATTENDRE'];
  const trade = data.trade || {};
  const tc    = TRADE_TYPE_CFG[trade.type] || TRADE_TYPE_CFG['WAIT'];
  const vol   = data.vol || {};
  const prob  = data.probability || {};
  const lvl   = data.levels || {};
  const reg   = data.regime || {};
  const deal  = data.dealer || {};
  const warns = data.warnings || [];
  const spot  = data.spot || 0;
  const ivCol = IV_REGIME_COLOR[vol.iv_regime] || '#94a3b8';

  el.innerHTML = `
    <!-- ── VERDICT PRINCIPAL ─────────────────────────────────── -->
    <div style="padding:14px 16px;background:${vc.bg};border:1.5px solid ${vc.border};border-radius:12px;margin-bottom:12px">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
        <div>
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:${vc.color};margin-bottom:4px">Verdict Pro Options</div>
          <div style="font-size:22px;font-weight:900;color:${vc.color};letter-spacing:.3px">${vc.icon} ${vc.label}</div>
          <div style="font-size:10px;color:var(--muted);margin-top:3px">${esc(data.action_label || '')}</div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-size:9px;color:var(--muted);margin-bottom:2px;text-transform:uppercase;letter-spacing:.5px">Conviction Pro</div>
          <div style="font-size:13px;font-weight:800;color:${CONVICTION_COLOR(data.conviction)}">${esc(data.conviction_label || '—')}</div>
          ${data.global_confidence != null ? `
          <div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.08)">
            <div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Confiance Arbiter</div>
            <div style="font-size:14px;font-weight:800;color:${data.global_confidence >= 60 ? '#22c55e' : data.global_confidence >= 35 ? '#f59e0b' : '#ef4444'}">${data.global_confidence}%</div>
          </div>` : ''}
        </div>
      </div>
      ${convictionBar(data.conviction || 0)}
    </div>

    <!-- ── THÈSE + TRADE ────────────────────────────────────── -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">

      <!-- Thèse principale -->
      <div style="padding:10px 12px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.09);border-radius:10px">
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:5px">Thèse</div>
        <div style="font-size:10.5px;color:#c9d1e0;line-height:1.55">${esc(data.primary_thesis || '—')}</div>
      </div>

      <!-- Trade suggéré -->
      <div style="padding:10px 12px;background:rgba(255,255,255,0.04);border:1px solid ${tc.color}33;border-radius:10px">
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:5px">Trade suggéré</div>
        ${trade.type !== 'WAIT' ? `
          <div style="font-size:13px;font-weight:800;color:${tc.color};margin-bottom:3px">${tc.icon} ${esc(trade.action || '—')}</div>
          <div style="font-size:9.5px;color:var(--muted);margin-bottom:5px">${esc(trade.rationale || '')}</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">
            ${trade.sizing_pct ? `<span style="padding:2px 6px;background:rgba(255,255,255,0.07);border-radius:4px;font-size:9px;font-weight:600;color:#f59e0b">Taille: ${trade.sizing_pct}%</span>` : ''}
            ${trade.risk_reward ? `<span style="padding:2px 6px;background:rgba(255,255,255,0.07);border-radius:4px;font-size:9px;color:var(--muted)">R/R: ${trade.risk_reward}:1</span>` : ''}
            ${['CALL_SPREAD','PUT_SPREAD'].includes(trade.type) ? `<span style="padding:2px 6px;background:rgba(100,116,139,0.15);border-radius:4px;font-size:9px;color:#94a3b8">Risque défini par la structure</span>` : (trade.stop_price ? `<span style="padding:2px 6px;background:rgba(239,68,68,0.12);border-radius:4px;font-size:9px;color:#f87171">Stop: ${fmtK(trade.stop_price)}</span>` : '')}
            ${trade.target_price ? `<span style="padding:2px 6px;background:rgba(34,197,94,0.10);border-radius:4px;font-size:9px;color:#4ade80">Target: ${fmtK(trade.target_price)}</span>` : ''}
          </div>
        ` : `<div style="font-size:12px;font-weight:700;color:#64748b">⊘ Pas de trade — conditions insuffisantes</div>`}
      </div>

    </div>

    <!-- ── FORCES ────────────────────────────────────────────── -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">

      <!-- Phase 4 — Forces/Signaux selon état Arbiter (SUPPRESSED ou normal) -->
      ${(() => {
        const suppressed = data.verdict === 'ATTENDRE' && (data.action_label || '').includes('non actionnable');
        if (suppressed) {
          const sigs = data.supporting_forces || [];
          return '<div style="padding:10px 12px;background:rgba(100,116,139,0.06);border:1px solid rgba(100,116,139,0.18);border-radius:10px;grid-column:1/-1">'
            + '<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#94a3b8;margin-bottom:5px">Signaux en présence</div>'
            + (sigs.length
                ? sigs.map(s => '<div style="font-size:10px;color:#94a3b8;line-height:1.45;padding:2px 0">· ' + esc(s) + '</div>').join('')
                : '<span style="font-size:10px;color:var(--muted)">—</span>')
            + '</div>';
        }
        const supHtml = (data.supporting_forces || []).length
          ? (data.supporting_forces || []).map(f => '<div style="font-size:10px;color:#c9d1e0;line-height:1.45;padding:2px 0">✓ ' + esc(f) + '</div>').join('')
          : '<span style="font-size:10px;color:var(--muted)">Aucune</span>';
        const oppHtml = (data.opposing_forces || []).length
          ? (data.opposing_forces || []).map(f => '<div style="font-size:10px;color:#c9d1e0;line-height:1.45;padding:2px 0">✗ ' + esc(f) + '</div>').join('')
          : '<span style="font-size:10px;color:var(--muted)">Aucune</span>';
        return '<div style="padding:10px 12px;background:rgba(34,197,94,0.04);border:1px solid rgba(34,197,94,0.15);border-radius:10px">'
          + '<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#4ade80;margin-bottom:5px">Forces favorables</div>'
          + supHtml + '</div>'
          + '<div style="padding:10px 12px;background:rgba(239,68,68,0.04);border:1px solid rgba(239,68,68,0.15);border-radius:10px">'
          + '<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#f87171;margin-bottom:5px">Forces adverses</div>'
          + oppHtml + '</div>';
      })()}

    </div>

    <!-- ── MÉTRIQUES CLÉ ──────────────────────────────────────── -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px">

      <!-- Régime GEX -->
      <div style="padding:8px 10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:9px;text-align:center">
        <div style="font-size:8.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px">Régime GEX</div>
        <div style="font-size:11px;font-weight:800;color:${reg.regime === 'AMPLIFICATEUR' ? '#ef4444' : '#4ade80'}">${esc(reg.regime || '—')}</div>
        ${reg.flip_level ? `<div style="font-size:8.5px;color:var(--muted);margin-top:2px">Flip ${fmtK(reg.flip_level)}</div>` : ''}
      </div>

      <!-- DEX Direction -->
      <div style="padding:8px 10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:9px;text-align:center">
        <div style="font-size:8.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px">DEX Dealer</div>
        <div style="font-size:10px;font-weight:800;color:${deal.direction && deal.direction.includes('BULL') ? '#4ade80' : deal.direction && deal.direction.includes('BEAR') ? '#f87171' : '#94a3b8'}">${esc((deal.direction || '—').replace('_FLOWS',''))}</div>
        <div style="font-size:8.5px;color:var(--muted);margin-top:2px">${esc(deal.intensity || '')}</div>
      </div>

      <!-- IV Rank -->
      <div style="padding:8px 10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:9px;text-align:center">
        <div style="font-size:8.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px">IV Rank</div>
        <div style="font-size:14px;font-weight:800;color:${ivCol}">${vol.iv_rank != null ? Math.round(vol.iv_rank) : '--'}<span style="font-size:9px">%</span></div>
        <div style="font-size:8.5px;color:${ivCol};margin-top:2px">${esc(vol.iv_regime || '')}</div>
      </div>

      <!-- Proba Edge -->
      <div style="padding:8px 10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:9px;text-align:center">
        <div style="font-size:8.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px">Edge</div>
        <div style="font-size:14px;font-weight:800;color:${prob.edge_quality === 'FORT' ? '#22c55e' : prob.edge_quality === 'MODERE' ? '#f59e0b' : '#94a3b8'}">${prob.dominant_prob != null ? Math.round(prob.dominant_prob) : '--'}<span style="font-size:9px">/100</span></div>
        <div style="font-size:8.5px;color:var(--muted);margin-top:2px">${esc(prob.dominant_direction || '')} ${esc(prob.dominant_horizon || '')}</div>
      </div>

    </div>

    <!-- ── NIVEAUX + INVALIDATION ─────────────────────────────── -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">

      <!-- Niveaux montée -->
      <div style="padding:10px 12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px">
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#4ade80;margin-bottom:6px">Résistances</div>
        ${renderLadder(lvl.upside_ladder, 'up')}
      </div>

      <!-- Niveaux descente -->
      <div style="padding:10px 12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px">
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#f87171;margin-bottom:6px">Supports</div>
        ${renderLadder(lvl.downside_ladder, 'down')}
      </div>

    </div>

    <!-- Invalidation -->
    ${data.invalidation_price ? `
    <div style="padding:8px 12px;background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.20);border-radius:8px;display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <span style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:#f87171;white-space:nowrap">Invalidation</span>
      <span style="font-size:12px;font-weight:800;color:#ef4444">${fmtK(data.invalidation_price)}</span>
      <span style="font-size:10px;color:var(--muted);line-height:1.4">${esc(data.invalidation_reason || '')}</span>
    </div>` : ''}

    <!-- ── WARNINGS ───────────────────────────────────────────── -->
    ${warns.length ? `
    <div style="display:flex;flex-direction:column;gap:5px">
      ${warns.map(w => `
        <div style="display:flex;align-items:flex-start;gap:8px;padding:6px 10px;background:${WARN_COLOR[w.level] ? WARN_COLOR[w.level] + '11' : 'rgba(255,255,255,0.04)'};border-left:2px solid ${WARN_COLOR[w.level] || '#64748b'};border-radius:0 6px 6px 0;font-size:10px;color:#c9d1e0;line-height:1.4">
          <span style="color:${WARN_COLOR[w.level] || '#64748b'};font-size:9px;font-weight:700;white-space:nowrap;margin-top:1px">${esc(w.level||'')}</span>
          <span>${esc(w.message || '')}</span>
        </div>`).join('')}
    </div>` : ''}
  `;
}
