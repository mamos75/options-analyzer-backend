"""
SPY Regime Engine — Classifie le régime de marché SPY/VIX.

Régimes :
  RISK_ON_TREND    — VIX bas, PCR bas, SPY en hausse tendancielle
  RISK_OFF_STRESS  — VIX > 20, puts en accumulation, SPY en baisse
  PANIC_REBOUND    — VIX > 25 ET en baisse → signal rebond imminent
  VOL_EXPANSION    — VIX en hausse rapide, expansion options OI
  VOL_CONTRACTION  — VIX en chute, contraction vol → tendance probable
  COMPLACENCY      — VIX < 13, PCR très bas, SPY proche ATH → danger
  NEUTRAL          — pas de signal dominant
"""

from __future__ import annotations
from typing import Optional


def compute_spy_regime(data: dict) -> str:
    vix        = data.get("vix")
    chg_1d     = data.get("vix_change_1d")
    chg_5d     = data.get("vix_change_5d")
    vix_regime = data.get("vix_regime")
    pcr_vol    = data.get("pcr_spy_volume")
    pcr_near   = data.get("pcr_spy_near")
    spy_chg    = data.get("spy_change_1d")
    spy_dd5    = data.get("spy_drawdown_5d")
    dist_ath   = data.get("spy_dist_52w_high")

    if vix is None:
        return "NEUTRAL"

    pcr = pcr_near or pcr_vol

    # PANIC_REBOUND — VIX > 25 qui redescend rapidement
    if vix > 25 and chg_1d is not None and chg_1d < -2:
        return "PANIC_REBOUND"

    # RISK_OFF_STRESS — VIX > 20, marché stressé
    if vix > 20:
        if spy_dd5 is not None and spy_dd5 < -3:
            return "RISK_OFF_STRESS"
        if pcr is not None and pcr > 1.0:
            return "RISK_OFF_STRESS"
        return "RISK_OFF_STRESS"

    # VOL_EXPANSION — VIX monte vite sans encore dépasser 20
    if chg_5d is not None and chg_5d > 3 and vix > 15:
        return "VOL_EXPANSION"

    # VOL_CONTRACTION — VIX chute vite
    if chg_5d is not None and chg_5d < -3 and vix <= 20:
        return "VOL_CONTRACTION"

    # COMPLACENCY — VIX très bas + PCR bas + SPY proche ATH
    if vix < 13:
        complacent_pcr = pcr is not None and pcr < 0.70
        near_ath = dist_ath is not None and dist_ath > -3
        if complacent_pcr or near_ath:
            return "COMPLACENCY"

    # RISK_ON_TREND — VIX bas + SPY en hausse + PCR normal
    if vix < 17 and (spy_chg is None or spy_chg >= 0):
        return "RISK_ON_TREND"

    return "NEUTRAL"


def spy_regime_description(regime: str) -> str:
    descriptions = {
        "RISK_ON_TREND":   "Marché en mode risk-on. VIX bas, tendance haussière SPY dominante.",
        "RISK_OFF_STRESS": "Stress de marché. VIX élevé, puts en accumulation, SPY sous pression.",
        "PANIC_REBOUND":   "Panique en train de se résorber. VIX en forte baisse. Setup rebond actif.",
        "VOL_EXPANSION":   "Expansion de volatilité. VIX en accélération. Mouvement directionnel imminent.",
        "VOL_CONTRACTION": "Contraction de volatilité. VIX en chute. Tendance probable en formation.",
        "COMPLACENCY":     "Complacence. VIX trop bas, PCR faible. Risque correction si déclencheur.",
        "NEUTRAL":         "Régime indéterminé. Pas de signal dominant identifié.",
    }
    return descriptions.get(regime, "")


def spy_regime_color(regime: str) -> str:
    colors = {
        "RISK_ON_TREND":   "#10B981",
        "RISK_OFF_STRESS": "#F97316",
        "PANIC_REBOUND":   "#06B6D4",
        "VOL_EXPANSION":   "#F59E0B",
        "VOL_CONTRACTION": "#8B5CF6",
        "COMPLACENCY":     "#EF4444",
        "NEUTRAL":         "#6B7280",
    }
    return colors.get(regime, "#6B7280")
