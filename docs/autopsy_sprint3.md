# Autopsie Sprint 3 — Dashboard Options Analyzer BTC

Date : 2026-07-15
Environnement : VPS root@138.68.80.156, /root/telegram-claude-bot/dashboard_options/

---

## Architecture constatée

### Calculs backend (source de vérité)
| Module | Rôle | Champ clé |
|--------|------|-----------|
| `gex.py` | GEX total, flip level, régime | `GEXProfile.regime_meca` (source unique) ; `GEXProfile.regime` (legacy, classif. GEX brut) |
| `options_walls.py` | Murs calls/puts | `OptionsWallsProfile.major_put_wall`, `.major_call_wall`, `.walls[]` |
| `narrative_resolver.py` | Niveaux bas/haut, max pain, convergence | `niveau_bas`, `niveau_haut`, `niveau_bas_label` |
| `pro_decision_engine.py` | Verdict Pro, conviction, trade | Lit `snap["dashboard"]["gex_regime"]` via `_read_regime()` |
| `decision_arbiter.py` | Confiance globale, état SUPPRESSED | `confidence_pct` = `global_confidence` |
| `coherence_checks.py` | Assertions runtime (a, j, o, p) | Branché sur `/api/decision` et `/api/pro_decision` |

### Endpoints front
- `/api/decision` → verdict Arbiter + confiance + signals_used
- `/api/pro_decision` → verdict Pro + conviction + forces + trade
- `/api/narrative` → niveau_bas/haut, max_pain_display, btc_price
- `/api/vex_cex` → VEX, CEX, gamma_flip, gamma_flip_regime
- `/api/snapshot` → walls.walls[], spot

### Snapshot options
- 297 options BTC fetchées via Deribit WebSocket
- Champ OI : `OptionData.oi` (mappé depuis `open_interest` Deribit)
- 256/297 options avec OI > 0, dont puts ET calls (OI puts présent ✓)
- Fieldset : `instrument, strike, expiry, option_type, oi, volume, gamma, delta, iv, mark_price, bid, ask`

---

## Bug 1 — "STABILISANT" fantôme dans le bloc Pro

### Symptôme
Le bloc Pro affiche "Régime GEX : STABILISANT" alors que tous les autres blocs affichent "ZONE_DE_FLIP".

### Ligne fautive
**`backend/main.py:3846`**
```python
snap_dict = {
    "spot": spot,
    "dashboard": {
        "btc_price":   spot,
        "gex_total":   gex_obj.total_gex,
        "gex_regime":  gex_obj.regime,       # ← BUG : champ legacy
        "flip_level":  gex_obj.flip_level,
        ...
    },
```

### Cause
`GEXProfile` possède **deux champs de régime** :
- `regime` : calculé par `_classify_regime(total_gex)` (signe du GEX brut total) → "STABILISANT" si GEX > 0
- `regime_meca` : source unique v3-bis, intègre la logique spot/flip avec hystérésis → "ZONE_DE_FLIP" si spot à < 1% du flip

Tous les autres endpoints (`/api/decision`, `/api/narrative`, `/api/vex_cex`) passent `gex_obj.regime_meca`. La construction `snap_dict` pour `/api/pro_decision` passe `gex_obj.regime` — l'ancien champ.

Preuve live :
```
gex_obj.regime      = 'STABILISANT'   ← legacy, signe GEX brut
gex_obj.regime_meca = 'ZONE_DE_FLIP'  ← source unique correcte
```

### Pourquoi les sprints 1-2 ont raté
Le fix Sprint 2 a corrigé le VEX/CEX manquant (H3), mais n'a pas inspecté la construction de `snap_dict` pour le champ `gex_regime`. Le bug survit car les deux champs co-existent avec des noms similaires.

### Fix : `main.py:3846` → `gex_obj.regime_meca`

---

## Bug 2 — "Put wall" fantôme dans la Convergence Triple

### Symptôme
La "Convergence triple" affiche "Gamma Flip + Max Pain + Put wall" alors qu'aucun put wall n'est identifié sous le spot. Le paragraphe mentionne "Put wall" en dur quand `flipNearBas = true`.

### Ligne fautive
**`frontend/js/widgets/regime.js:83-84`**
```javascript
'⚠️ <b>Convergence triple</b> autour de ' + fmtP(refPrice) + ' : Gamma Flip + Max Pain + ' +
(flipNearBas ? 'Put wall' : 'Call wall') + ' sont tous dans la même zone. ' +
```

### Cause
`lvlBas` (le "support" bas) est calculé dans `_compute_niveau_bas()` et peut être le **flip level lui-même** quand aucun support plus bas n'est identifié (c'est la priorité n°1 de `_compute_niveau_bas`). Quand flip ≈ max_pain ≈ lvlBas (tous autour de 65 000), `convergingCount >= 3` et `flipNearBas = true` → label "Put wall" affiché faussement pour un niveau qui est le flip.

