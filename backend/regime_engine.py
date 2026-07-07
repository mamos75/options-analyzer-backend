"""
Mamos Options Regime Engine — Moteur institutionnel explicable.

Détecte les changements de régime options AVANT le prix,
uniquement quand données options + futures + volatilité fournissent un edge mesurable.

Produit 10 champs obligatoires :
  1. regime_principal          — STABILISANT / AMPLIFICATEUR / NEUTRE + description humaine
  2. risque_dominant           — menace principale identifiée
  3. direction_mecanique       — direction si et seulement si edge backtest suffisant (winrate > 52% ET N ≥ 30)
  4. probabilites_par_horizon  — probabilité conditionnelle par horizon 4h / 24h / 72h
  5. strategie_recommandee     — action actionnable en langage humain
  6. a_eviter                  — comportement risqué à ne pas adopter
  7. niveau_invalidation       — prix qui invalide mécaniquement le scénario
  8. raisonnement_explicable   — forces actives + forces vetoed + contexte statistique
  9. qualite_donnees           — calibration, use_in_signal, stale, confiance
 10. confiance_statistique     — backtest résumé : grade, N, winrate, EV, p-value

Vocabulaire interdit (jamais dans les strings retournées) :
  - "objectif" (remplacé par "niveau d'attraction mécanique" ou "cible mécanique")
  - "prédiction certaine" (remplacé par "probabilité conditionnelle")
  - "les dealers vont pousser le prix" (remplacé par "pression de hedging mécanique")
  - "signal garanti" (remplacé par "edge statistiquement mesuré")
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from .narrative_resolver import NarrativeResolved
from .gex import GEXProfile
from .mopi import MOPIScore
from .dealer_pressure import DealerPressure, DEXLevels

# V2 — seuil DEX score pour crash régime (DEX score 0-100, score < 35 = flux très baissiers)
_DEX_CRASH_THRESHOLD = 35.0


# Edge minimum requis pour afficher une direction mécanique
_MIN_WINRATE_FOR_DIRECTION = 52.0   # < 52% = pas mieux que pile ou face
_MIN_N_FOR_DIRECTION = 30           # INSUFFICIENT DATA sous ce seuil
_MIN_EV_FOR_DIRECTION = 0.0         # EV doit être positive


@dataclass
class HorizonProbability:
    horizon: str
    probabilite_conditionnelle: Optional[float]   # % basé sur winrate backtest
    winrate: Optional[float]
    n_occurrences: int
    ev: Optional[float]
    grade: str
    has_edge: bool           # True si winrate > 52% ET N >= 30 ET EV > 0
    contexte: str            # phrase humaine expliquant la probabilité


@dataclass
class RegimeEngineOutput:
    # 1. Régime
    regime_principal: str
    regime_label: str        # "GEX STABILISANT" | "GEX AMPLIFICATEUR" | "Régime indéterminé"
    regime_description: str  # phrase humaine

    # 2. Risque
    risque_dominant: str

    # 3. Direction mécanique (None si edge insuffisant)
    direction_mecanique: Optional[str]        # "HAUSSIER" | "BAISSIER" | None
    direction_edge_grade: str                  # grade backtest justifiant la direction
    direction_edge_context: str               # pourquoi la direction est (ou n'est pas) affichée

    # 4. Probabilités par horizon
    probabilites: Dict[str, HorizonProbability]   # keys: "4h", "24h", "72h"

    # 5. Stratégie recommandée
    strategie_recommandee: str

    # 6. À éviter
    a_eviter: str

    # 7. Niveau d'invalidation
    niveau_invalidation: Optional[float]
    invalidation_label: str
    invalidation_context: str   # condition précise qui invalide mécaniquement le scénario

    # 8. Raisonnement explicable
    forces_actives: List[str]
    forces_vetoed: List[str]
    raisonnement: str

    # 9. Qualité des données
    qualite_donnees: Dict[str, Any]

    # 10. Confiance statistique
    confiance_statistique: Dict[str, Any]

    # V2 — Crash Regime Gate
    crash_regime_active: bool = False
    crash_regime_warning: str = ""


def _extract_backtest_horizon(backtest_result: Optional[Dict], horizon: str) -> Dict:
    """Extrait les stats backtest pour le signal V1 à un horizon donné."""
    if not backtest_result or backtest_result.get("status") != "OK":
        return {}
    sv1 = backtest_result.get("signal_v1", {})
    for direction in ("haussier", "baissier"):
        h = sv1.get(direction, {}).get(horizon, {})
        if h and not h.get("insufficient"):
            return {"direction": direction, **h}
    return {}


def _has_edge(stats: Dict) -> bool:
    """True si le signal a un edge statistique mesurable."""
    if not stats:
        return False
    n = stats.get("n", 0)
    wr = stats.get("winrate", 0.0)
    ev = stats.get("ev", -999.0)
    grade = stats.get("grade", "INSUFFICIENT DATA")
    if n < _MIN_N_FOR_DIRECTION:
        return False
    if grade == "INSUFFICIENT DATA":
        return False
    if wr <= _MIN_WINRATE_FOR_DIRECTION:
        return False
    if ev <= _MIN_EV_FOR_DIRECTION:
        return False
    return True


def _build_horizon_probability(
    horizon: str,
    stats: Dict,
    narrative: NarrativeResolved,
    gex: GEXProfile,
) -> HorizonProbability:
    """Construit la probabilité conditionnelle pour un horizon donné."""
    if not stats:
        return HorizonProbability(
            horizon=horizon,
            probabilite_conditionnelle=None,
            winrate=None,
            n_occurrences=0,
            ev=None,
            grade="INSUFFICIENT DATA",
            has_edge=False,
            contexte=(
                f"Données insuffisantes pour estimer une probabilité conditionnelle "
                f"à {horizon} — minimum {_MIN_N_FOR_DIRECTION} occurrences requises."
            ),
        )

    n = stats.get("n", 0)
    wr = stats.get("winrate")
    ev = stats.get("ev")
    grade = stats.get("grade", "INSUFFICIENT DATA")
    edge = _has_edge(stats)
    p_value = stats.get("bootstrap_p_value")

    if n < _MIN_N_FOR_DIRECTION:
        contexte = (
            f"INSUFFICIENT DATA — {n}/{_MIN_N_FOR_DIRECTION} occurrences. "
            f"Aucune probabilité conditionnelle fiable à ce stade."
        )
        prob = None
    elif not edge:
        contexte = (
            f"Winrate {wr:.1f}% à {horizon} (N={n}) — "
            f"insuffisant pour affirmer un edge directionnel (seuil : >{_MIN_WINRATE_FOR_DIRECTION}%)."
        )
        prob = wr
    else:
        p_str = f" | p-value bootstrap : {p_value:.2f}" if p_value is not None else ""
        contexte = (
            f"Probabilité conditionnelle {wr:.1f}% à {horizon} (N={n}, EV={ev:+.2f}%{p_str}). "
            f"Edge statistiquement mesuré — grade {grade}."
        )
        prob = wr

    return HorizonProbability(
        horizon=horizon,
        probabilite_conditionnelle=prob,
        winrate=wr,
        n_occurrences=n,
        ev=ev,
        grade=grade,
        has_edge=edge,
        contexte=contexte,
    )


def _detect_crash_regime(dp: DealerPressure, gex: GEXProfile) -> tuple[bool, str]:
    """V2 — Détecte le régime de crash : flux dealers extrêmes + GEX AMPLIFICATEUR.

    Hiérarchie V2 :
      1. Régime GEX / Regime Engine
      2. Dealer Pressure / DEX  ← dominant en crash
      3. GEX Momentum / contraction
      4. Distance au Flip
      5. Volatility Stress / IV / skew
      6. Futures OI / funding / liquidations
      7. Gravity / Max Pain ← secondaire uniquement (aimant conditionnel)

    Retourne (active, warning_message).
    """
    dex_score = getattr(dp, "score", 50.0)
    dex_direction = getattr(dp, "direction", "")
    gex_regime = gex.regime

    if (
        dex_score <= _DEX_CRASH_THRESHOLD
        and dex_direction == "BEARISH_FLOWS"
        and gex_regime == "AMPLIFICATEUR"
    ):
        warning = (
            f"CRASH REGIME GATE actif — "
            f"DEX score {dex_score:.0f}% (flux dealers extrêmement baissiers) "
            f"+ GEX {gex_regime}. "
            "Max Pain au-dessus = attraction résiduelle ignorée. "
            "Buy The Dip interdit. Stratégie imposée : Risk-Off / Trend Following baissier."
        )
        return True, warning
    return False, ""


def _regime_description(gex: GEXProfile, narrative: NarrativeResolved) -> tuple[str, str]:
    """Retourne (regime_principal, regime_description) selon le régime GEX actuel."""
    regime = gex.regime
    if regime == "STABILISANT":
        desc = (
            "Pression de hedging mécanique absorbante — les dealers freinent les mouvements. "
            "Amplitude réduite tant que ce régime est actif."
        )
    elif regime == "AMPLIFICATEUR":
        desc = (
            "Pression de hedging mécanique amplificatrice — chaque mouvement est exacerbé. "
            "Amplitude accrue, risque de cassures violentes dans les deux sens."
        )
    else:
        desc = (
            "Régime GEX indéterminé — pression de hedging neutre ou insuffisante "
            "pour orienter l'amplitude."
        )
    return regime, desc


def _build_strategy(
    narrative: NarrativeResolved,
    gex: GEXProfile,
    mopi: MOPIScore,
    spot: float,
    crash_regime: bool = False,
) -> tuple[str, str]:
    """Retourne (strategie_recommandee, a_eviter)."""
    regime = gex.regime
    flip = gex.flip_level
    spot_above_flip = flip is not None and spot > flip

    # V2 — Crash Regime Gate : priorité absolue sur toute autre logique
    # DEX extrême baissier + GEX AMPLIFICATEUR → Risk-Off forcé
    if crash_regime:
        strategie = (
            "Risk-Off / Trend Following baissier — Crash Regime Gate actif. "
            "Flux dealers extrêmement baissiers + GEX AMPLIFICATEUR. "
            "Réduire toute exposition longue. Laisser le flush se finaliser avant toute reprise."
        )
        a_eviter = (
            "Buy The Dip interdit tant que le Crash Regime Gate est actif. "
            "Max Pain au-dessus = attraction résiduelle non confirmée — ne pas l'interpréter comme signal haussier. "
            "Tout signal HAUSSIER dans ce contexte = rebond technique possible uniquement, pas direction mécanique."
        )
        return strategie, a_eviter

    # Régime STABILISANT : range / mean-reversion
    if regime == "STABILISANT":
        strategie = (
            "Trend Following light — jouer les bornes du range avec des positions réduites. "
            "Favoriser les niveaux d'attraction mécanique comme cibles probables."
        )
        a_eviter = (
            "Éviter les breakouts agressifs sans confirmation de volume. "
            "Un régime stabilisant absorbe les mouvements — les faux breakouts sont fréquents."
        )
    # Régime AMPLIFICATEUR avec MOPI baissier et BTC sous le flip
    elif regime == "AMPLIFICATEUR" and mopi.score < 45 and not spot_above_flip:
        strategie = (
            "Risk-Off / Trend Following baissier — réduire l'exposition longue. "
            "Laisser la pression de hedging mécanique opérer sans la contrarier."
        )
        a_eviter = (
            "Buy the dip agressif tant que BTC reste sous le niveau d'inversion mécanique. "
            "Acheter contre une pression de hedging amplificatrice = prise de risque asymétrique défavorable."
        )
    # Régime AMPLIFICATEUR avec MOPI haussier
    elif regime == "AMPLIFICATEUR" and mopi.score >= 55:
        strategie = (
            "Trend Following haussier — profiter de l'amplification mécanique dans le sens de la pression options. "
            "Gérer les risques de retournement brutal (régime amplificateur = volatilité élevée dans les deux sens)."
        )
        a_eviter = (
            "Positions sans stop — le régime amplificateur exacerbe aussi les retournements. "
            "Ne pas confondre pression haussière des options avec absence de risque baissier."
        )
    # Régime NEUTRE ou indéterminé
    else:
        strategie = (
            "Réduire la taille des positions — régime indéterminé. "
            "Attendre un signal mécanique plus clair avant d'augmenter l'exposition."
        )
        a_eviter = (
            "Positions directionnelles fortes sans confirmation options + futures alignés. "
            "Un régime neutre ne fournit pas d'edge mécanique mesurable."
        )

    return strategie, a_eviter


def _build_invalidation_context(
    narrative: NarrativeResolved,
    gex: GEXProfile,
    spot: float,
) -> str:
    """Phrase précise décrivant la condition d'invalidation mécanique."""
    flip = gex.flip_level
    inv = narrative.invalidation

    conditions = []
    if flip is not None:
        if spot > flip:
            conditions.append(
                f"cassure confirmée sous ${flip:,.0f} (niveau d'inversion mécanique)"
            )
        else:
            conditions.append(
                f"retour confirmé au-dessus de ${flip:,.0f} + pression dealer redevenue neutre"
            )

    if narrative.directional_bias and narrative.directional_bias.stop_logical:
        stop = narrative.directional_bias.stop_logical
        conditions.append(
            f"${stop:,.0f} (invalidation mécanique du biais directionnel)"
        )

    if conditions:
        return (
            f"Invalidation mécanique du régime si : {' ET '.join(conditions)}. "
            "Ce n'est pas un stop-loss — c'est le niveau qui change la structure options."
        )
    return (
        f"Invalidation mécanique au niveau ${inv:,.0f} — "
        "dépassement de ce niveau change structurellement la pression de hedging."
    )


