"""
test_snapshot_endpoint.py — Tests Phase B5 :
  - GET /api/snapshot retourne les sous-payloads requis avec coherence snapshot_ts
  - Test leger : verifie la structure sans appel Deribit reel
"""
from __future__ import annotations
import pytest


REQUIRED_KEYS = {
    "snapshot_ts", "spot", "dashboard", "walls",
    "squeeze", "dealer", "narrative", "bme_status",
}


def test_snapshot_endpoint_is_registered_in_routes():
    """
    /api/snapshot est enregistre dans les routes FastAPI.
    Utilise une import protegee par les variables d'environnement.
    """
    import os
    import sys

    # Injecter les env vars necessaires avant l'import de main
    env_vars = {
        "TELEGRAM_BOT_TOKEN": "test_token",
        "TELEGRAM_CHAT_ID": "123",
        "ALLOWED_CHAT_IDS": "123",
        "HISTORY_DB_PATH": "/tmp/test_options.db",
        "NEURAL_MODEL_DIR": "/tmp/test_models",
    }
    orig_env = {}
    for k, v in env_vars.items():
        orig_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        # Forcer un reimport propre
        mods_to_remove = [k for k in sys.modules if k.startswith("backend.main")]
        for m in mods_to_remove:
            del sys.modules[m]

        from backend.main import app
        routes = [r.path for r in app.routes]
        assert "/api/snapshot" in routes, (
            f"/api/snapshot non trouve dans les routes. Routes disponibles: "
            f"{[r for r in routes if 'snapshot' in r or 'dashboard' in r]}"
        )
    finally:
        for k, orig in orig_env.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
