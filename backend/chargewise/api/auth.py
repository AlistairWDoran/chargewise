"""Authentication dependency.

The internet-facing dashboard signs in with Microsoft/Google (OAuth) — the
frontend (Auth.js) handles the OAuth dance and forwards a bearer token, which
this dependency validates. In development (auth_disabled) it's a no-op so the
API and tests run without credentials. Real OAuth/JWT verification is wired in
when the auth provider is registered.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status


def require_user(request: Request) -> str:
    settings = request.app.state.settings
    if settings.auth_disabled:
        return "dev"
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # TODO: verify the OAuth token signature/claims against the configured provider.
    return header[7:].strip()
