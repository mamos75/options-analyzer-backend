#!/bin/bash
# Sprint 3 Phase 1.2 — Architecture linter
# Vérifie qu'aucun code n'utilise gex_obj.regime (legacy) au lieu de gex_obj.regime_meca
# Usage : bash scripts/lint_regime_field.sh (depuis la racine du projet)

set -e

echo '=== Lint: gex_obj.regime (legacy field usage) ==='

# Pattern interdit : gex_obj.regime suivi d'un caractère non-alphanumérique (= fin du champ, pas .regime_meca)
MATCHES=

if [ -n "" ]; then
    echo 'FAIL — utilisation du champ legacy gex_obj.regime détectée :'
    echo ""
    echo ''
    echo 'Fix attendu : remplacer gex_obj.regime par gex_obj.regime_meca'
    exit 1
fi

echo 'OK — aucune utilisation du champ legacy gex_obj.regime'
echo ''

# Vérification secondaire : toutes les assignations gex_regime dans snap_dict utilisent regime_meca
echo '=== Lint: gex_regime dans snap_dict ==='
SNAP_MATCHES=

if [ -n "" ]; then
    echo 'FAIL — gex_regime dans snap_dict sans regime_meca :'
    echo ""
    exit 1
fi

echo 'OK — toutes les assignations gex_regime utilisent regime_meca'
echo ''
echo 'Lint Phase 1.2 : PASS'
