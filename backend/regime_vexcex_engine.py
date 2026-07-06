"""
Regime VEX/CEX Engine — Classification pure des régimes options dealers.

Port Python de classifyRegimeFull (frontend/js/widgets/regime.js).

Ce module ne produit AUCUN verdict, AUCUN texte impératif, AUCUN sizing.
Il classifie uniquement le régime observed à partir des données options + GEX.

Outputs :
  regime_id       — identifiant court (ex. "COMP-0", "NEU-0", "EXP-UP")
  phase           — groupe de régime ("EXP" | "FB" | "FL" | "COMP" | "DIV" | "MOD" | "NEU")
  label           — libellé court lisible (ex. "COMPRESSION CRITIQUE")
  signals         — liste des signaux actifs ayant contribué à la classification
  magnitudes      — valeurs brutes utilisées pour la classification
  trends          — direction des tendances (vex_trend, cex_trend)
  flip_context    — contexte flip level (regime_meca, distance, incoherence)

Groupes de régime (en ordre de priorité) :
  NEU  — signaux faibles / zone morte (vexNeutral || cexNeutral)
  EXP  — expansion directionnelle (VEX + DEX alignés + magnitude élevée)
  FB   — feedback loop (VEX amplificateur + flip near)
  FL   — flip level (proximité flip < 1%)
  COMP — compression / squeeze (VEX + CEX contradictoires OU sans direction)
  DIV  — divergence (VEX et DEX de directions opposées)
  MOD  — modéré (signaux présents mais non extrêmes)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Seuils de magnitude (USD / BTC-delta) ─────────────────────────────────
# Calibration initiale sur données v2 (short-all) — à ajuster avec p80(|VEX|) historique
_VEX_BIG_THRESH = 500_000_000    # 500M USD — VEX significatif
_CEX_BIG_THRESH = 500            # 500 BTC/jour — CEX significatif
_VEX_EXTREME_THRESH = 2_000_000_000  # 2B USD — VEX extrême
_CEX_EXTREME_THRESH = 2_000         # 2000 BTC/jour — CEX extrême
_FLIP_NEAR_PCT = 0.01            # 1% — proximity seuil pour zone de flip
_FLIP_CRITICAL_PCT = 0.005       # 0.5% — zone critique flip


# ── Direction helpers ──────────────────────────────────────────────────────

def _is_bullish(direction: Optional[str]) -> bool:
    return bool(direction and direction.startswith("BULLISH"))


def _is_bearish(direction: Optional[str]) -> bool:
    return bool(direction and direction.startswith("BEARISH"))


def _is_neutral(direction: Optional[str]) -> bool:
    return not direction or direction == "NEUTRAL"


# ── Dataclass résultat ─────────────────────────────────────────────────────

@dataclass
class VexCexRegime:
    regime_id: str            # ex. "COMP-0", "NEU-0", "EXP-UP-1"
    phase: str                # "EXP" | "FB" | "FL" | "COMP" | "DIV" | "MOD" | "NEU"
    label: str                # libellé court (ex. "EXPANSION HAUSSIÈRE")
    urgency: str              # "CRITIQUE" | "ÉLEVÉE" | "MODÉRÉE" | "FAIBLE" | "NEUTRE"
    signals: list             # signaux actifs ayant contribué
    magnitudes: dict          # valeurs brutes: vex, cex, gex, dex, flip_dist_pct
    trends: dict              # vex_trend: "UP"|"DOWN"|"FLAT", cex_trend idem
    flip_context: dict        # regime_meca, flip_dist_pct, incoherent, flip_level


@dataclass
class VexCexInputs:
    """Valeurs LIVE + trends depuis l'historique v2."""
    # Valeurs brutes
    vex: float                          # USD
    cex: float                          # BTC/jour
    gex: float                          # USD (live depuis /api/dashboard)
    dex: float                          # USD net delta (live depuis /api/dealer_pressure)
    spot: float                         # BTC spot price

    # Directions (tri-état depuis le backend)
    vex_direction: Optional[str] = None   # "BULLISH_VANNA" | "BEARISH_VANNA" | "NEUTRAL"
    cex_direction: Optional[str] = None   # "BULLISH_CHARM" | "BEARISH_CHARM" | "NEUTRAL"

    # Trends (calculés depuis l'historique v2)
    vex_trend: str = "FLAT"             # "UP" | "DOWN" | "FLAT"
    cex_trend: str = "FLAT"             # "UP" | "DOWN" | "FLAT"

    # Contexte GEX flip (depuis V3-bis classify_regime_spot_flip)
    flip_level: Optional[float] = None
    flip_dist_pct: Optional[float] = None    # (spot - flip) / spot * 100
    regime_meca: str = "NEUTRE"              # STABILISANT | AMPLIFICATEUR | ZONE_DE_FLIP | NEUTRE
    regime_source: str = "gex_estime"        # "flip" | "gex_estime"
    gex_flip_incoherent: bool = False


