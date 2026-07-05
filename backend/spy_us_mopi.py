"""
US-MOPI — Mamos Options Pressure Index pour SPY (0-100).

0   = pression baissière extrême (put call extreme, VIX élevé)
50  = neutre
100 = pression haussière extrême (complacence options)

Logique contrarienne intégrée :
  US-MOPI très bas (<20) + VIX élevé → signal rebond contrarian possible
  US-MOPI très haut (>80) + VIX très bas (<12) → complacence → risque correction

Composantes :
  1. PCR Score (30 pts)  — Put/Call Ratio SPY near + global
  2. IV Rank Score (25 pts) — VIX rank 1an (IV rank élevé = fear = contrarian bullish)
  3. VIX Regime Score (25 pts) — Régime actuel
  4. Term Structure Score (15 pts) — Contango/Backwardation
  5. Drawdown Score (5 pts) — Distance depuis ATH récent
"""

from __future__ import annotations
from typing import Dict, Any, Optional


def compute_us_mopi(data: dict) -> dict:
    vix         = data.get("vix")
    iv_rank     = data.get("iv_rank")
    vix_regime  = data.get("vix_regime")
    pcr_vol     = data.get("pcr_spy_volume")
    pcr_oi      = data.get("pcr_spy_oi")
    pcr_near    = data.get("pcr_spy_near")
    pcr_equity  = data.get("pcr_equity")
    contango    = data.get("contango")
    dist_52w    = data.get("spy_dist_52w_high")

    components: Dict[str, Optional[float]] = {}
    weights: Dict[str, float] = {}

    # ── 1. PCR Score (30 pts) ─────────────────────────────────────────────────
    # PCR élevé = beaucoup de puts = peur = contrarian bullish → score haut
    # PCR < 0.7 = complacence = contrarian bearish → score bas
    pcr_score = None
    pcr_values = [v for v in [pcr_near, pcr_vol, pcr_equity] if v is not None]
    if pcr_values:
        pcr_avg = sum(pcr_values) / len(pcr_values)
        # PCR range typique 0.4 → 1.6
        # 0.4 → score 0 (complacence extrême)
        # 1.0 → score 50 (neutre)
        # 1.6 → score 100 (panique extrême = rebond contrarian)
        pcr_score = max(0.0, min(100.0, (pcr_avg - 0.4) / (1.6 - 0.4) * 100))
        components["pcr"] = round(pcr_score, 1)
        weights["pcr"] = 0.30
    elif pcr_oi is not None:
        pcr_avg = pcr_oi
        pcr_score = max(0.0, min(100.0, (pcr_avg - 0.4) / 1.2 * 100))
        components["pcr"] = round(pcr_score, 1)
        weights["pcr"] = 0.15  # poids réduit si seulement OI

    # ── 2. IV Rank Score (25 pts) ─────────────────────────────────────────────
    # IV Rank élevé = peur = contrarian bullish → score haut
    iv_score = None
    if iv_rank is not None:
        iv_score = float(iv_rank)  # déjà 0-100
        components["iv_rank"] = round(iv_score, 1)
        weights["iv_rank"] = 0.25

    # ── 3. VIX Regime Score (25 pts) ─────────────────────────────────────────
    # PANIC / PANIC_EXTREME → haut (rebond contrarian probable)
    # NORMAL / VOL_CRUSH → bas (complacence)
    regime_scores = {
        "PANIC_EXTREME": 95,
        "PANIC":         80,
        "RELIEF_RALLY":  70,
        "STRESS":        60,
        "ELEVATED":      45,
        "NORMAL":        30,
        "VOL_CRUSH":     15,
        "UNKNOWN":       50,
    }
    if vix_regime:
        regime_score = float(regime_scores.get(vix_regime, 50))
        components["vix_regime"] = regime_score
        weights["vix_regime"] = 0.25

    # ── 4. Term Structure Score (15 pts) ─────────────────────────────────────
    # Backwardation → stress → contrarian bullish → score haut
    # Contango profond → normalité / complacence → score bas
    term_score = None
    vix_vix3m = data.get("vix_vix3m_spread")
    if vix_vix3m is not None:
        # Backwardation (+valeur) → score haut ; contango (-valeur) → score bas
        # Range typique -5 à +10
        term_score = max(0.0, min(100.0, (vix_vix3m + 5) / 15 * 100))
        components["term_structure"] = round(term_score, 1)
        weights["term_structure"] = 0.15
    elif contango is not None:
        # Sans spread exact
        components["term_structure"] = 35.0 if contango else 65.0
        weights["term_structure"] = 0.08

    # ── 5. Drawdown Score (5 pts) ─────────────────────────────────────────────
    # Loin du ATH = stress = contrarian bullish
    if dist_52w is not None:
        # dist_52w : 0 = au ATH, -20 = -20% du ATH
        dd_score = max(0.0, min(100.0, abs(dist_52w) / 25.0 * 100))
        components["drawdown"] = round(dd_score, 1)
        weights["drawdown"] = 0.05

    # ── Score final ───────────────────────────────────────────────────────────
    if not weights:
        return {"score": None, "label": "NO_DATA", "components": {}, "contrarian_signal": None}

    total_weight = sum(weights.values())
    score = 0.0
    for comp_name, w in weights.items():
        comp_val = components.get(comp_name, 50.0)
        score += comp_val * (w / total_weight)

    score = max(0.0, min(100.0, score))
    score = round(score, 1)

    # ── Label ────────────────────────────────────────────────────────────────
    if score >= 80:
        label = "PRESSION_HAUSSIÈRE_EXTREME"
    elif score >= 65:
        label = "PRESSION_HAUSSIÈRE"
    elif score >= 55:
        label = "LÉGER_BIAIS_HAUSSIER"
    elif score >= 45:
        label = "NEUTRE"
    elif score >= 35:
        label = "LÉGER_BIAIS_BAISSIER"
    elif score >= 20:
        label = "PRESSION_BAISSIÈRE"
    else:
        label = "PRESSION_BAISSIÈRE_EXTREME"

    # ── Signal contrarian ────────────────────────────────────────────────────
    contrarian = None
    vix_val = vix or 0
    if score <= 20 and vix_val > 20:
        contrarian = "REBOND_CONTRARIAN"  # extrême peur + VIX élevé = setup rebond
    elif score >= 80 and vix_val < 13:
        contrarian = "RISQUE_CORRECTION"  # complacence extrême = risque

    # Phrase humaine
    narratives = {
        "PRESSION_HAUSSIÈRE_EXTREME":  "Pression options très haussière. Puts massifs = peur extrême. Historiquement favorable à un rebond.",
        "PRESSION_HAUSSIÈRE":          "Options signalent une pression haussière. Peur visible, rebond possible.",
        "LÉGER_BIAIS_HAUSSIER":        "Options légèrement favorables aux acheteurs. Contexte neutre à positif.",
        "NEUTRE":                       "Aucune pression options nette. Marché en attente.",
        "LÉGER_BIAIS_BAISSIER":        "Légère pression baissière options. Prudence.",
        "PRESSION_BAISSIÈRE":          "Pression baissière visible. Puts/Calls et IV défavorables.",
        "PRESSION_BAISSIÈRE_EXTREME":  "Pression options baissière extrême. Options sur SPY très chères. Complacence ou suroptimisme.",
    }

    return {
        "score": score,
        "label": label,
        "narrative": narratives.get(label, ""),
        "components": components,
        "weights": {k: round(v / total_weight, 3) for k, v in weights.items()},
        "contrarian_signal": contrarian,
    }
