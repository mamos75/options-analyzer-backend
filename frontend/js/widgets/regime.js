// js/widgets/regime.js — V5: evidence panel + verdict depuis /api/decision
// classifyRegimeFull supprimé (~680 lignes) — classification déléguée au backend
// buildLevelsContext conservé (données objectives, zéro verdict)
import { apiFetch } from '../api.js';
import { esc } from '../lib/fmt.js';
import { CFG } from '../config.js';
import { setLastRegime } from '../store.js';

// ── Mapping regime_id → couleur dashboard ────────────────────────────────
const _PHASE_COLOR = {
  EXP:  '#f59e0b',
  FB:   '#f97316',
  FL:   '#a855f7',
  COMP: '#8b5cf6',
  DIV:  '#eab308',
  MOD:  '#22c55e',
  NEU:  '#64748b',
};

const _URGENCY_COLOR = {
  'CRITIQUE': '#ef4444',
  'ÉLEVÉE':   '#f59e0b',
  'MODÉRÉE':  '#64748b',
  'FAIBLE':   '#475569',
  'NEUTRE':   '#334155',
};

const _VERDICT_COLOR = {
  'SIGNAL_UP':   '#22c55e',
  'SIGNAL_DOWN': '#ef4444',
  'OBSERVE':     '#f59e0b',
  'NO_TRADE':    '#64748b',
};

const _SYSTEM_COLOR = {
  'TRADEABLE': '#22c55e',
  'OBSERVE':   '#f59e0b',
  'CONFLICT':  '#f97316',
  'DEGRADED':  '#94a3b8',
  'OFFLINE':   '#475569',
};