def classify_regime_vexcex(inputs: VexCexInputs) -> VexCexRegime:
    """
    Classifie le régime VEX/CEX en un seul identifiant.

    Hiérarchie de priorité :
      1. NEU-0 — zone morte (vex OU cex NEUTRAL) → retour immédiat
      2. FL-0  — zone de flip (< 0.5% du flip)
      3. FL-1  — flip near (< 1% du flip)
      4. EXP   — expansion directionnelle (VEX + GEX/DEX alignés)
      5. FB    — feedback loop (VEX amplificateur + flip context)
      6. COMP  — compression / squeeze
      7. DIV   — divergence
      8. MOD   — modéré
      9. NEU-1 — résidu neutre (aucune condition précédente)
    """
    v = inputs
    vex = v.vex
    cex = v.cex
    gex = v.gex
    dex = v.dex

    vex_neutral = _is_neutral(v.vex_direction)
    cex_neutral = _is_neutral(v.cex_direction)
    vex_bull = _is_bullish(v.vex_direction) if v.vex_direction else vex > 0
    vex_bear = _is_bearish(v.vex_direction) if v.vex_direction else vex < 0
    cex_bull = _is_bullish(v.cex_direction) if v.cex_direction else cex > 0
    cex_bear = _is_bearish(v.cex_direction) if v.cex_direction else cex < 0

    big_vex = abs(vex) >= _VEX_BIG_THRESH
    big_cex = abs(cex) >= _CEX_BIG_THRESH
    extreme_vex = abs(vex) >= _VEX_EXTREME_THRESH
    extreme_cex = abs(cex) >= _CEX_EXTREME_THRESH

    near_flip = (
        v.flip_dist_pct is not None
        and abs(v.flip_dist_pct) <= _FLIP_NEAR_PCT * 100
    )
    critical_flip = (
        v.flip_dist_pct is not None
        and abs(v.flip_dist_pct) <= _FLIP_CRITICAL_PCT * 100
    )

    dex_bull = dex > 0
    dex_bear = dex < 0
    gex_amp = v.regime_meca == "AMPLIFICATEUR"
    gex_stab = v.regime_meca == "STABILISANT"

    magnitudes = {
        "vex": vex,
        "cex": cex,
        "gex": gex,
        "dex": dex,
        "flip_dist_pct": v.flip_dist_pct,
    }
    trends = {
        "vex_trend": v.vex_trend,
        "cex_trend": v.cex_trend,
    }
    flip_context = {
        "regime_meca": v.regime_meca,
        "flip_dist_pct": v.flip_dist_pct,
        "incoherent": v.gex_flip_incoherent,
        "flip_level": v.flip_level,
        "regime_source": v.regime_source,
    }

    def _mk(
        regime_id: str,
        phase: str,
        label: str,
        urgency: str,
        signals: list,
    ) -> VexCexRegime:
        return VexCexRegime(
            regime_id=regime_id,
            phase=phase,
            label=label,
            urgency=urgency,
            signals=signals,
            magnitudes=magnitudes,
            trends=trends,
            flip_context=flip_context,
        )

    # ── 1. NEU-0 — Zone morte (vex OU cex NEUTRAL) ────────────────────────
    # Priorité absolue : si l'un des signaux primaires est mort, pas de régime actionnable.
    if vex_neutral or cex_neutral:
        neutral_reasons = []
        if vex_neutral:
            neutral_reasons.append("VEX NEUTRAL")
        if cex_neutral:
            neutral_reasons.append("CEX NEUTRAL")
        return _mk(
            "NEU-0",
            "NEU",
            "SIGNAUX FAIBLES",
            "NEUTRE",
            neutral_reasons,
        )

    # ── 2. FL-0 — Zone de flip critique (< 0.5% du spot) ─────────────────
    if critical_flip:
        sigs = ["ZONE_FLIP_CRITIQUE"]
        if gex_amp:
            sigs.append("GEX_AMPLIFICATEUR")
        return _mk(
            "FL-0",
            "FL",
            "ZONE DE FLIP CRITIQUE",
            "CRITIQUE",
            sigs,
        )

    # ── 3. FL-1 — Flip near (< 1% du spot) ───────────────────────────────
    # Seulement si VEX/CEX ont une magnitude significative
    if near_flip and (big_vex or big_cex):
        sigs = ["FLIP_NEAR"]
        if big_vex:
            sigs.append("VEX_BIG")
        if big_cex:
            sigs.append("CEX_BIG")
        if gex_amp:
            sigs.append("GEX_AMPLIFICATEUR")
        return _mk(
            "FL-1",
            "FL",
            "PROXIMITÉ FLIP",
            "ÉLEVÉE",
            sigs,
        )

    # ── 4. EXP — Expansion directionnelle ─────────────────────────────────
    # Conditions : VEX big + DEX aligné + GEX amplificateur
    if big_vex and gex_amp:
        if vex_bull and dex_bull:
            urgency = "CRITIQUE" if extreme_vex else "ÉLEVÉE"
            return _mk(
                "EXP-UP-1",
                "EXP",
                "EXPANSION HAUSSIÈRE",
                urgency,
                ["VEX_BULL", "GEX_AMP", "DEX_BULL"],
            )
        if vex_bear and dex_bear:
            urgency = "CRITIQUE" if extreme_vex else "ÉLEVÉE"
            return _mk(
                "EXP-DOWN-1",
                "EXP",
                "EXPANSION BAISSIÈRE",
                urgency,
                ["VEX_BEAR", "GEX_AMP", "DEX_BEAR"],
            )

    # EXP sans DEX confirmation (VEX seul + GEX amp)
    if big_vex and gex_amp:
        if vex_bull:
            return _mk(
                "EXP-UP-0",
                "EXP",
                "EXPANSION HAUSSIÈRE PARTIELLE",
                "MODÉRÉE",
                ["VEX_BULL", "GEX_AMP"],
            )
        if vex_bear:
            return _mk(
                "EXP-DOWN-0",
                "EXP",
                "EXPANSION BAISSIÈRE PARTIELLE",
                "MODÉRÉE",
                ["VEX_BEAR", "GEX_AMP"],
            )

    # ── 5. FB — Feedback loop (VEX extrême + GEX stab + CEX aligné) ──────
    if extreme_vex:
        if vex_bull and cex_bull:
            return _mk(
                "FB-UP",
                "FB",
                "FEEDBACK HAUSSIER",
                "ÉLEVÉE",
                ["VEX_EXTREME", "CEX_BULL"],
            )
        if vex_bear and cex_bear:
            return _mk(
                "FB-DOWN",
                "FB",
                "FEEDBACK BAISSIER",
                "ÉLEVÉE",
                ["VEX_EXTREME", "CEX_BEAR"],
            )

    # ── 6. COMP-0 — Compression/squeeze (VEX + CEX contradictoires) ──────
    # Gate : magnitude requise (big_vex OR big_cex) — jamais sur signaux faibles
    if (big_vex or big_cex) and (
        (vex_bull and cex_bear) or (vex_bear and cex_bull)
    ):
        urgency = "CRITIQUE" if (extreme_vex or extreme_cex) else "ÉLEVÉE"
        sigs = []
        if vex_bull:
            sigs.append("VEX_BULL")
        else:
            sigs.append("VEX_BEAR")
        if cex_bull:
            sigs.append("CEX_BULL")
        else:
            sigs.append("CEX_BEAR")
        if gex_stab:
            sigs.append("GEX_STAB")
        return _mk(
            "COMP-0",
            "COMP",
            "COMPRESSION / SQUEEZE",
            urgency,
            sigs,
        )

    # COMP-6 — flip near sans magnitude (V3 gate : pas COMP-0)
    if near_flip and not (big_vex or big_cex):
        return _mk(
            "COMP-6",
            "COMP",
            "FLIP NEAR SANS MAGNITUDE",
            "FAIBLE",
            ["FLIP_NEAR", "VEX_SMALL", "CEX_SMALL"],
        )

    # ── 7. DIV — Divergence VEX/DEX ───────────────────────────────────────
    if big_vex and (
        (vex_bull and dex_bear) or (vex_bear and dex_bull)
    ):
        return _mk(
            "DIV-0",
            "DIV",
            "DIVERGENCE VEX/DEX",
            "MODÉRÉE",
            ["VEX_BULL" if vex_bull else "VEX_BEAR",
             "DEX_BEAR" if dex_bear else "DEX_BULL"],
        )

    # ── 8. MOD — Modéré (signaux présents mais non extrêmes) ─────────────
    if not vex_neutral and not cex_neutral:
        # Direction cohérente
        if (vex_bull and cex_bull) or (vex_bear and cex_bear):
            direction = "UP" if vex_bull else "DOWN"
            return _mk(
                f"MOD-{direction}",
                "MOD",
                "SIGNAL MODÉRÉ " + ("HAUSSIER" if direction == "UP" else "BAISSIER"),
                "FAIBLE",
                ["VEX_BULL" if vex_bull else "VEX_BEAR",
                 "CEX_BULL" if cex_bull else "CEX_BEAR"],
            )
        # Direction incohérente mais faible magnitude
        return _mk(
            "MOD-MIX",
            "MOD",
            "SIGNAUX MIXTES MODÉRÉS",
            "FAIBLE",
            ["VEX_BULL" if vex_bull else "VEX_BEAR",
             "CEX_BULL" if cex_bull else "CEX_BEAR"],
        )

    # ── 9. NEU-1 — Résidu neutre ───────────────────────────────────────────
    return _mk(
        "NEU-1",
        "NEU",
        "NEUTRE",
        "NEUTRE",
        [],
    )


def regime_to_dict(r: VexCexRegime) -> dict:
    """Sérialise VexCexRegime en dict JSON-compatible."""
    return {
        "regime_id": r.regime_id,
        "phase": r.phase,
        "label": r.label,
        "urgency": r.urgency,
        "signals": r.signals,
        "magnitudes": r.magnitudes,
        "trends": r.trends,
        "flip_context": r.flip_context,
    }
