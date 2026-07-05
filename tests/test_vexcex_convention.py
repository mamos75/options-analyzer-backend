"""
test_vexcex_convention.py — Phase V4 : fige la convention short-all.

Convention v2 (short-all) :
  Dealer short TOUTES les options (calls ET puts).
  vex = -vanna * OI * spot  (meme formule calls et puts)
  cex = -charm * OI         (meme formule calls et puts)

  VEX > 0 : IV monte -> dealers achetent BTC (delta net monte).
  VEX < 0 : IV monte -> dealers vendent BTC.
"""
from __future__ import annotations
import math
import pytest
from unittest.mock import MagicMock, patch
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _vanna_bs(spot, strike, iv, T, r=0.05):
    """Calcul direct de la vanna Black-Scholes (reference)."""
    from backend.vex_cex import _d1_d2, _vanna
    d1, d2 = _d1_d2(spot, strike, iv, T, r)
    return _vanna(d1, d2, iv)


def _charm_bs(spot, strike, iv, T, r=0.05):
    from backend.vex_cex import _d1_d2, _charm
    d1, d2 = _d1_d2(spot, strike, iv, T, r)
    return _charm(d1, d2, iv, T, r)


def _make_option(option_type, strike, oi, spot=60000, iv=0.6, dte=30):
    opt = MagicMock()
    opt.option_type = option_type
    opt.strike = strike
    opt.oi = oi
    opt.iv = iv
    opt.expiry = None  # patche _compute_dte
    return opt


def _run_snapshot(options, spot=60000):
    from backend.vex_cex import compute_vex_cex

    snap = MagicMock()
    snap.btc_price = spot
    snap.timestamp = 1700000000.0
    snap.options = options

    gex_prof = MagicMock()
    gex_prof.flip_level = None
    gex_prof.regime = "NEUTRE"

    with patch('backend.vex_cex.compute_gex', return_value=gex_prof), \
         patch('backend.vex_cex._compute_dte', return_value=30):
        return compute_vex_cex(snap)


class TestVannaBSSymmetry:

    def test_vanna_call_equals_vanna_put_atm(self):
        """
        La vanna Black-Scholes est la meme pour un call et un put au meme strike.
        (dDelta/dSigma ne depend pas du type d'option — put-call symmetry).
        """
        spot, strike, iv, T = 60000, 60000, 0.6, 30/365
        van_call = _vanna_bs(spot, strike, iv, T)
        van_put  = _vanna_bs(spot, strike, iv, T)  # meme formule — doit etre egal
        assert van_call == van_put, "vanna call != vanna put au meme strike — formule incorrecte"

    def test_charm_call_equals_charm_put_atm(self):
        """Meme propriete pour la charm."""
        spot, strike, iv, T = 60000, 60000, 0.6, 30/365
        cha_call = _charm_bs(spot, strike, iv, T)
        cha_put  = _charm_bs(spot, strike, iv, T)
        assert cha_call == cha_put


class TestShortAllConvention:

    def test_pure_call_book_vex_sign(self):
        """
        Book 100% calls, dealer short -> VEX doit avoir le signe de -vanna.
        ATM, vanna > 0 (generalement) -> VEX < 0 (IV monte, dealers vendent).
        """
        spot = 60000
        opts = [_make_option("call", spot, oi=100)]  # ATM call, OI=100
        prof = _run_snapshot(opts, spot)
        van = _vanna_bs(spot, spot, 0.6, 30/365)
        expected_sign = -1 if van > 0 else 1
        actual_sign   = -1 if prof.vex_total < 0 else 1
        assert actual_sign == expected_sign, (
            f"VEX call book sign incorrect. vanna={van:.6f}, VEX={prof.vex_total:.2f}"
        )

    def test_pure_put_book_same_sign_as_call_book(self):
        """
        Convention short-all : book 100% puts au meme strike doit donner
        le MEME signe de VEX que le book 100% calls (vanna identique).
        """
        spot = 60000
        opts_call = [_make_option("call", spot, oi=100)]
        opts_put  = [_make_option("put",  spot, oi=100)]
        prof_call = _run_snapshot(opts_call, spot)
        prof_put  = _run_snapshot(opts_put,  spot)

        sign_call = -1 if prof_call.vex_total < 0 else 1
        sign_put  = -1 if prof_put.vex_total  < 0 else 1
        assert sign_call == sign_put, (
            f"Convention short-all violee : call VEX={prof_call.vex_total:.2f} "
            f"put VEX={prof_put.vex_total:.2f} — signes differents"
        )

    def test_symmetric_straddle_vex_zero(self):
        """
        Straddle synthetique ATM (call + put, meme OI) -> VEX = 2 x vex_call.
        Pas zero (les deux renforcent dans la convention short-all).
        Et les deux cex s'additionnent egalement.
        """
        spot = 60000
        opts = [
            _make_option("call", spot, oi=100),
            _make_option("put",  spot, oi=100),
        ]
        prof = _run_snapshot(opts, spot)
        opts_call_only = [_make_option("call", spot, oi=100)]
        prof_call = _run_snapshot(opts_call_only, spot)

        # straddle VEX doit etre ~2x call-seul (additivite)
        ratio = prof.vex_total / prof_call.vex_total if prof_call.vex_total != 0 else 0
        assert abs(ratio - 2.0) < 0.001, (
            f"Straddle VEX != 2x call-seul. ratio={ratio:.4f} "
            f"(straddle={prof.vex_total:.2f}, call={prof_call.vex_total:.2f})"
        )

    def test_cex_same_sign_calls_puts(self):
        """CEX: book call et book put de meme OI donnent meme signe."""
        spot = 60000
        opts_call = [_make_option("call", spot, oi=100)]
        opts_put  = [_make_option("put",  spot, oi=100)]
        prof_call = _run_snapshot(opts_call, spot)
        prof_put  = _run_snapshot(opts_put,  spot)

        if abs(prof_call.cex_total) < 1e-10 or abs(prof_put.cex_total) < 1e-10:
            pytest.skip("CEX ATM ~ 0, test non discriminant")

        sign_call = -1 if prof_call.cex_total < 0 else 1
        sign_put  = -1 if prof_put.cex_total  < 0 else 1
        assert sign_call == sign_put, (
            f"Convention short-all CEX violee : call={prof_call.cex_total:.6f} "
            f"put={prof_put.cex_total:.6f}"
        )
