"""
test_vexcex_flip_phrase.py — Phase V0 : verifie que flip_interpretation
decrit la position du FLIP par rapport au spot (et non l'inverse).
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch


def _make_gex_prof(flip, spot, regime="AMPLIFICATEUR"):
    prof = MagicMock()
    prof.flip_level = flip
    prof.regime = regime
    return prof


def _run_compute(flip, spot, regime="AMPLIFICATEUR"):
    """Execute compute_vex_cex avec un snapshot minimal et un GEX prof mockee."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
    # Import apres injection du path
    from backend.vex_cex import compute_vex_cex

    snap = MagicMock()
    snap.btc_price = spot
    snap.timestamp = 1700000000.0
    snap.options = []  # pas d'options -> vex/cex = 0

    gex_prof = _make_gex_prof(flip, spot, regime)
    with patch('backend.vex_cex.compute_gex', return_value=gex_prof):
        return compute_vex_cex(snap)


class TestFlipPhrase:

    def test_flip_above_spot(self):
        """flip > spot → 'au-dessus' dans l'interpretation (le FLIP est au-dessus)."""
        prof = _run_compute(flip=65000, spot=62670)
        assert prof.gamma_flip_side == 'above', "flip_side doit etre 'above'"
        assert 'au-dessus' in prof.gamma_flip_interpretation, (
            f"Attendu 'au-dessus' dans: {prof.gamma_flip_interpretation}"
        )
        assert 'en-dessous' not in prof.gamma_flip_interpretation

    def test_flip_below_spot(self):
        """flip < spot → 'en-dessous' dans l'interpretation."""
        prof = _run_compute(flip=58000, spot=62670)
        assert prof.gamma_flip_side == 'below'
        assert 'en-dessous' in prof.gamma_flip_interpretation, (
            f"Attendu 'en-dessous' dans: {prof.gamma_flip_interpretation}"
        )
        assert 'au-dessus' not in prof.gamma_flip_interpretation

    def test_flip_none(self):
        """flip None → phrase 'non detecte'."""
        prof = _run_compute(flip=None, spot=62670)
        assert prof.gamma_flip is None
        assert 'non detecte' in prof.gamma_flip_interpretation.lower()

    def test_phrase_does_not_mention_spot(self):
        """La phrase ne dit pas 'le spot est' (sujet = le Gamma Flip)."""
        prof_above = _run_compute(flip=65000, spot=62670)
        prof_below = _run_compute(flip=58000, spot=62670)
        for prof in (prof_above, prof_below):
            assert 'le spot est' not in prof.gamma_flip_interpretation, (
                f"Sujet incorrect dans: {prof.gamma_flip_interpretation}"
            )
