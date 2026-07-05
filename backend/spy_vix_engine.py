"""
VIX Regime Engine — SPY Analyzer.

Régimes :
  NORMAL        — VIX < 15, marché calme
  ELEVATED      — VIX 15-20, vigilance
  STRESS        — VIX 20-25, pression
  PANIC         — VIX > 25, peur
  PANIC_EXTREME — VIX > 35, capitulation
  RELIEF_RALLY  — VIX en forte baisse depuis STRESS/PANIC
  VOL_CRUSH     — VIX < 13 + baisse rapide + retour contango
"""

from __future__ import annotations
from typing import Dict, Any, Optional


def compute_vix_features(data: dict) -> dict:
    """Calcule les features VIX dérivées nécessaires aux moteurs."""
    vix     = data.get("vix")
    vix9d   = data.get("vix9d")
    vix3m   = data.get("vix3m")
    vix6m   = data.get("vix6m")
    vvix    = data.get("vvix")
    chg_1d  = data.get("vix_change_1d")
    chg_5d  = data.get("vix_change_5d")
    pct     = data.get("vix_percentile")
    spread_93 = data.get("vix9d_vix_spread")   # VIX9D - VIX (positif = contango court terme)
    spread_m3 = data.get("vix_vix3m_spread")   # VIX - VIX3M (positif = backwardation)
    contango  = data.get("contango")            # 1=contango, 0=backwardation

    return {
        "vix_level": vix,
        "vix_change_1d": chg_1d,
        "vix_change_5d": chg_5d,
        "vix9d_vix_spread": spread_93,
        "vix_vix3m_spread": spread_m3,
        "contango_backwardation": "contango" if contango else "backwardation" if contango is not None else None,
        "vix_percentile": pct,
        "vol_of_vol": vvix,
        "is_backwardation": (spread_m3 is not None and spread_m3 > 0),
        "is_front_stressed": (spread_93 is not None and spread_93 < -1),  # VIX9D < VIX-1 → court terme amplifié
    }


def compute_vix_regime(features: dict) -> str:
    """Classifie le régime VIX à partir des features calculées."""
    vix     = features.get("vix_level")
    chg_1d  = features.get("vix_change_1d")
    chg_5d  = features.get("vix_change_5d")
    backwd  = features.get("is_backwardation", False)

    if vix is None:
        return "UNKNOWN"

    # PANIC EXTREME
    if vix > 35:
        if chg_1d is not None and chg_1d < -2:
            return "RELIEF_RALLY"
        return "PANIC_EXTREME"

    # PANIC
    if vix > 25:
        if chg_1d is not None and chg_1d < -2:
            return "RELIEF_RALLY"
        return "PANIC"

    # STRESS
    if vix > 20:
        if chg_1d is not None and chg_1d < -1.5 and chg_5d is not None and chg_5d < -3:
            return "RELIEF_RALLY"
        return "STRESS"

    # ELEVATED
    if vix > 15:
        return "ELEVATED"

    # VOL CRUSH — VIX très bas + baisse récente + contango
    if vix < 13 and chg_5d is not None and chg_5d < -3 and not backwd:
        return "VOL_CRUSH"

    return "NORMAL"


def vix_regime_to_dict(regime: str, features: dict) -> dict:
    """Sérialise le régime + description humaine pour l'API."""
    descriptions = {
        "NORMAL":        "Volatilité basse. Marché confiant. Faible pression optionnelle.",
        "ELEVATED":      "Volatilité en hausse. Vigilance recommandée. Incertitude présente.",
        "STRESS":        "Volatilité élevée. Pression de vente options visible. Contexte défavorable.",
        "PANIC":         "Panique. VIX > 25. Capitulation possible. Rebond contrarian historiquement fréquent.",
        "PANIC_EXTREME": "Panique extrême. VIX > 35. Capitulation. Rebonds violents historiques.",
        "RELIEF_RALLY":  "VIX en forte baisse depuis stress. Compression de volatilité probable. Risk-on.",
        "VOL_CRUSH":     "VIX très bas + décélération rapide. Complacence. Risque correction si déclencheur.",
        "UNKNOWN":       "Données insuffisantes pour classifier le régime.",
    }
    colors = {
        "NORMAL": "#10B981",
        "ELEVATED": "#F59E0B",
        "STRESS": "#F97316",
        "PANIC": "#EF4444",
        "PANIC_EXTREME": "#7C3AED",
        "RELIEF_RALLY": "#06B6D4",
        "VOL_CRUSH": "#6B7280",
        "UNKNOWN": "#9CA3AF",
    }
    return {
        "regime": regime,
        "description": descriptions.get(regime, ""),
        "color": colors.get(regime, "#9CA3AF"),
        "features": features,
    }