def build_regime_engine_output(
    narrative: NarrativeResolved,
    gex: GEXProfile,
    mopi: MOPIScore,
    dp: DealerPressure,
    spot: float,
    backtest_result: Optional[Dict] = None,
    data_stale: bool = False,
    calibration_status: str = "available",
) -> RegimeEngineOutput:
    """Point d'entrée principal — construit les 10 champs du Regime Engine."""

    # V2 — Crash Regime Gate (priorité sur toute autre logique)
    crash_regime_active, crash_regime_warning = _detect_crash_regime(dp, gex)

    # 1. Régime principal
    regime_principal, regime_description = _regime_description(gex, narrative)

    # 2. Risque dominant
    risque_dominant = narrative.risque_principal

    # 3. Direction mécanique — seulement si edge backtest suffisant
    # Sélectionner le meilleur horizon avec edge (EV maximale parmi ceux qui passent les seuils)
    candidate_horizons = []
    for h in ("4h", "24h", "72h"):
        bt_h = _extract_backtest_horizon(backtest_result, h)
        if bt_h and _has_edge(bt_h):
            candidate_horizons.append((bt_h.get("ev", 0.0), h, bt_h))
    candidate_horizons.sort(key=lambda x: x[0], reverse=True)

    best_bt_horizon = candidate_horizons[0][2] if candidate_horizons else None
    bt_72h = _extract_backtest_horizon(backtest_result, "72h")
    fallback_bt = bt_72h or _extract_backtest_horizon(backtest_result, "24h")

    if best_bt_horizon:
        best_bt = best_bt_horizon
        best_h_name = candidate_horizons[0][1]
        direction_mecanique = best_bt.get("direction", "").upper()
        direction_edge_grade = best_bt.get("grade", "?")
        direction_edge_context = (
            f"Direction affichée — edge statistiquement mesuré à {best_h_name} : "
            f"Winrate {best_bt.get('winrate', 0):.1f}% "
            f"(N={best_bt.get('n', 0)}, EV={best_bt.get('ev', 0):+.2f}%, grade {direction_edge_grade})."
        )
    else:
        direction_mecanique = None
        best_bt = fallback_bt
        n_bt = best_bt.get("n", 0) if best_bt else 0
        if n_bt < _MIN_N_FOR_DIRECTION:
            direction_edge_context = (
                f"Direction non affichée — INSUFFICIENT DATA "
                f"({n_bt}/{_MIN_N_FOR_DIRECTION} occurrences backtest). "
                "La direction sera affichée dès que l'historique sera suffisant."
            )
        else:
            wr = best_bt.get("winrate", 0) if best_bt else 0
            direction_edge_context = (
                f"Direction non affichée — winrate {wr:.1f}% insuffisant sur tous les horizons "
                f"(seuil >{_MIN_WINRATE_FOR_DIRECTION}%). Aucun edge mécanique mesurable."
            )
        direction_edge_grade = "INSUFFICIENT DATA"

    # 4. Probabilités par horizon
    horizons = {}
    for h in ("4h", "24h", "72h"):
        bt_h = _extract_backtest_horizon(backtest_result, h)
        horizons[h] = _build_horizon_probability(h, bt_h, narrative, gex)

    # 5 & 6. Stratégie et à éviter (V2 : Crash Regime Gate prioritaire)
    strategie_recommandee, a_eviter = _build_strategy(narrative, gex, mopi, spot, crash_regime_active)

    # 7. Invalidation
    niveau_invalidation = narrative.invalidation
    invalidation_label = (
        f"${niveau_invalidation:,.0f}" if isinstance(niveau_invalidation, (int, float))
        else str(niveau_invalidation)
    )
    invalidation_context = _build_invalidation_context(narrative, gex, spot)

    # 8. Raisonnement explicable
    forces_actives = []
    forces_vetoed = []

    if narrative.gex_use_in_signal:
        forces_actives.append(f"GEX {gex.regime} ({narrative.gex_activity_label})")
    else:
        forces_vetoed.append(f"GEX exclu du signal ({narrative.gex_activity_context})")

    if narrative.dex_use_in_signal:
        forces_actives.append(
            f"DEX {dp.direction} ({narrative.dex_activity_label}) — "
            f"pression de hedging {dp.direction.lower().replace('_flows', '').replace('_', ' ')}"
        )
    else:
        forces_vetoed.append(f"DEX exclu — stock delta inactif ({narrative.dex_activity_context})")


    if narrative.flip_use_in_signal:
        flip = gex.flip_level
        spot_vs_flip = "au-dessus" if spot > (flip or 0) else "en dessous"
        forces_actives.append(
            f"Niveau d'inversion mécanique ${flip:,.0f} — BTC {spot_vs_flip} ({narrative.flip_activity_tag or 'ACTIVE'})"
        )
    else:
        forces_vetoed.append(
            f"Niveau d'inversion exclu du signal ({narrative.flip_activity_context})"
        )

    # Raisonnement = scenario + forces actives (sans duplication)
    actives_str = " | ".join(forces_actives)
    raisonnement = f"{narrative.scenario_principal} — {actives_str}"

    # 9. Qualité des données
    qualite_donnees = {
        "calibration_status": calibration_status,
        "gex_use_in_signal": narrative.gex_use_in_signal,
        "dex_use_in_signal": narrative.dex_use_in_signal,
        "flip_use_in_signal": narrative.flip_use_in_signal,
        "data_stale": data_stale,
        "contradictions_detectees": len(narrative.contradictions),
        "contradictions": narrative.contradictions,
        "forces_actives_count": len(forces_actives),
        "forces_vetoed_count": len(forces_vetoed),
        "resume_qualite": _build_quality_summary(
            calibration_status, narrative, data_stale, forces_actives, forces_vetoed
        ),
    }

    # 10. Confiance statistique
    confiance_statistique = _build_statistical_confidence(backtest_result)

    return RegimeEngineOutput(
        regime_principal=regime_principal,
        regime_label=f"GEX {regime_principal}" if regime_principal != "NEUTRE" else "Régime GEX Neutre",
        regime_description=regime_description,
        risque_dominant=risque_dominant,
        direction_mecanique=direction_mecanique,
        direction_edge_grade=direction_edge_grade,
        direction_edge_context=direction_edge_context,
        probabilites=horizons,
        strategie_recommandee=strategie_recommandee,
        a_eviter=a_eviter,
        niveau_invalidation=niveau_invalidation,
        invalidation_label=invalidation_label,
        invalidation_context=invalidation_context,
        forces_actives=forces_actives,
        forces_vetoed=forces_vetoed,
        raisonnement=raisonnement,
        qualite_donnees=qualite_donnees,
        confiance_statistique=confiance_statistique,
        crash_regime_active=crash_regime_active,
        crash_regime_warning=crash_regime_warning,
    )