export function buildLevelsContext(btcSpot, lvlFlip, lvlHaut, lvlBas, mpStrike, mpDte, mpExpiry, flipDistPct, vexBull, cexBull, lvlHautLbl, lvlBasLbl, walls) {
  const fmtP = v => v ? '$' + Math.round(v).toLocaleString() : null;
  const lines = [];
  const THRESH = CFG.LEVELS_NEAR_THRESH;
  const near = (a, b) => a && b && Math.abs(a - b) / b < THRESH;

  // ── 1. Glossaire des roles ───────────────────────────────────────────
  // Glossaire fusionné : un strike = une ligne, multi-rôles cumulés
  const _rMap = new Map();
  const _add = (s, col, lbl) => { if (!s) return; const k = Math.round(s); if (!_rMap.has(k)) _rMap.set(k, {col, lbls:[lbl]}); else _rMap.get(k).lbls.push(lbl); };
  _add(lvlFlip,   '#f59e0b', 'Gamma Flip — les dealers <i>changent de comportement</i> mécaniquement');
  _add(mpStrike,  '#3d8eff', 'Max Pain (' + (mpExpiry||'') + ') — le marché <i>gravite</i> vers ce niveau à expiration');
  _add(lvlHaut,   '#22c55e', lvlHautLbl || 'Résistance options');
  _add(lvlBas,    '#ef4444', lvlBasLbl  || 'Support options');
  const roles = [..._rMap.entries()].sort((a,b) => b[0]-a[0]).map(
    ([k, {col, lbls}]) => '<b style="color:' + col + '">' + fmtP(k) + '</b> = ' + lbls.join(' + ')
  );
  if (roles.length) lines.push(roles.join('<br>'));

  // ── 2. Convergence triple ────────────────────────────────────────────
  const allLevels = [lvlFlip, mpStrike, lvlHaut, lvlBas].filter(Boolean);
  const refPrice  = mpStrike || lvlFlip;
  const convergingCount = refPrice
    ? allLevels.filter(l => Math.abs(l - refPrice) / refPrice < THRESH).length
    : 0;

  const flipNearMp   = near(lvlFlip, mpStrike);
  const flipNearBas  = near(lvlFlip, lvlBas);
  const flipNearHaut = near(lvlFlip, lvlHaut);
  const mpNearBas    = near(mpStrike, lvlBas);
  const mpNearHaut   = near(mpStrike, lvlHaut);
  const hautNearBas  = near(lvlHaut, lvlBas);

  if (convergingCount >= 3 && refPrice) {
    const dteTxt = mpDte === 0 ? 'aujourd\'hui' : mpDte === 1 ? 'demain' : mpDte !== null ? 'dans ' + mpDte + 'j' : '';
    const expTxt = mpExpiry ? ' (' + mpExpiry + ')' : '';
    lines.push(
      '&#9888;&#65039; <b>Convergence triple</b> autour de ' + fmtP(refPrice) + ' : Gamma Flip + Max Pain + ' +
      (flipNearBas ? 'Put wall' : 'Call wall') + ' sont tous dans la même zone. ' +
      (dteTxt ? 'Expiration ' + dteTxt + expTxt + ' — ' : '') +
      'le GEX remonte mécaniquement vers zéro car les déalers dénouent leurs positions avant fixing. ' +
      'Ce n\'est <b>pas un signal bull</b> : c\'est le marché qui gravite vers son point d\'expiration naturel.'
    );
  } else if (convergingCount === 2 && refPrice) {
    const which = flipNearMp   ? 'Gamma Flip et Max Pain' :
                  flipNearBas  ? 'Gamma Flip et Put wall' :
                  flipNearHaut ? 'Gamma Flip et Call wall' :
                  mpNearBas    ? 'Max Pain et Put wall' :
                  mpNearHaut   ? 'Max Pain et Call wall' :
                  hautNearBas  ? 'Call wall et Put wall (compression)' : 'deux niveaux clés';
    lines.push(
      '&#128204; ' + which + ' convergent en ' + fmtP(refPrice) + '. ' +
      'Cette confluence renforce l\'importance de ce niveau comme pivot — une cassure franche dans un sens déclenchera un mouvement plus ample qu\'un niveau isolé.'
    );
  }

  // ── 3. Divergence Flip / Max Pain (de part et d'autre du spot) ───────
  if (lvlFlip && mpStrike && btcSpot && !near(lvlFlip, mpStrike)) {
    const flipAbove = lvlFlip > btcSpot;
    const mpAbove   = mpStrike > btcSpot;
    if (flipAbove !== mpAbove) {
      lines.push(
        '&#128256; <b>Divergence Flip / Max Pain</b> : Gamma Flip (' + fmtP(lvlFlip) + ') et Max Pain (' + fmtP(mpStrike) + ') sont de part et d\'autre du spot. ' +
        'Les dealers subissent une traction dans deux directions opposées — configuration instable, volatilité élevée probable.'
      );
    } else {
      const distPct = (Math.abs(lvlFlip - mpStrike) / btcSpot * 100).toFixed(1);
      lines.push(
        '&#128204; Gamma Flip (' + fmtP(lvlFlip) + ') et Max Pain (' + fmtP(mpStrike) + ') sont du même côté mais écartés de ' + distPct + '%. ' +
        'Le marché a deux aimants successifs — progression probable par paliers.'
      );
    }
  }

  // ── 4. Position du spot par rapport aux niveaux ──────────────────────
  if (btcSpot && lvlFlip) {
    const aboveFlip   = btcSpot > lvlFlip;
    const distFlipPct = (Math.abs(btcSpot - lvlFlip) / btcSpot * 100).toFixed(1);
    if (parseFloat(distFlipPct) < 2) {
      lines.push(
        '&#128293; BTC (' + fmtP(Math.round(btcSpot)) + ') est à seulement ' + distFlipPct + '% du Gamma Flip. ' +
        (aboveFlip
          ? 'Tant que le spot tient <b>au-dessus</b> de ' + fmtP(lvlFlip) + ', le GEX reste stabilisateur. ' +
            'Une clôture <b>sous</b> ' + fmtP(lvlFlip) + ' active le régime amplificateur — les dealers vendent mécaniquement.'
          : 'Le spot est <b>en dessous</b> du Flip. Les dealers sont en mode amplificateur. ' +
            'Un retour <b>au-dessus</b> de ' + fmtP(lvlFlip) + ' inverserait le régime et déclencherait des rachats mécaniques.')
      );
    } else if (parseFloat(distFlipPct) < 5) {
      lines.push(
        '&#128204; BTC (' + fmtP(Math.round(btcSpot)) + ') est à ' + distFlipPct + '% du Gamma Flip ' + fmtP(lvlFlip) + '. ' +
        (aboveFlip
          ? 'Zone de vigilance : le régime stabilisateur tient, mais toute poussée vendeuse brève pourrait inverser la mécanique des dealers.'
          : 'Zone de vigilance : le régime amplificateur actif, mais un retour rapide au-dessus du Flip inverserait la mécanique.')
      );
    }
  }

  // ── 5. Spot entre Flip et Call wall (pocket haussier) ────────────────
  if (btcSpot && lvlFlip && lvlHaut && btcSpot > lvlFlip && btcSpot < lvlHaut) {
    const roomPct = ((lvlHaut - btcSpot) / btcSpot * 100).toFixed(1);
    lines.push(
      '&#128204; BTC est dans la <b>pocket haussier</b> : au-dessus du Flip (' + fmtP(lvlFlip) + ') et sous la résistance (' + fmtP(lvlHaut) + '). ' +
      'Espace libre de ' + roomPct + '% avant le Call wall — les dealers sont stabilisateurs dans cette zone.'
    );
  }

  // ── 6. Spot entre Put wall et Flip (pocket baissier) ─────────────────
  if (btcSpot && lvlBas && lvlFlip && btcSpot < lvlFlip && btcSpot > lvlBas) {
    const roomPct = ((btcSpot - lvlBas) / btcSpot * 100).toFixed(1);
    // F7.5 — detect nearest ATM or near-spot wall as first technical support
    let _atmLine = '';
    if (walls && walls.length) {
      const _nw = walls.filter(w => w.strike && Math.abs(w.strike - btcSpot) / btcSpot < 0.03);
      const _aw = _nw.find(w => w.side === 'AT_MONEY') || _nw.sort((a,b) => Math.abs(a.strike-btcSpot)-Math.abs(b.strike-btcSpot))[0] || null;
      if (_aw) {
        const _oi = _aw.total_oi != null ? Math.round(_aw.total_oi).toLocaleString() + ' BTC' : '';
        const _wt = _aw.side === 'AT_MONEY' ? 'Call wall ATM' : _aw.type === 'PUT_WALL' ? 'Put wall' : 'Call wall';
        _atmLine = ' Premier support technique : <b>' + _wt + ' ' + fmtP(_aw.strike) + '</b>' + (_oi ? ' (' + _oi + ' OI)' : '') + '.';
      }
    }
    lines.push(
      '&#9888;&#65039; BTC est dans la <b>zone de pression</b> : sous le Flip (' + fmtP(lvlFlip) + ') et au-dessus du support (' + fmtP(lvlBas) + '). ' +
      'Coussin de ' + roomPct + '% avant le Put wall — les dealers amplifient les mouvements dans cette zone.' + _atmLine
    );
  }

  // ── 7. Spot sous le Put wall (territoire bearish profond) ─────────────
  if (btcSpot && lvlBas && btcSpot < lvlBas) {
    lines.push(
      '&#9888;&#65039; BTC (' + fmtP(Math.round(btcSpot)) + ') est <b>sous le Put wall</b> ' + fmtP(lvlBas) + '. ' +
      'Zone de délivrance des puts — les vendeurs de puts rachètent des options, ce qui peut créer un rebond technique brutal même en tendance baissière.'
    );
  }

  // ── 8. Spot au-dessus du Call wall (territoire breakout) ─────────────
  if (btcSpot && lvlHaut && btcSpot > lvlHaut) {
    lines.push(
      '&#128204; BTC (' + fmtP(Math.round(btcSpot)) + ') est <b>au-dessus du Call wall</b> ' + fmtP(lvlHaut) + '. ' +
      'Territoire de gamma squeeze : les vendeurs de calls rachètent pour se couvrir, amplifiant la hausse mécaniquement.'
    );
  }

  // ── 9. Max Pain entre Flip et spot ───────────────────────────────────
  if (btcSpot && lvlFlip && mpStrike) {
    const mpBetweenFlipSpot = (lvlFlip < btcSpot && mpStrike > lvlFlip && mpStrike < btcSpot) ||
                               (lvlFlip > btcSpot && mpStrike < lvlFlip && mpStrike > btcSpot);
    if (mpBetweenFlipSpot) {
      lines.push(
        '&#128204; Max Pain (' + fmtP(mpStrike) + ') se trouve <b>entre le Gamma Flip et le spot</b>. ' +
        'Double gravité : le marché est attiré vers le Max Pain tout en étant retenu par la mécanique du Flip. ' +
        'Oscillation probable autour de ces deux niveaux.'
      );
    }
  }

  // ── 10. Urgence expiration ─────────────────────────────────────────
  if (mpDte !== null && mpDte <= 2 && mpStrike) {
    const urgTxt = mpDte === 0 ? 'AUJOURD\'HUI' : mpDte === 1 ? 'DEMAIN' : 'dans 2 jours';
    const expTxt = mpExpiry ? ' (' + mpExpiry + ')' : '';
    lines.push(
      '&#9200; <b>Expiration ' + urgTxt + expTxt + '</b> : le GEX se résorbera mécaniquement vers zéro après le fixing. ' +
      'Le signal GEX actuel est temporaire — ne pas le confondre avec un changement de tendance de fond.'
    );
  } else if (mpDte !== null && mpDte <= 7 && mpStrike) {
    lines.push(
      '&#9200; Expiration dans ' + mpDte + ' jours' + (mpExpiry ? ' (' + mpExpiry + ')' : '') + ' : ' +
      'le GEX commence à se résorber. La gravité vers le Max Pain ' + fmtP(mpStrike) + ' s\'intensifie progressivement.'
    );
  }

  // ── 11. Rappel VEX/CEX si GEX remonte mais reste baissier structurel ──
  if (!vexBull && !cexBull) {
    lines.push(
      '&#9888;&#65039; Même si le GEX remonte, VEX et CEX restent négatifs : les dealers sont structurellement vendeurs de BTC sur toute hausse de volatilité ou écoulement du temps. Le rebond du GEX est mécanique, pas directionnel.'
    );
  }

  // ── 12. VEX/CEX haussiers + GEX amplificateur (tension) ──────────────
  if (vexBull && cexBull && lvlFlip && btcSpot && btcSpot < lvlFlip) {
    lines.push(
      '&#128256; <b>Tension structurelle</b> : VEX et CEX haussiers mais le spot est <b>sous le Gamma Flip</b>. ' +
      'Les options positionnent pour une hausse mais les dealers amplifient les baisses — premier signal d\'un potentiel retournement si le Flip est reconquis.'
    );
  }

  return lines.length ? lines.join('<br><br>') : null;
}

