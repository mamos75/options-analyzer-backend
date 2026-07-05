// js/widgets/regime.js — Market Regime Classification (Phase 5)
import { apiFetch } from '../api.js';
import { esc } from '../lib/fmt.js';
import { CFG } from '../config.js';
import { setLastRegime } from '../store.js';

export function classifyRegimeFull({ vex, cex, gex, dex, vexTrend, cexTrend, gexTrend, dexTrend, flipDistPct, flipLevel }) { /* FLIP_PRICE_v1 */
  // Signs
  const vexBull = vex > 0;   // VEX+ = dealers buy BTC on IV up
  const cexBull = cex > 0;   // CEX+ = dealers buy BTC as time passes
  const gexBull = gex >= 0;  // GEX+ = stabilisateur
  const dexBull = dex <= 0;  // DEX- = dealers buy BTC on dip = support

  // Trends (up/down/flat)
  const vexUp   = vexTrend === 'up';
  const vexDown = vexTrend === 'down';
  const cexUp   = cexTrend === 'up';
  const cexDown = cexTrend === 'down';
  const gexUp   = gexTrend === 'up';
  const gexDown = gexTrend === 'down';
  const dexUp   = dexTrend === 'up';
  const dexDown = dexTrend === 'down';

  // Trend helpers: is a signal "turning around"?
  // vex is negative but rising = bearish force weakening
  const vexTurning  = !vexBull && vexUp;   // VEX- turning up  → bear pressure easing
  const vexStrength = vexBull  && vexUp;   // VEX+ strengthening
  const vexFading   = vexBull  && vexDown; // VEX+ fading
  const vexDeep     = !vexBull && vexDown; // VEX- deepening    → bear pressure growing
  const cexTurning  = !cexBull && cexUp;   // CEX- turning up  → bear charm easing
  const cexStrength = cexBull  && cexUp;   // CEX+ strengthening
  const cexFading   = cexBull  && cexDown; // CEX+ fading
  const cexDeep     = !cexBull && cexDown; // CEX- deepening

  const bigVex = Math.abs(vex) > CFG.GEX_BIG_VEX;
  const bigCex = Math.abs(cex) > CFG.GEX_BIG_CEX;
  const bigExp = bigVex || bigCex;
  const nearFlip2 = flipDistPct !== null && Math.abs(flipDistPct) <= CFG.FLIP_NEAR_PCT;
  const nearFlip5 = flipDistPct !== null && Math.abs(flipDistPct) <= CFG.FLIP_WARN_PCT;
  // Format flip level as "$60,000" or fallback to distance only
  const fmtFlip = flipLevel
    ? `$${Math.round(flipLevel).toLocaleString()} (${flipDistPct >= 0 ? '+' : ''}${flipDistPct !== null ? flipDistPct.toFixed(1) : '?'}% du spot)`
    : (flipDistPct !== null ? `${Math.abs(flipDistPct).toFixed(1)}% du spot` : '—');
  const flipDir  = flipDistPct !== null && flipDistPct < 0 ? 'EN-DESSOUS du spot' : 'AU-DESSUS du spot';
  const bullCount = [vexBull, cexBull, gexBull, dexBull].filter(Boolean).length;

  const fmtM   = v => (v >= 0 ? '+' : '') + (v / 1e6).toFixed(1) + 'M';
  const fmtB   = v => (v >= 0 ? '+' : '') + (v / 1e9).toFixed(2) + 'B';
  const fmtDex = v => (v >= 0 ? '+' : '') + Math.round(v).toLocaleString();
  const arr    = t => t === 'up' ? '↑' : t === 'down' ? '↓' : '→';

  const signals = [
    { name:'VEX',        formatted:fmtM(vex),    bull:vexBull, trendUp:vexUp,  trendFlat:vexTrend==='flat' },
    { name:'CEX',        formatted:fmtM(cex),    bull:cexBull, trendUp:cexUp,  trendFlat:cexTrend==='flat' },
    { name:'GEX',        formatted:fmtB(gex),    bull:gexBull, trendUp:gexUp,  trendFlat:gexTrend==='flat' },
    { name:'DEX',        formatted:fmtDex(dex),  bull:dexBull, trendUp:dexUp,  trendFlat:dexTrend==='flat' },
    { name:'Gamma Flip',
      formatted:flipLevel
        ? `$${Math.round(flipLevel).toLocaleString()} (${flipDistPct!==null?(flipDistPct>=0?'+':'')+flipDistPct.toFixed(1)+'%':'?'})`
        : (flipDistPct!==null?(flipDistPct>=0?'+':'')+flipDistPct.toFixed(1)+'%':'—'),
      bull:flipDistPct===null||flipDistPct>0, trendUp:null, trendFlat:true },
    { name:'Régime GEX', formatted:gexBull?'STABILISATEUR':'AMPLIFICATEUR',
      bull:gexBull, trendUp:null, trendFlat:true },
  ];

  const mk = (id, label, phase, color, confidence, urgency, bias, plain, raison, pro) => ({
    id, label, phase, color, dot:color, confidence, urgency, bias,
    signals, plain, raison, pro, advice: raison + ' ' + pro,
  });

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 1 — EXPLOSIF (flip ≤ 2% + bigExp)          [EXP-1..8]
  // ══════════════════════════════════════════════════════════════════
  if (nearFlip2 && bigExp) {

    // EXP-1b : bullish + double trend confirmation
    if (vexBull && dexBull && vexStrength && cexStrength) return mk(
      'EXP-1b','EXPLOSION IMMINENTE — SQUEEZE HAUSSIER CONFIRMÉ','explosive','#ff6b00',
      'ÉLEVÉE','CRITIQUE','LONG',
      `Gamma Flip à ${fmtFlip} + VEX ${arr("up")} + CEX ${arr("up")} : la pression haussière des dealers s’accélère ET le flip est à portée. Tous les feux sont au vert pour un squeeze.`,
      `Raison : VEX+↑/CEX+↑/DEX- avec flip < 2% = configuration squeeze la plus forte.`,
      `Un trader pro entre LONG à taille complète, stop sous le flip exact, objectif prochain niveau de résistance majeur.`
    );

    // EXP-1 : bullish aligné
    if (vexBull && dexBull) return mk(
      'EXP-1','PRÉPARATION EXPLOSIVE — BIAIS HAUSSIER','explosive','#f59e0b',
      'ÉLEVÉE','CRITIQUE','LONG',
      `Gamma Flip à ${fmtFlip}. VEX+ et DEX- : les deux forces directionnelles poussent à la hausse. Un franchissement du flip vers le haut déclenche un squeeze mécanique.`,
      `Raison : VEX+/DEX- alignés haussiers + flip à portée = squeeze potentiel.`,
      `Un trader pro entre LONG à taille réduite (⅓), stop serré sous le flip exact, scale-in sur clôture horaire au-dessus.`
    );

    // EXP-2b : bearish + double trend deepening
    if (!vexBull && !dexBull && vexDeep && cexDeep) return mk(
      'EXP-2b','EXPLOSION IMMINENTE — CASCADE BAISSIÈRE CONFIRMÉE','explosive','#b91c1c',
      'ÉLEVÉE','CRITIQUE','SHORT',
      `Gamma Flip à ${fmtFlip} + VEX ${arr("down")} + CEX ${arr("down")} : la pression vendeuse des dealers s’emballe AND le flip est imminent. Cascade de re-hedging pratiquement certaine.`,
      `Raison : VEX-↓/CEX-↓/GEX-/DEX+ + flip < 2% = configuration cascade baissière maximale.`,
      `Un trader pro entre SHORT à taille complète ou achète des puts échéance < 1 semaine. Stop au-dessus du flip. Objectif : -5% minimum.`
    );

    // EXP-2 : bearish aligné
    if (!vexBull && !dexBull && !gexBull) return mk(
      'EXP-2','PRÉPARATION EXPLOSIVE — BIAIS BAISSIER','explosive','#ef4444',
      'ÉLEVÉE','CRITIQUE','SHORT',
      `Gamma Flip à ${fmtFlip}. VEX-/CEX-/GEX- actifs : les dealers sont en mode vente et amplifient. Un passage sous le flip déclenche la cascade.`,
      `Raison : VEX-/GEX- baissiers + DEX+ (pression) + flip imminent = cascade down probable.`,
      `Un trader pro se positionne SHORT ou achète des puts courte échéance. Stop serré au-dessus du flip. Vendre les résistances, pas les bas.`
    );

    // EXP-3b : retournement en cours + flip proche
    if (vexTurning && cexTurning) return mk(
      'EXP-3b','EXPLOSION — RETOURNEMENT HAUSSIER EN COURS','explosive','#f97316',
      'MODÉRÉE','CRITIQUE','LONG',
      `Gamma Flip à ${fmtFlip}. VEX- mais ${arr("up")} + CEX- mais ${arr("up")} : les forces baissières se retournent simultanément avec le flip tout proche. Rebond explosif possible.`,
      `Raison : double retournement VEX+CEX + flip < 2% = rebond potentiellement violent.`,
      `Un trader pro prépare un LONG conditionnel : entre uniquement sur clôture au-dessus du flip avec confirmation des deux trends. Stop sous le low du jour.`
    );

    // EXP-3 : ambiguïté directionnelle
    if (vexBull !== dexBull) return mk(
      'EXP-3','PRÉPARATION EXPLOSIVE — DIRECTION INCONNUE','explosive','#f59e0b',
      'MODÉRÉE','CRITIQUE','STRANGLE',
      `Gamma Flip à ${fmtFlip}. VEX et DEX se contredisent sur la direction. Le mouvement sera violent — le sens reste incertain.`,
      `Raison : explosion certaine, direction inconnue = strangle obligatoire.`,
      `Un trader pro achète un strangle proche échéance dimensionné pour rentabiliser un move ≥ 5% dans n’importe quel sens.`
    );

    // EXP-4b : momentum confirmé → actionnable
    if ((vexStrength || vexUp) && (cexStrength || cexUp)) return mk(
      'EXP-4b','PRÉPARATION EXPLOSIVE — MOMENTUM CONFIRMÉ','explosive','#fb923c',
      'MODÉRÉE','CRITIQUE','LONG',
      `Gamma Flip à ${fmtFlip}. Les trends VEX ${arr(vexTrend)} et CEX ${arr(cexTrend)} confirment un momentum haussier même si les signes restent mixtes.`,
      `Raison : EXP + trends VEX/CEX confirmés = momentum suffisant pour entrer.`,
      `Un trader pro entre LONG à demi-taille sur cassure du flip, stop sous le low récent. Ne pas attendre une confirmation parfaite.`
    );

    // EXP-4 : pas de momentum
    return mk(
      'EXP-4','PRÉPARATION EXPLOSIVE — EN ATTENTE','explosive','#f59e0b',
      'MODÉRÉE','ÉLEVÉE','PATIENCE',
      `Gamma Flip à ${fmtFlip}. Conditions explosives réunies mais momentum GEX/DEX/VEX/CEX absent. Le déclencheur n’est pas encore arrivé.`,
      `Raison : setup explosif confirmé mais sans momentum = risque de fakeout.`,
      `Un trader pro place une alerte au flip exact et entre UNIQUEMENT sur clôture horaire au-delà. Pas d’entrée anticipée.`
    );
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 2 — FULL BEAR (4/4 baissiers)               [FB-1..5]
  // ══════════════════════════════════════════════════════════════════
  if (!vexBull && !cexBull && !gexBull && !dexBull) {

    // FB-2b : bear + vex/cex qui s'approfondissent
    if (vexDeep && cexDeep && gexDown) return mk(
      'FB-2b','ACCÉLÉRATION BAISSIÈRE — MAXIMUM BEARISH','acceleration','#7f1d1d',
      'ÉLEVÉE','CRITIQUE','SHORT',
      `4/4 baissiers + VEX ${arr("down")} + CEX ${arr("down")} + GEX ${arr("down")} : les trois forces baissières s’intensifient simultanément. Configuration la plus dangereuse pour être long.`,
      `Raison : triple deepening VEX/CEX/GEX = momentum baissier maximal.`,
      `Un trader pro ENTRE SHORT à taille maximale ou achète des puts OTM. Chaque rebond est une opportunité de renforcer. Objectifs : supports majeurs.`
    );

    // FB-2 : gex trend down
    if (gexDown) return mk(
      'FB-2','ACCÉLÉRATION BAISSIÈRE — RENFORCÉE','acceleration','#dc2626',
      'ÉLEVÉE','CRITIQUE','SHORT',
      `4/4 baissiers ET GEX ${arr("down")}. VEX-/CEX- : les dealers vendent mécaniquement. GEX- qui plonge : l’amplification augmente. Chaque baisse peut s’emballer.`,
      `Raison : 4/4 baissiers + GEX qui accélère = conditions les plus dangereuses pour être long.`,
      `Un trader pro RENFORCE le SHORT ou achète des puts supplémentaires. Profits partiels aux supports en chemin.`
    );

    // FB-1b : bear mais vex/cex qui se retournent
    if (vexTurning && cexTurning) return mk(
      'FB-1b','ACCÉLÉRATION BAISSIÈRE — RETOURNEMENT VEX/CEX','acceleration','#f87171',
      'MODÉRÉE','NORMALE','SHORT',
      `4/4 baissiers MAIS VEX ${arr("up")} et CEX ${arr("up")} : les deux flows options commencent à se retourner. La pression baissière de fond existe encore mais perd son moteur.`,
      `Raison : 4/4 bear mais double retournement VEX+CEX = prendre les profits, ne pas renforcer.`,
      `Un trader pro SORT 50% du SHORT immédiatement. Garde le reste avec stop serré. Surveille si les signes VEX/CEX basculent en positif.`
    );

    // FB-1 : gex trend up
    if (gexUp) return mk(
      'FB-1','ACCÉLÉRATION BAISSIÈRE — FREIN EN FORMATION','acceleration','#f87171',
      'MODÉRÉE','ÉLEVÉE','SHORT',
      `4/4 baissiers mais GEX ${arr("up")}. La pression vendeuse est intacte mais les dealers réduisent l’amplification. Un rebond technique est possible.`,
      `Raison : 4/4 bear mais GEX remonte = momentum baissier qui s’essouffle légèrement.`,
      `Un trader pro RÉDUIT le SHORT de moitié, serre le stop, attend une nouvelle consolidation avant de re-shorter.`
    );

    // FB-3 : flat
    return mk(
      'FB-3','ACCÉLÉRATION BAISSIÈRE — STABLE','acceleration','#ef4444',
      'ÉLEVÉE','ÉLEVÉE','SHORT',
      `4/4 baissiers, régime stable ${arr("flat")}. VEX-/CEX- forcent les dealers à vendre. GEX- amplifie. DEX+ n’apporte aucun soutien.`,
      `Raison : 4/4 bear régime établi = tenir les shorts sans stress.`,
      `Un trader pro MAINTIENT le SHORT avec stop au-dessus du dernier swing high.`
    );
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 3 — FULL BULL (4/4 haussiers)               [FL-1..5]
  // ══════════════════════════════════════════════════════════════════
  if (vexBull && cexBull && gexBull && dexBull) {

    // FL-2b : bull + vex/cex qui s'accélèrent
    if (vexStrength && cexStrength && gexUp) return mk(
      'FL-2b','RALENTISSEMENT HAUSSIER — MAXIMUM BULLISH','slowdown','#14532d',
      'ÉLEVÉE','NORMALE','LONG',
      `4/4 haussiers + VEX ${arr("up")} + CEX ${arr("up")} + GEX ${arr("up")} : les trois forces haussières s’intensifient. Configuration la plus solide possible. Les corrections seront minimes et brèves.`,
      `Raison : triple renforcement VEX/CEX/GEX = structure haussière auto-renforcée.`,
      `Un trader pro entre LONG à taille maximale. Tient sans modifier. Invalide uniquement si GEX passe en négatif.`
    );

    // FL-2 : gex up
    if (gexUp) return mk(
      'FL-2','RALENTISSEMENT HAUSSIER — RENFORCÉ','slowdown','#22c55e',
      'ÉLEVÉE','NORMALE','LONG',
      `4/4 haussiers ET GEX ${arr("up")}. VEX+/CEX+ : les dealers achètent mécaniquement. GEX+ croissant : le pin haussier se renforce. DEX- : support sur les dips.`,
      `Raison : 4/4 haussiers + GEX croissant = structure la plus solide possible.`,
      `Un trader pro RENFORCE le LONG avec confiance. Ajoute sur cassure de résistance. Invalide si GEX passe en négatif.`
    );

    // FL-1b : bull mais vex/cex qui s'estompent
    if (vexFading && cexFading) return mk(
      'FL-1b','RALENTISSEMENT HAUSSIER — SORTIE PRÉCOCE','slowdown','#bbf7d0',
      'MODÉRÉE','NORMALE','LONG',
      `4/4 haussiers MAIS VEX ${arr("down")} et CEX ${arr("down")} : les deux flows options s’affaiblissent simultanément. La structure tient encore mais le moteur s’éteint.`,
      `Raison : double fading VEX+CEX malgré 4/4 bull = sortir avant les autres.`,
      `Un trader pro PREND 50% des profits immédiatement. Élève le stop agressivement. Ne renforce pas. Surveille si un signe bascule en négatif.`
    );

    // FL-1 : gex down
    if (gexDown) return mk(
      'FL-1','RALENTISSEMENT HAUSSIER — FRAGILE','slowdown','#84cc16',
      'MODÉRÉE','NORMALE','LONG',
      `4/4 haussiers mais GEX ${arr("down")}. La structure tient encore mais la force stabilisatrice s’érode.`,
      `Raison : 4/4 bull mais GEX décroissant = surveillance accrue.`,
      `Un trader pro PREND 30% des profits, élève le stop sous le dernier support. Ne renforce pas.`
    );

    // FL-3 : flat
    return mk(
      'FL-3','RALENTISSEMENT HAUSSIER — STABLE','slowdown','#4ade80',
      'ÉLEVÉE','NORMALE','LONG',
      `4/4 haussiers, régime stable. VEX+/CEX+ font acheter les dealers. GEX+/DEX- amortissent les corrections. Mécanique de soutien pleinement opérationnelle.`,
      `Raison : 4/4 bull régime stable = tenir sans modifier.`,
      `Un trader pro MAINTIENT le LONG sans changement. Surveille uniquement une cassure sous le Gamma Flip.`
    );
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 4 — COMPRESSION VEX≠CEX                     [COMP-0..6]
  // ══════════════════════════════════════════════════════════════════
  if (vexBull !== cexBull) {

    // COMP-0 : flip proche = pré-explosif
    if (nearFlip5) return mk(
      'COMP-0','COMPRESSION PRÉ-EXPLOSIVE','compression','#a855f7',
      'ÉLEVÉE','CRITIQUE','STRANGLE',
      `VEX et CEX s’opposent ET Gamma Flip ${fmtFlip} (zone < 5%). Double tension : forces contradictoires + proximité du flip. Breakout violent imminent dans un sens ou l’autre.`,
      `Raison : compression interne + flip proche = énergie accumulée sans direction.`,
      `Un trader pro achète un strangle ou straddle. Le Gamma Flip est le déclencheur à surveiller.`
    );

    // COMP-5 : compression EN FORMATION (trends divergents)
    if (vexTrend !== 'flat' && cexTrend !== 'flat' && vexTrend !== cexTrend) return mk(
      'COMP-5','COMPRESSION EN FORMATION — PRÉPARER LE STRANGLE','compression','#c084fc',
      'MODÉRÉE','ÉLEVÉE','STRANGLE',
      `VEX ${arr(vexTrend)} et CEX ${arr(cexTrend)} : les deux trends divergent. Les signes ne sont pas encore opposés mais ils y vont. La compression se forme — agir avant qu’elle soit établie.`,
      `Raison : trends VEX/CEX divergents = compression en construction = acheter la vol maintenant avant qu’elle monte.`,
      `Un trader pro ACHÈTE le strangle MAINTENANT pendant que la vol est encore bon marché. Plus rentable qu’attendre la compression établie.`
    );

    // COMP-1 : VEX+/CEX-/GEX+/DEX-
    if (vexBull && !cexBull && gexBull && dexBull) return mk(
      'COMP-1','COMPRESSION TEMPORELLE — BIAIS HAUSSIER','compression','#8b5cf6',
      'MODÉRÉE','NORMALE','LONG',
      `VEX+ (vol haussière) mais CEX- (temps baissier). GEX+/DEX- stabilisent. Tension vol vs temps avec socle haussier. Résolution probable sur mouvement de vol ou expiration.`,
      `Raison : vanna haussière domine sur gamma/dex stables = biais long spot, pas via options.`,
      `Un trader pro est LONG en spot/futures. Évite les calls (charm détruit leur valeur). Surveille les expirations comme déclencheur.`
    );

    // COMP-2 : VEX-/CEX+/GEX-/DEX+
    if (!vexBull && cexBull && !gexBull && !dexBull) return mk(
      'COMP-2','COMPRESSION TEMPORELLE — BIAIS BAISSIER','compression','#7c3aed',
      'MODÉRÉE','NORMALE','SHORT',
      `CEX+ (temps haussier) mais VEX- (vol baissière). GEX-/DEX+ amplifient. Charm positif mais tout le reste pousse à la baisse. Spike de vol déclenche les ventes.`,
      `Raison : charm seul face à vanna/gex/dex baissiers = biais short en spot/futures.`,
      `Un trader pro est SHORT en spot/futures. Évite les puts (charm limite leur dégradation). Vigilant sur les spikes de vol.`
    );

    // COMP-3 : VEX+/CEX-/GEX-/DEX+
    if (vexBull && !cexBull && !gexBull && !dexBull) return mk(
      'COMP-3','COMPRESSION — TENSION NON RÉSOLUE','compression','#9333ea',
      'FAIBLE','NORMALE','PATIENCE',
      `VEX+ contre CEX-/GEX-/DEX+. Un signal haussier contre trois baissiers. Les dealers reçoivent des signaux contradictoires selon le niveau de vol.`,
      `Raison : un seul signal positif contre trois négatifs = pas d’edge directionnel.`,
      `Un trader pro ATTEND. Surveille si VEX+ s’amplifie (bigExp) pour valider la direction.`
    );

    // COMP-4 : VEX-/CEX+/GEX+/DEX-
    return mk(
      'COMP-4','COMPRESSION — STABLE CONDITIONNELLE','compression','#a78bfa',
      'FAIBLE','NORMALE','PATIENCE',
      `VEX- contre CEX+/GEX+/DEX-. GEX et DEX soutiennent mais la vanna négative menace si la vol monte. Conditionnellement stable.`,
      `Raison : structure conditionnellement haussière mais VEX- est une bombe si la vol monte.`,
      `Un trader pro entre LONG uniquement si la vol reste basse. Stop sous le support DEX.`
    );
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 5 — DIVERGENCE GEX vs VEX+CEX consensus     [DIV-1..6]
  // ══════════════════════════════════════════════════════════════════
  if (vexBull === cexBull && gexBull !== vexBull) {

    // DIV-1b : GEX- + double renforcement baissier options
    if (!gexBull && vexBull && cexBull && dexBull && vexStrength && cexStrength) return mk(
      'DIV-1b','DIVERGENCE — LONG HAUSSIER RENFORCÉ','divergence','#ca8a04',
      'MODÉRÉE','ÉLEVÉE','LONG',
      `VEX ${arr("up")}/CEX ${arr("up")} : les deux flows options s’accélèrent. GEX- amplifie — les corrections seront violentes MAIS les hausses aussi. Triple haussier options avec amplification.`,
      `Raison : VEX+↑/CEX+↑/DEX- avec GEX- = mouvement haussier potentiellement explosif.`,
      `Un trader pro entre LONG à taille réduite avec protection put obligatoire. Profite de l’amplification GEX- à la hausse mais se couvre contre le retournement.`
    );

    // DIV-1 : GEX- vs VEX+/CEX+
    if (!gexBull && vexBull && cexBull && dexBull) return mk(
      'DIV-1','DIVERGENCE — HAUSSIER AVEC GAMMA AMPLIFIÉ','divergence','#eab308',
      'MODÉRÉE','ÉLEVÉE','LONG',
      `VEX+/CEX+/DEX- haussiers mais GEX- amplifie tous les moves. Direction haussière mais chaque correction sera exagérée.`,
      `Raison : 3/4 haussiers mais GEX- = corrections exagérées = protection obligatoire.`,
      `Un trader pro est LONG spot avec puts protection (≤2% OTM). Stop plus large que la normale.`
    );

    // DIV-2b : GEX+ pin + vex/cex qui s'approfondissent
    if (gexBull && !vexBull && !cexBull && !dexBull && vexDeep && cexDeep) return mk(
      'DIV-2b','DIVERGENCE — TEMPÊTE IMMINENTE SOUS LE PIN','divergence','#92400e',
      'ÉLEVÉE','CRITIQUE','PATIENCE',
      `GEX+ pin le marché mais VEX ${arr("down")}/CEX ${arr("down")} : les deux flows baissiers s’accélèrent sous la surface. La pression s’accumule. Quand le pin cède, le move sera très violent.`,
      `Raison : GEX+ retient mais VEX/CEX deepening = tempête sous tension = préparer le SHORT.`,
      `Un trader pro accumule des PUTS à faible coût pendant le pin. Prépare un SHORT conditionnel déclenché sur cassure du GEX+. Position sizing maximal.`
    );

    // DIV-2 : GEX+ vs VEX-/CEX-
    if (gexBull && !vexBull && !cexBull && !dexBull) return mk(
      'DIV-2','DIVERGENCE — CALME ARTIFICIEL','divergence','#ca8a04',
      'MODÉRÉE','ÉLEVÉE','PATIENCE',
      `GEX+ pin le marché artificiellement. VEX-/CEX-/DEX+ signalent une pression vendeuse profonde. Calme trompeur avant tempête.`,
      `Raison : GEX+ maintient la stabilité mais flows options intégralement baissiers = temporaire.`,
      `Un trader pro accumule des PUTS à faible coût pendant le pin. Attend l’expiration ou cassure du GEX+.`
    );

    // DIV-3 : GEX- vs mixed
    if (!gexBull && vexBull && !cexBull) return mk(
      'DIV-3','DIVERGENCE — VOL PLAY AMPLIFIÉ','divergence','#d97706',
      'MODÉRÉE','ÉLEVÉE','STRANGLE',
      `GEX- amplifie, VEX+ haussier mais CEX- baissier. Volatilité accrue sans direction nette — le marché peut partir violemment dans les deux sens.`,
      `Raison : GEX amplificateur + signaux mixtes = strangle idéal.`,
      `Un trader pro achète un strangle et gère le delta au fil du mouvement.`
    );

    // DIV-4 : GEX+ contre mixte
    return mk(
      'DIV-4','DIVERGENCE — PIN TEMPORAIRE','divergence','#b45309',
      'FAIBLE','NORMALE','PATIENCE',
      `GEX+ stabilise mais tension sous-jacente mixte. Le pin est temporaire.`,
      `Raison : GEX+ retient mais pression sous-jacente réelle = attendre le signal.`,
      `Un trader pro prépare un SHORT conditionnel déclenché si GEX_trend passe à ’down’. Zéro position en attendant.`
    );
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 6 — MODERATE BULL (3/4 haussiers)            [MOD-B1..8]
  // ══════════════════════════════════════════════════════════════════
  if (bullCount === 3) {

    // Signal divergent = GEX-
    if (!gexBull) {
      if (vexStrength && cexStrength) return mk(
        'MOD-B1b','HAUSSIER MODÉRÉ — OPTIONS ACCÉLÈRENT','slowdown','#22c55e',
        'MODÉRÉE','ÉLEVÉE','LONG',
        `VEX ${arr("up")}/CEX ${arr("up")} : les flows options s’accélèrent côté haussier malgré GEX-. La dynamique options va probablement forcer GEX à remonter sous peu.`,
        `Raison : VEX+↑/CEX+↑ avec GEX- = pression pour que GEX remonte = position longue avec conviction croissante.`,
        `Un trader pro entre LONG à taille normale. Le stop reste plus large à cause du GEX- mais la conviction est là. Surveille GEX pour renforcer.`
      );
      return mk(
        'MOD-B1','HAUSSIER MODÉRÉ — GAMMA AMPLIFIE','slowdown','#4ade80',
        'MODÉRÉE','ÉLEVÉE','LONG',
        `VEX+/CEX+/DEX- haussiers mais GEX- amplifie tous les moves. Direction haussière, corrections exagérées.`,
        `Raison : 3/4 bull mais GEX- = corrections exagérées = stop plus large obligatoire.`,
        `Un trader pro est LONG avec stop 30% plus large que d’habitude. Size réduit de 20%.`
      );
    }

    // Signal divergent = DEX+
    if (!dexBull) return mk(
      'MOD-B2','HAUSSIER MODÉRÉ — REBONDS VENDUS','slowdown','#86efac',
      'MODÉRÉE','NORMALE','LONG',
      `VEX+/CEX+/GEX+ haussiers mais DEX+ : dealers vendent les rebonds. Hausse en escalier avec pullbacks fréquents.`,
      `Raison : 3/4 bull mais DEX+ = acheter les dips, pas chasser les hausses.`,
      `Un trader pro ACHÈTE LES DIPS. Prend profits rapides sur chaque leg. Évite le levier sur les sommets.`
    );

    // Signal divergent = CEX-
    if (!cexBull) {
      if (cexTurning) return mk(
        'MOD-B3b','HAUSSIER MODÉRÉ — CHARM QUI SE RETOURNE','slowdown','#4ade80',
        'MODÉRÉE','NORMALE','LONG',
        `VEX+/GEX+/DEX- haussiers + CEX- MAIS cexTrend ${arr("up")} : le charm baissier est en train de se retourner. Dans quelques heures il pourrait passer positif et donner le signal 4/4.`,
        `Raison : 3/4 bull + CEX- qui se retourne = structure qui s’améliore = bon timing d’entrée.`,
        `Un trader pro entre LONG maintenant en anticipation du passage CEX+. Stop sous le dernier support. Objectif : FL-3 dans les 24-48h.`
      );
      if (gexUp) return mk(
        'MOD-B3','HAUSSIER MODÉRÉ — COURT TERME SEULEMENT','slowdown','#4ade80',
        'MODÉRÉE','NORMALE','LONG',
        `VEX+/GEX+${arr("up")}/DEX- haussiers mais CEX- : le temps travaille contre. GEX monte — bon court terme.`,
        `Raison : 3/4 bull + CEX- + GEX qui monte = long court terme uniquement.`,
        `Un trader pro prend des CALLS COURTE ÉCHÉANCE (< 2 semaines) ou trade le spot sur 24-48h max.`
      );
      return mk(
        'MOD-B4','HAUSSIER MODÉRÉ — STRUCTURE EN DÉGRADATION','slowdown','#a3e635',
        'FAIBLE','NORMALE','PATIENCE',
        `VEX+/GEX+/DEX- haussiers mais CEX- ET GEX ${arr(gexTrend)}. Double érosion charm + GEX.`,
        `Raison : 3/4 bull mais CEX- + GEX décroissant = double érosion = sécuriser les gains.`,
        `Un trader pro SÉCURISE 30% des gains. Attend stabilisation du GEX trend avant de recharger.`
      );
    }

    // Signal divergent = VEX-
    if (!vexBull) {
      if (vexTurning) return mk(
        'MOD-B5b','HAUSSIER MODÉRÉ — VANNA QUI SE RETOURNE','slowdown','#4ade80',
        'MODÉRÉE','NORMALE','LONG',
        `CEX+/GEX+/DEX- haussiers + VEX- MAIS vexTrend ${arr("up")} : la vanna baissière se retourne. Potentiel de passage à 4/4 haussiers.`,
        `Raison : 3/4 bull + VEX- qui remonte = signal d’amélioration = renforcer en anticipation.`,
        `Un trader pro renforce légèrement le LONG en anticipation du passage VEX+. Stop sous support récent.`
      );
      if (dexDown) return mk(
        'MOD-B5','HAUSSIER MODÉRÉ — SPOT SANS OPTIONS','slowdown','#4ade80',
        'MODÉRÉE','NORMALE','LONG',
        `CEX+/GEX+/DEX- haussiers mais VEX- : toute hausse de vol détruira les calls.`,
        `Raison : 3/4 bull mais VEX- = options chères et inefficaces = trader le sous-jacent direct.`,
        `Un trader pro est LONG en spot ou futures uniquement. Ignore les calls dans ce régime.`
      );
      return mk(
        'MOD-B6','HAUSSIER MODÉRÉ — SUPPORT QUI S’AFFAIBLIT','slowdown','#86efac',
        'FAIBLE','NORMALE','LONG',
        `CEX+/GEX+/DEX- haussiers mais VEX- et DEX trend ${arr(dexTrend)}.`,
        `Raison : 3/4 bull mais VEX- + DEX support en diminution = stop proche obligatoire.`,
        `Un trader pro TIENT le long avec stop juste sous le dernier support DEX. Pas de renforcement.`
      );
    }
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 7 — MODERATE BEAR (3/4 baissiers)            [MOD-S1..8]
  // ══════════════════════════════════════════════════════════════════
  if (bullCount === 1) {

    // Signal divergent = GEX+
    if (gexBull) {
      if (vexTurning && cexTurning) return mk(
        'MOD-S1b','BAISSIER MODÉRÉ — RETOURNEMENT OPTIONS','acceleration','#fca5a5',
        'MODÉRÉE','NORMALE','PATIENCE',
        `VEX-/CEX-/DEX+ baissiers mais GEX+ stabilise ET VEX ${arr("up")}/CEX ${arr("up")} : les flows options commencent à remonter. Signal de retournement précoce.`,
        `Raison : 3/4 bear mais double retournement VEX+CEX + GEX stabilisateur = ne pas shorter ici.`,
        `Un trader pro FERME les shorts existants. Surveille le passage VEX/CEX en positif pour initier un long.`
      );
      return mk(
        'MOD-S1','BAISSIER MODÉRÉ — GAMMA AMORTIT','acceleration','#f87171',
        'MODÉRÉE','NORMALE','SHORT',
        `VEX-/CEX-/DEX+ baissiers mais GEX+ pin le marché. Baisse graduelle avec rebonds violents.`,
        `Raison : 3/4 bear mais GEX+ = baisse en escalier = vendre les rebonds.`,
        `Un trader pro VEND LES REBONDS vers les strikes de forte OI.`
      );
    }

    // Signal divergent = DEX-
    if (dexBull) return mk(
      'MOD-S2','BAISSIER MODÉRÉ — SUPPORTS TENACES','acceleration','#fca5a5',
      'MODÉRÉE','NORMALE','SHORT',
      `VEX-/CEX-/GEX- baissiers mais DEX- amène des acheteurs mécaniques sur les dips.`,
      `Raison : 3/4 bear mais DEX- = supports qui tiennent = prendre profits aux supports.`,
      `Un trader pro PREND PROFITS PARTIELS aux niveaux support DEX. Re-shorte sur les rebonds.`
    );

    // Signal divergent = CEX+
    if (cexBull) {
      if (cexFading) return mk(
        'MOD-S3b','BAISSIER MODÉRÉ — CHARM QUI S’EFFACE','acceleration','#ef4444',
        'ÉLEVÉE','ÉLEVÉE','SHORT',
        `VEX-/GEX-/DEX+ baissiers + CEX+ MAIS cexTrend ${arr("down")} : le seul frein au bearish est en train de céder. Quand CEX passe négatif, c’est FB-3.`,
        `Raison : 3/4 bear + CEX+ qui fane = transition vers full bear imminente = renforcer maintenant.`,
        `Un trader pro RENFORCE le SHORT en anticipation du passage CEX-. La fenêtre d’entrée optimale est maintenant.`
      );
      if (gexDown) return mk(
        'MOD-S3','BAISSIER MODÉRÉ — ACCÉLÉRATION','acceleration','#ef4444',
        'ÉLEVÉE','ÉLEVÉE','SHORT',
        `VEX-/GEX-${arr("down")}/DEX+ baissiers, CEX+ ralentit mais GEX s’aggrave.`,
        `Raison : 3/4 bear + GEX qui plonge = accélération = renforcer le short.`,
        `Un trader pro RENFORCE le SHORT ou ajoute des puts à chaque rebond faible.`
      );
      return mk(
        'MOD-S4','BAISSIER MODÉRÉ — MOMENTUM QUI S’ÉPUISE','acceleration','#fca5a5',
        'MODÉRÉE','NORMALE','SHORT',
        `VEX-/GEX-/DEX+ baissiers, CEX+ et GEX ${arr(gexTrend)}.`,
        `Raison : 3/4 bear mais GEX remonte + CEX+ = momentum baissier s’épuise = réduire.`,
        `Un trader pro PREND 50% des profits short. Attend si GEX redevient positif.`
      );
    }

    // Signal divergent = VEX+
    if (vexBull) {
      if (vexFading) return mk(
        'MOD-S5b','BAISSIER MODÉRÉ — VANNA QUI CÈDE','acceleration','#ef4444',
        'MODÉRÉE','ÉLEVÉE','SHORT',
        `CEX-/GEX-/DEX+ baissiers + VEX+ MAIS vexTrend ${arr("down")} : le seul signal haussier restant s’affaiblit. Transition vers 4/4 bear en cours.`,
        `Raison : 3/4 bear + VEX+ qui fane = renforcer le short avant le passage complet.`,
        `Un trader pro RENFORCE le SHORT progressivement. La vanna en baisse confirme la direction.`
      );
      if (dexUp) return mk(
        'MOD-S5','BAISSIER MODÉRÉ — PRESSION CROISSANTE','acceleration','#f87171',
        'MODÉRÉE','ÉLEVÉE','SHORT',
        `CEX-/GEX-/DEX+ baissiers, VEX+ limite mais DEX trend ${arr("up")} augmente la pression.`,
        `Raison : 3/4 bear + DEX pressure qui monte = short valide malgré VEX+.`,
        `Un trader pro est SHORT avec stop serré. Size réduit de 20% à cause du VEX+.`
      );
      return mk(
        'MOD-S6','BAISSIER MODÉRÉ — MOMENTUM DOUTEUX','acceleration','#fca5a5',
        'FAIBLE','NORMALE','PATIENCE',
        `CEX-/GEX-/DEX+ baissiers mais VEX+ et DEX trend ${arr(dexTrend)}.`,
        `Raison : 3/4 bear mais VEX+ + DEX pression qui baisse = ne pas initier de short.`,
        `Un trader pro ATTEND. Ne rentre pas short ici. Surveille si VEX+ prend le dessus.`
      );
    }
  }

  // ══════════════════════════════════════════════════════════════════
  // GROUPE 8 — NEUTRAL (2/4)                            [NEU-1..6]
  // ══════════════════════════════════════════════════════════════════

  // NEU-5 : neutralité avec trends convergents haussiers → breakout bull
  if (bullCount === 2 && vexUp && cexUp && gexUp) return mk(
    'NEU-5','NEUTRALITÉ — BREAKOUT HAUSSIER EN PRÉPARATION','neutral','#22c55e',
    'MODÉRÉE','ÉLEVÉE','LONG',
    `2/4 signaux haussiers MAIS VEX ${arr("up")}/CEX ${arr("up")}/GEX ${arr("up")} : les trois trends convergent à la hausse. Les signes retarderont mais les trends annoncent la rupture.`,
    `Raison : neutralité des signes mais triple trend haussier = breakout bull probable dans 2-4h.`,
    `Un trader pro prend une POSITION LONG LÉGÈRE (25% taille) en anticipation. Stop sous support. La rupture des signes sera la confirmation pour renforcer.`
  );

  // NEU-6 : neutralité avec trends convergents baissiers → breakout bear
  if (bullCount === 2 && vexDown && cexDown && gexDown) return mk(
    'NEU-6','NEUTRALITÉ — BREAKOUT BAISSIER EN PRÉPARATION','neutral','#ef4444',
    'MODÉRÉE','ÉLEVÉE','SHORT',
    `2/4 signaux haussiers MAIS VEX ${arr("down")}/CEX ${arr("down")}/GEX ${arr("down")} : les trois trends convergent à la baisse. Les signes retarderont mais les trends annoncent la rupture.`,
    `Raison : neutralité des signes mais triple trend baissier = breakout bear probable dans 2-4h.`,
    `Un trader pro prend une POSITION SHORT LÉGÈRE (25% taille) en anticipation. Stop au-dessus résistance. La rupture des signes sera la confirmation pour renforcer.`
  );

  if (bullCount === 2) {
    if (vexBull === cexBull && gexBull === dexBull && vexBull !== gexBull) {
      if (!vexBull && gexBull) return mk(
        'NEU-1','NEUTRALITÉ — VENDRE LA VOL','neutral','#64748b',
        'MODÉRÉE','NORMALE','STRANGLE',
        `VEX-/CEX- vs GEX+/DEX-. Deux forces s’annulent. Marché en range comprimé.`,
        `Raison : forces opposées qui s’équilibrent = pas de direction = collecter la prime.`,
        `Un trader pro VEND des strangles à faible delta (≥15% OTM) et collecte la prime.`
      );
      return mk(
        'NEU-2','NEUTRALITÉ — ACHETER LA VOL','neutral','#6366f1',
        'MODÉRÉE','ÉLEVÉE','STRANGLE',
        `VEX+/CEX+ vs GEX-/DEX+. Options haussières + GEX amplificateur.`,
        `Raison : options haussières + GEX amplificateur = vol accrue sans direction = acheter la vol.`,
        `Un trader pro ACHÈTE un strangle et profite de l’amplification GEX dans un sens ou l’autre.`
      );
    }
    if (vexBull && gexBull && !cexBull && !dexBull) return mk(
      'NEU-3','NEUTRALITÉ — ATTENDRE LE CHARM','neutral','#475569',
      'FAIBLE','NORMALE','PATIENCE',
      `VEX+/GEX+ soutiennent, CEX-/DEX+ tirent à la baisse. CEX trend ou GEX trend donnera la direction.`,
      `Raison : 2/2 forces opposées = attendre que le charm se résolve.`,
      `Un trader pro ATTEND que CEX_trend donne un signal. Prépare les deux scénarios.`
    );
    if (!vexBull && !gexBull && cexBull && dexBull) return mk(
      'NEU-4','NEUTRALITÉ — ATTENDRE LA RÉSOLUTION','neutral','#334155',
      'FAIBLE','NORMALE','PATIENCE',
      `VEX-/GEX- amplifient la baisse, CEX+/DEX- soutiennent. Tension non résolue.`,
      `Raison : forces baissières et haussières en guerre = direction inconnue.`,
      `Un trader pro PRÉPARE les deux breakouts avec ordres stops dans les deux directions.`
    );
  }

  // ══════════════════════════════════════════════════════════════════
  // CATCH-ALL
  // ══════════════════════════════════════════════════════════════════
  return mk(
    'NEUTRAL','INDÉCISION — SIGNAUX MIXTES','neutral','#94a3b8',
    'FAIBLE','NORMALE','PATIENCE',
    `Les quatre indicateurs ne dégagent pas de consensus. Les forces haussières et baissières s’équilibrent partiellement.`,
    `Raison : pas de consensus = pas d’edge directionnel = rester à l’écart.`,
    `Un trader pro RÉDUIT L’EXPOSITION et attend un alignement d’au moins 3/4 signaux.`
  );
}

export function buildLevelsContext(btcSpot, lvlFlip, lvlHaut, lvlBas, mpStrike, mpDte, mpExpiry, flipDistPct, vexBull, cexBull) {
  const fmtP = v => v ? '$' + Math.round(v).toLocaleString() : null;
  const lines = [];
  const THRESH = CFG.LEVELS_NEAR_THRESH;
  const near = (a, b) => a && b && Math.abs(a - b) / b < THRESH;

  // ── 1. Glossaire des roles ───────────────────────────────────────────
  const roles = [];
  if (lvlFlip)  roles.push('<b style="color:#f59e0b">' + fmtP(lvlFlip) + '</b> = Gamma Flip : niveau o\u00f9 les dealers <i>changent de comportement</i> m\u00e9caniquement');
  if (mpStrike) roles.push('<b style="color:#3d8eff">' + fmtP(mpStrike) + '</b> = Max Pain (' + (mpExpiry||'') + ') : niveau vers lequel le march\u00e9 <i>gravite</i> \u00e0 l\u2019approche de l\u2019expiration');
  if (lvlHaut)  roles.push('<b style="color:#22c55e">' + fmtP(lvlHaut) + '</b> = Call wall : <i>r\u00e9sistance</i> o\u00f9 les vendeurs d\u2019options sont concentr\u00e9s');
  if (lvlBas)   roles.push('<b style="color:#ef4444">' + fmtP(lvlBas) + '</b> = Put wall : <i>support</i> o\u00f9 les acheteurs de puts sont concentr\u00e9s');
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
    const dteTxt = mpDte === 0 ? 'aujourd\u2019hui' : mpDte === 1 ? 'demain' : mpDte !== null ? 'dans ' + mpDte + 'j' : '';
    const expTxt = mpExpiry ? ' (' + mpExpiry + ')' : '';
    lines.push(
      '&#9888;&#65039; <b>Convergence triple</b> autour de ' + fmtP(refPrice) + ' : Gamma Flip + Max Pain + ' +
      (flipNearBas ? 'Put wall' : 'Call wall') + ' sont tous dans la m\u00eame zone. ' +
      (dteTxt ? 'Expiration ' + dteTxt + expTxt + ' \u2014 ' : '') +
      'le GEX remonte m\u00e9caniquement vers z\u00e9ro car les d\u00e9alers d\u00e9nouent leurs positions avant fixing. ' +
      'Ce n\u2019est <b>pas un signal bull</b> : c\u2019est le march\u00e9 qui gravite vers son point d\u2019expiration naturel.'
    );
  } else if (convergingCount === 2 && refPrice) {
    const which = flipNearMp   ? 'Gamma Flip et Max Pain' :
                  flipNearBas  ? 'Gamma Flip et Put wall' :
                  flipNearHaut ? 'Gamma Flip et Call wall' :
                  mpNearBas    ? 'Max Pain et Put wall' :
                  mpNearHaut   ? 'Max Pain et Call wall' :
                  hautNearBas  ? 'Call wall et Put wall (compression)' : 'deux niveaux cl\u00e9s';
    lines.push(
      '&#128204; ' + which + ' convergent en ' + fmtP(refPrice) + '. ' +
      'Cette confluence renforce l\u2019importance de ce niveau comme pivot \u2014 une cassure franche dans un sens d\u00e9clenchera un mouvement plus ample qu\u2019un niveau isol\u00e9.'
    );
  }

  // ── 3. Divergence Flip / Max Pain (de part et d'autre du spot) ───────
  if (lvlFlip && mpStrike && btcSpot && !near(lvlFlip, mpStrike)) {
    const flipAbove = lvlFlip > btcSpot;
    const mpAbove   = mpStrike > btcSpot;
    if (flipAbove !== mpAbove) {
      lines.push(
        '&#128256; <b>Divergence Flip / Max Pain</b> : Gamma Flip (' + fmtP(lvlFlip) + ') et Max Pain (' + fmtP(mpStrike) + ') sont de part et d\u2019autre du spot. ' +
        'Les dealers subissent une traction dans deux directions oppos\u00e9es \u2014 configuration instable, volatilit\u00e9 \u00e9lev\u00e9e probable.'
      );
    } else {
      const distPct = (Math.abs(lvlFlip - mpStrike) / btcSpot * 100).toFixed(1);
      lines.push(
        '&#128204; Gamma Flip (' + fmtP(lvlFlip) + ') et Max Pain (' + fmtP(mpStrike) + ') sont du m\u00eame c\u00f4t\u00e9 mais \u00e9cart\u00e9s de ' + distPct + '%. ' +
        'Le march\u00e9 a deux aimants successifs \u2014 progression probable par paliers.'
      );
    }
  }

  // ── 4. Position du spot par rapport aux niveaux ──────────────────────
  if (btcSpot && lvlFlip) {
    const aboveFlip   = btcSpot > lvlFlip;
    const distFlipPct = (Math.abs(btcSpot - lvlFlip) / btcSpot * 100).toFixed(1);
    if (parseFloat(distFlipPct) < 2) {
      lines.push(
        '&#128293; BTC (' + fmtP(Math.round(btcSpot)) + ') est \u00e0 seulement ' + distFlipPct + '% du Gamma Flip. ' +
        (aboveFlip
          ? 'Tant que le spot tient <b>au-dessus</b> de ' + fmtP(lvlFlip) + ', le GEX reste stabilisateur. ' +
            'Une cl\u00f4ture <b>sous</b> ' + fmtP(lvlFlip) + ' active le r\u00e9gime amplificateur \u2014 les dealers vendent m\u00e9caniquement.'
          : 'Le spot est <b>en dessous</b> du Flip. Les dealers sont en mode amplificateur. ' +
            'Un retour <b>au-dessus</b> de ' + fmtP(lvlFlip) + ' inverserait le r\u00e9gime et d\u00e9clencherait des rachats m\u00e9caniques.')
      );
    } else if (parseFloat(distFlipPct) < 5) {
      lines.push(
        '&#128204; BTC (' + fmtP(Math.round(btcSpot)) + ') est \u00e0 ' + distFlipPct + '% du Gamma Flip ' + fmtP(lvlFlip) + '. ' +
        (aboveFlip
          ? 'Zone de vigilance : le r\u00e9gime stabilisateur tient, mais toute pouss\u00e9e vendeuse br\u00e8ve pourrait inverser la m\u00e9canique des dealers.'
          : 'Zone de vigilance : le r\u00e9gime amplificateur actif, mais un retour rapide au-dessus du Flip inverserait la m\u00e9canique.')
      );
    }
  }

  // ── 5. Spot entre Flip et Call wall (pocket haussier) ────────────────
  if (btcSpot && lvlFlip && lvlHaut && btcSpot > lvlFlip && btcSpot < lvlHaut) {
    const roomPct = ((lvlHaut - btcSpot) / btcSpot * 100).toFixed(1);
    lines.push(
      '&#128204; BTC est dans la <b>pocket haussier</b> : au-dessus du Flip (' + fmtP(lvlFlip) + ') et sous la r\u00e9sistance (' + fmtP(lvlHaut) + '). ' +
      'Espace libre de ' + roomPct + '% avant le Call wall \u2014 les dealers sont stabilisateurs dans cette zone.'
    );
  }

  // ── 6. Spot entre Put wall et Flip (pocket baissier) ─────────────────
  if (btcSpot && lvlBas && lvlFlip && btcSpot < lvlFlip && btcSpot > lvlBas) {
    const roomPct = ((btcSpot - lvlBas) / btcSpot * 100).toFixed(1);
    lines.push(
      '&#9888;&#65039; BTC est dans la <b>zone de pression</b> : sous le Flip (' + fmtP(lvlFlip) + ') et au-dessus du support (' + fmtP(lvlBas) + '). ' +
      'Coussin de ' + roomPct + '% avant le Put wall \u2014 les dealers amplifient les mouvements dans cette zone.'
    );
  }

  // ── 7. Spot sous le Put wall (territoire bearish profond) ─────────────
  if (btcSpot && lvlBas && btcSpot < lvlBas) {
    lines.push(
      '&#9888;&#65039; BTC (' + fmtP(Math.round(btcSpot)) + ') est <b>sous le Put wall</b> ' + fmtP(lvlBas) + '. ' +
      'Zone de d\u00e9livrance des puts \u2014 les vendeurs de puts rachètent des options, ce qui peut cr\u00e9er un rebond technique brutal m\u00eame en tendance baissière.'
    );
  }

  // ── 8. Spot au-dessus du Call wall (territoire breakout) ─────────────
  if (btcSpot && lvlHaut && btcSpot > lvlHaut) {
    lines.push(
      '&#128204; BTC (' + fmtP(Math.round(btcSpot)) + ') est <b>au-dessus du Call wall</b> ' + fmtP(lvlHaut) + '. ' +
      'Territoire de gamma squeeze : les vendeurs de calls rachètent pour se couvrir, amplifiant la hausse m\u00e9caniquement.'
    );
  }

  // ── 9. Max Pain entre Flip et spot ───────────────────────────────────
  if (btcSpot && lvlFlip && mpStrike) {
    const mpBetweenFlipSpot = (lvlFlip < btcSpot && mpStrike > lvlFlip && mpStrike < btcSpot) ||
                               (lvlFlip > btcSpot && mpStrike < lvlFlip && mpStrike > btcSpot);
    if (mpBetweenFlipSpot) {
      lines.push(
        '&#128204; Max Pain (' + fmtP(mpStrike) + ') se trouve <b>entre le Gamma Flip et le spot</b>. ' +
        'Double gravit\u00e9 : le march\u00e9 est attir\u00e9 vers le Max Pain tout en \u00e9tant retenu par la m\u00e9canique du Flip. ' +
        'Oscillation probable autour de ces deux niveaux.'
      );
    }
  }

  // ── 10. Urgence expiration ─────────────────────────────────────────
  if (mpDte !== null && mpDte <= 2 && mpStrike) {
    const urgTxt = mpDte === 0 ? 'AUJOURD\u2019HUI' : mpDte === 1 ? 'DEMAIN' : 'dans 2 jours';
    const expTxt = mpExpiry ? ' (' + mpExpiry + ')' : '';
    lines.push(
      '&#9200; <b>Expiration ' + urgTxt + expTxt + '</b> : le GEX se r\u00e9sorbera m\u00e9caniquement vers z\u00e9ro apr\u00e8s le fixing. ' +
      'Le signal GEX actuel est temporaire \u2014 ne pas le confondre avec un changement de tendance de fond.'
    );
  } else if (mpDte !== null && mpDte <= 7 && mpStrike) {
    lines.push(
      '&#9200; Expiration dans ' + mpDte + ' jours' + (mpExpiry ? ' (' + mpExpiry + ')' : '') + ' : ' +
      'le GEX commence \u00e0 se r\u00e9sorber. La gravit\u00e9 vers le Max Pain ' + fmtP(mpStrike) + ' s\u2019intensifie progressivement.'
    );
  }

  // ── 11. Rappel VEX/CEX si GEX remonte mais reste baissier structurel ──
  if (!vexBull && !cexBull) {
    lines.push(
      '&#9888;&#65039; M\u00eame si le GEX remonte, VEX et CEX restent n\u00e9gatifs : les dealers sont structurellement vendeurs de BTC sur toute hausse de volatilit\u00e9 ou \u00e9coulement du temps. Le rebond du GEX est m\u00e9canique, pas directionnel.'
    );
  }

  // ── 12. VEX/CEX haussiers + GEX amplificateur (tension) ──────────────
  if (vexBull && cexBull && lvlFlip && btcSpot && btcSpot < lvlFlip) {
    lines.push(
      '&#128256; <b>Tension structurelle</b> : VEX et CEX haussiers mais le spot est <b>sous le Gamma Flip</b>. ' +
      'Les options positionnent pour une hausse mais les dealers amplifient les baisses \u2014 premier signal d\u2019un potentiel retournement si le Flip est reconquis.'
    );
  }

  return lines.length ? lines.join('<br><br>') : null;
}

export async function loadRegimeSummary(signal) {
  const el = document.getElementById('regime-summary-content');
  const dot = document.getElementById('regime-dot-indicator');
  try {
    const [vcData, mopiData, vcHistory, narrData] = await Promise.all([
      apiFetch('/api/vex_cex', signal),
      apiFetch('/api/mopi_vs_btc?period=7d', signal),
      apiFetch('/api/vex_cex_history?period=7d', signal),
      apiFetch('/api/narrative', signal),   // for niveau_haut / niveau_bas / btc_price
    ]);

    if (!vcData || vcData.error) throw new Error('vex_cex indisponible');

    // ── Helper: compute trend from array of numbers (5 last vs 5 prev) ──
    function calcTrend(arr, thresh) {
      if (!arr || arr.length < 10) return 'flat';
      const n = arr.length;
      const recent = arr.slice(n - 5).reduce((a, b) => a + b, 0) / 5;
      const prev   = arr.slice(n - 10, n - 5).reduce((a, b) => a + b, 0) / 5;
      const delta  = recent - prev;
      const t = Math.abs(prev) * 0.01 + thresh;
      return delta > t ? 'up' : delta < -t ? 'down' : 'flat';
    }

    // ── GEX / DEX trends from mopi_vs_btc ──────────────────────────
    let gexTrend = 'flat', dexTrend = 'flat';
    let gexLast = 0, dexLast = 0;
    if (mopiData && mopiData.gex && mopiData.gex.length >= 10) {
      gexTrend = calcTrend(mopiData.gex, CFG.GEX_TREND_GEX_THRESH);
      dexTrend = calcTrend(mopiData.dex, 100);
      gexLast  = mopiData.gex[mopiData.gex.length - 1];
      dexLast  = mopiData.dex[mopiData.dex.length - 1];
    }

    // ── VEX / CEX trends from vex_cex_history ──────────────────────
    let vexTrend = 'flat', cexTrend = 'flat';
    if (vcHistory && vcHistory.points && vcHistory.points.length >= 10) {
      const pts = vcHistory.points;
      const vexArr = pts.map(p => p.vex);
      const cexArr = pts.map(p => p.cex);
      vexTrend = calcTrend(vexArr, CFG.GEX_TREND_VEX_THRESH);   // threshold: 1M
      cexTrend = calcTrend(cexArr, CFG.GEX_TREND_CEX_THRESH);   // threshold: 0.5M
    }

    const regime = classifyRegimeFull({
      vex: vcData.vex_total,
      cex: vcData.cex_total,
      gex: gexLast,
      dex: dexLast,
      vexTrend,
      cexTrend,
      gexTrend,
      dexTrend,
      flipDistPct: vcData.gamma_flip_dist_pct,
      flipLevel:   vcData.gamma_flip          // absolute $ level
    });

    // ── Key levels from narrative ────────────────────────────────────
    const btcSpot    = (narrData && narrData.btc_price)   || vcData.btc_price || null;
    const lvlFlip    = vcData.gamma_flip                  || null;
    const lvlHaut    = (narrData && narrData.niveau_haut) || null;
    const lvlBas     = (narrData && narrData.niveau_bas)  || null;
    const lvlHautLbl = (narrData && narrData.niveau_haut_label) || null;
    const lvlBasLbl  = (narrData && narrData.niveau_bas_label)  || null;
    // Max Pain  /* MAXPAIN_v1 */
    const mpData     = (narrData && narrData.max_pain_display) || null;
    const mpStrike   = mpData ? mpData.strike  : null;
    const mpExpiry   = mpData ? mpData.expiry  : null;  // e.g. "28JUN26"
    const mpDte      = mpData ? mpData.dte     : null;  // days to expiry
    const mpLabel    = mpData ? mpData.label   : null;
    const fmtP = v => v ? `$${Math.round(v).toLocaleString()}` : '—';

    // Store regime for loadVexCex() reuse
    setLastRegime(regime);

    // Update dot color
    if (dot) dot.style.background = regime.dot;

    const trendArrow = t => t === 'up' ? '↑' : t === 'down' ? '↓' : '→';

    const signalMinis = regime.signals.map(s => {
      const color = s.bull ? '#22c55e' : '#ef4444';
      let trendHtml = '';
      if (!s.trendFlat) {
        const trendColor = (s.bull && s.trendUp) || (!s.bull && !s.trendUp) ? '#22c55e' : '#f87171';
        trendHtml = `<div class="regime-signal-mini-trend" style="color:${trendColor}">${s.trendUp ? '↑' : '↓'} ${s.trendUp ? 'hausse' : 'baisse'}</div>`;
      }
      return `<div class="regime-signal-mini">
        <div class="regime-signal-mini-name">${s.name}</div>
        <div class="regime-signal-mini-val" style="color:${color}">${s.formatted}</div>
        ${trendHtml}
      </div>`;
    }).join('');

    const urgencyColor = regime.urgency === 'CRITIQUE' ? '#ef4444' :
                         regime.urgency === 'ÉLEVÉE'   ? '#f59e0b' : '#64748b';
    const confColor    = regime.confidence === 'ÉLEVÉE'  ? '#22c55e' :
                         regime.confidence === 'MODÉRÉE' ? '#f59e0b' : '#94a3b8';

    // Bias color & icon
    const biasColor = {
      'LONG':     '#22c55e',
      'SHORT':    '#ef4444',
      'STRANGLE': '#a855f7',
      'PATIENCE': '#64748b',
    }[regime.bias] || '#94a3b8';
    const biasIcon = {
      'LONG':     '▲ LONG',
      'SHORT':    '▼ SHORT',
      'STRANGLE': '⟺ STRANGLE',
      'PATIENCE': '⏸ ATTENDRE',
    }[regime.bias] || regime.bias;

    el.innerHTML = `
      <div class="regime-top-hero">
        <div class="regime-top-badge" style="color:${regime.color};border-color:${regime.color}55;background:${regime.color}11">
          <div class="regime-top-dot" style="background:${regime.dot}"></div>
          ${regime.label}
        </div>
        <span class="regime-top-urgency" style="color:${urgencyColor};border-color:${urgencyColor}55;background:${urgencyColor}11">
          URGENCE : ${regime.urgency}
        </span>
        <span class="regime-top-urgency" style="color:${confColor};border-color:${confColor}55;background:${confColor}11">
          CONFIANCE : ${regime.confidence}
        </span>
        <span style="font-size:11px;color:var(--muted);margin-left:auto">
          VEX ${trendArrow(vexTrend)} · CEX ${trendArrow(cexTrend)} · GEX ${trendArrow(gexTrend)} · DEX ${trendArrow(dexTrend)}
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
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#22c55e;margin-bottom:3px">▲ Résistance options</div>
          <div style="font-size:15px;font-weight:800;color:#22c55e;font-variant-numeric:tabular-nums">${fmtP(lvlHaut)}</div>
          <div style="font-size:10px;color:#64748b;margin-top:2px">${lvlHautLbl || ''}</div>
        </div>` : ''}

        ${lvlBas ? `
        <div style="background:#ef44440d;border:1px solid #ef444433;border-radius:10px;padding:10px 12px">
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#ef4444;margin-bottom:3px">▼ Support options</div>
          <div style="font-size:15px;font-weight:800;color:#ef4444;font-variant-numeric:tabular-nums">${fmtP(lvlBas)}</div>
          <div style="font-size:10px;color:#64748b;margin-top:2px">${lvlBasLbl || ''}</div>
        </div>` : ''}

        ${mpStrike ? `
        <div style="grid-column:1/-1;background:#3d8eff11;border:1.5px solid #3d8eff44;border-radius:10px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:8px">
          <div>
            <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#3d8eff;margin-bottom:3px">🎯 Max Pain — gravité expiration</div>
            <div style="font-size:16px;font-weight:900;color:#3d8eff;font-variant-numeric:tabular-nums">${fmtP(mpStrike)}</div>
            <div style="font-size:10px;color:#64748b;margin-top:2px">${mpLabel || ''}</div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:13px;font-weight:800;color:#3d8eff">${mpExpiry || ''}</div>
            <div style="font-size:11px;color:#64748b;margin-top:2px">${mpDte !== null ? (mpDte === 0 ? 'Expiration aujourd’hui' : mpDte === 1 ? 'J−1' : 'J−' + mpDte) : ''}</div>
          </div>
        </div>` : ''}

      </div>

      ${(() => {
        const ctx = buildLevelsContext(
          btcSpot, lvlFlip, lvlHaut, lvlBas,
          mpStrike, mpDte, mpExpiry,
          vcData.gamma_flip_dist_pct,
          regime.signals[0].bull,
          regime.signals[1].bull
        );
        return ctx
          ? `<div style="font-size:12px;color:#c9d1e0;line-height:1.85;margin-bottom:14px;padding:14px 16px;background:rgba(255,255,255,0.03);border-radius:10px;border-left:3px solid #475569">${ctx}</div>`
          : '';
      })()}

      <div class="regime-plain-text">${regime.plain}</div>

      <div style="border:2px solid ${biasColor};border-radius:14px;padding:16px 18px;margin-bottom:14px;background:${biasColor}0d">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
          <div style="font-size:20px;font-weight:900;color:${biasColor};letter-spacing:.5px">${biasIcon}</div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:${biasColor};opacity:.8">VERDICT TRADER</div>
        </div>
        <div style="font-size:13px;color:#e2e8f0;line-height:1.75;margin-bottom:8px">${regime.raison}</div>
        <div style="font-size:12px;color:#94a3b8;line-height:1.65;border-top:1px solid ${biasColor}33;padding-top:8px;margin-top:4px">${regime.pro}</div>
      </div>

      <div class="regime-advice-bar" style="border-left-color:${regime.color}">
        <div class="regime-advice-label" style="color:${regime.color}">Contexte structurel</div>
        <div class="regime-advice-text">${regime.plain}</div>
      </div>`;
  } catch (e) {
    if (el) el.innerHTML = `<div class="error-state"><div class="error-icon">⚠</div>Régime indisponible : ${e.message}</div>`;
  }
}

export function classifyRegime(vex, cex, gexTotal, dexTotal, flipDistPct) {
  const vexBull = vex > 0;
  const cexBull = cex > 0;
  const gexBull = gexTotal >= 0;
  const dexBull = dexTotal >= 0;

  const bigVex = Math.abs(vex) > CFG.GEX_BIG_VEX;
  const bigCex = Math.abs(cex) > CFG.GEX_BIG_CEX;

  // Near flip (within 2%) → explosive potential regardless of direction
  const nearFlip = flipDistPct !== null && Math.abs(flipDistPct) <= CFG.FLIP_NEAR_PCT;

  // Count bullish signals
  const bullCount = [vexBull, cexBull, gexBull, dexBull].filter(Boolean).length;

  if (nearFlip && (bigVex || bigCex)) {
    return {
      label: 'PRÉPARATION EXPLOSIVE',
      color: '#f59e0b',
      dot: '#f59e0b',
      plain: `Le spot BTC se situe à moins de 2 % du Gamma Flip, avec un VEX et/ou CEX de grande amplitude. Les dealers sont exposés à un repositionnement brutal : un franchissement du flip déclencherait une cascade d'achats ou de ventes de couverture, amplifiant fortement le mouvement initial. C'est la configuration la plus explosive de toutes.`,
      advice: `Attention aux breakouts : même un faible catalyseur peut déclencher un mouvement extrême. Les options ATM sont très chères, mais les spreads directionnels restent pertinents.`
    };
  }

  if (bullCount === 4) {
    return {
      label: 'RALENTISSEMENT HAUSSIER',
      color: '#22c55e',
      dot: '#22c55e',
      plain: `Les quatre indicateurs de flux dealers sont alignés à la hausse. VEX positif : une hausse de volatilité pousse les dealers à acheter du BTC. CEX positif : le temps qui passe les force aussi à acheter. GEX et DEX haussiers : ils absorbent les ventes et amortissent les baisses. Le marché est en mode stabilisation — les corrections sont limitées.`,
      advice: `Privilégier les stratégies vendeuses de volatilité (short puts, iron condors). Les dealers joueront le rôle d'amortisseur. Éviter les longs straddles.`
    };
  }

  if (bullCount === 0) {
    return {
      label: 'ACCÉLÉRATION BAISSIÈRE',
      color: '#ef4444',
      dot: '#ef4444',
      plain: `Les quatre indicateurs sont négatifs. VEX négatif : toute hausse de volatilité oblige les dealers à vendre du BTC. CEX négatif : le temps qui passe aggrave la pression vendeuse. GEX et DEX négatifs : les dealers amplifient chaque mouvement au lieu de l'amortir. Le marché est en mode amplificateur — les baisses peuvent s'emballer.`,
      advice: `Configuration idéale pour les stratégies long volatilité (long puts, long straddles). Les corrections peuvent être profondes et rapides. Gérer les stops serrément.`
    };
  }

  if (bullCount >= 3) {
    return {
      label: 'RALENTISSEMENT MODÉRÉ',
      color: '#4ade80',
      dot: '#4ade80',
      plain: `La majorité des indicateurs de flux pointent vers un soutien des dealers, mais un signal diverge. Le marché est globalement stabilisant, mais avec quelques tensions internes. Les corrections restent contenues, mais le rebond automatique est moins garanti.`,
      advice: `Neutre légèrement haussier. Les stratégies de range (iron condors, short strangles) fonctionnent bien. Être vigilant sur le signal qui diverge.`
    };
  }

  if (bullCount <= 1) {
    return {
      label: 'ACCÉLÉRATION MODÉRÉE',
      color: '#f87171',
      dot: '#f87171',
      plain: `La majorité des indicateurs signalent une pression vendeuse de la part des dealers. Les forces d'amplification dominent, mais pas unanimement. Le marché peut s'emballer à la baisse, mais un signal positif peut servir de frein temporaire.`,
      advice: `Prudent côté acheteur. Les rallyes risquent d'être vendus. Privilégier la protection par des puts ou une réduction d'exposition.`
    };
  }

  // bullCount === 2 → balanced / compression
  const vexCexOpposite = vexBull !== cexBull;
  if (vexCexOpposite) {
    return {
      label: 'COMPRESSION — BREAKOUT IMMINENT',
      color: '#a855f7',
      dot: '#a855f7',
      plain: `VEX et CEX envoient des signaux opposés : la volatilité implicite et le temps tirent les dealers dans des directions contraires. Cette tension interne comprime le marché dans une fourchette étroite, accumulant de l'énergie. Le breakout, quand il arrive, est souvent violent et unilatéral.`,
      advice: `Excellent timing pour les stratégies long volatilité directionnelles (straddles, strangles longs). Les options bon marché par rapport à la volatilité réalisée représentent une opportunité. Ne pas être trop directionnel.`
    };
  }

  return {
    label: 'INDÉCISION — SIGNAUX MIXTES',
    color: '#94a3b8',
    dot: '#94a3b8',
    plain: `Les indicateurs GEX, DEX, VEX et CEX ne dégagent pas de consensus clair. Les forces haussières et baissières s'équilibrent. Le marché manque de direction et les dealers n'ont pas de biais dominant. Ce type de configuration précède souvent soit une consolidation prolongée, soit une soudaine prise de direction.`,
    advice: `Rester neutre ou réduire l'exposition. Attendre un alignement des signaux avant de prendre une position directionnelle forte.`
  };
}
