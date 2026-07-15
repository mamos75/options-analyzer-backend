"""
test_coherence_sprint3.py — Tests mutation Phase 4 Sprint 3.

Couvre les assertions (l) et (m1) de coherence_checks.py :
  - (l)  gex_regime source unique : regime_meca
  - (m1) niveaux cles avec montants valides dans les textes

Format : tests mutation — on injecte un payload invalide et on verifie
que l'assertion est declenchee (violation loggee) sans crash.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.coherence_checks import (
    run_coherence_checks,
    check_regime_source,
    check_level_amounts,
    check_level_type_coherence,
    _violation_counts,
    _violation_log,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _count_violations(assertion: str) -> int:
    return _violation_counts.get(assertion, 0)


def _last_violation(assertion: str) -> dict | None:
    for v in reversed(_violation_log):
        if v["assertion"] == assertion:
            return v
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Assertion (l) — regime source unique
# ─────────────────────────────────────────────────────────────────────────────

class TestAssertionL:
    """(l) gex_regime doit provenir de regime_meca, pas du champ legacy."""

    def test_legacy_source_triggers_violation(self):
        """Injection _gex_source='legacy' → violation (l) BLOQUANT loggee."""
        before = _count_violations("(l) regime_source")
        payload = {
            "gex_regime": "STABILISANT",
            "_gex_source": "legacy",
            "verdict": "ATTENDRE",
        }
        result = check_regime_source(payload)
        after = _count_violations("(l) regime_source")
        assert after > before, "(l) violation not recorded for legacy source"
        v = _last_violation("(l) regime_source")
        assert v is not None
        assert v["severity"] == "BLOQUANT"
        assert "legacy" in v["detail"]

    def test_regime_meca_source_no_violation(self):
        """_gex_source='regime_meca' → aucune violation."""
        before = _count_violations("(l) regime_source")
        payload = {
            "gex_regime": "STABILISANT",
            "_gex_source": "regime_meca",
            "verdict": "ATTENDRE",
        }
        check_regime_source(payload)
        after = _count_violations("(l) regime_source")
        assert after == before, "(l) violation incorrectement declenchee pour regime_meca"

    def test_no_gex_source_field_no_violation(self):
        """Sans champ _gex_source → pas de violation (champ optionnel)."""
        before = _count_violations("(l) regime_source")
        payload = {"gex_regime": "ZONE_DE_FLIP"}
        check_regime_source(payload)
        after = _count_violations("(l) regime_source")
        assert after == before, "(l) violation sur payload sans _gex_source"

    def test_payload_returned_unchanged(self):
        """check_regime_source retourne toujours le payload (pas de mutation)."""
        payload = {"gex_regime": "NEUTRE", "_gex_source": "legacy"}
        result = check_regime_source(payload)
        # Le champ est log-only : le payload n'est pas modifie
        assert result["gex_regime"] == "NEUTRE"

    def test_run_coherence_checks_integrates_l(self):
        """run_coherence_checks appelle check_regime_source -> violation visible."""
        before = _count_violations("(l) regime_source")
        payload = {
            "verdict": "ATTENDRE",
            "gex_regime": "AMPLIFICATEUR",
            "_gex_source": "legacy",
        }
        result = run_coherence_checks(payload, "/api/decision")
        assert _count_violations("(l) regime_source") > before
        # coherence:{} doit etre present
        assert "coherence" in result
        assert result["coherence"]["violations_total"] >= 0


# ─────────────────────────────────────────────────────────────────────────────
# Assertion (m1) — montants $ dans niveaux
# ─────────────────────────────────────────────────────────────────────────────

class TestAssertionM1:
    """(m1) Niveaux mentionnes dans les textes doivent avoir un montant $ valide."""

    def _payload_with_flip_none(self, field: str, text: str) -> dict:
        return {
            "verdict": "ATTENDRE",
            field: text,
            "_niveau_types": {
                "niveau_bas_type": "flip",
                "niveau_haut_type": "flip",
                "flip_level": None,   # ← manquant
                "max_pain": None,
            },
        }

    def test_flip_none_with_flip_in_text_warns(self):
        """Texte mentionne 'flip' mais flip_level=None -> violation (m1) WARNING."""
        before = _count_violations("(m1) level_amounts")
        payload = self._payload_with_flip_none(
            "primary_thesis",
            "Le spot teste le gamma flip, seuil pivot clé."
        )
        check_level_amounts(payload)
        after = _count_violations("(m1) level_amounts")
        assert after > before, "(m1) violation not recorded when flip_level=None"
        v = _last_violation("(m1) level_amounts")
        assert v["severity"] == "WARNING"

    def test_flip_none_no_mention_no_warning(self):
        """flip_level=None mais texte sans mention de flip -> pas de violation."""
        before = _count_violations("(m1) level_amounts")
        payload = {
            "primary_thesis": "Le marche consolide en range.",
            "_niveau_types": {"flip_level": None, "max_pain": 64000.0},
        }
        check_level_amounts(payload)
        after = _count_violations("(m1) level_amounts")
        assert after == before, "(m1) faux positif sur texte sans mention flip"

    def test_flip_valid_no_violation(self):
        """flip_level=65000 et texte mentionne 'flip' -> pas de violation (m1)."""
        before = _count_violations("(m1) level_amounts")
        payload = {
            "primary_thesis": "Resistance au gamma flip $65,000.",
            "_niveau_types": {
                "flip_level": 65000.0,
                "max_pain": 64000.0,
            },
        }
        check_level_amounts(payload)
        after = _count_violations("(m1) level_amounts")
        assert after == before, "(m1) faux positif avec flip_level valide"

    def test_max_pain_none_with_mention_warns(self):
        """Texte mentionne 'max pain' mais max_pain=None -> violation (m1)."""
        before = _count_violations("(m1) level_amounts")
        payload = {
            "primary_thesis": "La structure converge vers le max pain hebdo.",
            "_niveau_types": {"flip_level": 65000.0, "max_pain": None},
        }
        check_level_amounts(payload)
        after = _count_violations("(m1) level_amounts")
        assert after > before, "(m1) violation not recorded when max_pain=None"

    def test_no_niveau_types_skip(self):
        """Sans _niveau_types -> assertion (m1) ignoree (skip gracieux)."""
        before = _count_violations("(m1) level_amounts")
        payload = {
            "primary_thesis": "Le flip est un niveau clé.",
        }
        check_level_amounts(payload)
        after = _count_violations("(m1) level_amounts")
        assert after == before, "(m1) violation sur payload sans _niveau_types"

    def test_payload_not_modified_by_m1(self):
        """(m1) est WARNING : ne modifie pas le payload."""
        payload = {
            "primary_thesis": "Gamma flip test.",
            "_niveau_types": {"flip_level": None, "max_pain": None},
        }
        original_thesis = payload["primary_thesis"]
        result = check_level_amounts(payload)
        assert result.get("primary_thesis") == original_thesis, \
            "(m1) ne doit pas modifier le texte (WARNING, pas BLOQUANT)"


# ─────────────────────────────────────────────────────────────────────────────
# Integration : coherence:{} dans le payload retourne
# ─────────────────────────────────────────────────────────────────────────────

class TestCoherenceBlock:
    """Le bloc coherence:{} doit etre present dans chaque payload traite."""

    def test_coherence_key_present(self):
        payload = {"verdict": "ATTENDRE"}
        result = run_coherence_checks(payload, "/api/test")
        assert "coherence" in result, "coherence:{} manquant dans le payload"

    def test_coherence_structure(self):
        payload = {"verdict": "ATTENDRE"}
        result = run_coherence_checks(payload, "/api/test")
        coh = result["coherence"]
        assert "status" in coh
        assert "violations_total" in coh
        assert "endpoint" in coh
        assert coh["endpoint"] == "/api/test"

    def test_coherence_status_ok_when_no_violations(self):
        """Payload propre -> status OK ou VIOLATIONS selon historique global."""
        # Note: les violations sont globales (rolling), on verifie juste le type
        payload = {"verdict": "ATTENDRE"}
        result = run_coherence_checks(payload, "/api/test")
        assert result["coherence"]["status"] in ("OK", "VIOLATIONS")

    def test_coherence_violations_total_is_int(self):
        payload = {"verdict": "ATTENDRE"}
        result = run_coherence_checks(payload, "/api/test")
        assert isinstance(result["coherence"]["violations_total"], int)

    def test_coherence_not_removed_by_subsequent_checks(self):
        """run_coherence_checks ne doit pas supprimer coherence d'un payload deja traite."""
        payload = {"verdict": "ATTENDRE"}
        result1 = run_coherence_checks(payload, "/api/decision")
        assert "coherence" in result1
        # Si on re-passe le payload (cas hypothetique double-passage)
        result2 = run_coherence_checks(result1, "/api/decision")
        assert "coherence" in result2


