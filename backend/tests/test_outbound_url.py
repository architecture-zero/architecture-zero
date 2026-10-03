"""Operator-set outbound URLs are checked before anything is written.

ollama_base_url (PUT /api/settings) is where every prompt goes, and
rerank_remote_url (PATCH /api/admin/config) is POSTed candidate chunk text.
Until 2026-10-02 both stored any string - the cloud metadata address included.
The check refuses a non-http(s) scheme and a link-local destination, allows
loopback and private ranges (a self-hosted model is internal), and runs in the
pre-write block, so a refused address writes nothing else from the same body.
"""
import pytest
from fastapi import HTTPException

from app.config import get_config, set_config
from app.security import validate_outbound_url


@pytest.mark.parametrize("url", [
    "", "http://127.0.0.1:11434", "http://10.0.0.5:8080",
    "http://[::1]:11434", "https://203.0.113.7/v1",
])
def test_internal_and_public_addresses_pass(url):
    assert validate_outbound_url(url) == url


@pytest.mark.parametrize("url", [
    "ftp://x", "file:///etc/passwd", "http:///nohost",
    "http://169.254.169.254/latest/meta-data",
    "http://[fe80::1]/", "http://[::ffff:169.254.169.254]/",
])
def test_metadata_and_non_http_addresses_are_refused(url):
    with pytest.raises(HTTPException) as e:
        validate_outbound_url(url)
    assert e.value.status_code == 400


def test_settings_refuses_a_metadata_address_and_writes_nothing(client, admin_headers):
    before_url = get_config("ollama_base_url", "")
    before_flag = get_config("provider_ollama_enabled", "")
    r = client.put("/api/settings", headers=admin_headers,
                   json={"ollama_enabled": before_flag != "true",
                         "ollama_base_url": "http://169.254.169.254/latest",
                         "current_password": "AdminPass1"})
    assert r.status_code == 400, r.text
    assert get_config("ollama_base_url", "") == before_url
    assert get_config("provider_ollama_enabled", "") == before_flag, \
        "a toggle in the same body was written before the refusal"


def test_rerank_remote_url_is_checked_before_anything_is_written(client, admin_headers):
    before = get_config("rerank_remote_url", "")
    before_rag = get_config("default_rag_enabled", "")
    try:
        r = client.patch("/api/admin/config", headers=admin_headers,
                         json={"rerank_remote_url": "http://169.254.169.254/",
                               "default_rag_enabled": before_rag != "true"})
        assert r.status_code == 400, r.text
        assert get_config("rerank_remote_url", "") == before
        assert get_config("default_rag_enabled", "") == before_rag
        ok = client.patch("/api/admin/config", headers=admin_headers,
                          json={"rerank_remote_url": "http://10.0.0.9:9000/rerank"})
        assert ok.status_code == 200, ok.text
        assert get_config("rerank_remote_url", "") == "http://10.0.0.9:9000/rerank"
    finally:
        set_config("rerank_remote_url", before)
