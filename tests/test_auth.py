from __future__ import annotations

import base64
import json

import pytest
from ops_investigator import auth


def _jwt(claims: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


def test_cloud_actor_from_sub():
    tok = _jwt({"sub": "3f1c-user", "iss": "https://cognito"})
    assert auth.actor_from_headers({"Authorization": f"Bearer {tok}"}, profile="cloud") == "3f1c-user"


def test_cloud_header_case_insensitive():
    tok = _jwt({"sub": "abc"})
    assert auth.actor_from_headers({"authorization": f"bearer {tok}"}, profile="cloud") == "abc"


def test_cloud_missing_header_is_error():
    with pytest.raises(auth.ActorError, match="Authorization"):
        auth.actor_from_headers({}, profile="cloud")


def test_cloud_ignores_payload_actor():
    tok = _jwt({"sub": "real"})
    assert auth.actor_from_headers({"Authorization": f"Bearer {tok}"}, profile="cloud", payload_actor="spoof") == "real"


@pytest.mark.parametrize("bad", ["../x", "a/b", "x" * 65, "", "sp ace"])
def test_actor_regex(bad):
    tok = _jwt({"sub": bad})
    with pytest.raises(auth.ActorError, match="actor"):
        auth.actor_from_headers({"Authorization": f"Bearer {tok}"}, profile="cloud")


def test_local_fallbacks():
    assert auth.actor_from_headers({"X-Actor-Id": "igor"}, profile="local") == "igor"
    assert auth.actor_from_headers({}, profile="local", payload_actor="p") == "p"
    assert auth.actor_from_headers({}, profile="local") == "local-dev"
