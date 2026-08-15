from typing import Dict

from fastapi import APIRouter, Depends, HTTPException

from core.auth import ADMIN_USER, ADMIN_PASSWORD, create_admin_token, require_admin

router = APIRouter(tags=["auth"])


@router.post("/api/auth/login")
async def admin_login(body: Dict):
    user = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    if pw == ADMIN_PASSWORD and (not ADMIN_USER or user == ADMIN_USER or not user):
        return {"token": create_admin_token(), "user": ADMIN_USER}
    raise HTTPException(status_code=401, detail="Falsche Zugangsdaten")


@router.get("/api/auth/verify")
async def admin_verify(_: bool = Depends(require_admin)):
    return {"valid": True}
