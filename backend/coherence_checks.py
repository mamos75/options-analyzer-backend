"""
coherence_checks.py — Middleware d'assertions runtime inter-moteurs (Phase 6, Sprint 2).

Branché comme étape obligatoire du pipeline de publication :
  payload → run_coherence_checks(payload, context) → publication

Politique par sévérité :
  BLOQUANT → violation loggée, champ fautif remplacé par fallback
  WARNING  → violation loggée, rendu normal

Assertions :
  (a) forces vs direction trade : supporting_forces ne peut pas contenir une direction
      opposée au verdict (ex: "BULL" dans forces favorables d'un verdict BEAR)
  (j) une seule valeur de confiance : global_confidence == confidence_pct dans /api/decision
  (n) snapshot_id cohérence (si disponible)
  (o) état SUPPRESSED conforme : si verdict OBSERVE ou arbiter < 30%, aucun trade directionnel
  (p) primary_thesis sans stop/target quand trade.type=WAIT
  (m2) types de niveaux dans les textes = types reels du snapshot
  (l)  gex_regime provient de regime_meca (source unique v3-bis)
  (m1) niveaux mentionnes dans textes ont un montant $ valide
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("coherence")

# ── Compteurs de violations (rolling, réinitialisés au redémarrage) ──────────
_violation_counts: dict[str, int] = {}
_violation_log: list[dict] = []   # dernières 200 violations
_MAX_LOG = 200


def _record(assertion: str, severity: str, detail: str, context: str = ""):
    global _violation_log
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "assertion": assertion,
        "severity": severity,
        "detail": detail,
        "context": context,
    }
    _violation_counts[assertion] = _violation_counts.get(assertion, 0) + 1
    _violation_log.append(entry)
    if len(_violation_log) > _MAX_LOG:
        _violation_log = _violation_log[-_MAX_LOG:]
    level = logging.ERROR if severity == "BLOQUANT" else logging.WARNING
    log.log(level, "[coherence %s] %s — %s %s", severity, assertion, detail, context)


# ── Assertion (a) — forces mal classées ─────────────────────────────────────
def check_forces_direction(payload: dict) -> dict:
    """
    (a) supporting_forces ne peut contenir une direction opposée au verdict.
    Sévérité BLOQUANT : retirer les forces mal classées + swap vers opposing.
    """
    verdict = payload.get("verdict", "")
    supporting = list(payload.get("supporting_forces", []))
    opposing   = list(payload.get("opposing_forces", []))

    if not verdict or verdict in ("ATTENDRE", "NEUTRE_RANGE", "ZONE_DE_FLIP_BINAIRE"):
        return payload  # pas de direction → rien à vérifier

    is_bear = "BEAR" in verdict
    is_bull = "BULL" in verdict
    if not (is_bear or is_bull):
        return payload

    moved = []
    clean_supporting = []
    for force in supporting:
        force_upper = force.upper()
        # Force qui mentionne explicitement la direction opposée
        if is_bear and re.search(r'\bBULL\b|\bHAUSSIER\b|\bBULL\b', force_upper):
            moved.append(force)
            _record("(a) forces_direction", "BLOQUANT",
                    f"Force favorable mentionne BULL dans verdict {verdict}: «{force[:60]}»")
        elif is_bull and re.search(r'\bBEAR\b|\bBAISSIER\b', force_upper):
            moved.append(force)
            _record("(a) forces_direction", "BLOQUANT",
                    f"Force favorable mentionne BEAR dans verdict {verdict}: «{force[:60]}»")
        else:
            clean_supporting.append(force)

    if moved:
        payload = dict(payload)
        payload["supporting_forces"] = clean_supporting
        payload["opposing_forces"]   = opposing + moved
    return payload


# ── Assertion (j) — une seule valeur de confiance ───────────────────────────
def check_confidence_unity(payload: dict) -> dict:
    """
    (j) global_confidence doit == confidence_pct dans /api/decision.
    Sévérité WARNING (incohérence tracée, pas bloquée — diagnostic uniquement).
    """
    gc = payload.get("global_confidence")
    cp = payload.get("confidence_pct")
    if gc is not None and cp is not None and gc != cp:
        _record("(j) confidence_unity", "WARNING",
                f"global_confidence={gc} ≠ confidence_pct={cp}")
    return payload


# ── Assertion (o) — état SUPPRESSED conforme ────────────────────────────────
def check_suppressed_state(payload: dict) -> dict:
    """
    (o) Si arbiter_confidence_pct < 30 ou verdict OBSERVE :
        - Aucun trade directionnel (type != WAIT interdit)
        - Aucun stop/target dans primary_thesis
    Sévérité BLOQUANT : neutraliser le trade + nettoyer la thèse.
    """
    arb_pct  = payload.get("arbiter_confidence_pct")
    verdict  = payload.get("verdict", "")
    trade    = payload.get("trade") or {}
    thesis   = payload.get("primary_thesis", "")

    suppressed = (arb_pct is not None and arb_pct < 30)
    if not suppressed:
        return payload

    trade_type = trade.get("type", "WAIT")
    if trade_type != "WAIT":
        _record("(o) suppressed_state", "BLOQUANT",
                f"trade.type={trade_type} interdit quand arbiter={arb_pct}%")
        payload = dict(payload)
        payload["trade"] = {
            "type": "WAIT",
            "action": "Aucun trade — attendre confirmation",
            "rationale": f"Arbiter {arb_pct}% (seuil 30%). Neutralisé par assertion (o).",
            "expiry_suggested": "—",
            "dte_suggested": 0,
            "strike_primary": None,
            "strike_secondary": None,
            "max_iv_acceptable": None,
            "sizing_pct": 0.0,
            "stop_price": None,
            "target_price": None,
            "risk_reward": None,
        }

    # Retirer mentions de stop/target/cible dans la thèse quand SUPPRESSED
    if re.search(r'(Stop|Cible|Target)\s*:\s*\$[\d,]+', thesis):
        _record("(o) suppressed_state", "BLOQUANT",
                f"primary_thesis contient stop/target avec arbiter={arb_pct}%: «{thesis[:80]}»")
        # Tronquer au premier "Stop" ou "Cible"
        cleaned = re.sub(r'\s*(Stop|Cible|Target)\s*:\s*\$[\d,]+[^.]*\.?', '', thesis).strip()
        payload = dict(payload)
        payload["primary_thesis"] = cleaned if cleaned else "Structure détectée — conditions insuffisantes."

    return payload


# ── Assertion (p) — thesis propre quand WAIT ────────────────────────────────
def check_thesis_no_trade_residues(payload: dict) -> dict:
    """
    (p) Quand trade.type=WAIT, primary_thesis ne doit pas mentionner de stop/target/cible.
    Sévérité WARNING.
    """
    trade = payload.get("trade") or {}
    if trade.get("type") != "WAIT":
        return payload
    thesis = payload.get("primary_thesis", "")
    if re.search(r'(Stop|Cible mécanique|Target|Objectif)\s*:\s*\$[\d,]+', thesis):
        _record("(p) thesis_clean", "WARNING",
                f"Thèse WAIT contient stop/target: «{thesis[:80]}»")
    return payload



# -- Assertion (m2) -- type de niveau dans texte = type reel snapshot ----
import re as _re_m2

_M2_PATTERNS = [
    (r'\bput wall\b',    "put_wall"),
    (r'\bcall wall\b',   "call_wall"),
    (r'\bgamma flip\b',  "flip"),
    (r'\bflip\b',        "flip"),
    (r'\bmax pain\b',    "max_pain"),
]

def check_level_type_coherence(payload: dict) -> dict:
    niveau_types = payload.get("_niveau_types")
    if not niveau_types:
        return payload
    bas_type  = niveau_types.get("niveau_bas_type", "")
    haut_type = niveau_types.get("niveau_haut_type", "")
    flip_val  = niveau_types.get("flip_level")
    mp_val    = niveau_types.get("max_pain")
    TEXT_FIELDS = ["primary_thesis", "phrase_synthese", "scenario_principal"]
    payload = dict(payload)
    for field in TEXT_FIELDS:
        text = payload.get(field, "")
        if not text:
            continue
        text_lower = text.lower()
        for pattern, mentioned_type in _M2_PATTERNS:
            if not _re_m2.search(pattern, text_lower):
                continue
            mismatch = False
            detail = ""
            if mentioned_type == "put_wall" and bas_type not in ("put_wall", "", "fallback"):
                mismatch = True
                detail = f"Texte mentionne 'put wall' mais niveau_bas_type='{bas_type}'. Champ: {field}."
            elif mentioned_type == "call_wall" and haut_type not in ("call_wall", "", "fallback"):
                mismatch = True
                detail = f"Texte mentionne 'call wall' mais niveau_haut_type='{haut_type}'. Champ: {field}."
            elif mentioned_type == "flip" and flip_val is None:
                mismatch = True
                detail = f"Texte mentionne 'flip' mais flip_level=None. Champ: {field}."
            elif mentioned_type == "max_pain" and mp_val is None:
                mismatch = True
                detail = f"Texte mentionne 'max pain' mais max_pain=None. Champ: {field}."
            if mismatch:
                _record("(m2) level_type_coherence", "BLOQUANT", detail)
                flip_str = f"${flip_val:,.0f}" if flip_val else "N/A"
                mp_str   = f"${mp_val:,.0f}" if mp_val else "N/A"
                payload[field] = f"Niveaux cles : flip {flip_str}, max pain {mp_str}."
                break
    return payload



# -- Assertion (l) -- regime source unique : regime_meca ----------------------
def check_regime_source(payload: dict) -> dict:
    """
    (l) gex_regime dans le payload doit provenir de regime_meca (source v3-bis).
    Si "_gex_source" == "legacy" -> violation BLOQUANT.
    Champ "_gex_source" injecte par main.py pour audit (optionnel).
    """
    gex_source = payload.get("_gex_source")
    if gex_source == "legacy":
        regime = payload.get("gex_regime", "?")
        _record("(l) regime_source", "BLOQUANT",
                f"gex_regime='{regime}' provient du champ legacy (gex_obj.regime) "
                f"au lieu de regime_meca. Corrigez l'appel dans main.py.")
    return payload


# -- Assertion (m1) -- niveaux cles avec montants valides ---------------------
_M1_KEYWORDS = [
    ("flip_level",  ["flip", "zone de flip", "gamma flip"]),
    ("max_pain",    ["max pain"]),
    ("call_wall",   ["call wall"]),
    ("put_wall",    ["put wall"]),
]

def check_level_amounts(payload: dict) -> dict:
    """
    (m1) Si un texte mentionne un niveau cle, sa valeur numerique doit etre non-nulle.
    Cela detecte les cas ou on parle d'un flip/max_pain/wall a $0 ou None.
    Severite WARNING : la valeur 0/None est loggee, le texte n'est pas remplace.
    """
    text_fields = ["primary_thesis", "phrase_synthese", "scenario_principal"]
    levels = payload.get("_niveau_types") or {}
    level_map = {
        "flip_level": levels.get("flip_level"),
        "max_pain":   levels.get("max_pain"),
        "call_wall":  None,   # montant non transmis dans _niveau_types actuellement
        "put_wall":   None,
    }
    # Fallback : chercher dans levels directement si expose
    if not levels:
        return payload

    for field in text_fields:
        text = payload.get(field, "")
        if not text:
            continue
        text_lower = text.lower()
        for level_key, keywords in _M1_KEYWORDS:
            val = level_map.get(level_key)
            if val is None or val == 0:
                for kw in keywords:
                    if kw in text_lower:
                        _record("(m1) level_amounts", "WARNING",
                                f"'{kw}' mentionne dans {field} mais {level_key}={val!r}. "
                                f"Montant inconnu ou nul.")
                        break
    return payload

# ── Point d'entrée principal ─────────────────────────────────────────────────
def run_coherence_checks(payload: dict, endpoint: str = "") -> dict:
    """
    Applique toutes les assertions sur le payload avant publication.
    Retourne le payload (potentiellement modifié si violations BLOQUANT).
    """
    if not payload or not isinstance(payload, dict):
        return payload

    payload = check_regime_source(payload)
    payload = check_forces_direction(payload)
    payload = check_confidence_unity(payload)
    payload = check_suppressed_state(payload)
    payload = check_thesis_no_trade_residues(payload)
    payload = check_level_type_coherence(payload)
    payload = check_level_amounts(payload)
    # Injecter le bloc coherence:{} dans la reponse
    n_violations = sum(v for k, v in _violation_counts.items())
    payload['coherence'] = {
        'status': 'OK' if n_violations == 0 else 'VIOLATIONS',
        'violations_total': n_violations,
        'endpoint': endpoint,
    }
    return payload


# ── Endpoint /admin/violations ───────────────────────────────────────────────
def get_violations_summary() -> dict:
    """Retourne le tableau de bord des violations (24h glissantes approx)."""
    return {
        "counts": dict(_violation_counts),
        "recent": _violation_log[-20:],
        "total": sum(_violation_counts.values()),
    }
