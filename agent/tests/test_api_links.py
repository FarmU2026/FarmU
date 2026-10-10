"""fieldwatch.links: HMAC link tokens (sign/verify, scopes, expiry, key from SSM)."""
import pytest

from fieldwatch import links

KEY = b"k" * 32


def test_round_trip():
    t = links.sign("farm_1", "report", "inv_1", ttl_hours=1, key=KEY)
    c = links.verify(t, scope="report", subject_id="inv_1", key=KEY)
    assert (c.farm_id, c.scope, c.subject_id) == ("farm_1", "report", "inv_1")


def test_expired():
    t = links.sign("farm_1", "report", "inv_1", ttl_hours=1, key=KEY, now=1_000_000)
    with pytest.raises(links.LinkError) as e:
        links.verify(t, key=KEY, now=1_000_000 + 3601)
    assert e.value.code == "TOKEN_EXPIRED"


@pytest.mark.parametrize("kwargs", [{"scope": "work_order"}, {"subject_id": "inv_2"}])
def test_wrong_scope_or_subject(kwargs):
    t = links.sign("farm_1", "report", "inv_1", key=KEY)
    with pytest.raises(links.LinkError) as e:
        links.verify(t, key=KEY, **kwargs)
    assert e.value.code == "TOKEN_SCOPE"


@pytest.mark.parametrize("bad", [None, "", "abc", "a.b.c", "!!!.???"])
def test_malformed(bad):
    with pytest.raises(links.LinkError) as e:
        links.verify(bad, key=KEY)
    assert e.value.code == "TOKEN_SCOPE"


def test_tampered_payload_and_other_key():
    t = links.sign("farm_1", "report", "inv_1", key=KEY)
    other = links.sign("farm_2", "report", "inv_1", key=KEY)
    forged = other.split(".")[0] + "." + t.split(".")[1]
    for token, key in ((forged, KEY), (t, b"other-key")):
        with pytest.raises(links.LinkError):
            links.verify(token, key=key)


def test_unknown_scope_rejected():
    with pytest.raises(ValueError):
        links.sign("farm_1", "admin", "x", key=KEY)


def test_url():
    assert links.url("https://d.example.net/", "work_order", "act_1", "TOK") == \
        "https://d.example.net/work-order.html?id=act_1&t=TOK"


def test_key_from_ssm(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.delenv("FIELDWATCH_LINK_KEY", raising=False)
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-west-2",
                 "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    links.clear_key_cache()
    with mock_aws():
        boto3.client("ssm", region_name="us-west-2").put_parameter(
            Name="/fieldwatch/link-signing-key", Value="from-ssm", Type="SecureString")
        t = links.sign("farm_1", "upload", "fld_1")
        assert links.verify(t, key=b"from-ssm").subject_id == "fld_1"
    links.clear_key_cache()
