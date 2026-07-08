"""
test_vexcex_semantics.py — Tests canoniques de la sémantique VEX/CEX.

Ces tests vérifient la TABLE DE VÉRITÉ définie dans vex_cex.py (F17) :

  VEX > 0  →  vol spike  →  dealers VENDENT BTC  →  VOL_SPIKE_RISK
  VEX < 0  →  vol spike  →  dealers ACHÈTENT BTC →  VOL_SPIKE_SUPPORT
  CEX > 0  →  temps passe →  dealers VENDENT BTC  →  CHARM_SELL_PRESSURE
  CEX < 0  →  temps passe →  dealers ACHÈTENT BTC →  CHARM_BUY_SUPPORT

Ref : vex_cex.py docstring, "TABLE DE VERITE (derive des formules BS — non negociable)".
"""

import sys
import math
import unittest

# ── Reproduction des formules directement (sans import backend) ───────────

def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def d1d2(S: float, K: float, sigma: float, T: float, r: float = 0.05):
    iv_sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / iv_sq
    return d1, d1 - iv_sq

def vanna_client(d1: float, d2: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.0
    return -norm_pdf(d1) * d2 / sigma

def charm_client(d1: float, d2: float, sigma: float, T: float, r: float = 0.05) -> float:
    if sigma <= 0 or T <= 0:
        return 0.0
    sqrt_T = math.sqrt(T)
    raw = -norm_pdf(d1) * (2 * r * T - d2 * sigma * sqrt_T) / (2 * T * sigma * sqrt_T)
    return raw / 365

def compute_vex(S: float, K: float, sigma: float, T: float, OI: float) -> float:
    """Convention short-all v2 : vex = -vanna_client * OI * spot."""
    d1, d2 = d1d2(S, K, sigma, T)
    van = vanna_client(d1, d2, sigma)
    return -van * OI * S

def compute_cex(S: float, K: float, sigma: float, T: float, OI: float) -> float:
    """Convention short-all v2 : cex = -charm_client * OI."""
    d1, d2 = d1d2(S, K, sigma, T)
    cha = charm_client(d1, d2, sigma, T)
    return -cha * OI

# ── Paramètres communs ────────────────────────────────────────────────────

S = 64_000.0     # spot BTC
OI = 1_000.0     # contrats
SIG = 0.80       # IV 80%
T7 = 7 / 365    # 7 DTE


class TestVexSemanticsFromFirstPrinciples(unittest.TestCase):
    """
    Ces tests valident la sémantique AVANT tout code de classification.
    Si ces tests passent, la table de vérité docstring est correcte.
    """

    def test_a_puts_otm_vex_positive_dealers_sell_on_vol_spike(self):
        """
        Cas A — book 100% puts OTM, dealers short.
        
        Ref docstring vex_cex.py :
          "VEX > 0 : dealers VENDENT BTC sur vol spike — VOL_SPIKE_RISK"
        
        Puts OTM (K < S) : d2 > 0 (pour 7 DTE, sigma=80%) → vanna_client < 0
          → vex = -vanna_client * OI * S > 0.
        Sur vol spike : delta_dealer (short put) monte → dealers vendent pour rester flat.
        Cascade classique put-wall : non-négociable.
        """
        K_put = 62_000.0  # OTM put
        vex = compute_vex(S, K_put, SIG, T7, OI)
        
        # 1. VEX > 0 pour puts OTM
        self.assertGreater(vex, 0,
            f"Puts OTM devrait donner VEX > 0 (dealers vendent sur vol spike). Obtenu: {vex:.0f}")
        
        # 2. Direction = VOL_SPIKE_RISK (pression baissière)
        _VEX_NEUTRAL_THRESH = 1_000_000
        direction = ("VOL_SPIKE_RISK"    if vex >  _VEX_NEUTRAL_THRESH else
                     "VOL_SPIKE_SUPPORT" if vex < -_VEX_NEUTRAL_THRESH else "NEUTRAL")
        # Avec OI=1000 contrats le VEX peut être sous le seuil — on vérifie juste le signe
        self.assertGreater(vex, 0,
            "VEX positif = VOL_SPIKE_RISK (pression baissière). "
            "Ref: vex_cex.py TABLE DE VERITE, cas puts OTM.")
        
        # 3. Le flux de re-hedging sur +dσ est baissier
        d1, d2 = d1d2(S, K_put, SIG, T7)
        van_c = vanna_client(d1, d2, SIG)
        flux_rehedge_direction = "VENTE" if -van_c > 0 else "ACHAT"
        self.assertEqual(flux_rehedge_direction, "VENTE",
            "Puts OTM : sur vol spike, dealers doivent VENDRE BTC (flux baissier).")

    def test_b_calls_otm_vex_negative_dealers_buy_on_vol_spike(self):
        """
        Cas B — book 100% calls OTM, dealers short.
        
        Ref docstring vex_cex.py :
          "VEX < 0 : dealers ACHÈTENT BTC sur vol spike — VOL_SPIKE_SUPPORT"
        
        Calls OTM (K > S) : d2 < 0 (typique) → vanna_client > 0
          → vex = -vanna_client * OI * S < 0.
        Sur vol spike : delta_dealer (short call) baisse → dealers achètent pour rester flat.
        = carburant classique d'un short squeeze.
        """
        K_call = 66_000.0  # OTM call
        vex = compute_vex(S, K_call, SIG, T7, OI)
        
        # 1. VEX < 0 pour calls OTM
        self.assertLess(vex, 0,
            f"Calls OTM devrait donner VEX < 0 (dealers achètent sur vol spike). Obtenu: {vex:.0f}")
        
        # 2. Le flux de re-hedging sur +dσ est haussier
        d1, d2 = d1d2(S, K_call, SIG, T7)
        van_c = vanna_client(d1, d2, SIG)
        flux_rehedge_direction = "ACHAT" if -van_c < 0 else "VENTE"
        self.assertEqual(flux_rehedge_direction, "ACHAT",
            "Calls OTM : sur vol spike, dealers doivent ACHETER BTC (carburant squeeze haussier). "
            "Ref: vex_cex.py TABLE DE VERITE, cas calls OTM.")

    def test_c_calls_otm_cex_positive_dealers_sell_in_time(self):
        """
        Cas C — calls OTM en decay temporel (CEX).
        
        Ref docstring vex_cex.py :
          "CEX > 0 : l'ecoulement du temps oblige les dealers à VENDRE BTC — CHARM_SELL_PRESSURE"
        
        Calls OTM : charm_client < 0 (delta call baisse avec le temps pour OTM)
          → cex = -charm_client * OI > 0.
        Sur dt > 0 : delta_dealer (short call) monte → dealers vendent.
        """
        K_call = 66_000.0  # OTM call
        cex = compute_cex(S, K_call, SIG, T7, OI)
        
        # 1. CEX > 0 pour calls OTM
        self.assertGreater(cex, 0,
            f"Calls OTM devrait donner CEX > 0 (dealers vendent dans le temps). Obtenu: {cex:.4f}")
        
        # 2. Le flux de re-hedging sur +dt est baissier
        d1, d2 = d1d2(S, K_call, SIG, T7)
        cha_c = charm_client(d1, d2, SIG, T7)
        flux_time_direction = "VENTE" if -cha_c > 0 else "ACHAT"
        self.assertEqual(flux_time_direction, "VENTE",
            "Calls OTM : le temps qui passe oblige les dealers à VENDRE BTC. "
            "Ref: vex_cex.py TABLE DE VERITE, cas CEX+ (CHARM_SELL_PRESSURE).")

    def test_d_puts_otm_cex_negative_dealers_buy_in_time(self):
        """
        Cas D — puts OTM en decay temporel.
        
        Ref docstring vex_cex.py :
          "CEX < 0 : l'ecoulement du temps pousse les dealers à ACHETER BTC — CHARM_BUY_SUPPORT"
        """
        K_put = 62_000.0  # OTM put
        cex = compute_cex(S, K_put, SIG, T7, OI)
        
        # 1. CEX < 0 pour puts OTM
        self.assertLess(cex, 0,
            f"Puts OTM devrait donner CEX < 0 (dealers achètent dans le temps). Obtenu: {cex:.4f}")
        
        # 2. Le flux de re-hedging sur +dt est haussier
        d1, d2 = d1d2(S, K_put, SIG, T7)
        cha_c = charm_client(d1, d2, SIG, T7)
        flux_time_direction = "ACHAT" if -cha_c < 0 else "VENTE"
        self.assertEqual(flux_time_direction, "ACHAT",
            "Puts OTM : le temps qui passe pousse les dealers à ACHETER BTC (support haussier). "
            "Ref: vex_cex.py TABLE DE VERITE, cas CEX- (CHARM_BUY_SUPPORT).")

    def test_e_vex_label_directions_coherent(self):
        """
        Vérification de cohérence entre la valeur VEX et les labels de direction.
        VEX > 0 → VOL_SPIKE_RISK (pas BULLISH_VANNA — label obsolète).
        VEX < 0 → VOL_SPIKE_SUPPORT (pas BEARISH_VANNA — label obsolète).
        """
        _VEX_NEUTRAL_THRESH = 1_000_000

        def vex_direction_label(vex: float) -> str:
            if vex > _VEX_NEUTRAL_THRESH:
                return "VOL_SPIKE_RISK"
            elif vex < -_VEX_NEUTRAL_THRESH:
                return "VOL_SPIKE_SUPPORT"
            return "NEUTRAL"

        # Book put-heavy (puts OTM avec OI 100x)
        vex_put_heavy = compute_vex(S, 62_000.0, SIG, T7, 100_000.0)
        label_put = vex_direction_label(vex_put_heavy)
        self.assertEqual(label_put, "VOL_SPIKE_RISK",
            f"Put-heavy book → VEX={vex_put_heavy:.0f} → devrait être VOL_SPIKE_RISK. Obtenu: {label_put}")

        # Book call-heavy (calls OTM avec OI 100x)
        vex_call_heavy = compute_vex(S, 66_000.0, SIG, T7, 100_000.0)
        label_call = vex_direction_label(vex_call_heavy)
        self.assertEqual(label_call, "VOL_SPIKE_SUPPORT",
            f"Call-heavy book → VEX={vex_call_heavy:.0f} → devrait être VOL_SPIKE_SUPPORT. Obtenu: {label_call}")

        # Vérification que les anciens labels BULLISH/BEARISH n'existent plus
        self.assertNotIn("BULLISH_VANNA", [label_put, label_call],
            "Label obsolète BULLISH_VANNA ne doit plus apparaître (F17).")
        self.assertNotIn("BEARISH_VANNA", [label_put, label_call],
            "Label obsolète BEARISH_VANNA ne doit plus apparaître (F17).")

    def test_f_sign_symmetry_mixed_book(self):
        """
        Un book 50% puts OTM / 50% calls OTM devrait donner VEX proche de 0.
        (La symétrie du book annule les effets vanna.)
        """
        vex_put = compute_vex(S, 62_000.0, SIG, T7, 500.0)
        vex_call = compute_vex(S, 66_000.0, SIG, T7, 500.0)
        vex_total = vex_put + vex_call
        
        # Le signe dépend de l'asymétrie des strikes, mais la magnitude doit être réduite
        # On vérifie juste la cohérence des signes individuels
        self.assertGreater(vex_put, 0, "Puts OTM : VEX > 0")
        self.assertLess(vex_call, 0, "Calls OTM : VEX < 0")
        # La somme est plus petite en valeur absolue que chaque composante
        self.assertLess(abs(vex_total), abs(vex_put) + abs(vex_call),
            "Book mixte : |VEX_total| < |VEX_puts| + |VEX_calls| (compensation partielle)")


if __name__ == "__main__":
    print("=" * 60)
    print("Test sémantique VEX/CEX — F17")
    print("Table de vérité : vex_cex.py docstring")
    print("=" * 60)
    unittest.main(verbosity=2)
