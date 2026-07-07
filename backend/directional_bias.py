"""
Directional Bias Score — synthèse directionnelle des options BTC.

Score -100 à +100 : positif = biais haussier, négatif = biais baissier.

F15 — 3 signaux renormalisés à 100 % (MOPI retiré le 07/07/2026) :
  1. DEX Actionable (poids 50) — flux dealers exploitables maintenant
  2. PCR Weighted   (poids 28) — Put/Call ratio contrarian
  3. GEX Asymétrie  (poids 22) — asymétrie du risque GEX

DISCONTINUITÉ : scores non comparables avant/après le 07/07/2026.
Cible probable + stop logique basés sur la mécanique options (pas une prédiction TA).
Stop = niveau qui invalide MÉCANIQUEMENT le régime, pas un stop-loss trader.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# Cap DEX actionable calibré sur l'ordre de grandeur réel BTC options
_DEX_ACTIONABLE_CAP_BTC = 50.0

# Poids des signaux — F15 renormalisés (3 sources, somme = 100)
# Anciens poids : DEX=35, PCR=20, GEX=15 sur 70 → x(100/70)
_W_DEX = 50.0   # 35/70 * 100 ≈ 50
_W_PCR = 28.0   # 20/70 * 100 ≈ 28.6 → 28
_W_GEX = 22.0   # 15/70 * 100 ≈ 21.4 → 22


@dataclass
class DirectionalBiasSignal:
    name: str
    contribution: float       # contribution signée au score total
    direction: str            # "BULL" | "BEAR" | "NEUTRAL"
    active: bool              # False si signal exclu (dormant/structurel)
    detail: str               # phrase humaine expliquant la contribution


@dataclass
class DirectionalBias:
    score: float              # -100 à +100
    label: str                # "HAUSSIER FORT" | "HAUSSIER MODÉRÉ" | "NEUTRE" | "BAISSIER MODÉRÉ" | "BAISSIER FORT"
    emoji: str
    confidence: str           # "Très haute (4/4)" | "Haute (3/4)" | "Moyenne (2/4)" | "Faible (1/4)"
    confluence_count: int     # 0-4 signaux convergents

    # Niveaux d'attraction mécanique — cible 1 (la plus proche) et cible 2 (l'extension)
    target_up: Optional[float]        # cible si cassure hausse (niveau wall/attraction au-dessus)
    target_up_label: str
    target_up_2: Optional[float]      # deuxième cible haussière (extension)
    target_up_2_label: str
    target_down: Optional[float]      # cible si cassure baisse (niveau wall/attraction en dessous)
    target_down_label: str
    target_down_2: Optional[float]    # deuxième cible baissière (extension)
    target_down_2_label: str

    # Stop logique — invalide MÉCANIQUEMENT le scénario options
    stop_logical: Optional[float]     # niveau d'invalidation mécanique du régime
    stop_label: str
    stop_type: str                    # "flip_level" | "niveau_haut" | "niveau_bas" | "none"

    # Cible principale selon le biais
    primary_target: Optional[float]   # = target_up si bullish, target_down si bearish
    primary_target_pct: Optional[float]  # distance % depuis spot

    # Détail signaux
    signals: list = field(default_factory=list)   # List[DirectionalBiasSignal]

    # Phrase décision
    phrase: str = ""


def compute_directional_bias(
    pc_ratio_weighted: float,
    gex_use_in_signal: bool,
    dex_use_in_signal: bool,
    dex_actionable_btc: float,
    asymmetric_side: str,        # "DOWN" | "UP" | "BALANCED" | "NEUTRAL"
    spot: float,
    flip_level: Optional[float],
    niveau_haut: float,
    niveau_haut_label: str,
    niveau_bas: float,
    niveau_bas_label: str,
    max_pain_near_strike: Optional[float] = None,
    gravity_target: Optional[float] = None,
    gravity_targets_list: Optional[list] = None,  # list[tuple[float, str]]
) -> DirectionalBias:

    signals: list[DirectionalBiasSignal] = []

    # ── 1. DEX Actionable (poids 50, uniquement si dex_use_in_signal) ────────
    if dex_use_in_signal:
        dex_norm = max(-1.0, min(1.0, dex_actionable_btc / _DEX_ACTIONABLE_CAP_BTC))
        dex_contrib = round(dex_norm * _W_DEX, 1)
        dex_dir = "BULL" if dex_contrib > 1 else ("BEAR" if dex_contrib < -1 else "NEUTRAL")
        dex_btc_abs = abs(dex_actionable_btc)
        if dex_actionable_btc > 5:
            dex_detail = f"Dealers acheteurs net ({dex_btc_abs:.1f} BTC actionnable) — pression haussière"
        elif dex_actionable_btc < -5:
            dex_detail = f"Dealers vendeurs net ({dex_btc_abs:.1f} BTC actionnable) — pression baissière"
        else:
            dex_detail = f"DEX actionnable quasi-neutre ({dex_btc_abs:.1f} BTC)"
        signals.append(DirectionalBiasSignal("DEX Actionable", dex_contrib, dex_dir, True, dex_detail))
    else:
        signals.append(DirectionalBiasSignal(
            "DEX Actionable", 0.0, "NEUTRAL", False,
            "DEX structurel ou dormant — stock de delta, pas un flux exploitable"
        ))
        dex_contrib = 0.0

    # ── 2. PCR Weighted contrarian (poids 28) ────────────────────────────────
    pc = pc_ratio_weighted
    if pc >= 1.5:
        pcr_norm = min(1.0, (pc - 1.5) / 0.5 + 0.6)  # 0.6 à 1.0 au-dessus de 1.5
    elif pc <= 0.5:
        pcr_norm = max(-1.0, -(0.5 - pc) / 0.5 - 0.6)
    else:
        pcr_norm = (pc - 1.0) / 0.5 * (-0.6)  # inverse : PCR=1.0 = neutre, PCR>1 = plus de puts = contrarian bullish
    # PCR contrarian : beaucoup de puts → le marché se protège → signal haussier contrarian
    pcr_contrib = round(pcr_norm * _W_PCR, 1)
    pcr_dir = "BULL" if pcr_contrib > 1 else ("BEAR" if pcr_contrib < -1 else "NEUTRAL")
    if pc >= 1.3:
        pcr_detail = f"PCR {pc:.2f} — excès de protection put (contrarian haussier)"
    elif pc <= 0.7:
        pcr_detail = f"PCR {pc:.2f} — excès calls, marché sur-optimiste (contrarian baissier)"
    else:
        pcr_detail = f"PCR {pc:.2f} — équilibre put/call neutre"
    signals.append(DirectionalBiasSignal("PCR Weighted", pcr_contrib, pcr_dir, True, pcr_detail))

    # ── 3. GEX Asymétrie (poids 22, uniquement si gex_use_in_signal) ─────────
    if gex_use_in_signal and asymmetric_side in ("UP", "DOWN"):
        gex_contrib = _W_GEX if asymmetric_side == "UP" else -_W_GEX
        gex_contrib = round(gex_contrib, 1)
        gex_dir = "BULL" if asymmetric_side == "UP" else "BEAR"
        if asymmetric_side == "UP":
            gex_detail = "Asymétrie GEX vers le haut — si cassure, amplification haussière mécanique"
        else:
            gex_detail = "Asymétrie GEX vers le bas — si cassure, amplification baissière mécanique"
    else:
        gex_contrib = 0.0
        gex_dir = "NEUTRAL"
        if not gex_use_in_signal:
            gex_detail = "GEX dormant/structurel — exclu du signal directionnel"
        else:
            gex_detail = f"Asymétrie GEX {asymmetric_side} — neutre ou équilibré"
    signals.append(DirectionalBiasSignal("GEX Asymétrie", gex_contrib, gex_dir, gex_use_in_signal, gex_detail))

    # ── Score final ───────────────────────────────────────────────────────────
    raw_score = dex_contrib + pcr_contrib + gex_contrib
    score = round(max(-100.0, min(100.0, raw_score)), 1)

    label, emoji = _classify_bias(score)

    # ── Confiance (convergence des signaux actifs) ────────────────────────────
    score_sign = 1 if score > 0 else (-1 if score < 0 else 0)
    convergent = sum(
        1 for s in signals
        if s.active and s.contribution * score_sign > 1
    )
    total_active = sum(1 for s in signals if s.active)
    confidence, confluence_count = _build_confidence(convergent, total_active)

    # ── Cibles mécaniques ──────────────────────────────────────────────────────
    (target_up, target_up_label), target_up_2_pair = _best_targets_up(
        spot, niveau_haut, niveau_haut_label, gravity_target, max_pain_near_strike,
        gravity_targets_list=gravity_targets_list,
    )
    target_up_2 = target_up_2_pair[0] if target_up_2_pair else None
    target_up_2_label = target_up_2_pair[1] if target_up_2_pair else ""

    (target_down, target_down_label), target_down_2_pair = _best_targets_down(
        spot, niveau_bas, niveau_bas_label, gravity_target, max_pain_near_strike,
        gravity_targets_list=gravity_targets_list,
    )
    target_down_2 = target_down_2_pair[0] if target_down_2_pair else None
    target_down_2_label = target_down_2_pair[1] if target_down_2_pair else ""

    # ── Stop logique (invalidation mécanique) ────────────────────────────────
    stop_logical, stop_label, stop_type = _compute_stop(
        score, spot, flip_level, niveau_haut, niveau_bas
    )

    # ── Cible principale selon biais ─────────────────────────────────────────
    if score > 15:
        primary_target = target_up
        primary_target_pct = round((target_up - spot) / spot * 100, 1) if target_up else None
    elif score < -15:
        primary_target = target_down
        primary_target_pct = round((target_down - spot) / spot * 100, 1) if target_down else None
    else:
        primary_target = None
        primary_target_pct = None

    phrase = _build_phrase(score, label, primary_target, primary_target_pct, stop_logical, spot, confidence)

    return DirectionalBias(
        score=score,
        label=label,
        emoji=emoji,
        confidence=confidence,
        confluence_count=confluence_count,
        target_up=target_up,
        target_up_label=target_up_label,
        target_up_2=target_up_2,
        target_up_2_label=target_up_2_label,
        target_down=target_down,
        target_down_label=target_down_label,
        target_down_2=target_down_2,
        target_down_2_label=target_down_2_label,
        stop_logical=stop_logical,
        stop_label=stop_label,
        stop_type=stop_type,
        primary_target=primary_target,
        primary_target_pct=primary_target_pct,
        signals=signals,
        phrase=phrase,
    )


def _classify_bias(score: float) -> tuple[str, str]:
    if score >= 60:
        return "HAUSSIER FORT", "🟢"
    elif score >= 25:
        return "HAUSSIER MODÉRÉ", "🟩"
    elif score > -25:
        return "NEUTRE", "🟡"
    elif score > -60:
        return "BAISSIER MODÉRÉ", "🟧"
    return "BAISSIER FORT", "🔴"


def _build_confidence(convergent: int, total_active: int) -> tuple[str, int]:
    # F15 — max 3 sources (MOPI retiré) : seuil Très haute à 3
    if total_active == 0:
        return "N/A (0 signal actif)", 0
    if convergent >= 3:
        return f"Très haute ({convergent}/{total_active})", convergent
    elif convergent == 2:
        return f"Haute ({convergent}/{total_active})", convergent
    elif convergent == 1:
        return f"Faible ({convergent}/{total_active})", convergent
    return f"Conflictuelle (0/{total_active})", 0


def _dedup_candidates(candidates: list[tuple[float, str]], threshold_pct: float = 0.3) -> list[tuple[float, str]]:
    """Supprime les doublons (niveaux à moins de threshold_pct% d'écart)."""
    result: list[tuple[float, str]] = []
    for c in candidates:
        if not result or abs(c[0] - result[-1][0]) / result[-1][0] * 100 > threshold_pct:
            result.append(c)
    return result


def _best_targets_up(
    spot: float,
    niveau_haut: float,
    niveau_haut_label: str,
    gravity_target: Optional[float],
    max_pain_near: Optional[float],
    gravity_targets_list: Optional[list] = None,  # list[tuple[float, str]]
) -> tuple[tuple[float, str], Optional[tuple[float, str]]]:
    """Retourne les 2 meilleures cibles haussières triées par distance au spot (obj1=plus proche, obj2=suivant)."""
    candidates: list[tuple[float, str]] = []

    if niveau_haut > spot * 1.001:
        candidates.append((niveau_haut, niveau_haut_label or f"Résistance ${niveau_haut:,.0f}"))

    if gravity_targets_list:
        for g_price, g_label in gravity_targets_list:
            if g_price > spot * 1.001:
                candidates.append((g_price, g_label))
    elif gravity_target and gravity_target > spot * 1.001:
        candidates.append((gravity_target, f"Zone Gravity ${gravity_target:,.0f}"))

    if max_pain_near and max_pain_near > spot * 1.001:
        candidates.append((max_pain_near, f"Max Pain ${max_pain_near:,.0f}"))

    if not candidates:
        return (niveau_haut, niveau_haut_label or f"Résistance ${niveau_haut:,.0f}"), None

    candidates_sorted = sorted(candidates, key=lambda c: c[0])
    candidates_dedup = _dedup_candidates(candidates_sorted)

    obj1 = candidates_dedup[0]
    obj2 = candidates_dedup[1] if len(candidates_dedup) > 1 else None
    return obj1, obj2


def _best_targets_down(
    spot: float,
    niveau_bas: float,
    niveau_bas_label: str,
    gravity_target: Optional[float],
    max_pain_near: Optional[float],
    gravity_targets_list: Optional[list] = None,  # list[tuple[float, str]]
) -> tuple[tuple[float, str], Optional[tuple[float, str]]]:
    """Retourne les 2 meilleures cibles baissières triées par distance au spot (obj1=plus proche, obj2=suivant)."""
    candidates: list[tuple[float, str]] = []

    if niveau_bas < spot * 0.999:
        candidates.append((niveau_bas, niveau_bas_label or f"Support ${niveau_bas:,.0f}"))

    if gravity_targets_list:
        for g_price, g_label in gravity_targets_list:
            if g_price < spot * 0.999:
                candidates.append((g_price, g_label))
    elif gravity_target and gravity_target < spot * 0.999:
        candidates.append((gravity_target, f"Zone Gravity ${gravity_target:,.0f}"))

    if max_pain_near and max_pain_near < spot * 0.999:
        candidates.append((max_pain_near, f"Max Pain ${max_pain_near:,.0f}"))

    if not candidates:
        return (niveau_bas, niveau_bas_label or f"Support ${niveau_bas:,.0f}"), None

    # Trier du plus proche (plus haut) au plus loin (plus bas)
    candidates_sorted = sorted(candidates, key=lambda c: c[0], reverse=True)
    candidates_dedup = _dedup_candidates(candidates_sorted)

    obj1 = candidates_dedup[0]
    obj2 = candidates_dedup[1] if len(candidates_dedup) > 1 else None
    return obj1, obj2


def _compute_stop(
    score: float,
    spot: float,
    flip_level: Optional[float],
    niveau_haut: float,
    niveau_bas: float,
) -> tuple[Optional[float], str, str]:
    """Stop logique = niveau qui invalide MÉCANIQUEMENT le régime options.

    Bullish  : le flip level EN DESSOUS du spot (dealers basculent AMPLIFICATEUR baissier)
    Bearish  : le flip level AU DESSUS du spot (dealers basculent AMPLIFICATEUR haussier)
    Neutre   : pas de stop logique clair
    """
    if score > 15:
        # Scénario haussier — stop = là où les dealers basculent contre le mouvement
        if flip_level is not None and flip_level < spot:
            dist_pct = round((flip_level - spot) / spot * 100, 1)
            return (
                flip_level,
                f"Flip GEX ${flip_level:,.0f} ({dist_pct:+.1f}%) — dealers basculent AMPLIFICATEUR baissier",
                "flip_level",
            )
        else:
            dist_pct = round((niveau_bas - spot) / spot * 100, 1)
            return (
                niveau_bas,
                f"Support options ${niveau_bas:,.0f} ({dist_pct:+.1f}%) — invalidation si cassé",
                "niveau_bas",
            )

    elif score < -15:
        # Scénario baissier — stop = là où les dealers basculent contre la baisse
        if flip_level is not None and flip_level > spot:
            dist_pct = round((flip_level - spot) / spot * 100, 1)
            return (
                flip_level,
                f"Flip GEX ${flip_level:,.0f} ({dist_pct:+.1f}%) — dealers basculent AMPLIFICATEUR haussier",
                "flip_level",
            )
        else:
            dist_pct = round((niveau_haut - spot) / spot * 100, 1)
            return (
                niveau_haut,
                f"Résistance options ${niveau_haut:,.0f} ({dist_pct:+.1f}%) — invalidation si franchie",
                "niveau_haut",
            )

    return None, "Signal trop faible — pas de stop mécanique défini", "none"


def _build_phrase(
    score: float,
    label: str,
    primary_target: Optional[float],
    primary_target_pct: Optional[float],
    stop_logical: Optional[float],
    spot: float,
    confidence: str,
) -> str:
    if score >= 60:
        base = "Options très alignées hausse."
    elif score >= 25:
        base = "Biais haussier modéré selon options."
    elif score > -25:
        base = "Signal mixte — pas de direction claire."
    elif score > -60:
        base = "Biais baissier modéré selon options."
    else:
        base = "Options très alignées baisse."

    parts = [base]

    if primary_target and primary_target_pct:
        sign = "+" if primary_target_pct > 0 else ""
        parts.append(f"Objectif mécanique : ${primary_target:,.0f} ({sign}{primary_target_pct:.1f}%).")

    if stop_logical:
        stop_pct = round((stop_logical - spot) / spot * 100, 1)
        sign = "+" if stop_pct > 0 else ""
        parts.append(f"Stop mécanique : ${stop_logical:,.0f} ({sign}{stop_pct:.1f}%).")

    parts.append(f"Confiance : {confidence}.")

    return " ".join(parts)


def directional_bias_to_dict(db: DirectionalBias) -> dict:
    return {
        "score": db.score,
        "label": db.label,
        "emoji": db.emoji,
        "confidence": db.confidence,
        "confluence_count": db.confluence_count,
        "target_up": db.target_up,
        "target_up_label": db.target_up_label,
        "target_up_2": db.target_up_2,
        "target_up_2_label": db.target_up_2_label,
        "target_down": db.target_down,
        "target_down_label": db.target_down_label,
        "target_down_2": db.target_down_2,
        "target_down_2_label": db.target_down_2_label,
        "stop_logical": db.stop_logical,
        "stop_label": db.stop_label,
        "stop_type": db.stop_type,
        "primary_target": db.primary_target,
        "primary_target_pct": db.primary_target_pct,
        "phrase": db.phrase,
        "signals": [
            {
                "name": s.name,
                "contribution": s.contribution,
                "direction": s.direction,
                "active": s.active,
                "detail": s.detail,
            }
            for s in db.signals
        ],
    }