def _build_quality_summary(
    calibration_status: str,
    narrative: NarrativeResolved,
    data_stale: bool,
    forces_actives: List[str],
    forces_vetoed: List[str],
) -> str:
    """Résumé humain de la qualité des données disponibles."""
    issues = []
    if calibration_status == "unavailable":
        issues.append("calibration GEX indisponible")
    elif calibration_status == "degraded":
        issues.append("calibration GEX dégradée")
    elif calibration_status == "stale":
        issues.append("calibration GEX ancienne")
    if data_stale:
        issues.append("données Deribit non fraîches")
    if not narrative.gex_use_in_signal:
        issues.append("GEX dormant — exclu du signal")
    if not narrative.dex_use_in_signal:
        issues.append("DEX structurel — exclu du signal")

    n_active = len(forces_actives)
    if not issues:
        return (
            f"{n_active} force(s) active(s) — données disponibles et calibrées. "
            "Contexte statistique fiable."
        )
    return (
        f"{n_active} force(s) active(s) — "
        f"limitations : {', '.join(issues)}. "
        "Interpréter avec prudence."
    )


def _build_statistical_confidence(backtest_result: Optional[Dict]) -> Dict:
    """Résumé de la confiance statistique globale basé sur le backtest."""
    if not backtest_result:
        return {
            "status": "NO_BACKTEST",
            "label": "Backtest non disponible",
            "contexte_statistique": (
                "Aucune donnée historique pour valider l'edge. "
                "Le moteur opère en mode hypothèse V1."
            ),
        }

    status = backtest_result.get("status")
    if status == "NO_DATA":
        return {
            "status": "NO_DATA",
            "label": "INSUFFICIENT DATA",
            "contexte_statistique": "Base de données vide. Accumulation en cours.",
        }
    if status == "ACCUMULATING":
        meta = backtest_result.get("meta", {})
        n = backtest_result.get("n", 0)
        span = backtest_result.get("span_days", 0)
        return {
            "status": "ACCUMULATING",
            "label": "INSUFFICIENT DATA",
            "n_snapshots": n,
            "span_days": span,
            "contexte_statistique": (
                f"INSUFFICIENT DATA — {n}/30 occurrences minimum requises. "
                f"Accumulation en cours ({span:.1f} jours)."
            ),
        }
    if status != "OK":
        return {"status": status, "label": "Indéterminé", "contexte_statistique": "—"}

    meta = backtest_result.get("meta", {})
    n_snapshots = meta.get("n_snapshots", 0)
    span = meta.get("span_days", 0)
    oos = backtest_result.get("out_of_sample", {})
    oos_stable = oos.get("oos_stable", None)

    # Signal V1 HAUSSIER 72h — signal phare
    sv1_h = backtest_result.get("signal_v1", {}).get("haussier", {})
    sv1_h_72 = sv1_h.get("72h", {}) if sv1_h else {}
    grade = sv1_h_72.get("grade", "INSUFFICIENT DATA") if sv1_h_72 else "INSUFFICIENT DATA"
    wr = sv1_h_72.get("winrate") if sv1_h_72 else None
    ev = sv1_h_72.get("ev") if sv1_h_72 else None
    conf = sv1_h_72.get("confidence_label", "Low") if sv1_h_72 else "Low"
    p_value = sv1_h_72.get("bootstrap_p_value") if sv1_h_72 else None

    # V2 — Affichage N correct : séparer snapshots collectés / signaux testés / wins / losses
    # n_total dans _backtest_condition = nombre de fois que le signal a déclenché
    n_signals_haussier = sv1_h.get("haussier", {}).get("n_total", 0) if sv1_h else 0
    # Fallback : lire depuis sv1_h_72.n si disponible
    n_signals_tested = sv1_h_72.get("n", 0) if sv1_h_72 else 0
    n_wins = 0
    n_losses = 0
    if sv1_h_72 and wr is not None and n_signals_tested > 0:
        n_wins = round(n_signals_tested * wr / 100)
        n_losses = n_signals_tested - n_wins

    if grade == "INSUFFICIENT DATA":
        contexte = (
            f"INSUFFICIENT DATA — {n_snapshots} snapshots collectés | "
            f"{n_signals_tested} signaux testés (minimum 30 requis)."
        )
    else:
        oos_str = ""
        if oos_stable is True:
            oos_str = " | Out-of-sample : stable."
        elif oos_stable is False:
            oos_str = " | Out-of-sample : dégradation détectée — signal potentiellement overfit."
        p_str = f" | p-value bootstrap : {p_value:.2f}" if p_value is not None else ""
        contexte = (
            f"Grade {grade} | Winrate {wr:.1f}% ({n_wins} gagnants / {n_losses} perdants) | "
            f"EV {ev:+.2f}% | Confiance {conf} "
            f"({n_signals_tested} signaux testés sur {n_snapshots} snapshots, {span:.1f} jours)"
            f"{p_str}{oos_str}."
        )

    return {
        "status": "OK",
        "grade": grade,
        "winrate_72h": wr,
        "ev_72h": ev,
        "confidence_label": conf,
        # V2 — séparation claire snapshots / signaux
        "n_snapshots": n_snapshots,
        "n_signals_tested": n_signals_tested,
        "n_signals_wins": n_wins,
        "n_signals_losses": n_losses,
        "span_days": span,
        "bootstrap_p_value": p_value,
        "out_of_sample_stable": oos_stable,
        "contexte_statistique": contexte,
    }


