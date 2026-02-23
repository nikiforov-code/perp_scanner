import os

from fastapi import Request


ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()


def is_admin(request: Request) -> bool:
    """
    Admin auth for /admin and /api/admin/*
    Accepts token via:
      - header: X-Admin-Token
      - query:  ?token=...
    """
    if not ADMIN_TOKEN:
        return False

    token_h = (request.headers.get("X-Admin-Token") or "").strip()
    token_q = (request.query_params.get("token") or "").strip()
    token = token_h or token_q
    return token == ADMIN_TOKEN
