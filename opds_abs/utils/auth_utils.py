"""Authentication for OPDS-ABS.

OPDS clients authenticate with HTTP Basic. The credentials are checked against
Audiobookshelf (``POST /login``) and the resulting ABS token is used for every
upstream API call made on that client's behalf.

Supported credentials:

- ``Authorization: Basic base64(username:password)``: primary method.
- ``Authorization: Basic base64(username:apikey)``: when ``API_KEY_AUTH_ENABLED``
  and the secret is not a valid password, it is tried as an ABS API key.
- ``Authorization: Bearer <apikey>``: when ``API_KEY_AUTH_ENABLED``.
- ``?token=<apikey>``: when ``API_KEY_AUTH_ENABLED``, for clients that cannot send
  headers. Never emitted by this server.

SECURITY INVARIANTS (each one closed a real hole, see Jacob-Lasky/opds-abs#1):

- DO NOT key the token cache on username alone. It used to be, so Basic auth with
  ANY password succeeded for a user whose token was cached, including book
  downloads from the public internet. The cache key is a hash of the scheme,
  username AND secret, so a cache hit proves the caller presented the same secret
  that ABS accepted.
- DO NOT fall back to "some cached token for this username" when a request carries
  no credentials. That fallback served a user's catalog to anonymous callers. With
  ``AUTH_ENABLED`` a request without valid credentials gets a 401 challenge.
- DO NOT persist ABS tokens to disk (``cache.pkl``). The token cache is memory
  only; a restart costs one ``/login`` per client.
- DO NOT log secrets, tokens, or ABS response bodies (they contain tokens).
"""
import asyncio
import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import aiohttp
from fastapi import HTTPException, Request

from opds_abs.config import (
    API_KEY_AUTH_ENABLED,
    AUDIOBOOKSHELF_INTERNAL_URL,
    AUTH_CACHE_EXPIRY,
    AUTH_ENABLED,
    AUTH_TOKEN_CACHING,
)
from opds_abs.utils.error_utils import AuthenticationError

logger = logging.getLogger(__name__)

CHALLENGE_HEADERS = {"WWW-Authenticate": 'Basic realm="OPDS-ABS", charset="UTF-8"'}
_ABS_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass(frozen=True)
class Credentials:
    """Credentials extracted from a request.

    ``scheme`` is ``basic``, ``bearer`` or ``query``. ``username`` is None for
    bearer/query API keys; ABS tells us who the key belongs to.
    """

    scheme: str
    username: Optional[str]
    secret: str

    def cache_key(self) -> str:
        """Hash of everything the caller presented. See module docstring."""
        raw = f"{self.scheme}\0{self.username or ''}\0{self.secret}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


# cache_key -> lock serialising the upstream login for one set of credentials
_LOGIN_LOCKS: Dict[str, asyncio.Lock] = {}

# cache_key -> (token, abs_username, abs_user_type, expires_at)
_TOKEN_CACHE: Dict[str, Tuple[str, str, str, float]] = {}


class AbsUnavailableError(AuthenticationError):
    """Audiobookshelf could not be reached while checking credentials."""


def _cache_get(creds: Credentials) -> Optional[Tuple[str, str, str]]:
    if not AUTH_TOKEN_CACHING:
        return None
    entry = _TOKEN_CACHE.get(creds.cache_key())
    if entry is None:
        return None
    *identity, expires_at = entry
    if time.monotonic() >= expires_at:
        _TOKEN_CACHE.pop(creds.cache_key(), None)
        return None
    return tuple(identity)


def _cache_put(creds: Credentials, identity: Tuple[str, str, str]) -> None:
    if AUTH_TOKEN_CACHING:
        _TOKEN_CACHE[creds.cache_key()] = (*identity, time.monotonic() + AUTH_CACHE_EXPIRY)


def invalidate_token(token: str) -> None:
    """Drop every cache entry holding ``token`` (ABS rejected it upstream)."""
    for key in [k for k, v in _TOKEN_CACHE.items() if v[0] == token]:
        del _TOKEN_CACHE[key]


def clear_token_cache() -> None:
    """Forget every cached token."""
    _TOKEN_CACHE.clear()


def get_credentials_from_request(request: Request) -> Optional[Credentials]:
    """Extract credentials from the request, or None if it carries none."""
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()
    value = value.strip()

    if scheme == "basic" and value:
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            logger.warning("Malformed Basic Authorization header")
            return None
        username, sep, secret = decoded.partition(":")
        if not sep or not username or not secret:
            logger.warning("Basic Authorization header is not username:password")
            return None
        return Credentials("basic", username, secret)

    if scheme == "bearer" and value:
        if not API_KEY_AUTH_ENABLED:
            logger.warning("Bearer credentials rejected: API_KEY_AUTH_ENABLED is false")
            return None
        return Credentials("bearer", None, value)

    if header:
        # DO NOT log the scheme text: "Bearerabc123" (no space) is one token, and
        # logging it disclosed the credential.
        logger.warning("Unsupported or malformed Authorization header (%d chars)", len(header))

    token_param = request.query_params.get("token")
    if token_param and API_KEY_AUTH_ENABLED:
        return Credentials("query", None, token_param)
    return None


async def _abs_login(session: aiohttp.ClientSession, username: str, password: str) -> Optional[Tuple[str, str, str]]:
    """Password login. Returns (token, abs_username, user_type), or None if ABS rejects it."""
    async with session.post(
        f"{AUDIOBOOKSHELF_INTERNAL_URL}/login",
        json={"username": username, "password": password},
    ) as response:
        if response.status in (401, 403):
            return None
        if response.status != 200:
            # 429 (ABS login rate limit), 5xx: the password was not judged, so
            # DO NOT answer the reader with "wrong password".
            raise AbsUnavailableError(f"Audiobookshelf /login returned HTTP {response.status}")
        user = (await response.json()).get("user") or {}
    # Prefer the legacy non-expiring token; ABS >= 2.26 also returns an expiring
    # accessToken, which works until it expires and then forces a re-login.
    token = user.get("token") or user.get("accessToken")
    if not token:
        raise AuthenticationError("Audiobookshelf /login response contained no token")
    return token, user.get("username") or username, user.get("type") or "user"


