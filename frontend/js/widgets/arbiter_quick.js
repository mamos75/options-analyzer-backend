// js/widgets/arbiter_quick.js — F5: Bloc "Lecture en 5 secondes"
// Source unique : /api/decision (Arbiter) + /api/narrative (niveau déclencheur)
import { apiFetch } from '../api.js';
import { esc } from '../lib/fmt.js';

// F8.7 — Supprime le préfixe prix "$XX,XXX — " des labels
const stripPrice = (lbl) => lbl ? lbl.replace(/^\$[\d,]+ — /, '') : lbl;

const _VERDICT_CFG = {
  'SIGNAL_UP':   { icon: '▲', label: 'HAUSSIER',  color: '#22c55e', bg: '#22c55e0d', border: '#22c55e33' },
  'SIGNAL_DOWN': { icon: '▼', label: 'BAISSIER',  color: '#ef4444', bg: '#ef44440d', border: '#ef444433' },
  'OBSERVE':     { icon: '➡', label: 'INDÉCIS',   color: '#f59e0b', bg: '#f59e0b0d', border: '#f59e0b33' },
  'NO_TRADE':    { icon: '⊘', label: 'NO TRADE',  color: '#64748b', bg: '#64748b0d', border: '#64748b33' },
};

// F9.1 — Action en français (affiché dans le bloc "Quoi faire")
const _ACTION_FR = {
  'AGIR_LONG':  { icon: '▲', label: 'AGIR LONG',  color: '#22c55e' },
  'AGIR_SHORT': { icon: '▼', label: 'AGIR SHORT', color: '#ef4444' },
  'PRÉPARER':   { icon: '◎', label: 'PRÉPARER',   color: '#f59e0b' },
  'OBSERVER':   { icon: '◌', label: 'OBSERVER',   color: '#64748b' },
};

const _URGENCY_CFG = {
  'CRITIQUE': { color: '#ef4444', label: 'zone critique' },
  'ÉLEVÉE':   { color: '#f59e0b', label: 'zone de pression' },
  'MODÉRÉE':  { color: '#64748b', label: 'zone modérée' },
  'FAIBLE':   { color: '#475569', label: 'zone faible' },
  'NEUTRE':   { color: '#334155', label: 'zone neutre' },
};

