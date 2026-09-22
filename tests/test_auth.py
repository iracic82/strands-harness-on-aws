from __future__ import annotations

import base64
import json

import pytest
from ops_investigator import auth


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _jwt_with_payload(payload) -> str:
    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"


def _jwt(claims: dict) -> str:
    return _jwt_with_payload(claims)


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


def test_cloud_payload_not_a_json_object():
    tok = _jwt_with_payload(["not", "an", "object"])
    with pytest.raises(auth.ActorError, match="JSON object"):
        auth.actor_from_headers({"Authorization": f"Bearer {tok}"}, profile="cloud")


@pytest.mark.parametrize("bad_sub", [True, 12345])
def test_cloud_sub_must_be_string(bad_sub):
    tok = _jwt({"sub": bad_sub})
    with pytest.raises(auth.ActorError, match="sub"):
        auth.actor_from_headers({"Authorization": f"Bearer {tok}"}, profile="cloud")


def test_local_empty_actor_header_falls_back():
    assert auth.actor_from_headers({"X-Actor-Id": ""}, profile="local", payload_actor="p") == "p"
    assert auth.actor_from_headers({"X-Actor-Id": ""}, profile="local") == "local-dev"
