"""
Conviction Score V1 — passe de "événement détecté" à "conviction mesurée".

Score sur 10. Alerte Telegram uniquement si score ≥ MIN_SCORE_TO_SEND (défaut : 5).

Composantes :
  1. Tag d'activité  DORMANT→-3  STRUCTURAL→-2  ACTIVE→+1  ACTIONABLE→+2
  2. Distance spot   ≤2%→+2      ≤5%→+1         >5%→0
  3. Confluence      DEX→+1      GEX→+1          Gravity→+1
  4. Contradiction   −1

Règle clé (feedback UX) : distance >5% = JAMAIS un déclencheur, même ACTIONABLE.
"""

from dataclasses import dataclass
from typing import Optional

MIN_SCORE_TO_SEND = 5   # seuil d'envoi Telegram

_TAG_PTS = {
    "ACTIONABLE": 2,
    "ACTIVE":     1,
    "STRUCTURAL": -2,
    "DORMANT":    -3,
}


@dataclass
class ConvictionResult:
    score:     int
    send:      bool
    reason:    str    # phrase humaine expliquant la décision
    breakdown: dict   # détail pour logs


def compute_conviction_score(
    activity_tag: str,
    distance_pct: float,
    dex_confirms: bool = False,
    gex_confirms: bool = False,
    gravity_confirms: bool = False,
    major_contradiction: bool = False,
    min_score: int = MIN_SCORE_TO_SEND,
) -> ConvictionResult:
    score = 0

    tag_pts = _TAG_PTS.get(activity_tag, 0)
    score += tag_pts

    if distance_pct <= 2.0:
        dist_pts = 2
    elif distance_pct <= 5.0:
        dist_pts = 1
    else:
        dist_pts = 0
    score += dist_pts

    conf_pts = int(dex_confirms) + int(gex_confirms) + int(gravity_confirms)
    score += conf_pts

    if major_contradiction:
        score -= 1

    score = max(0, min(10, score))

    # Règle absolue : distance >5% du spot = JAMAIS un déclencheur, score ignoré
    if distance_pct > 5.0:
        return ConvictionResult(
            score=score,
            send=False,
            reason=(
                f"Niveau à {distance_pct:.1f}% du spot → hors portée (>5%), "
                f"contexte structurel uniquement — score {score}/10 ignoré"
            ),
            breakdown={
                "tag":          {"value": activity_tag, "pts": tag_pts},
                "distance":     {"pct": round(distance_pct, 2), "pts": dist_pts},
                "confluence":   {
                    "dex": dex_confirms, "gex": gex_confirms,
                    "gravity": gravity_confirms, "pts": conf_pts,
                },
                "contradiction": -1 if major_contradiction else 0,
            },
        )

    send  = score >= min_score

    if not send:
        if activity_tag == "DORMANT":
            reason = (
                f"DORMANT à {distance_pct:.1f}% du spot → score {score}/10, "
                f"zéro activité réelle sur ce niveau"
            )
        elif distance_pct > 5.0:
            reason = (
                f"Niveau à {distance_pct:.1f}% du spot → hors portée (>5%), "
                f"contexte structurel uniquement"
            )
        else:
            reason = (
                f"Score {score}/10 insuffisant (seuil {min_score}) — "
                f"confluence manquante ({activity_tag}, {distance_pct:.1f}%)"
            )
    else:
        confs = []
        if dex_confirms:     confs.append("DEX")
        if gex_confirms:     confs.append("GEX")
        if gravity_confirms: confs.append("Gravity")
        conf_str = " + ".join(confs) if confs else "sans confluence"
        reason = f"Score {score}/10 — {activity_tag}, {distance_pct:.1f}% du spot, {conf_str}"

    return ConvictionResult(
        score=score,
        send=send,
        reason=reason,
        breakdown={
            "tag":          {"value": activity_tag, "pts": tag_pts},
            "distance":     {"pct": round(distance_pct, 2), "pts": dist_pts},
            "confluence":   {
                "dex": dex_confirms, "gex": gex_confirms,
                "gravity": gravity_confirms, "pts": conf_pts,
            },
            "contradiction": -1 if major_contradiction else 0,
        },
    )


# ── Simulation de référence ───────────────────────────────────────────────────

_SIMULATION_SCENARIOS = [
    # (label, tag, dist_pct, dex, gex, gravity, contradiction)
    ("Wall DORMANT  8% du spot",            "DORMANT",     8.0, False, False, False, False),
    ("Wall DORMANT  2% du spot",            "DORMANT",     2.0, False, False, False, False),
    ("Wall STRUCTURAL  3% du spot",         "STRUCTURAL",  3.0, False, False, False, False),
    ("Wall STRUCTURAL  3% + GEX confirme",  "STRUCTURAL",  3.0, False, True,  False, False),
    ("Wall ACTIVE  3% du spot",             "ACTIVE",      3.0, False, False, False, False),
    ("Wall ACTIVE  1.5% + DEX confirme",    "ACTIVE",      1.5, True,  False, False, False),
    ("Wall ACTIONABLE  6% du spot",         "ACTIONABLE",  6.0, False, False, False, False),
    ("Wall ACTIONABLE  2% seul",            "ACTIONABLE",  2.0, False, False, False, False),
    ("Wall ACTIONABLE  1% + DEX confirme",  "ACTIONABLE",  1.0, True,  False, False, False),
    ("Squeeze ACTIONABLE  1% triple conf",  "ACTIONABLE",  1.0, True,  True,  True,  False),
    ("Wall ACTIVE  2% contradiction",       "ACTIVE",      2.0, False, False, False, True),
    ("GEX Flip DORMANT  0% (sur spot)",     "DORMANT",     0.0, False, False, False, False),
    ("GEX Flip ACTIONABLE  0% + DEX + GEX","ACTIONABLE",  0.0, True,  True,  False, False),
]


def run_simulation() -> list:
    """Retourne la liste des résultats de simulation pour rapport d'audit."""
    results = []
    for label, tag, dist, dex, gex, grav, contra in _SIMULATION_SCENARIOS:
        r = compute_conviction_score(tag, dist, dex, gex, grav, contra)
        results.append({
            "label":   label,
            "score":   r.score,
            "send":    r.send,
            "reason":  r.reason,
        })
    return results


def format_simulation_report() -> str:
    """Rapport de simulation prêt pour Telegram ou /api."""
    rows = run_simulation()
    suppressed = sum(1 for r in rows if not r["send"])
    sent       = len(rows) - suppressed
    pct        = suppressed / len(rows) * 100 if rows else 0

    lines = [
        "🧪 **SIMULATION CONVICTION SCORE V1**\n",
        f"Scénarios testés : {len(rows)}",
        f"Alertes envoyées : {sent}  |  Supprimées : {suppressed} ({pct:.0f}%)\n",
    ]
    for r in rows:
        icon = "✅" if r["send"] else "🚫"
        lines.append(f"{icon} [{r['score']}/10] {r['label']}")
        if not r["send"]:
            lines.append(f"  ↳ {r['reason']}")
    return "\n".join(lines)
