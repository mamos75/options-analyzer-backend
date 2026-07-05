"""
Auth module — JWT + SQLite
Endpoints: POST /api/auth/login, GET /api/auth/verify, POST /api/auth/register
"""

import sqlite3
import os
import logging
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

log = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET", "mamos-change-me-in-prod")
JWT_EXPIRY_DAYS = int(os.environ.get("JWT_EXPIRY_DAYS", "30"))
DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/users.db")

router = APIRouter(prefix="/api/auth")
security = HTTPBearer(auto_error=False)


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    conn.commit()
    conn.close()
    log.info("[auth] DB initialisée: %s", DB_PATH)


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    plan: str = "free"
    admin_secret: str = ""


def _make_token(user_id: int, email: str, plan: str) -> tuple[str, int]:
    expires = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS)
    token = jwt.encode(
        {"sub": str(user_id), "email": email, "plan": plan, "exp": expires},
        JWT_SECRET,
        algorithm="HS256",
    )
    return token, int(expires.timestamp() * 1000)


@router.post("/login")
async def login(req: LoginRequest):
    conn = _get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?", (req.email.lower(),)
    ).fetchone()
    conn.close()

    if not user or not bcrypt.checkpw(req.password.encode(), user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Identifiants invalides")

    token, expires_at_ms = _make_token(user["id"], user["email"], user["plan"])
    return {
        "token": token,
        "plan": user["plan"],
        "userId": user["id"],
        "expiresAt": expires_at_ms,
    }


@router.get("/verify")
async def verify(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        return {"valid": False, "plan": "free"}

    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        return {
            "valid": True,
            "plan": payload.get("plan", "free"),
            "userId": payload.get("sub"),
        }
    except jwt.ExpiredSignatureError:
        return {"valid": False, "plan": "free", "reason": "expired"}
    except jwt.InvalidTokenError:
        return {"valid": False, "plan": "free", "reason": "invalid"}


@router.post("/register")
async def register(req: RegisterRequest):
    admin_secret = os.environ.get("AUTH_ADMIN_SECRET", "")
    if not admin_secret or req.admin_secret != admin_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    if req.plan not in ("free", "premium"):
        raise HTTPException(status_code=400, detail="Plan invalide (free | premium)")

    hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()

    try:
        conn = _get_db()
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, plan) VALUES (?, ?, ?)",
            (req.email.lower(), hashed, req.plan),
        )
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()
        log.info("[auth] Nouvel utilisateur créé: %s plan=%s", req.email, req.plan)
        return {"success": True, "email": req.email, "plan": req.plan, "userId": user_id}
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(status_code=409, detail="Email déjà utilisé")
        raise HTTPException(status_code=500, detail="Erreur serveur")


@router.patch("/plan")
async def update_plan(
    email: str,
    plan: str,
    admin_secret: str = "",
):
    """Mettre à jour le plan d'un utilisateur (admin seulement)."""
    secret = os.environ.get("AUTH_ADMIN_SECRET", "")
    if not secret or admin_secret != secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    if plan not in ("free", "premium"):
        raise HTTPException(status_code=400, detail="Plan invalide (free | premium)")

    conn = _get_db()
    result = conn.execute(
        "UPDATE users SET plan = ? WHERE email = ?", (plan, email.lower())
    )
    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    log.info("[auth] Plan mis à jour: %s → %s", email, plan)
    return {"success": True, "email": email, "plan": plan}
