"""
Market Decision Layer — logique métier pure.

Prend les objets déjà calculés (narrative, gex, squeeze, ...) et retourne
le dict de réponse de /api/market_decision.
Séparé de main.py pour être testable sans dépendances FastAPI/deribit.
"""
from __future__ import annotations

from typing import Optional

from .narrative_resolver import NarrativeResolved
from .gex import GEXProfile
from .squeeze_score import SqueezeScore


def build_market_decision(
    narrative: NarrativeResolved,
    gex: GEXProfile,
    sq: SqueezeScore,
    spot: float,
    data_stale: bool,
) -> dict:
    """Construit la réponse /api/market_decision à partir des objets calculés.

    Garanties :
    - Jamais de null dangereux côté frontend
    - warnings[] toujours présent (peut être vide)
    - source_status{} toujours présent
    - confidence toujours présent
    - Aucune contradiction silencieuse
    """
    bias = narrative.directional_bias
    bias_score = round(bias.score) if bias else 0
    mp_near = gex.max_pain_profile.near if gex.max_pain_profile else None
    mp_dist_pct = abs(mp_near.strike - spot) / spot if mp_near else 1.0

    # ── Source status ──────────────────────────────────────────────────────
    source_status: dict = {
        "gex": "ok" if gex.flip_level is not None else "partial",
        "narrative": "ok" if (narrative.niveau_haut and narrative.niveau_bas) else "partial",
        "directional_bias": "ok" if bias else "unavailable",
        "squeeze": "ok",
        "invalidation": "ok" if narrative.invalidation else "partial",
        "flip": (
            "actionnable" if (gex.flip_level and narrative.flip_use_in_signal)
            else ("dormant" if gex.flip_level else "absent")
        ),
        "data_stale": data_stale,
    }

    # ── Warnings : données manquantes ─────────────────────────────────────
    warnings: list = []
    if bias is None:
        warnings.append("Bias directionnel non calculé — direction indéterminée")
    if gex.flip_level is None:
        warnings.append("Flip GEX absent — aucun niveau de changement de régime disponible")
    elif not narrative.flip_use_in_signal:
        warnings.append("Flip GEX présent mais non actionnable (dormant ou structurel) — ne pas utiliser comme déclencheur")
    if not narrative.niveau_haut:
        warnings.append("Résistance principale absente — niveaux partiels")
    if not narrative.niveau_bas:
        warnings.append("Support principal absent — niveaux partiels")
    if not narrative.invalidation:
        warnings.append("Niveau d'invalidation non disponible — utiliser les bornes structurelles")
    if data_stale:
        warnings.append("Snapshot Deribit en cache — données potentiellement périmées")

    # ── Warnings : contradictions logiques ────────────────────────────────
    if gex.regime == "AMPLIFICATEUR" and abs(bias_score) <= 20:
        warnings.append(
            "Régime AMPLIFICATEUR sans direction confirmée — piège directionnel possible, "
            "les deux sens peuvent s'emballer ; attendre un signal clair avant de s'exposer"
        )
    if sq.score >= 55 and gex.regime == "STABILISANT":
        warnings.append(
            f"Squeeze {sq.label} dans un régime STABILISANT — dealers freinent le mouvement, "
            "amplitude réelle probablement inférieure au score"
        )
    if mp_near and bias and bias.score < -20 and mp_near.strike > spot:
        warnings.append(
            f"Biais BAISSIER mais Max Pain ${mp_near.strike:,.0f} au-dessus du spot — "
            "attraction haussière court terme possible avant toute baisse"
        )
    if mp_near and bias and bias.score > 20 and mp_near.strike < spot:
        warnings.append(
            f"Biais HAUSSIER mais Max Pain ${mp_near.strike:,.0f} en-dessous du spot — "
            "attraction baissière court terme possible avant toute hausse"
        )
    if bias and 20 < abs(bias_score) <= 35:
        warnings.append(
            "Conviction directionnelle modérée — niveaux de confirmation/invalidation à traiter avec prudence"
        )

    # ── Q1 : Directionnel ou non ? ────────────────────────────────────────
    is_directional = not narrative.range_mode and abs(bias_score) > 20
    if narrative.range_mode:
        dir_label = "Range / Non-directionnel"
        dir_color = "yellow"
        dir_reason = "Le marché oscille entre les bornes — les breakouts échouent fréquemment"
    elif abs(bias_score) <= 20:
        dir_label = "Neutre — pas de direction claire"
        dir_color = "yellow"
        dir_reason = "Les signaux options se neutralisent — pas de prise de position directionnelle recommandée"
    elif bias_score > 50:
        dir_label = "Directionnel HAUSSIER FORT"
        dir_color = "green"
        dir_reason = bias.phrase if bias and bias.phrase else "Confluence forte de signaux haussiers"
    elif bias_score > 20:
        dir_label = "Directionnel HAUSSIER modéré"
        dir_color = "green"
        dir_reason = bias.phrase if bias and bias.phrase else "Biais haussier sans confluence totale"
    elif bias_score < -50:
        dir_label = "Directionnel BAISSIER FORT"
        dir_color = "red"
        dir_reason = bias.phrase if bias and bias.phrase else "Confluence forte de signaux baissiers"
    else:
        dir_label = "Directionnel BAISSIER modéré"
        dir_color = "red"
        dir_reason = bias.phrase if bias and bias.phrase else "Biais baissier sans confluence totale"

    # ── Q2 : Dealers ──────────────────────────────────────────────────────
    _regime_phrases = {
        "STABILISANT": "Les dealers achètent les baisses et vendent les hausses — ils freinent les mouvements brusques",
        "AMPLIFICATEUR": "Les dealers vendent les baisses et achètent les hausses — ils accélèrent chaque mouvement",
        "NEUTRE": "Les dealers n'ont pas d'effet directionnel net en ce moment",
    }
    _dealer_labels = {
        "STABILISANT": "Amortisseurs (frein)",
        "AMPLIFICATEUR": "Amplificateurs (accélérateur)",
        "NEUTRE": "Neutres",
    }
    _dealer_colors = {
        "STABILISANT": "blue",
        "AMPLIFICATEUR": "red",
        "NEUTRE": "yellow",
    }
    dealer_label = _dealer_labels.get(gex.regime, "Inconnu")
    dealer_phrase = _regime_phrases.get(gex.regime, "Régime indéterminé")
    dealer_color = _dealer_colors.get(gex.regime, "yellow")

    # ── Q3 : Risque dominant ──────────────────────────────────────────────
    dominant_risk_type = "none"
    dominant_risk_label = "Aucun risque dominant identifié"
    dominant_risk_color = "yellow"
    dominant_risk_phrase = narrative.risque_principal or "Structure options stable — pas de risque immédiat détecté"

    if sq.score >= 70:
        if sq.direction_bias == "UP":
            dominant_risk_type = "squeeze"
            dominant_risk_label = f"Squeeze HAUSSIER ({sq.label})"
            dominant_risk_color = "green"
            dominant_risk_phrase = "Pression haussière explosive — short-sellers contraints de couvrir, accélération probable"
        elif sq.direction_bias == "DOWN":
            dominant_risk_type = "flush"
            dominant_risk_label = f"Flush BAISSIER ({sq.label})"
            dominant_risk_color = "red"
            dominant_risk_phrase = "Pression baissière explosive — longs contraints de couper, chute probable"
        else:
            dominant_risk_type = "squeeze"
            dominant_risk_label = f"Squeeze bidirectionnel ({sq.label})"
            dominant_risk_color = "yellow"
            dominant_risk_phrase = "Compression extrême dans les deux sens — mouvement violent à venir, direction non confirmée"
    elif gex.regime == "AMPLIFICATEUR" and abs(bias_score) <= 20:
        dominant_risk_type = "directional_trap"
        dominant_risk_label = "Piège directionnel — amplificateur sans direction"
        dominant_risk_color = "yellow"
        dominant_risk_phrase = "Régime qui amplifie les mouvements mais direction non confirmée — risque de faux break dans les deux sens"
    elif narrative.range_mode:
        dominant_risk_type = "range"
        dominant_risk_label = "Piège de range — faux breakouts fréquents"
        dominant_risk_color = "yellow"
        dominant_risk_phrase = "Le marché élimine les traders directionnels dans les deux sens"
    elif mp_near and mp_dist_pct < 0.015:
        dominant_risk_type = "max_pain_magnet"
        dominant_risk_label = f"Aimant Max Pain — ${mp_near.strike:,.0f} ({mp_dist_pct*100:.1f}%)"
        dominant_risk_color = "purple"
        dominant_risk_phrase = f"BTC à moins de 1.5% du Max Pain — attraction magnétique vers ${mp_near.strike:,.0f} avant expiry"
    elif not is_directional and abs(bias_score) > 10:
        dominant_risk_type = "directional_trap"
        dominant_risk_label = "Piège directionnel — signal faible, pas de confluence"
        dominant_risk_color = "yellow"
        dominant_risk_phrase = "Signal directionnel insuffisant pour justifier une exposition — attendre confluence"

    # ── Q4 : 2 niveaux BTC les plus importants ───────────────────────────
    level_candidates = []

    if gex.flip_level and narrative.flip_use_in_signal:
        dist_flip = abs(gex.flip_level - spot) / spot
        level_candidates.append({
            "price": round(gex.flip_level, 0),
            "role": "Flip GEX",
            "distance_pct": round(dist_flip * 100, 2),
            "side": "above" if gex.flip_level > spot else "below",
        })

    if narrative.niveau_haut:
        dist_h = abs(narrative.niveau_haut - spot) / spot
        level_candidates.append({
            "price": round(narrative.niveau_haut, 0),
            "role": narrative.niveau_haut_label or "Résistance principale",
            "distance_pct": round(dist_h * 100, 2),
            "side": "above",
        })

    if narrative.niveau_bas:
        dist_b = abs(narrative.niveau_bas - spot) / spot
        level_candidates.append({
            "price": round(narrative.niveau_bas, 0),
            "role": narrative.niveau_bas_label or "Support principal",
            "distance_pct": round(dist_b * 100, 2),
            "side": "below",
        })

    if mp_near and mp_dist_pct < 0.05:
        level_candidates.append({
            "price": round(mp_near.strike, 0),
            "role": f"Max Pain {mp_near.expiry}",
            "distance_pct": round(mp_dist_pct * 100, 2),
            "side": "above" if mp_near.strike > spot else "below",
        })

    seen_prices: set = set()
    final_levels: list = []
    above = sorted([l for l in level_candidates if l["side"] == "above"], key=lambda x: x["distance_pct"])
    below = sorted([l for l in level_candidates if l["side"] == "below"], key=lambda x: x["distance_pct"])
    for lvl in (above[:1] + below[:1]):
        if lvl["price"] not in seen_prices:
            seen_prices.add(lvl["price"])
            final_levels.append(lvl)
    if len(final_levels) < 2:
        for lvl in sorted(level_candidates, key=lambda x: x["distance_pct"]):
            if lvl["price"] not in seen_prices:
                seen_prices.add(lvl["price"])
                final_levels.append(lvl)
            if len(final_levels) >= 2:
                break

    # ── Q5 : Niveau de confirmation ───────────────────────────────────────
    _conf_prudent = " (conviction modérée — attendre confirmation claire)" if bias and 20 < abs(bias_score) <= 35 else ""
    if is_directional and bias and bias.score > 0 and narrative.niveau_haut:
        conf_price = round(narrative.niveau_haut, 0)
        conf_label = narrative.niveau_haut_label or "Résistance principale"
        conf_phrase = f"Cassure confirmée au-dessus de ${conf_price:,.0f} — scénario haussier validé{_conf_prudent}"
    elif is_directional and bias and bias.score < 0 and narrative.niveau_bas:
        conf_price = round(narrative.niveau_bas, 0)
        conf_label = narrative.niveau_bas_label or "Support principal"
        conf_phrase = f"Cassure confirmée sous ${conf_price:,.0f} — scénario baissier validé{_conf_prudent}"
    else:
        conf_h = round(narrative.niveau_haut, 0) if narrative.niveau_haut else None
        conf_b = round(narrative.niveau_bas, 0) if narrative.niveau_bas else None
        conf_price = conf_h
        conf_label = "Sortie de range confirmée"
        if conf_h and conf_b:
            conf_phrase = f"Sortie directionnelle des bornes ${conf_b:,.0f}–${conf_h:,.0f} confirme la direction"
        elif conf_h:
            conf_phrase = f"Cassure au-dessus de ${conf_h:,.0f} confirme la direction"
        elif conf_b:
            conf_phrase = f"Cassure sous ${conf_b:,.0f} confirme la direction"
        else:
            conf_phrase = "Niveaux de confirmation non disponibles — données partielles"

    # ── Q6 : Niveau d'invalidation ────────────────────────────────────────
    inval_price: Optional[int] = None
    if narrative.invalidation:
        inval_price = round(narrative.invalidation, 0)
    elif gex.flip_level and narrative.flip_use_in_signal:
        inval_price = round(gex.flip_level, 0)
    # Flip dormant → ne pas l'utiliser comme invalidation active

    if inval_price:
        _inval_side = "dépasse" if inval_price > spot else "casse sous"
        inval_phrase = f"Si BTC {_inval_side} ${inval_price:,.0f}, le régime change — scénario actuel invalidé"
    else:
        inval_phrase = "Niveau d'invalidation non disponible — se référer aux bornes structurelles"

    # ── Q7 : Message de surveillance ─────────────────────────────────────
    watch_parts: list = []
    if gex.flip_level and narrative.flip_use_in_signal:
        fd = (gex.flip_level - spot) / spot * 100
        watch_parts.append(f"Flip GEX ${gex.flip_level:,.0f} ({fd:+.1f}%)")
    if narrative.range_mode:
        watch_parts.append("Rester patient en range — attendre la sortie des bornes")
    elif is_directional and bias:
        if bias.score > 0 and narrative.niveau_haut:
            watch_parts.append(f"Cassure ${narrative.niveau_haut:,.0f} confirme la hausse")
        elif bias.score < 0 and narrative.niveau_bas:
            watch_parts.append(f"Cassure sous ${narrative.niveau_bas:,.0f} confirme la baisse")
    if sq.score >= 55:
        watch_parts.append(f"Risque de mouvement violent ({sq.label}) — réduire la taille des positions")
    if not watch_parts:
        watch_parts.append("Continuer à surveiller les niveaux de structure options")

    # ── Confidence globale ────────────────────────────────────────────────
    n_warnings = len(warnings)
    has_contradictions = any(
        kw in w for kw in (
            "Régime AMPLIFICATEUR sans direction",
            "Squeeze",
            "BAISSIER mais Max Pain",
            "HAUSSIER mais Max Pain",
        )
        for w in warnings
    )
    if data_stale or not bias:
        confidence = "faible"
    elif has_contradictions or n_warnings >= 3:
        confidence = "modérée"
    elif n_warnings >= 1:
        confidence = "bonne"
    else:
        confidence = "élevée"

    return {
        "btc_price": spot,
        "directional": {
            "is_directional": is_directional,
            "label": dir_label,
            "score": bias_score,
            "color": dir_color,
            "confidence": bias.confidence if bias else "—",
            "confluence": bias.confluence_count if bias else 0,
            "reason": dir_reason,
        },
        "dealer_regime": {
            "mode": gex.regime,
            "label": dealer_label,
            "color": dealer_color,
            "phrase": dealer_phrase,
        },
        "dominant_risk": {
            "type": dominant_risk_type,
            "label": dominant_risk_label,
            "color": dominant_risk_color,
            "phrase": dominant_risk_phrase,
            "squeeze_score": sq.score,
        },
        "key_levels": final_levels,
        "confirmation": {
            "price": conf_price,
            "label": conf_label,
            "phrase": conf_phrase,
        },
        "invalidation": {
            "price": inval_price,
            "label": "Invalidation scénario",
            "phrase": inval_phrase,
        },
        "watch_message": " · ".join(watch_parts),
        "confidence": confidence,
        "warnings": warnings,
        "source_status": source_status,
    }
