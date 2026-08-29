def test_probe_empty_prompt_is_accepted_and_served(client, admin_headers):
    from app.config import get_system_prompt
    original = client.get("/api/admin/config", headers=admin_headers).json()["system_prompt"]
    try:
        r = client.patch("/api/admin/config", json={"system_prompt": ""},
                         headers=admin_headers)
        print("PATCH status:", r.status_code)
        print("served repr:", repr(get_system_prompt()))
        print("GET repr:", repr(client.get("/api/admin/config", headers=admin_headers).json()["system_prompt"]))
        assert r.status_code == 200
        assert get_system_prompt() == ""
    finally:
        client.patch("/api/admin/config", json={"system_prompt": original},
                     headers=admin_headers)
