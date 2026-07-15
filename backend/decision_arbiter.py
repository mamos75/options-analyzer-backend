"""
Decision Arbiter — Moteur supérieur qui tranche entre tous les signaux.

Rôle : produire UNE seule conclusion actionnelle et expliquer les signaux ignorés.

Pipeline :
  1. Vérifier la fraîcheur des données (data_health)
  2. Vérifier les contradictions (narrative)
  3. Vérifier l'edge statistique (arena stats)
  4. Intégrer le régime VEX/CEX (V5)
  5. Dégrader les moteurs faibles
  6. Produire la décision finale
  7. Expliquer les exclusions

V5 — Fusion VEX/CEX Regime :
  Le régime VEX/CEX (regime_id depuis regime_vexcex_engine) est un nouvel input.
  La table de verdicts est indexée par regime_id + contexte (phase, urgency, arena).
  Règles de dégradation spécifiques au régime :
    - NEU-* : confiance plafonnée à 20%
    - COMP-0 CRITIQUE : confiance dégradée si PE "edge insuffisant"
    - EXP-*-1 + arena LEADER_CLEAR : boost de confiance +15
    - FL-0 CRITIQUE : verdict forcé OBSERVE (flip zone = pas directionnel)
"""

from __future__ import annotations

# F15 — MOPI retiré du moteur de décision (07/07/2026)

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ArbiteredDecision:
    verdict: str              # "NO_TRADE" | "OBSERVE" | "SIGNAL_UP" | "SIGNAL_DOWN"
    confidence: str           # "FAIBLE" | "MODÉRÉ" | "ÉLEVÉ"
    confidence_pct: int       # 0-100
    phrase: str               # phrase de décision lisible
    signals_used: list        # signaux actifs ayant contribué
    signals_ignored: list     # signaux exclus + raison
    contradictions: list      # contradictions détectées
    data_quality: str         # "OK" | "DEGRADED" | "INSUFFICIENT"
    arena_status: str         # "NO_EDGE" | "COLLECTING" | "LEADER_CLEAR"
    arena_leader: Optional[str]
    arena_leader_wr: Optional[float]
    arena_leader_ev: Optional[float]
    generated_at: str
    # P0.5 — Statut global lisible par le dashboard (un seul mot, une seule couleur).
    # TRADEABLE   : signal convergent + confiance ≥ 60 + données OK
    # OBSERVE     : signal présent mais non convergent ou confiance faible
    # CONFLICT    : contradictions non résolues entre indicateurs
    # DEGRADED    : données stale ou calibration dégradée
    # OFFLINE     : données insuffisantes ou erreur collecte
    system_status: str        # "TRADEABLE" | "OBSERVE" | "CONFLICT" | "DEGRADED" | "OFFLINE"
    # V5 — régime VEX/CEX
    vexcex_regime_id: Optional[str] = None   # ex. "EXP-UP-1", "NEU-0", "COMP-0"
    vexcex_phase: Optional[str] = None       # ex. "EXP", "NEU", "COMP"
    vexcex_urgency: Optional[str] = None     # ex. "CRITIQUE", "ÉLEVÉE", "NEUTRE"
    vexcex_label: Optional[str] = None       # ex. "EXPANSION HAUSSIÈRE"
    vexcex_contribution: Optional[str] = None  # "BOOST" | "DEGRADED" | "NEUTRAL" | "BLOCKED"
    # F8.3 — Vocabulaire desk premium
    state: str = "RAS"    # "RAS" | "TENSION" | "CONFLIT" | "CRITIQUE"
    action: str = "OBSERVER"  # "OBSERVER" | "PRÉPARER" | "AGIR_LONG" | "AGIR_SHORT"
    # P2 — Directional Bias score transmis (pour cap conviction Pro)
    directional_bias_score: Optional[float] = None
    # P3 — Probability Engine signal transmis (pour échelle unifiée)
    pe_dominant_direction: Optional[str] = None
    pe_dominant_probability: Optional[float] = None
    # P3 — Confiance globale unifiée (alias de confidence_pct, source de vérité unique)
    global_confidence: Optional[int] = None
    # P4 — TTL / décroissance pré-expiration
    pre_expiration_warning: Optional[str] = None   # phrase d'alerte si signal dominé par contrat expirant
    signal_dte_degraded: bool = False               # True si ≥1 signal dégradé par TTL


