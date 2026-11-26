import pytest
from fastapi.testclient import TestClient
from teamup_server.collaboration import Collaboration, CollaborationError
from teamup_server.main import create_app
from test_console_server import console_token
from test_contract import auth, credential
from test_server_accounts import NEW_PASSWORD, PASSWORD, login, login_token


def test_admin_capabilities_follow_current_grants_and_session_purpose(tmp_path):
    db = tmp_path / "teamup.db"
    owner = Collaboration(db, {})
    owner.create_account("alice", PASSWORD)
    with TestClient(create_app(db)) as client:
        enrollment = auth(login_token(client, "alice", PASSWORD))
        console = auth(console_token(client, "alice", PASSWORD))
        path = "/v1/auth/capabilities"
        assert client.get(path, headers=enrollment).json() == {"admin": False}
        assert client.get(path, headers=console).status_code == 401
        assert client.get(path, headers=auth(credential("agent"))).status_code == 401
        owner.grant_console_read("alice")
        assert client.get(path, headers=enrollment).json() == {"admin": True}
        assert client.get("/v1/console/capabilities", headers=console).json() == {
            "maintenance": False,
            "accounts": False,
        }
        owner.set_maintenance_grant("alice", "logs_read", True)
        assert client.get("/v1/console/capabilities", headers=console).json() == {
            "maintenance": True,
            "accounts": False,
        }
        owner.revoke_console_read("alice")
        assert client.get(path, headers=enrollment).json() == {"admin": False}
        assert (
            client.get("/v1/console/capabilities", headers=console).status_code == 403
        )


def test_account_admin_manages_standard_credentials_and_keeps_own_grant(tmp_path):
    db = tmp_path / "teamup.db"
    owner = Collaboration(db, {})
    for name in ("alice", "operator"):
        owner.create_account(name, PASSWORD)
        owner.grant_console_read(name)
    with TestClient(create_app(db)) as client:
        alice = auth(console_token(client, "alice", PASSWORD))
        enrollment = auth(login_token(client, "alice", PASSWORD))
        path = "/v1/console/accounts"
        credentials = {"username": "new-user", "password": PASSWORD}
        assert client.post(path, headers=alice, json=credentials).status_code == 403
        owner.set_account_admin("alice", granted=True)
        assert (
            client.post(path, headers=enrollment, json=credentials).status_code == 401
        )
        assert client.post(path, headers=alice, json=credentials).status_code == 201
        assert client.post(path, headers=alice, json=credentials).status_code == 409
        old = auth(login_token(client, "new-user", PASSWORD))
        updated = {"password": NEW_PASSWORD}
        assert (
            client.post(
                f"{path}/operator/password", headers=alice, json=updated
            ).status_code
            == 403
        )
        assert client.post(f"{path}/alice/disable", headers=alice).status_code == 403
        assert (
            client.post(
                f"{path}/new-user/password", headers=alice, json=updated
            ).status_code
            == 200
        )
        assert client.get("/v1/auth/capabilities", headers=old).status_code == 401
        assert login(client, "new-user", NEW_PASSWORD).status_code == 200
        assert client.post(f"{path}/new-user/disable", headers=alice).status_code == 200
        assert client.post(f"{path}/new-user/disable", headers=alice).status_code == 200
        assert login(client, "new-user", NEW_PASSWORD).status_code == 401
        response = client.get(path, headers=alice)
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "accounts": [
                {
                    "username": "alice",
                    "disabled": False,
                    "can_reset_password": False,
                    "can_disable": False,
                },
                {
                    "username": "new-user",
                    "disabled": True,
                    "can_reset_password": False,
                    "can_disable": False,
                },
                {
                    "username": "operator",
                    "disabled": False,
                    "can_reset_password": False,
                    "can_disable": False,
                },
            ]
        }
        assert (
            client.post(
                f"{path}/alice/password", headers=alice, json=updated
            ).status_code
            == 403
        )
        Collaboration(db, {}).reset_account_password("alice", NEW_PASSWORD)
        assert client.get(path, headers=alice).status_code == 401
        refreshed = auth(console_token(client, "alice", NEW_PASSWORD))
        assert client.get(path, headers=refreshed).status_code == 200
        Collaboration(db, {}).set_account_admin("alice", granted=False)
        assert client.get(path, headers=refreshed).status_code == 403


def test_password_reset_rechecks_target_grants_after_hashing(tmp_path, monkeypatch):
    db = tmp_path / "teamup.db"
    owner, local_cli = Collaboration(db, {}), Collaboration(db, {})
    owner.create_account("alice", PASSWORD)
    owner.create_account("bob", PASSWORD)
    owner.grant_console_read("alice")
    owner.set_account_admin("alice", granted=True)
    token = owner.login("alice", PASSWORD, "console")["access_token"]
    hash_password = owner._new_password_digest

    def promote_after_hashing(password, operator_token):
        digest = hash_password(password, operator_token)
        local_cli.grant_console_read("bob")
        return digest

    monkeypatch.setattr(owner, "_new_password_digest", promote_after_hashing)
    with pytest.raises(CollaborationError) as result:
        owner.reset_account_password("bob", NEW_PASSWORD, operator_token=token)
    assert result.value.code == "forbidden"
    assert owner.login("bob", PASSWORD, "console")["access_token"]