async def _abs_authorize_api_key(session: aiohttp.ClientSession, api_key: str) -> Optional[Tuple[str, str, str]]:
    """API key check. Returns (api_key, abs_username, user_type), or None if ABS rejects it.

    ABS API keys are used as Bearer tokens. ``POST /api/authorize`` returns
    ``{"user": {...}}``; ABS has no X-Api-Key header.
    """
    async with session.post(
        f"{AUDIOBOOKSHELF_INTERNAL_URL}/api/authorize",
        headers={"Authorization": f"Bearer {api_key}"},
    ) as response:
        if response.status in (401, 403):
            return None
        if response.status != 200:
            raise AbsUnavailableError(f"Audiobookshelf /api/authorize returned HTTP {response.status}")
        user = (await response.json()).get("user") or {}
    if not user.get("username"):
        raise AuthenticationError("Audiobookshelf /api/authorize response contained no user")
    return api_key, user["username"], user.get("type") or "user"


async def authenticate(creds: Credentials) -> Tuple[str, str, str, str]:
    """Resolve credentials to (abs_username, token, user_type, method).

    ``method`` is ``cache``, ``password`` or ``apikey`` and exists for logging.

    Raises:
        AuthenticationError: ABS rejected the credentials.
        AbsUnavailableError: ABS could not be reached.
    """
    cached = _cache_get(creds)
    if cached:
        token, abs_username, user_type = cached
        return abs_username, token, user_type, "cache"

    # Readers fire several requests at once on open; without this each one
    # logged in separately, and every ABS /login creates a session row.
    key = creds.cache_key()
    try:
        async with _LOGIN_LOCKS.setdefault(key, asyncio.Lock()):
            cached = _cache_get(creds)
            if cached:
                token, abs_username, user_type = cached
                return abs_username, token, user_type, "cache"
            return await _authenticate_upstream(creds)
    finally:
        # Waiters keep their reference; dropping it here keeps the dict from
        # growing with every distinct (e.g. wrong) password ever tried.
        _LOGIN_LOCKS.pop(key, None)


async def _authenticate_upstream(creds: Credentials) -> Tuple[str, str, str, str]:
    try:
        async with aiohttp.ClientSession(timeout=_ABS_TIMEOUT) as session:
            result = None
            method = "password"
            if creds.scheme == "basic":
                result = await _abs_login(session, creds.username, creds.secret)
                if result is None and API_KEY_AUTH_ENABLED:
                    result = await _abs_authorize_api_key(session, creds.secret)
                    method = "apikey"
                    if result and result[1] != creds.username:
                        logger.warning(
                            "API key presented as user %r belongs to %r; using %r",
                            creds.username, result[1], result[1])
            else:
                result = await _abs_authorize_api_key(session, creds.secret)
                method = "apikey"
    except (aiohttp.ClientError, TimeoutError) as e:
        raise AbsUnavailableError(f"Cannot reach Audiobookshelf at {AUDIOBOOKSHELF_INTERNAL_URL}: {e}") from None

    if result is None:
        raise AuthenticationError("Invalid credentials")
    _cache_put(creds, result)
    token, abs_username, user_type = result
    return abs_username, token, user_type, method


def _challenge(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers=CHALLENGE_HEADERS)


async def get_authenticated_user(request: Request) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """FastAPI dependency: authenticate the request.

    Returns ``(username, token, display_name)``; username and display_name are
    both the ABS username. With ``AUTH_ENABLED=false`` returns ``(None, None, None)``.

    With ``AUTH_ENABLED`` a request without valid credentials is answered with a
    401 Basic challenge, which is what makes OPDS clients prompt for or send
    their stored credentials. The outcome is recorded on ``request.state`` for
    the access log.
    """
    if not AUTH_ENABLED:
        request.state.auth_method = "disabled"
        return None, None, None

    creds = get_credentials_from_request(request)
    if creds is None:
        request.state.auth_method = "none"
        raise _challenge("Authentication required")

    request.state.auth_user = creds.username
    try:
        username, token, user_type, method = await authenticate(creds)
    except AbsUnavailableError as e:
        request.state.auth_method = "abs-unavailable"
        logger.error("Auth check failed for %r: %s", creds.username, e)
        raise HTTPException(status_code=503, detail="Audiobookshelf server is unavailable") from None
    except AuthenticationError as e:
        request.state.auth_method = f"{creds.scheme}-rejected"
        logger.warning("Authentication failed for %r via %s: %s", creds.username, creds.scheme, e)
        raise _challenge("Invalid credentials") from None

    request.state.auth_user = username
    request.state.auth_method = method
    request.state.abs_user_type = user_type
    return username, token, username


ADMIN_USER_TYPES = frozenset({"root", "admin"})


async def require_admin(request: Request) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """FastAPI dependency: an authenticated ABS root or admin user.

    The /admin endpoints clear and inspect every user's cache; any ABS account
    could call them before. With auth disabled there is no way to tell, so
    they are refused.
    """
    auth_info = await get_authenticated_user(request)
    if getattr(request.state, "abs_user_type", None) not in ADMIN_USER_TYPES:
        raise HTTPException(status_code=403, detail="Audiobookshelf admin required")
    return auth_info
