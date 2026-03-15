import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

import jwt
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/oauth/callback",
)
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")
TOKEN_FILE = Path("tokens.json")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

SCOPES = ["openid", "email", "profile"]

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise RuntimeError(
        "Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET. "
        "Add them to your environment or a .env file."
    )

app = FastAPI(title="Google OAuth 2.0 Client Demo")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


def load_tokens() -> Optional[Dict[str, Any]]:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def save_tokens(tokens: Dict[str, Any]) -> None:
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def build_auth_url(state: str) -> str:
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return requests.Request("GET", AUTH_URL, params=params).prepare().url


def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    response = requests.post(TOKEN_URL, data=data, timeout=20)
    response.raise_for_status()
    token_data = response.json()

    token_data["expires_at"] = int(time.time()) + int(token_data.get("expires_in", 3600))
    return token_data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    response = requests.post(TOKEN_URL, data=data, timeout=20)
    response.raise_for_status()
    refreshed = response.json()

    tokens = load_tokens() or {}
    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_in"] = refreshed.get("expires_in", 3600)
    tokens["expires_at"] = int(time.time()) + int(refreshed.get("expires_in", 3600))

    if "id_token" in refreshed:
        tokens["id_token"] = refreshed["id_token"]

    if "refresh_token" in refreshed:
        tokens["refresh_token"] = refreshed["refresh_token"]

    save_tokens(tokens)
    return tokens


def get_valid_access_token() -> str:
    tokens = load_tokens()
    if not tokens:
        raise HTTPException(status_code=401, detail="No tokens found. Authenticate first.")

    expires_at = int(tokens.get("expires_at", 0))
    now = int(time.time())

    if now >= expires_at - 60:
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=401, detail="No refresh token available.")
        tokens = refresh_access_token(refresh_token)

    return tokens["access_token"]


def validate_google_id_token(id_token: str) -> Dict[str, Any]:
    jwks_response = requests.get(JWKS_URL, timeout=20)
    jwks_response.raise_for_status()
    jwks = jwks_response.json()

    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")

    public_key = None
    for jwk in jwks["keys"]:
        if jwk["kid"] == kid:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
            break

    if public_key is None:
        raise HTTPException(status_code=401, detail="Matching JWK not found for token.")

    try:
        payload = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=GOOGLE_CLIENT_ID,
            issuer="https://accounts.google.com",
        )
        return payload
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid ID token: {exc}") from exc


def get_userinfo(access_token: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(USERINFO_URL, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    tokens = load_tokens()
    logged_in = bool(tokens and "access_token" in tokens)

    return f"""
    <html>
      <body style="font-family: sans-serif; max-width: 800px; margin: 40px auto;">
        <h1>Google OAuth 2.0 Client Demo</h1>
        <p>Status: <strong>{"Authenticated" if logged_in else "Not authenticated"}</strong></p>

        <ul>
          <li><a href="/login">Login with Google</a></li>
          <li><a href="/profile">Call authenticated API (userinfo)</a></li>
          <li><a href="/validate-id-token">Validate ID token</a></li>
          <li><a href="/logout">Logout</a></li>
        </ul>

        <p>Tokens are stored locally in <code>tokens.json</code> for demo purposes.</p>
      </body>
    </html>
    """


@app.get("/login")
def login(request: Request):
    state = secrets.token_hex(16)
    request.session["oauth_state"] = state
    return RedirectResponse(build_auth_url(state))


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

    expected_state = request.session.get("oauth_state")
    if not expected_state or not state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    token_data = exchange_code_for_tokens(code)

    existing = load_tokens() or {}
    if "refresh_token" not in token_data and "refresh_token" in existing:
        token_data["refresh_token"] = existing["refresh_token"]

    save_tokens(token_data)
    return RedirectResponse(url="/")


@app.get("/profile")
def profile():
    access_token = get_valid_access_token()
    return get_userinfo(access_token)


@app.get("/validate-id-token")
def validate_id_token():
    tokens = load_tokens()
    if not tokens or "id_token" not in tokens:
        raise HTTPException(status_code=400, detail="No ID token available.")

    payload = validate_google_id_token(tokens["id_token"])
    return {
        "message": "ID token is valid",
        "claims": payload,
    }


@app.get("/logout")
def logout():
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    return RedirectResponse(url="/")