# ─────────────────────────────────────────────────────────────────────────────
# Mutation : (m2) non-regression apres Phase 4
# ─────────────────────────────────────────────────────────────────────────────

class TestM2NonRegression:
    """(m2) doit toujours fonctionner apres l'ajout des nouvelles assertions."""

    def test_m2_still_blocks_put_wall_mismatch(self):
        """Texte 'put wall' mais niveau_bas_type='flip' -> BLOQUANT remplacement."""
        before = _count_violations("(m2) level_type_coherence")
        payload = {
            "primary_thesis": "Le put wall soutient le spot.",
            "_niveau_types": {
                "niveau_bas_type": "flip",       # mismatch !
                "niveau_haut_type": "call_wall",
                "flip_level": 65000.0,
                "max_pain": 64000.0,
            },
        }
        result = check_level_type_coherence(payload)
        after = _count_violations("(m2) level_type_coherence")
        assert after > before, "(m2) violation not recorded for put_wall mismatch"
        # Texte remplace par fallback
        assert "put wall" not in result.get("primary_thesis", "").lower(), \
            "(m2) texte fautif non remplace"

    def test_m2_no_false_positive_when_types_match(self):
        """Texte 'put wall' et niveau_bas_type='put_wall' -> aucune violation (m2)."""
        before = _count_violations("(m2) level_type_coherence")
        payload = {
            "primary_thesis": "Le put wall a $60,000 constitue le support.",
            "_niveau_types": {
                "niveau_bas_type": "put_wall",  # match correct
                "niveau_haut_type": "call_wall",
                "flip_level": 65000.0,
                "max_pain": 64000.0,
            },
        }
        check_level_type_coherence(payload)
        after = _count_violations("(m2) level_type_coherence")
        assert after == before, "(m2) faux positif quand les types correspondent"