export async function loadArbiterQuick(signal) {
  const el = document.getElementById('f5-quick-content');
  if (!el) return;

  try {
    const [dec, narr] = await Promise.all([
      apiFetch('/api/decision', signal),
      apiFetch('/api/narrative', signal),
    ]);

    if (!dec) { el.innerHTML = '<div style="color:var(--muted);font-size:12px">Données indisponibles</div>'; return; }

    const verdict   = dec.verdict        || 'OBSERVE';
    const action    = dec.action         || null;   // F8.3
    const state     = dec.state          || null;   // F8.3
    const confPct   = dec.confidence_pct ?? 0;
    const phrase    = dec.phrase         || '';
    const dataStale = dec.data_quality === 'STALE';
    const urgency   = dec.vexcex_urgency || 'NEUTRE';
    const regime    = dec.vexcex_label   || '';

    // Fiabilité réduite si stale OU flip incohérent (vient du dashboard, fallback sur dec)
    // On expose aussi gex_flip_incoherent dans /api/decision si possible, sinon on skip
    const flipIncoherent = !!(dec.gex_flip_incoherent);
    const structureMixte = !!(dec.structure_mixte);
    // structure_mixte est informatif seulement — ne réduit pas la fiabilité
    const reliabilityReduced = dataStale || (flipIncoherent && !structureMixte) || confPct < 20;

    const vc   = _VERDICT_CFG[verdict]  || _VERDICT_CFG['OBSERVE'];
    const urg  = _URGENCY_CFG[urgency]  || _URGENCY_CFG['NEUTRE'];

    // Niveau déclencheur : flip_level de narrative, sinon niveau_haut/bas
    const btcSpot  = narr?.btc_price    || dec.btc_price || null;
    const flipLvl  = narr?.flip_level   || null;
    const lvlHaut  = narr?.niveau_haut  || null;
    const lvlBas   = narr?.niveau_bas   || null;
    const lvlHautLbl = narr?.niveau_haut_label || null;
    const lvlBasLbl  = narr?.niveau_bas_label  || null;

    // F8.6 — Flip zone stability
    const flipZone = dec.flip_zone || null;
    const flipStable = !flipZone || flipZone.stable !== false;
    let triggerExtra = null;

    // Choix du niveau le plus pertinent selon le verdict
    let triggerLevel = null, triggerLbl = null, triggerAbove = null, triggerBelow = null;
    if (flipLvl && btcSpot) {
      triggerLevel = flipLvl;
      const above = btcSpot > flipLvl;
      triggerAbove = above ? 'maintien au-dessus = régime stabilisateur' : 'reconquête = retournement haussier';
      triggerBelow = above ? 'cassure en-dessous = amplification baissière' : 'maintien en-dessous = pression baissière';
      triggerLbl = 'Gamma Flip';
    } else if (verdict === 'SIGNAL_UP' && lvlBas) {
      triggerLevel = lvlBas; triggerLbl = stripPrice(lvlBasLbl) || 'Support';  // F8.7
      triggerAbove = 'au-dessus = signal valide'; triggerBelow = 'cassure = signal annulé';
    } else if (verdict === 'SIGNAL_DOWN' && lvlHaut) {
      triggerLevel = lvlHaut; triggerLbl = stripPrice(lvlHautLbl) || 'Résistance';  // F8.7
      triggerAbove = 'cassure = signal annulé'; triggerBelow = 'sous = pression baissière';
    } else if (lvlHaut || lvlBas) {
      triggerLevel = flipLvl || lvlHaut || lvlBas;
      triggerLbl = flipLvl ? 'Gamma Flip' : lvlHaut ? (stripPrice(lvlHautLbl) || 'Résistance') : (stripPrice(lvlBasLbl) || 'Support');  // F8.7
      triggerAbove = 'au-dessus = hausse'; triggerBelow = 'en-dessous = baisse';
    }

    // F8.6 — Override triggerLbl if flip is unstable
    if (!flipStable && flipZone && flipZone.n >= 3 && flipZone.amplitude_pct >= 1.0) {
      const zoneMin = Math.round(flipZone.min).toLocaleString();
      const zoneMax = Math.round(flipZone.max).toLocaleString();
      triggerLbl = flipZone.moving ? 'Flip en déplacement (expiration proche)' : 'Zone Gamma Flip (instable)';
      if (flipZone.display) triggerLevel = flipZone.display;
      triggerExtra = `Zone : $${zoneMin} – $${zoneMax}`;
    }

    const fmtP = v => v ? '$' + Math.round(v).toLocaleString() : null;

    // Action phrase : verdict + contexte urgence (JAMAIS afficher CRITIQUE sans "quoi faire")
    const urgCtx = urgency === 'CRITIQUE'
      ? ` La zone ${urg.label} exige une surveillance accrue — pas d'action mécanique sans confirmation.`
      : urgency === 'ÉLEVÉE'
      ? ` Zone de pression active.`
      : '';
    const actionPhrase = phrase + urgCtx;

    // Fiabilité effective = min(confPct, stale degradation)
    const reliabilityPct = reliabilityReduced ? Math.min(confPct, 15) : confPct;
    const reliabilityColor = reliabilityPct >= 50 ? '#22c55e' : reliabilityPct >= 25 ? '#f59e0b' : '#ef4444';

    el.innerHTML = `
      ${reliabilityReduced ? `
      <div style="display:flex;align-items:center;gap:8px;padding:7px 12px;background:#ef444411;border:1px solid #ef444433;border-radius:8px;font-size:11px;color:#ef4444;margin-bottom:10px;font-weight:600">
        ⚠ FIABILITÉ RÉDUITE — ${dataStale ? 'données périmées' : flipIncoherent ? 'signaux GEX/flip contradictoires' : 'confiance faible'}
      </div>` : ''}

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">

        <!-- DIRECTION -->
        <div style="grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:${vc.bg};border:1.5px solid ${vc.border};border-radius:12px;gap:12px">
          <div>
            <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:${vc.color};margin-bottom:4px">Direction</div>
            <div style="font-size:22px;font-weight:900;color:${vc.color};letter-spacing:.5px">${vc.icon} ${vc.label}</div>
            ${regime ? `<div style="font-size:10px;color:${urg.color};margin-top:3px">${esc(regime)}</div>` : ''}
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:9px;color:var(--muted);margin-bottom:2px;text-transform:uppercase;letter-spacing:.5px">Fiabilité</div>
            <div style="font-size:20px;font-weight:900;color:${reliabilityColor}">${reliabilityPct}%</div>
          </div>
        </div>

        <!-- LE NIVEAU -->
        ${triggerLevel ? `
        <div style="padding:10px 14px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:10px">
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#f59e0b;margin-bottom:4px">Le niveau</div>
          <div style="font-size:17px;font-weight:900;color:#f59e0b;font-variant-numeric:tabular-nums">${fmtP(triggerLevel)}</div>
          <div style="font-size:10px;color:var(--muted);margin-top:3px">${esc(triggerLbl || '')}</div>
          ${triggerExtra ? `<div style="font-size:10px;color:#f59e0b;margin-top:3px">${esc(triggerExtra)}</div>` : ''}
          <div style="font-size:10px;color:#94a3b8;margin-top:6px;line-height:1.5">
            ▲ ${esc(triggerAbove || '')}<br>▼ ${esc(triggerBelow || '')}
          </div>
        </div>` : ''}

        <!-- QUOI FAIRE -->
        <div style="padding:10px 14px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:10px${triggerLevel ? '' : ';grid-column:1/-1'}">
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:4px">Quoi faire</div>
          <!-- F9.1 — action FR au lieu du verdict EN -->
          <div style="font-size:11px;font-weight:700;color:${(_ACTION_FR[action]||_ACTION_FR['OBSERVER']).color};margin-bottom:4px">${(_ACTION_FR[action]||_ACTION_FR['OBSERVER']).icon} ${(_ACTION_FR[action]||_ACTION_FR['OBSERVER']).label}</div>
          <div style="font-size:11px;color:#c9d1e0;line-height:1.55">${esc(actionPhrase)}</div>
        </div>

      </div>
    `;
  } catch(e) {
    if (el) el.innerHTML = `<div style="color:var(--muted);font-size:11px">⚠ Bloc indisponible</div>`;
  }
}