def regime_engine_to_dict(output: RegimeEngineOutput) -> Dict:
    """Sérialise RegimeEngineOutput en dict JSON-compatible."""

    def prob_to_dict(p: HorizonProbability) -> Dict:
        return {
            "horizon": p.horizon,
            "probabilite_conditionnelle": p.probabilite_conditionnelle,
            "winrate": p.winrate,
            "n_occurrences": p.n_occurrences,
            "ev": p.ev,
            "grade": p.grade,
            "has_edge": p.has_edge,
            "contexte": p.contexte,
        }

    return {
        # 1. Régime
        "regime_principal": output.regime_principal,
        "regime_label": output.regime_label,
        "regime_description": output.regime_description,
        # 2. Risque
        "risque_dominant": output.risque_dominant,
        # 3. Direction mécanique
        "direction_mecanique": output.direction_mecanique,
        "direction_edge_grade": output.direction_edge_grade,
        "direction_edge_context": output.direction_edge_context,
        # 4. Probabilités par horizon
        "probabilites_par_horizon": {k: prob_to_dict(v) for k, v in output.probabilites.items()},
        # 5. Stratégie
        "strategie_recommandee": output.strategie_recommandee,
        # 6. À éviter
        "a_eviter": output.a_eviter,
        # 7. Invalidation
        "niveau_invalidation": output.niveau_invalidation,
        "invalidation_label": output.invalidation_label,
        "invalidation_context": output.invalidation_context,
        # 8. Raisonnement
        "forces_actives": output.forces_actives,
        "forces_vetoed": output.forces_vetoed,
        "raisonnement": output.raisonnement,
        # 9. Qualité données
        "qualite_donnees": output.qualite_donnees,
        # 10. Confiance statistique
        "confiance_statistique": output.confiance_statistique,
        # V2 — Crash Regime Gate
        "crash_regime_active": output.crash_regime_active,
        "crash_regime_warning": output.crash_regime_warning,
    }
