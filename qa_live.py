#!/usr/bin/env python3
"""
qa_live.py — Sprint 1 + Sprint 2 QA live : vérifie les invariants inter-moteurs sur les données réelles.

Usage :
  python3 qa_live.py [--host http://localhost] [--verbose]

Retourne exit code 0 si tous les checks passent, 1 si des violations sont détectées.
Peut être appelé en cron ou après chaque déploiement.
"""
import sys
import json
import argparse
import urllib.request
import urllib.error

HOST = "http://localhost"
TIMEOUT = 10
TIMEOUT_SLOW = 60  # endpoints lourds (decision, pro_decision, narrative, shadow)

CHECKS_PASSED = []
CHECKS_FAILED = []


def _get(path: str, timeout: int = None) -> dict:
    url = HOST + path
    t = timeout or TIMEOUT
    try:
        with urllib.request.urlopen(url, timeout=t) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"__error__": f"HTTP {e.code}", "__path__": path}
    except Exception as e:
        return {"__error__": str(e), "__path__": path}


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        CHECKS_PASSED.append(name)
        print(f"  OK  {name}")
    else:
        CHECKS_FAILED.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")


def run_checks():
    print(f"\n=== QA Live — {HOST} ===\n")

    # ── 1. Endpoints disponibles ─────────────────────────────────────────────
    print("[ Endpoints ]")
    decision = _get("/api/decision", timeout=TIMEOUT_SLOW)
    pro     = _get("/api/pro_decision", timeout=TIMEOUT_SLOW)
    shadow  = _get("/api/decision/shadow", timeout=TIMEOUT_SLOW)
    narr    = _get("/api/narrative",       timeout=TIMEOUT_SLOW)

    check("decision reachable",   "__error__" not in decision,  decision.get("__error__", ""))
    check("pro_decision reachable", "__error__" not in pro,     pro.get("__error__", ""))
    check("shadow reachable",     "__error__" not in shadow,    shadow.get("__error__", ""))
    check("narrative reachable",  "__error__" not in narr,      narr.get("__error__", ""))

    if "__error__" in decision or "__error__" in pro:
        print("\n[!] Endpoints critiques non joignables — arrêt des checks.")
        return

    # ── 2. Champs P2/P3/P4 présents dans /api/decision ───────────────────────
    print("\n[ Champs P2/P3/P4 dans /api/decision ]")
    check("global_confidence présent",   "global_confidence"    in decision, "champ manquant")
    check("signal_dte_degraded présent", "signal_dte_degraded"  in decision, "champ manquant")
    check("pre_expiration_warning présent", "pre_expiration_warning" in decision, "champ manquant")
    check("directional_bias_score présent", "directional_bias_score" in decision, "champ manquant")

    # ── 3. Invariant P3 : global_confidence == confidence_pct ────────────────
    print("\n[ Invariant P3 — alias global_confidence ]")
    gc = decision.get("global_confidence")
    cp = decision.get("confidence_pct")
    check("global_confidence == confidence_pct",
          gc == cp,
          f"global_confidence={gc} confidence_pct={cp}")

    # ── 4. Invariant P4 : cohérence signal_dte_degraded ↔ pre_expiration_warning
    print("\n[ Invariant P4 — cohérence TTL ]")
    degraded = decision.get("signal_dte_degraded", False)
    warning  = decision.get("pre_expiration_warning")
    if degraded:
        check("pre_expiration_warning non-None quand degraded",
              warning is not None,
              "signal_dte_degraded=True mais pre_expiration_warning=None")
    else:
        check("pre_expiration_warning None quand non-degraded",
              warning is None,
              f"signal_dte_degraded=False mais pre_expiration_warning='{warning}'")

    # ── 5. Invariant bornes global_confidence ────────────────────────────────
    print("\n[ Invariant bornes ]")
    check("global_confidence in [0,100]",
          gc is not None and 0 <= gc <= 100,
          f"valeur={gc}")
    check("confidence_pct in [0,100]",
          cp is not None and 0 <= cp <= 100,
          f"valeur={cp}")

    # ── 6. Champs Pro Decision ────────────────────────────────────────────────
    print("\n[ Champs Pro Decision ]")
    check("conviction présent",         "conviction"            in pro, "champ manquant")
    check("global_confidence Pro présent", "global_confidence"  in pro, "champ manquant")
    check("arbiter_confidence_pct présent", "arbiter_confidence_pct" in pro, "champ manquant")

    # ── 7. Invariant P2 : conviction ≤ arbiter_confidence_pct // 10 ──────────
    print("\n[ Invariant P2 — conviction cap ]")
    conv     = pro.get("conviction")
    arb_pct  = pro.get("arbiter_confidence_pct")
    gc_pro   = pro.get("global_confidence")
    if conv is not None and arb_pct is not None:
        cap = arb_pct // 10
        check("conviction <= arbiter_confidence_pct // 10",
              conv <= cap,
              f"conviction={conv} cap={cap} (arbiter={arb_pct}%)")
    else:
        check("conviction et arbiter_confidence_pct disponibles",
              False,
              f"conviction={conv} arb_pct={arb_pct}")

    # ── 8. Invariant P3 Pro : global_confidence == arbiter_confidence_pct ────
    print("\n[ Invariant P3 Pro — alias global_confidence ]")
    if gc_pro is not None and arb_pct is not None:
        check("Pro global_confidence == arbiter_confidence_pct",
              gc_pro == arb_pct,
              f"global_confidence={gc_pro} arbiter_confidence_pct={arb_pct}")
    else:
        check("Pro alias disponibles",
              gc_pro is None and arb_pct is None,
              f"gc_pro={gc_pro} arb_pct={arb_pct}")

    # ── 9. Invariant V5 : NEU-* → confidence_pct ≤ 25 ───────────────────────
    print("\n[ Invariant V5 — régime NEU ]")
    regime_id = decision.get("vexcex_regime_id", "")
    if regime_id and regime_id.startswith("NEU"):
        cap_neu = 20 if regime_id == "NEU-0" else 25
        check(f"{regime_id} confidence_pct <= {cap_neu}",
              cp is not None and cp <= cap_neu,
              f"confidence_pct={cp} > cap={cap_neu}")
    else:
        check("NEU cap N/A (régime courant: " + (regime_id or "none") + ")",
              True)

    # ── 10. Shadow diff cohérent ──────────────────────────────────────────────
    print("\n[ Shadow mode ]")
    if "__error__" not in shadow:
        s_curr = shadow.get("current", {})
        s_delta = shadow.get("delta", {})
        check("shadow.current.global_confidence présent",
              "global_confidence" in s_curr, "champ manquant")
        check("shadow.delta.confidence_delta est un entier",
              isinstance(s_delta.get("confidence_delta"), (int, float)),
              f"type={type(s_delta.get('confidence_delta'))}")
        s_base = shadow.get("baseline", {})
        if s_curr.get("signal_dte_degraded"):
            check("P4 actif → baseline >= current confidence",
                  s_base.get("confidence_pct", 0) >= s_curr.get("confidence_pct", 0),
                  f"baseline={s_base.get('confidence_pct')} current={s_curr.get('confidence_pct')}")
    else:
        check("shadow disponible", False, shadow.get("__error__", ""))

    # ── 11. Cohérence /api/decision ↔ /api/narrative ─────────────────────────
    print("\n[ Cohérence decision ↔ narrative ]")
    if "__error__" not in narr:
        narr_stale = narr.get("data_stale", False)
        dec_quality = decision.get("data_quality", "OK")
        if narr_stale:
            check("narrative stale → data_quality DEGRADED",
                  dec_quality in ("DEGRADED", "INSUFFICIENT"),
                  f"data_stale=True mais data_quality={dec_quality}")
        else:
            check("narrative non-stale → data_quality OK",
                  dec_quality == "OK",
                  f"data_stale=False mais data_quality={dec_quality}")

    # ── 12. system_status cohérent ───────────────────────────────────────────
    print("\n[ Invariant P0.5 — system_status ]")
    sstat   = decision.get("system_status")
    verdict = decision.get("verdict")
    contrad = decision.get("contradictions", [])
    dq      = decision.get("data_quality")

    valid_statuses = {"TRADEABLE", "OBSERVE", "CONFLICT", "DEGRADED", "OFFLINE"}
    check("system_status valeur connue",
          sstat in valid_statuses,
          f"valeur inattendue: '{sstat}'")

    if dq == "INSUFFICIENT":
        check("INSUFFICIENT → system_status OFFLINE",
              sstat == "OFFLINE",
              f"data_quality=INSUFFICIENT mais system_status={sstat}")
    if contrad and verdict == "OBSERVE":
        check("contradictions + OBSERVE → system_status CONFLICT",
              sstat == "CONFLICT",
              f"contradictions présentes + OBSERVE mais system_status={sstat}")

    # ════════════════════════════════════════════════════════════════════════
    # ── Sprint 2 — Invariants P4/P6/H3 ─────────────────────────────────────
    # ════════════════════════════════════════════════════════════════════════

    # ── S2-1. H3 — convergence des confiances Arbiter ────────────────────────
    print("\n[ S2 — H3 convergence Arbiter ]")
    # /api/decision.global_confidence et /api/pro_decision.arbiter_confidence_pct
    # doivent être proches (même pipeline VEX/CEX depuis le fix H3)
    # Tolérance : ≤ 5pts d'écart (ils peuvent légèrement diverger si le timing
    # des snapshots diffère entre les deux appels HTTP).
    gc_dec = decision.get("global_confidence")
    gc_arb = pro.get("arbiter_confidence_pct")
    if gc_dec is not None and gc_arb is not None:
        delta_h3 = abs(gc_dec - gc_arb)
        check("H3 |decision.global_confidence - pro.arbiter_confidence_pct| <= 5",
              delta_h3 <= 5,
              f"decision={gc_dec}% pro_arb={gc_arb}% delta={delta_h3}pts")
    else:
        check("H3 confiances disponibles", False,
              f"decision_gc={gc_dec} pro_arb={gc_arb}")

    # ── S2-2. Phase 4 — état SUPPRESSED conforme ─────────────────────────────
    print("\n[ S2 — Phase 4 SUPPRESSED state ]")
    pro_verdict     = pro.get("verdict", "")
    pro_arb         = pro.get("arbiter_confidence_pct")
    pro_trade       = pro.get("trade") or {}
    pro_trade_type  = pro_trade.get("type", "")
    pro_action      = pro.get("action_label", "")
    pro_supporting  = pro.get("supporting_forces", [])
    pro_opposing    = pro.get("opposing_forces", [])

    if pro_arb is not None and pro_arb < 30:
        # État SUPPRESSED actif
        check("SUPPRESSED → verdict ATTENDRE",
              pro_verdict == "ATTENDRE",
              f"arbiter={pro_arb}% mais verdict={pro_verdict}")
        check("SUPPRESSED → trade.type WAIT",
              pro_trade_type == "WAIT",
              f"arbiter={pro_arb}% mais trade.type={pro_trade_type}")
        check("SUPPRESSED → action_label contient 'non actionnable'",
              "non actionnable" in pro_action,
              f"action_label='{pro_action}'")
        check("SUPPRESSED → opposing_forces vide",
              pro_opposing == [],
              f"opposing_forces non-vide: {pro_opposing[:2]}")
        # Stop/target absents du trade
        check("SUPPRESSED → trade sans stop_price",
              pro_trade.get("stop_price") is None,
              f"stop_price={pro_trade.get('stop_price')}")
        check("SUPPRESSED → trade sans target_price",
              pro_trade.get("target_price") is None,
              f"target_price={pro_trade.get('target_price')}")
        check("SUPPRESSED → trade sizing_pct == 0",
              pro_trade.get("sizing_pct", -1) == 0.0,
              f"sizing_pct={pro_trade.get('sizing_pct')}")
    else:
        # État normal — vérifier la cohérence directionnelle des forces
        check("SUPPRESSED N/A (arbiter >= 30%)",
              True,
              f"arbiter={pro_arb}%")
        # (a) Assertion forces_direction : aucune force favorable ne doit mentionner
        # la direction opposée au verdict
        if pro_verdict and ("BEAR" in pro_verdict or "BULL" in pro_verdict):
            is_bear = "BEAR" in pro_verdict
            bad_forces = []
            import re
            for f in pro_supporting:
                fu = f.upper()
                if is_bear and re.search(r'\bBULL\b|\bHAUSSIER\b', fu):
                    bad_forces.append(f[:50])
                elif not is_bear and re.search(r'\bBEAR\b|\bBAISSIER\b', fu):
                    bad_forces.append(f[:50])
            check("(a) supporting_forces cohérentes avec verdict",
                  len(bad_forces) == 0,
                  f"forces mal classées: {bad_forces}")

    # ── S2-3. Phase 5 — pas de résidu "signal souverain" ────────────────────
    print("\n[ S2 — Phase 5 artefacts supprimés ]")
    pro_thesis = pro.get("primary_thesis", "")
    signals_used = decision.get("signals_used", [])
    souverain_in_thesis = "souverain" in pro_thesis.lower()
    check("primary_thesis sans 'souverain'",
          not souverain_in_thesis,
          f"thèse contient 'souverain': «{pro_thesis[:80]}»")
    souverain_in_signals = any("souverain" in str(s).lower() for s in signals_used)
    check("signals_used sans 'souverain'",
          not souverain_in_signals,
          "signal souverain présent dans signals_used")

    # ── S2-4. Phase 6 — middleware violations ────────────────────────────────
    print("\n[ S2 — Phase 6 middleware violations ]")
    violations = _get("/api/admin/violations")
    if "__error__" in violations:
        check("admin/violations reachable", False, violations.get("__error__", ""))
    else:
        check("admin/violations reachable", True)
        total_v = violations.get("total", -1)
        bloquant_count = violations.get("counts", {}).get("(a) forces_direction", 0)
        check("violations.total est un entier",
              isinstance(total_v, int),
              f"type={type(total_v)}")
        # En fonctionnement normal, on n'attend pas de violations BLOQUANT (a)
        # Si le système est SUPPRESSED, les assertions (o) sont en fait bloquantes
        # mais gérées côté engine → pas de violation attendue dans le middleware
        # (le middleware est un filet de sécurité, pas un chemin nominal)
        print(f"    info: total violations depuis démarrage = {total_v}")
        if total_v > 0:
            print(f"    info: détail counts = {violations.get('counts', {})}")
        check("violations (a) forces_direction == 0 en session courante",
              bloquant_count == 0,
              f"(a) violations = {bloquant_count} — forces mal classées atteignent le middleware")

    # ── S2-5. Pro Decision — champs Sprint 2 présents ────────────────────────
    print("\n[ S2 — Champs Sprint 2 dans /api/pro_decision ]")
    check("action_label présent",    "action_label"    in pro, "champ manquant")
    check("primary_thesis présent",  "primary_thesis"  in pro, "champ manquant")
    check("supporting_forces présent", "supporting_forces" in pro, "champ manquant")
    check("trade présent",           "trade"           in pro, "champ manquant")
    check("scenario_type présent",   "scenario_type"   in pro, "champ manquant")

    # ── S2-6. Cohérence verdict directional → forces non-vides ───────────────
    print("\n[ S2 — Cohérence verdict ↔ forces ]")
    if pro_arb is not None and pro_arb >= 30:
        # Hors SUPPRESSED : si verdict directionnel, on attend des forces
        if pro_verdict and ("BEAR" in pro_verdict or "BULL" in pro_verdict):
            check("verdict directionnel → supporting_forces non-vides",
                  len(pro_supporting) > 0,
                  f"verdict={pro_verdict} mais supporting_forces=[]")
        else:
            check("pas de verdict directionnel (N/A pour forces check)",
                  True)
    else:
        # SUPPRESSED : supporting_forces contient les signaux neutres
        check("SUPPRESSED → supporting_forces = signaux neutres (non-vide)",
              len(pro_supporting) > 0,
              f"supporting_forces vide en état SUPPRESSED")


def main():
    global HOST
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://localhost")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    HOST = args.host.rstrip("/")

    run_checks()

    print(f"\n{'='*50}")
    print(f"  {len(CHECKS_PASSED)} OK  |  {len(CHECKS_FAILED)} FAILED")
    if CHECKS_FAILED:
        print("\nViolations:")
        for f in CHECKS_FAILED:
            print(f"  - {f}")
        print()
        sys.exit(1)
    else:
        print("\n  Tous les invariants sont respectés en conditions réelles.")
        sys.exit(0)


if __name__ == "__main__":
    main()