# ── Table de verdicts VEX/CEX par regime_id ───────────────────────────────
#
# Format : regime_id → dict de modificateurs :
#   confidence_delta  : ajout/retrait de confiance_pct (+15, -10, etc.)
#   confidence_cap    : plafond confiance_pct (None = pas de cap)
#   force_verdict     : forcer un verdict précis (None = pas de forçage)
#   direction_hint    : "UP" | "DOWN" | "RANGE" | None (indice directionnel)
#   contribution      : "BOOST" | "DEGRADED" | "NEUTRAL" | "BLOCKED"
#
# Priorités :
#   1. force_verdict (absolu)
#   2. confidence_cap
#   3. confidence_delta
#   4. direction_hint (influence signal selection)

_REGIME_VERDICT_TABLE: dict = {
    # ── NEU : zone morte — dégradation systématique
    "NEU-0": {
        "confidence_cap": 20,
        "confidence_delta": -10,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "DEGRADED",
    },
    "NEU-1": {
        "confidence_cap": 25,
        "confidence_delta": -5,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "DEGRADED",
    },
    # ── FL : zone de flip — observation requise
    "FL-0": {
        "confidence_cap": 30,
        "confidence_delta": 0,
        "force_verdict": "OBSERVE",   # flip critique → jamais directionnel
        "direction_hint": None,
        "contribution": "BLOCKED",
    },
    "FL-1": {
        "confidence_cap": 40,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    },
    "COMP-6": {
        "confidence_cap": 35,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    },
    # ── EXP : expansion directionnelle
    "EXP-UP-1": {
        "confidence_cap": None,
        "confidence_delta": +15,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "EXP-DOWN-1": {
        "confidence_cap": None,
        "confidence_delta": +15,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    "EXP-UP-0": {
        "confidence_cap": None,
        "confidence_delta": +8,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "EXP-DOWN-0": {
        "confidence_cap": None,
        "confidence_delta": +8,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    # ── FB : feedback loop
    "FB-UP": {
        "confidence_cap": None,
        "confidence_delta": +10,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "FB-DOWN": {
        "confidence_cap": None,
        "confidence_delta": +10,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    # ── COMP : compression / squeeze
    "COMP-0": {
        "confidence_cap": 40,
        "confidence_delta": -5,
        "force_verdict": None,
        "direction_hint": None,       # pas de direction — attendre résolution
        "contribution": "DEGRADED",
    },
    # ── DIV : divergence
    "DIV-0": {
        "confidence_cap": 35,
        "confidence_delta": -10,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "DEGRADED",
    },
    # ── MOD : modéré
    "MOD-UP": {
        "confidence_cap": None,
        "confidence_delta": +5,
        "force_verdict": None,
        "direction_hint": "UP",
        "contribution": "BOOST",
    },
    "MOD-DOWN": {
        "confidence_cap": None,
        "confidence_delta": +5,
        "force_verdict": None,
        "direction_hint": "DOWN",
        "contribution": "BOOST",
    },
    "MOD-MIX": {
        "confidence_cap": 45,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    },
}

# Règle : si regime urgency = CRITIQUE et phase = COMP → dégradation supplémentaire
_COMP_CRITIQUE_EXTRA_CAP = 30


def _get_regime_modifiers(
    regime_id: Optional[str],
    urgency: Optional[str],
    phase: Optional[str],
) -> dict:
    """Retourne les modificateurs pour le regime_id donné."""
    if not regime_id:
        return {
            "confidence_cap": None,
            "confidence_delta": 0,
            "force_verdict": None,
            "direction_hint": None,
            "contribution": "NEUTRAL",
        }
    mods = dict(_REGIME_VERDICT_TABLE.get(regime_id, {
        "confidence_cap": None,
        "confidence_delta": 0,
        "force_verdict": None,
        "direction_hint": None,
        "contribution": "NEUTRAL",
    }))
    # Dégradation supplémentaire : COMP CRITIQUE
    if phase == "COMP" and urgency == "CRITIQUE":
        mods["confidence_cap"] = min(
            mods.get("confidence_cap") or 100,
            _COMP_CRITIQUE_EXTRA_CAP,
        )
    return mods


def compute_decision(
    narrative_data: dict,
    arena_data: Optional[dict] = None,
    health_data: Optional[dict] = None,
    flip_use_in_signal: bool = True,
    gex_use_in_signal: bool = True,
    dex_use_in_signal: bool = True,
    # V5 — régime VEX/CEX
    vexcex_regime_id: Optional[str] = None,
    vexcex_phase: Optional[str] = None,
    vexcex_urgency: Optional[str] = None,
    vexcex_label: Optional[str] = None,
    # P2 — Directional Bias score (-100 → +100) transmis depuis /api/decision
    directional_bias_score: Optional[float] = None,
    # P3 — Probability Engine dominant signal (direction + score 0-100 centré à 50)
    pe_dominant_direction: Optional[str] = None,  # "BULL" | "BEAR" | None
    pe_dominant_probability: Optional[float] = None,  # 0-100, 50 = équilibre
    # P4 — Contexte DTE du signal dominant pour décroissance pré-expiration
    signal_dte_context: Optional[dict] = None,  # {"max_pain_dte": int, "flip_top_dte": int}
) -> ArbiteredDecision:
    """
    Arbitrage final entre tous les signaux disponibles.

    Règles de dégradation :
    - Neural WR < 45% → ignoré (LAB ONLY)
    - flip_use_in_signal=False → aucune conclusion basée sur flip
    - Contradictions non résolues → cap confiance à 40%
    - Data stale/insufficient → NO_TRADE automatique
    - NEU-* VEX/CEX → confiance plafonnée à 20% (V5)
    - FL-0 → verdict forcé OBSERVE (V5)
    - EXP-*-1 → boost +15 confiance (V5)
    - |directional_bias| ≥ 70 → override vers SIGNAL_UP/DOWN + boost confiance (P2)
    - PE dominant probability > 57% ou < 43% → signal contributif (P3)
    - Contrat dominant DTE ≤ 3 → signal dégradé (TTL pré-expiration) + warning (P4)
    - Contrat dominant DTE ≤ 1 → signal ignoré (expiré ou J-expiration)

    Règle de décision :
    - 0 signal actif → NO_TRADE
    - 1 signal actif → OBSERVE
    - 2+ signaux convergents sans contradiction → SIGNAL_UP ou SIGNAL_DOWN
    - 2+ signaux divergents → NO_TRADE
    """
    signals_used = []
    signals_ignored = []
    contradictions = narrative_data.get("contradictions", [])

    # ── 1. Qualité des données ──────────────────────────────────────────────
    data_quality = "OK"
    data_stale = narrative_data.get("data_stale", False)
    if data_stale:
        data_quality = "DEGRADED"

    if health_data:
        live_outcomes = health_data.get("live_outcomes", 0)
        if live_outcomes < 10:
            data_quality = "INSUFFICIENT"

    # ── P4 — Contexte TTL / pré-expiration ─────────────────────────────────
    _dte_ctx = signal_dte_context or {}
    _mp_dte   = _dte_ctx.get("max_pain_dte", 99)    # DTE du contrat max_pain dominant
    _flip_dte = _dte_ctx.get("flip_top_dte", 99)    # DTE du contrat flip dominant
    _min_dte  = min(_mp_dte, _flip_dte)             # DTE le plus court parmi les signaux clés

    # Facteur de décroissance TTL : 1.0 si DTE > 3, décroît jusqu'à 0 à DTE=0
    if _min_dte <= 0:
        _ttl_factor = 0.0      # expiré → signal invalide
    elif _min_dte == 1:
        _ttl_factor = 0.25     # J-expiration → signal très dégradé
    elif _min_dte <= 3:
        _ttl_factor = 0.55     # pré-expiration → signal modérément dégradé
    else:
        _ttl_factor = 1.0      # signal normal

    _pre_expiration_warning = None
    _signal_dte_degraded = False
    if _ttl_factor < 1.0:
        _signal_dte_degraded = True
        _expiry_label = f"J-{_min_dte}" if _min_dte > 0 else "J-expiration"
        _pre_expiration_warning = (
            f"Signal piloté par un contrat {_expiry_label} — "
            f"structure GEX/flip à réévaluer après l'expiration. "
            f"Thesis basée sur une structure à court terme uniquement."
        )

    # ── 2. Collecter les signaux actifs et les raisons d'exclusion ─────────
    range_mode = narrative_data.get("range_mode", False)
    asymmetric_side = narrative_data.get("asymmetric_side", "NEUTRAL")

    # GEX
    if gex_use_in_signal:
        gex_regime = narrative_data.get("gex_regime", "NEUTRE")
        # P4 — Décroissance TTL : si contrat dominant expire bientôt, dégrader le poids GEX
        _gex_weight = "élevé"
        _gex_ttl_note = ""
        if _ttl_factor <= 0.0:
            # Signal GEX piloté par un contrat expiré → ignorer
            signals_ignored.append({
                "name": "GEX",
                "reason": f"Contrat dominant expiré (DTE {_min_dte}) — signal invalide jusqu'au renouvellement",
            })
            gex_use_in_signal = False
        elif _ttl_factor < 1.0:
            _gex_weight = "faible" if _ttl_factor <= 0.25 else "modéré"
            _gex_ttl_note = f" [⚠ J-{_min_dte} — structure à réévaluer post-expiration]"

        if gex_use_in_signal:
            if gex_regime == "AMPLIFICATEUR":
                signals_used.append({
                    "name": "GEX",
                    "direction": "AMPLIFICATEUR",
                    "detail": f"Régime amplificateur actif{_gex_ttl_note}",
                    "weight": _gex_weight,
                })
            elif gex_regime == "STABILISANT":
                signals_used.append({
                    "name": "GEX",
                    "direction": "RANGE",
                    "detail": f"Régime stabilisant — compression{_gex_ttl_note}",
                    "weight": _gex_weight,
                })
    else:
        signals_ignored.append({
            "name": "GEX",
            "reason": "GEX dormant ou structurel — stock gamma inactif",
        })

    # DEX
    if dex_use_in_signal:
        dex_dir = narrative_data.get("dex_direction", "NEUTRAL")
        if dex_dir in ("BULLISH_FLOWS", "BEARISH_FLOWS"):
            # F8.8 — Enrichir le detail avec dex_activity_label si dispo
            _dex_lbl = narrative_data.get("dex_activity_label", "")
            _dex_ctx_used = narrative_data.get("dex_activity_context", "")
            _dex_detail = f"Flux dealers : {dex_dir}"
            if _dex_lbl:
                _dex_detail = f"Flux dealers : {dex_dir} ({_dex_lbl})"
            signals_used.append({
                "name": "DEX",
                "direction": "UP" if dex_dir == "BULLISH_FLOWS" else "DOWN",
                "detail": _dex_detail,
                "weight": "élevé",
            })
    else:
        # F8.8 — Raison explicite depuis dex_activity_context (narrative_resolver)
        _dex_ctx = narrative_data.get("dex_activity_context")
        signals_ignored.append({
            "name": "DEX",
            "reason": _dex_ctx or "DEX structurel ou dormant — pas de flux exploitable",
        })

    # Flip
    if not flip_use_in_signal:
        signals_ignored.append({
            "name": "Flip Level",
            "reason": "flip_level=None (all_gamma_negative) ou niveau dormant — non confirmé comme déclencheur",
        })

    # Neural engines — toujours ignorés tant que non validés
    signals_ignored.append({
        "name": "Neural (MLP + GRU)",
        "reason": "LAB ONLY — validation insuffisante. Non utilisés dans la décision.",
    })

    # P2 — Directional Bias (-100 → +100) : signal souverain si |score| ≥ 70
    # P4 — Dégradé par TTL si contrat GEX dominant expire bientôt (DB est partiellement GEX-driven)
    _db_score = directional_bias_score if directional_bias_score is not None else 0.0
    _db_ttl_note = f" [⚠ J-{_min_dte} — partiellement piloté par contrat expirant]" if _signal_dte_degraded else ""
    if directional_bias_score is not None:
        if abs(_db_score) >= 70:
            _db_dir = "UP" if _db_score > 0 else "DOWN"
            # P4 : si DTE très court, rétrograder de "élevé" à "modéré"
            _db_weight = ("modéré" if _ttl_factor <= 0.25 else "élevé") if _signal_dte_degraded else "élevé"
            signals_used.append({
                "name": "Directional Bias",
                "direction": _db_dir,
                "detail": f"Biais directionnel fort ({_db_score:+.0f}/100){_db_ttl_note}",
                "weight": _db_weight,
            })
        elif abs(_db_score) >= 40:
            _db_dir = "UP" if _db_score > 0 else "DOWN"
            _db_weight = "faible" if _signal_dte_degraded else "modéré"
            signals_used.append({
                "name": "Directional Bias",
                "direction": _db_dir,
                "detail": f"Biais directionnel modéré ({_db_score:+.0f}/100){_db_ttl_note}",
                "weight": _db_weight,
            })
        else:
            signals_ignored.append({
                "name": "Directional Bias",
                "reason": f"Biais faible ({_db_score:+.0f}/100) — sous le seuil d'action (±40)",
            })

    # P3 — Probability Engine : signal contributif si écart > seuil (|prob - 50| > 7pts)
    _pe_threshold = 7.0  # points au-dessus de 50 pour être contributif
    if pe_dominant_probability is not None and pe_dominant_direction is not None:
        _pe_edge = abs(pe_dominant_probability - 50.0)
        if _pe_edge > _pe_threshold:
            _pe_signal_dir = "UP" if pe_dominant_direction == "BULL" else "DOWN"
            signals_used.append({
                "name": "Probability Engine",
                "direction": _pe_signal_dir,
                "detail": f"Règles options : {pe_dominant_direction} {pe_dominant_probability:.0f}% (edge {_pe_edge:+.0f} pts)",
                "weight": "modéré" if _pe_edge >= 12 else "faible",
            })
        else:
            signals_ignored.append({
                "name": "Probability Engine",
                "reason": f"Probabilité {pe_dominant_probability:.0f}% — équilibre (edge {_pe_edge:+.1f} pts sous seuil {_pe_threshold})",
            })
    else:
        signals_ignored.append({
            "name": "Probability Engine",
            "reason": "Données PE non disponibles pour cet appel",
        })

    # V5 — VEX/CEX regime signal
    regime_mods = _get_regime_modifiers(vexcex_regime_id, vexcex_urgency, vexcex_phase)
    if vexcex_regime_id and vexcex_phase not in ("NEU",):
        dir_hint = regime_mods.get("direction_hint")
        contribution = regime_mods.get("contribution", "NEUTRAL")
        signals_used.append({
            "name": f"VEX/CEX [{vexcex_regime_id}]",
            "direction": dir_hint or "RANGE",
            "detail": vexcex_label or vexcex_regime_id,
            "weight": "modéré" if contribution == "BOOST" else "faible",
        })
    elif vexcex_regime_id:
        signals_ignored.append({
            "name": f"VEX/CEX [{vexcex_regime_id}]",
            "reason": f"Régime {vexcex_phase} — zone morte ou signaux faibles. Non contributif.",
        })

    # ── 3. Edge statistique Arena ───────────────────────────────────────────
    arena_status = "COLLECTING"
    arena_leader = None
    arena_leader_wr = None
    arena_leader_ev = None

    if arena_data:
        health = arena_data.get("arena_health") or arena_data
        live_oc = health.get("live_outcomes", 0) or 0
        lb = health.get("leaderboard_detail", {}) or {}

        if live_oc >= 100:
            best_mn, best_wr = None, 0.0
            for mn, info in lb.items():
                if mn in ("neural_tabular_engine", "temporal_neural_engine"):
                    continue
                wr = info.get("winrate") or 0.0
                n = info.get("live_outcomes", 0) or 0
                if n >= 30 and wr > best_wr:
                    best_wr = wr
                    best_mn = mn
                    arena_leader_ev = info.get("ev")

            if best_mn and best_wr > 0.52:
                arena_status = "LEADER_CLEAR"
                arena_leader = best_mn
                arena_leader_wr = round(best_wr, 3)
            else:
                arena_status = "NO_EDGE"
        else:
            arena_status = "COLLECTING"

    # ── 4. Décision finale ──────────────────────────────────────────────────
    n_signals = len(signals_used)
    up_signals   = [s for s in signals_used if s.get("direction") in ("UP", "AMPLIFICATEUR")]
    down_signals = [s for s in signals_used if s.get("direction") in ("DOWN", "AMPLIFICATEUR")]
    range_signals = [s for s in signals_used if s.get("direction") == "RANGE"]

    # Contradictions non résolues = confiance plafonnée
    has_contradiction = len(contradictions) > 0

    # V5 — force_verdict prend priorité absolue
    forced_verdict = regime_mods.get("force_verdict")

    if data_quality == "INSUFFICIENT":
        verdict = "NO_TRADE"
        phrase = "Données insuffisantes — attendre la collecte d'un historique exploitable."
        confidence_pct = 0
    elif forced_verdict:
        verdict = forced_verdict
        _display_verdict = "OBSERVER" if forced_verdict == "OBSERVE" else forced_verdict
        phrase = f"Régime VEX/CEX {vexcex_regime_id} ({vexcex_label}) — {_display_verdict} imposé."
        confidence_pct = 30
    elif n_signals == 0:
        verdict = "NO_TRADE"
        phrase = "Aucun signal actif — tous les indicateurs sont dormants ou non extrêmes."
        confidence_pct = 5
    elif range_signals and not up_signals and not down_signals:
        verdict = "OBSERVE"
        phrase = "Régime stabilisant — BTC en range. Attendre une cassure confirmée."
        confidence_pct = 40
    elif up_signals and not down_signals and not has_contradiction:
        verdict = "SIGNAL_UP"
        names = " + ".join(s["name"] for s in up_signals)
        # F13.5 — "convergent" seulement si ≥2 sources ; niveau nommé si disponible
        niveau_haut = narrative_data.get("niveau_haut")
        if len(up_signals) >= 2:
            phrase = f"Signal haussier convergent ({names})."
        else:
            phrase = f"Signal haussier ({names})."
        # F14.4 — trigger nommé avec OI du mur upside
        upside_ladder = narrative_data.get("upside_ladder") or []
        _trigger = upside_ladder[0] if upside_ladder else None
        if _trigger:
            _price = _trigger.get("price") or niveau_haut
            _oi = _trigger.get("oi")
            _oi_str = f" (mur {_oi:,.0f} BTC)" if _oi else ""
            if _price:
                phrase += f" Attendre cassure de ${_price:,.0f}{_oi_str} confirmée."
            else:
                phrase += " Attendre cassure du premier niveau upside confirmée."
        elif niveau_haut:
            phrase += f" Attendre cassure de ${niveau_haut:,.0f} confirmée."
        else:
            phrase += " Attendre cassure du premier niveau upside confirmée."
        confidence_pct = min(70, 40 + len(up_signals) * 15)
    elif down_signals and not up_signals and not has_contradiction:
        verdict = "SIGNAL_DOWN"
        names = " + ".join(s["name"] for s in down_signals)
        # F13.5 — "convergent" seulement si ≥2 sources ; niveau nommé si disponible
        niveau_bas = narrative_data.get("niveau_bas")
        if len(down_signals) >= 2:
            phrase = f"Signal baissier convergent ({names})."
        else:
            phrase = f"Signal baissier ({names})."
        # F14.4 — trigger nommé avec OI du mur downside
        downside_ladder = narrative_data.get("downside_ladder") or []
        _trigger_dn = downside_ladder[0] if downside_ladder else None
        if _trigger_dn:
            _price_dn = _trigger_dn.get("price") or niveau_bas
            _oi_dn = _trigger_dn.get("oi")
            _oi_str_dn = f" (mur {_oi_dn:,.0f} BTC)" if _oi_dn else ""
            if _price_dn:
                phrase += f" Attendre cassure de ${_price_dn:,.0f}{_oi_str_dn} confirmée."
            else:
                phrase += " Attendre cassure du premier niveau downside confirmée."
        elif niveau_bas:
            phrase += f" Attendre cassure de ${niveau_bas:,.0f} confirmée."
        else:
            phrase += " Attendre cassure du premier niveau downside confirmée."
        confidence_pct = min(70, 40 + len(down_signals) * 15)
    elif has_contradiction:
        verdict = "OBSERVE"
        phrase = f"Contradiction : {contradictions[0]['detail']}" if contradictions else "Signaux contradictoires — attendre résolution."  # F8.3
        confidence_pct = 20
    else:
        verdict = "OBSERVE"
        phrase = "Signaux mixtes ou insuffisants — pas d'edge directionnel clair."
        confidence_pct = 15

    # ── 5. Modifieurs de confiance ─────────────────────────────────────────
    # P2 — Directional Bias renforce la confiance quand cohérent avec le verdict
    if directional_bias_score is not None and verdict in ("SIGNAL_UP", "SIGNAL_DOWN"):
        _db_dir_verdict = "UP" if verdict == "SIGNAL_UP" else "DOWN"
        _db_dir_signal  = "UP" if _db_score > 0 else "DOWN"
        if _db_dir_verdict == _db_dir_signal:
            # Signal cohérent : boost proportionnel à l'intensité du biais
            if abs(_db_score) >= 70:
                confidence_pct = min(100, confidence_pct + 15)
            elif abs(_db_score) >= 40:
                confidence_pct = min(100, confidence_pct + 8)
        else:
            # Signal divergent : dégrader la confiance
            confidence_pct = max(0, confidence_pct - 15)

    # Arena
    if arena_status == "NO_EDGE":
        confidence_pct = min(confidence_pct, 30)
    elif arena_status == "LEADER_CLEAR" and arena_leader_wr and arena_leader_wr > 0.55:
        confidence_pct = min(100, confidence_pct + 10)

    # P3 — Probability Engine : modifier si signal cohérent/divergent avec verdict
    if pe_dominant_probability is not None and pe_dominant_direction is not None and verdict in ("SIGNAL_UP", "SIGNAL_DOWN"):
        _pe_verdict_dir = "BULL" if verdict == "SIGNAL_UP" else "BEAR"
        _pe_edge = abs(pe_dominant_probability - 50.0)
        if pe_dominant_direction == _pe_verdict_dir and _pe_edge > _pe_threshold:
            # Cohérent : boost modeste (PE est un moteur de règles, pas backtestvvalidé)
            _pe_boost = round(min(8, _pe_edge * 0.5))
            confidence_pct = min(100, confidence_pct + _pe_boost)
        elif pe_dominant_direction != _pe_verdict_dir and _pe_edge > _pe_threshold:
            # Divergent : légère dégradation
            confidence_pct = max(0, confidence_pct - 5)

    # P4 — TTL : décroissance pré-expiration appliquée à la confiance finale
    if _signal_dte_degraded:
        # Pénalité proportionnelle au facteur TTL : TTL=0.25 → -20pts, TTL=0.55 → -10pts
        _ttl_penalty = round((1.0 - _ttl_factor) * 25)
        confidence_pct = max(0, confidence_pct - _ttl_penalty)

    # V5 — VEX/CEX regime modifiers (seulement si pas forced_verdict déjà appliqué)
    if not forced_verdict:
        delta = regime_mods.get("confidence_delta", 0)
        cap = regime_mods.get("confidence_cap")
        confidence_pct = confidence_pct + delta
        if cap is not None:
            confidence_pct = min(confidence_pct, cap)

    confidence_pct = max(0, min(100, confidence_pct))

    if confidence_pct >= 60:
        confidence = "ÉLEVÉ"
    elif confidence_pct >= 35:
        confidence = "MODÉRÉ"
    else:
        confidence = "FAIBLE"

    # ── P0.5 — Calcul system_status ────────────────────────────────────────
    if data_quality == "INSUFFICIENT":
        system_status = "OFFLINE"
    elif data_quality == "DEGRADED":
        system_status = "DEGRADED"
    elif len(contradictions) > 0 and verdict == "OBSERVE":
        system_status = "CONFLICT"
    elif verdict in ("SIGNAL_UP", "SIGNAL_DOWN") and confidence_pct >= 60:
        system_status = "TRADEABLE"
    else:
        system_status = "OBSERVE"

    # F8.3 — Calcul state + action depuis verdict + contradictions + confidence
    if data_quality == "INSUFFICIENT":
        state = "RAS"
    elif verdict == "NO_TRADE":
        state = "RAS"
    elif verdict == "OBSERVE" and len(contradictions) > 0:
        state = "CONFLIT"
    elif verdict == "OBSERVE":
        state = "TENSION"
    elif confidence_pct < 40:
        state = "CRITIQUE"
    else:
        state = "TENSION"

    # F9.5 — AGIR_* exige ≥2 sources directionnelles convergentes ; 1 source = PRÉPARER + raison
    if verdict in ("SIGNAL_UP", "SIGNAL_DOWN"):
        _dir_signals = up_signals if verdict == "SIGNAL_UP" else down_signals
        _n_dir = len(_dir_signals)
        if confidence_pct >= 60 and _n_dir >= 2:
            action = "AGIR_LONG" if verdict == "SIGNAL_UP" else "AGIR_SHORT"
        elif _n_dir == 1:
            action = "PRÉPARER"
            _solo_name = _dir_signals[0]["name"]
            phrase = phrase + f" ({_solo_name} seul — attendre 2e confirmation)"
        else:
            action = "PRÉPARER"
    else:
        action = "OBSERVER"

    return ArbiteredDecision(
        verdict=verdict,
        confidence=confidence,
        confidence_pct=confidence_pct,
        phrase=phrase,
        signals_used=signals_used,
        signals_ignored=signals_ignored,
        contradictions=contradictions,
        data_quality=data_quality,
        arena_status=arena_status,
        arena_leader=arena_leader,
        arena_leader_wr=arena_leader_wr,
        arena_leader_ev=arena_leader_ev,
        generated_at=datetime.now(timezone.utc).isoformat(),
        system_status=system_status,
        vexcex_regime_id=vexcex_regime_id,
        vexcex_phase=vexcex_phase,
        vexcex_urgency=vexcex_urgency,
        vexcex_label=vexcex_label,
        vexcex_contribution=regime_mods.get("contribution"),
        state=state,
        action=action,
        directional_bias_score=directional_bias_score,
        pe_dominant_direction=pe_dominant_direction,
        pe_dominant_probability=pe_dominant_probability,
        global_confidence=confidence_pct,  # P3 — alias source de vérité unique
        pre_expiration_warning=_pre_expiration_warning,  # P4
        signal_dte_degraded=_signal_dte_degraded,        # P4
    )
