"""Cloudflare Access: verify the identity Cloudflare attaches to tunnelled requests.

The dashboard is published on the internet through a Cloudflare Tunnel, with a
Cloudflare Access application (company SSO or e-mail one-time PIN) in front of
it. Cloudflare then adds a signed JWT to every request it forwards
(``Cf-Access-Jwt-Assertion``). This module checks that token against the
team's public keys, so:

* a request that came through Cloudflare but carries no valid token (Access
  not configured, a policy removed by mistake, a token for another app) is
  refused — the rig fails closed instead of being public with login off;
* the verified e-mail becomes the user name on events ("video.uploaded by
  alice@…"), so remote actions are attributable.

Requests that did not come through Cloudflare (office LAN) are unaffected in
the default ``tunnel`` mode. ``all`` mode requires a valid token for every
request (use it when the rig is reachable only through the tunnel).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

log = logging.getLogger(__name__)

TUNNEL_HEADERS = ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion")
TOKEN_HEADER = "cf-access-jwt-assertion"
TOKEN_COOKIE = "CF_Authorization"


def team_url(team: str) -> str:
    """'acme' or 'acme.cloudflareaccess.com' or 'https://acme.cloudflareaccess.com/' -> issuer URL."""
    t = team.strip().removeprefix("https://").removeprefix("http://").strip("/")
    if "." not in t:
        t = f"{t}.cloudflareaccess.com"
    return f"https://{t}"


def via_cloudflare(headers) -> bool:
    return any(h in headers for h in TUNNEL_HEADERS)


@dataclass
class Identity:
    email: str
    claims: dict


class AccessError(Exception):
    pass


class CloudflareAccess:
    def __init__(self, team: str, audiences: list[str], mode: str = "tunnel",
                 keys_loader=None, refresh_s: float = 3600.0):
        self.issuer = team_url(team) if team else ""
        self.audiences = [a.strip() for a in audiences if a.strip()]
        self.mode = mode
        self.refresh_s = refresh_s
        self._loader = keys_loader or self._fetch_keys
        self._keys: dict[str, object] = {}
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.audiences)

    def required(self, headers) -> bool:
        return self.mode == "all" or via_cloudflare(headers)

    async def _fetch_keys(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{self.issuer}/cdn-cgi/access/certs")
            r.raise_for_status()
            return r.json()["keys"]

    async def _key(self, kid: str, force: bool = False):
        now = time.monotonic()
        if kid in self._keys and not force and now - self._fetched < self.refresh_s:
            return self._keys[kid]
        async with self._lock:
            # refetch at most every 30 s for unknown key ids (rotation), else hourly
            if force or not self._keys or now - self._fetched >= self.refresh_s or (
                    kid not in self._keys and now - self._fetched > 30):
                try:
                    jwks = await self._loader()
                except Exception as exc:
                    log.error("could not fetch Cloudflare Access keys: %s", exc)
                    if not self._keys:
                        raise AccessError("Cloudflare Access keys are unavailable.") from exc
                else:
                    self._keys = {k["kid"]: jwt.PyJWK(k).key for k in jwks if "kid" in k}
                    self._fetched = time.monotonic()
        if kid not in self._keys:
            raise AccessError("Token signed by an unknown key.")
        return self._keys[kid]

    async def verify(self, token: str) -> Identity:
        if not self.configured:
            raise AccessError("Cloudflare Access is not configured on this server "
                              "(RTSPM_CF_ACCESS_TEAM / RTSPM_CF_ACCESS_AUD).")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AccessError("Malformed access token.") from exc
        if header.get("alg") != "RS256":
            raise AccessError("Unexpected token algorithm.")
        key = await self._key(header.get("kid", ""))
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audiences,
                                issuer=self.issuer, leeway=30,
                                options={"require": ["exp", "iat", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            raise AccessError(f"Access token rejected: {exc}") from exc
        email = claims.get("email") or claims.get("common_name") or claims.get("sub") or "cloudflare-user"
        return Identity(str(email), claims)

    async def check(self, headers, cookies) -> Identity | None:
        """Identity for a request, None when no token is needed; raises AccessError to refuse."""
        token = headers.get(TOKEN_HEADER) or cookies.get(TOKEN_COOKIE)
        if not self.required(headers):
            if token and self.configured:
                try:
                    return await self.verify(token)
                except AccessError:
                    return None
            return None
        if not token:
            raise AccessError("This server is reached through Cloudflare but the request has no "
                              "Cloudflare Access token. Protect the hostname with an Access application.")
        return await self.verify(token)
