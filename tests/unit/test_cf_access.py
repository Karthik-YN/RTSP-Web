"""Phase 10: Cloudflare Access in front of the dashboard (fail closed, identity on events)."""

import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.cf_access import AccessError, CloudflareAccess, team_url

TEAM, AUD = "acme", "aud-tag-123"
ISS = "https://acme.cloudflareaccess.com"


def _keypair(kid):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update(kid=kid, alg="RS256", use="sig")
    return priv, jwk


PRIV, JWK = _keypair("k1")
OTHER, _ = _keypair("k2")


def token(email="alice@acme.com", aud=AUD, iss=ISS, exp_in=600, key=PRIV, kid="k1", alg="RS256"):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in, "sub": "u1"},
                      key, algorithm=alg, headers={"kid": kid})


def cf(**kw):
    async def keys():
        return [JWK]
    return CloudflareAccess(TEAM, [AUD], keys_loader=keys, **kw)


def run(coro):
    return asyncio.run(coro)


def test_team_url_forms():
    for t in ("acme", "acme.cloudflareaccess.com", "https://acme.cloudflareaccess.com/"):
        assert team_url(t) == ISS


def test_valid_token_gives_the_email():
    assert run(cf().verify(token())).email == "alice@acme.com"


@pytest.mark.parametrize("bad", [
    lambda: token(aud="another-app"), lambda: token(iss="https://evil.cloudflareaccess.com"),
    lambda: token(exp_in=-120), lambda: token(key=OTHER), lambda: token(kid="unknown"),
    lambda: "not-a-jwt", lambda: jwt.encode({"aud": AUD, "iss": ISS}, "secret", algorithm="HS256"),
])
def test_bad_tokens_are_refused(bad):
    with pytest.raises(AccessError):
        run(cf().verify(bad()))


def test_tunnel_requests_need_a_token_and_lan_requests_do_not():
    c = cf()
    assert run(c.check({}, {})) is None  # LAN, no token: open as before
    with pytest.raises(AccessError, match="no Cloudflare Access token"):
        run(c.check({"cf-ray": "x", "cf-connecting-ip": "1.2.3.4"}, {}))
    assert run(c.check({"cf-ray": "x"}, {"CF_Authorization": token()})).email == "alice@acme.com"
    with pytest.raises(AccessError):  # all mode: LAN needs a token too
        run(cf(mode="all").check({}, {}))


def test_unconfigured_server_refuses_tunnel_traffic():
    c = CloudflareAccess("", [])
    assert run(c.check({}, {})) is None
    with pytest.raises(AccessError, match="not configured"):
        run(c.check({"cf-ray": "x", "cf-access-jwt-assertion": token()}, {}))


def test_middleware_fails_closed_and_names_the_actor(rig_factory, monkeypatch):
    rig = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False, cf_access_team=TEAM, cf_access_aud=AUD)
    with rig:
        async def keys():
            return [JWK]
        monkeypatch.setattr(rig.app.state.cf_access, "_loader", keys)
        c = rig.client
        assert c.get("/api/config").status_code == 200  # office LAN
        tunnel = {"cf-ray": "abc", "cf-connecting-ip": "203.0.113.7", "host": "rig02.example.com"}
        r = c.get("/api/config", headers=tunnel)
        assert r.status_code == 403 and "Cloudflare Access" in r.json()["detail"]
        assert c.get("/", headers=tunnel).status_code == 403  # the dashboard itself too
        bad = c.get("/api/config", headers={**tunnel, "cf-access-jwt-assertion": token(aud="x")})
        assert bad.status_code == 403
        good = {**tunnel, "cf-access-jwt-assertion": token(email="bob@acme.com")}
        assert c.get("/api/auth/me", headers=good).json()["username"] == "bob@acme.com"
        vid = c.get("/api/videos", headers=good).json()[0]["id"]
        r = c.post("/api/streams", headers=good, json={"name": "remote1", "video_id": vid, "start_now": False})
        assert r.status_code == 201
        ev = [e for e in c.get("/api/events?limit=20").json() if e["kind"] == "stream.created"]
        assert ev and ev[0]["username"] == "bob@acme.com"