export async function loadRegimeSummary(signal) {
  const el  = document.getElementById('regime-summary-content');
  const dot = document.getElementById('regime-dot-indicator');
  try {
    const [vcData, decisionData, narrData, snapData] = await Promise.all([
      apiFetch('/api/vex_cex', signal),
      apiFetch('/api/decision', signal),
      apiFetch('/api/narrative', signal),
      apiFetch('/api/snapshot', signal),
    ]);
    const walls = snapData?.walls?.walls || [];

    if (!vcData || vcData.error) throw new Error('vex_cex indisponible');

    // ── Build regime object from /api/decision (V5) ──────────────────
    const regimeId  = decisionData?.vexcex_regime_id || 'NEU-0';
    const phase     = decisionData?.vexcex_phase     || 'NEU';
    const urgency   = decisionData?.vexcex_urgency   || 'NEUTRE';
    const label     = decisionData?.vexcex_label     || 'SIGNAUX FAIBLES';
    const verdict   = decisionData?.verdict          || 'OBSERVE';
    const sysStatus = decisionData?.system_status    || 'OBSERVE';
    const confPct   = decisionData?.confidence_pct   ?? 0;
    const phrase    = decisionData?.phrase           || '';

    const phaseColor   = _PHASE_COLOR[phase]        || '#64748b';
    const urgencyColor = _URGENCY_COLOR[urgency]    || '#64748b';
    const verdictColor = _VERDICT_COLOR[verdict]    || '#64748b';
    const sysColor     = _SYSTEM_COLOR[sysStatus]   || '#64748b';

    // Objet minimal exposé au store (consommé par vex_cex.js)
    const regime = {
      label,
      color:  phaseColor,
      dot:    urgencyColor,
      plain:  phrase,
    };
    setLastRegime(regime);
    if (dot) dot.style.background = urgencyColor;

    // ── Evidence panel : signaux actifs ──────────────────────────────
    const signalsUsed    = decisionData?.signals_used    || [];
    const signalsIgnored = decisionData?.signals_ignored || [];

    const fmtVex = v => { const a=Math.abs(v),s=v>=0?'+':'-'; return a>=1e9?s+(a/1e9).toFixed(2)+'B':a>=1e6?s+(a/1e6).toFixed(1)+'M':s+Math.round(a).toLocaleString(); };
    const fmtCex = v => { const a=Math.abs(v),s=v>=0?'+':'-'; return a>=1000?s+(a/1000).toFixed(1)+'K Δ/j':s+a.toFixed(1)+' Δ/j'; };

    const signalMinis = [
      { name: 'VEX',        val: fmtVex(vcData.vex_total), bull: vcData.vex_total >= 0 },
      { name: 'CEX',        val: fmtCex(vcData.cex_total), bull: vcData.cex_total >= 0 },
      { name: 'Gamma Flip', val: vcData.gamma_flip ? `$${Math.round(vcData.gamma_flip).toLocaleString()}` : '—', bull: vcData.gamma_flip_side !== 'below' },
      { name: 'Régime GEX', val: esc(vcData.gamma_flip_regime || '—'), bull: vcData.gamma_flip_regime !== 'AMPLIFICATEUR' },
    ].map(s => `
      <div class="regime-signal-mini">
        <div class="regime-signal-mini-name">${s.name}</div>
        <div class="regime-signal-mini-val" style="color:${s.bull ? '#22c55e' : '#ef4444'}">${s.val}</div>
      </div>`).join('');

    // ── Key levels from narrative ────────────────────────────────────
    const btcSpot    = narrData?.btc_price          || vcData.btc_price || null;
    const lvlFlip    = vcData.gamma_flip            || null;
    const lvlHaut    = narrData?.niveau_haut        || null;
    const lvlBas     = narrData?.niveau_bas         || null;
    const lvlHautLbl = narrData?.niveau_haut_label  || null;
    const lvlBasLbl  = narrData?.niveau_bas_label   || null;
    const mpData     = narrData?.max_pain_display   || null;
    const mpStrike   = mpData?.strike  ?? null;
    const mpExpiry   = mpData?.expiry  ?? null;
    const mpDte      = mpData?.dte     ?? null;
    const mpLabel    = mpData?.label   ?? null;
    const fmtP = v => v ? `$${Math.round(v).toLocaleString()}` : '—';
    const vexBull    = vcData.vex_total >= 0;
    const cexBull    = vcData.cex_total >= 0;

    // ── Signals used/ignored rows ────────────────────────────────────
    const usedRows = signalsUsed.map(s => `
      <div class="regime-signal-item">
        <span class="regime-signal-name">${esc(s.name)}</span>
        <span class="regime-signal-val" style="color:#22c55e">${esc(s.detail || s.direction || '')}</span>
      </div>`).join('');

    const ignoredRows = signalsIgnored.filter(s => s.name !== 'Neural (MLP + GRU)').map(s => `
      <div class="regime-signal-item" style="opacity:.55">
        <span class="regime-signal-name" style="text-decoration:line-through">${esc(s.name)}</span>
        <span class="regime-signal-val" style="color:#64748b;font-size:10px">${esc(s.reason || '')}</span>
      </div>`).join('');

    el.innerHTML = `
      <div class="regime-top-hero">
        <div class="regime-top-badge" style="color:${phaseColor};border-color:${phaseColor}55;background:${phaseColor}11">
          <div class="regime-top-dot" style="background:${urgencyColor}"></div>
          ${esc(label)}
        </div>
        <span class="regime-top-urgency" style="color:${urgencyColor};border-color:${urgencyColor}55;background:${urgencyColor}11">
          ${esc(urgency)}
        </span>
        <span class="regime-top-urgency" style="color:${sysColor};border-color:${sysColor}55;background:${sysColor}11">
          ${esc(sysStatus)}
        </span>
      </div>

      <div class="regime-signals-grid">${signalMinis}</div>

      <!-- LEVELS BLOCK_v1 -->
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px">
        ${lvlFlip ? `
        <div style="grid-column:1/-1;background:#f59e0b11;border:1.5px solid #f59e0b55;border-radius:10px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:8px">
          <div>
            <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#f59e0b;margin-bottom:3px">⚡ Gamma Flip — déclencheur mécanique dealers</div>
            <div style="font-size:16px;font-weight:900;color:#f59e0b;font-variant-numeric:tabular-nums">${fmtP(lvlFlip)}</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px;font-weight:700;color:${vcData.gamma_flip_dist_pct < 0 ? '#ef4444' : '#22c55e'}">${vcData.gamma_flip_dist_pct !== null ? (vcData.gamma_flip_dist_pct >= 0 ? '+' : '') + vcData.gamma_flip_dist_pct.toFixed(1) + '% du spot' : ''}</div>
            <div style="font-size:10px;color:#94a3b8;margin-top:2px">${vcData.gamma_flip_side === 'below' ? '▼ En-dessous du spot' : '▲ Au-dessus du spot'}</div>
          </div>
        </div>` : ''}
        ${lvlHaut ? `
        <div style="background:#22c55e0d;border:1px solid #22c55e33;border-radius:10px;padding:10px 12px">
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#22c55e;margin-bottom:3px">${lvlHautLbl ? esc(lvlHautLbl) : '▲ Résistance options'}</div>
          <div style="font-size:15px;font-weight:800;color:#22c55e;font-variant-numeric:tabular-nums">${fmtP(lvlHaut)}</div>
        </div>` : ''}
        ${lvlBas ? `
        <div style="background:#ef44440d;border:1px solid #ef444433;border-radius:10px;padding:10px 12px">
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#ef4444;margin-bottom:3px">${lvlBasLbl ? esc(lvlBasLbl) : '▼ Support options'}</div>
          <div style="font-size:15px;font-weight:800;color:#ef4444;font-variant-numeric:tabular-nums">${fmtP(lvlBas)}</div>
        </div>` : ''}
        ${mpStrike ? `
        <div style="grid-column:1/-1;background:#3d8eff11;border:1.5px solid #3d8eff44;border-radius:10px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:8px">
          <div>
            <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#3d8eff;margin-bottom:3px">🎯 Max Pain — gravité expiration</div>
            <div style="font-size:16px;font-weight:900;color:#3d8eff;font-variant-numeric:tabular-nums">${fmtP(mpStrike)}</div>
            <div style="font-size:10px;color:#64748b;margin-top:2px">${esc(mpLabel || '')}</div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:13px;font-weight:800;color:#3d8eff">${esc(mpExpiry || '')}</div>
            <div style="font-size:11px;color:#64748b;margin-top:2px">${mpDte !== null ? (mpDte === 0 ? "Expiration aujourd'hui" : mpDte === 1 ? 'J−1' : 'J−' + mpDte) : ''}</div>
          </div>
        </div>` : ''}
      </div>

      ${(() => {
        const ctx = buildLevelsContext(
          btcSpot, lvlFlip, lvlHaut, lvlBas,
          mpStrike, mpDte, mpExpiry,
          vcData.gamma_flip_dist_pct,
          vexBull, cexBull,
          lvlHautLbl, lvlBasLbl, walls
        );
        return ctx
          ? `<div style="font-size:12px;color:#c9d1e0;line-height:1.85;margin-bottom:14px;padding:14px 16px;background:rgba(255,255,255,0.03);border-radius:10px;border-left:3px solid #475569">${ctx}</div>`
          : '';
      })()}

      <div class="regime-plain-text">${esc(phrase)}</div>

      ${signalsUsed.length ? `
      <div style="margin-top:12px">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:6px">Signaux actifs</div>
        <div class="regime-signals">${usedRows}</div>
      </div>` : ''}

      ${ignoredRows ? `
      <div style="margin-top:10px">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:6px">Signaux exclus</div>
        <div class="regime-signals">${ignoredRows}</div>
      </div>` : ''}

      <div style="margin-top:14px;padding:10px 14px;background:${verdictColor}0d;border:1.5px solid ${verdictColor}44;border-radius:10px;display:flex;align-items:center;justify-content:space-between;gap:12px">
        <div>
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:${verdictColor};margin-bottom:3px">Décision Arbiter</div>
          <div style="font-size:15px;font-weight:900;color:${verdictColor}">${esc(verdict)}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:20px;font-weight:900;color:${verdictColor}">${confPct}%</div>
          <div style="font-size:10px;color:#64748b">confiance</div>
        </div>
      </div>
    `;
  } catch (e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Régime indisponible : ${esc(e.message)}</div>`;
  }
}