`lvlBas` n'est pas nécessairement un put wall — c'est le niveau support le plus pertinent, qui peut être : flip, explosive, support gravity, put wall ou fallback spot-5%.

### Situation OI Puts (vérification R6)
**OI puts PRÉSENT dans les données brutes** — pas de STOP R6.
- 143 puts sur 297 options totales
- Exemples : strike 65000 put OI=42.2, strike 64500 put OI=60.5
- `major_put_wall = None` car aucun put wall n'est **sous** le spot (tous les puts OI significatifs sont autour du flip qui est au niveau du spot)
- L'absence de put wall identifié est correcte — les puts sont concentrés ATM, pas sous le spot

### Fix
Dans le label de convergence, remplacer l'heuristique `flipNearBas ? 'Put wall' : 'Call wall'` par le label réel de `lvlBas` (disponible dans `lvlBasLbl`).

---

## Bug 3 — Middleware d'assertions non bloquant pour les bugs 1 et 2

### État du middleware
`coherence_checks.py` existe et est **branché** sur les deux endpoints :
- `main.py:1579` : `return run_coherence_checks(_decision_payload, "/api/decision")`
- `main.py:3978` : `return run_coherence_checks(_pro_payload, "/api/pro_decision")`

### Assertions implémentées (active)
- **(a)** forces_direction — BLOQUANT : forces ne contiennent pas direction opposée au verdict
- **(j)** confidence_unity — WARNING : global_confidence == confidence_pct
- **(o)** suppressed_state — BLOQUANT : trade WAIT si arbiter < 30%
- **(p)** thesis_clean — WARNING : pas de stop/target dans thèse WAIT

### Pourquoi les bugs passent
Les assertions implémentées ne couvrent **pas** :
- **(l)** régime unique dans tout le payload — **assertion manquante**
- **(m1)** montants $ dans un texte doivent exister dans les niveaux du snapshot — **assertion manquante**
- **(m2)** types de niveaux des textes = types réels — **assertion manquante**

Le bug 1 (régime divergent) n'est pas intercepté car aucune assertion ne compare `snap_dict["dashboard"]["gex_regime"]` au régime des autres blocs. Le bug 2 (label "Put wall" faux) se produit dans le frontend, hors du périmètre du middleware backend.

### Chemins de sérialisation
Un seul serializer actif pour chaque endpoint :
- `/api/decision` → `_decision_payload` dict → `run_coherence_checks` → return
- `/api/pro_decision` → `json.loads(json.dumps(asdict(decision)))` → `run_coherence_checks` → return

Pas de chemin parallèle identifié. Le middleware est correctement positionné mais ses assertions sont insuffisantes.

---

## Grep exhaustif — occurrences STABILISANT/AMPLIFICATEUR/ZONE_DE_FLIP

### Enum / définitions (légitimes)
- `gex.py:73` : `regime: str   # "STABILISANT" | "AMPLIFICATEUR" | "NEUTRE"` (legacy)
- `gex.py:84` : `regime_meca: str = "NEUTRE"  # STABILISANT | AMPLIFICATEUR | ZONE_DE_FLIP | NEUTRE`
- `gex.py:660,662,664,676,678` : attribution `regime_meca`
- `regime_vexcex_engine.py:105` : commentaire enum

### Templates / textes générés (légitimes)
- `pro_decision_engine.py:182-203` : `implications` dict par régime
- `pro_decision_engine.py:1058-1065` : forces favorables/adverses selon régime
- `narrative_resolver.py` : paragraphes adaptatifs

### Calcul (source du bug)
- `gex.py:167` : `regime = _classify_regime(total_gex)` → legacy
- `main.py:3846` : **`"gex_regime": gex_obj.regime`** → **BUG**

### Frontend
- `regime.js:162` : label "Put wall" si `_aw.type === 'PUT_WALL'` — correct
- `regime.js:83-84` : **`flipNearBas ? 'Put wall' : 'Call wall'`** → **BUG**
- `pro_decision.js:180` : `reg.regime` depuis `data.regime` (lu depuis payload, déjà faux à la source)

---

## Résumé des actions Phase 1+

| Bug | Fichier | Ligne | Action |
|-----|---------|-------|--------|
| 1 — STABILISANT fantôme | `backend/main.py` | 3846 | `gex_obj.regime` → `gex_obj.regime_meca` |
| 2 — Put wall fantôme | `frontend/js/widgets/regime.js` | 83-84 | Utiliser `lvlBasLbl` au lieu de `flipNearBas ? 'Put wall'` |
| 3 — Assertion (l) manquante | `backend/coherence_checks.py` | — | Ajouter assertion (l) régime unique |
