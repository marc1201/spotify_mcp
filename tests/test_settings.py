"""spotify_mcp.settings — the credential store ($CREDENTIALS_DIRECTORY/<NAME>) wins over the
environment, the environment keeps today's names as a fallback, and no value ever leaks into an
error message."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_mcp.settings import (
    CREDENTIALS,
    SettingsError,
    check_name,
    credentials_dir,
    load_settings,
    secret,
)

ENV_OK = {
    "SPOTIFY_CLIENT_ID": "client-id-from-env",
    "SPOTIFY_CLIENT_SECRET": "env-not-a-real-secret",
    "FASTMCP_JWT_KEY": "env-legacy-jwt-key",
    "BASE_URL": "https://mcp.test/",
    "SPOTIFY_ALLOWED_USER_IDS": "alice bob",
}


def _store(tmp_path: Path, **files: str) -> dict[str, str]:
    for name, value in files.items():
        (tmp_path / name).write_text(value + "\n")
    return {"CREDENTIALS_DIRECTORY": str(tmp_path)}


def test_credential_ids_are_the_prefixed_names():
    assert set(CREDENTIALS) == {
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_FASTMCP_JWT_KEY",
    }
    assert CREDENTIALS["SPOTIFY_FASTMCP_JWT_KEY"] == ("SPOTIFY_FASTMCP_JWT_KEY", "FASTMCP_JWT_KEY")


@pytest.mark.parametrize("bad", ["", "lower", "../X", "A/B", "1ABC", "A" * 65, None, 3])
def test_check_name_rejects(bad):
    with pytest.raises(SettingsError):
        check_name(bad)


def test_credentials_dir_requires_an_absolute_path(tmp_path: Path):
    assert credentials_dir({}) is None
    assert credentials_dir({"CREDENTIALS_DIRECTORY": "relative/dir"}) is None
    assert credentials_dir({"CREDENTIALS_DIRECTORY": f" {tmp_path} "}) == tmp_path


def test_credential_file_wins_over_environment(tmp_path: Path):
    env = {**ENV_OK, **_store(tmp_path, SPOTIFY_CLIENT_SECRET="file-not-a-real-secret")}
    assert secret("SPOTIFY_CLIENT_SECRET", env) == "file-not-a-real-secret"
    # not loaded for this unit → the environment still serves it
    assert secret("SPOTIFY_CLIENT_ID", env) == "client-id-from-env"
    # an empty credential file is not a value
    (tmp_path / "SPOTIFY_CLIENT_ID").write_text("\n")
    assert secret("SPOTIFY_CLIENT_ID", env) == "client-id-from-env"


def test_jwt_key_prefixed_name_first_then_legacy_env(tmp_path: Path):
    assert secret("SPOTIFY_FASTMCP_JWT_KEY", ENV_OK) == "env-legacy-jwt-key"
    both = {**ENV_OK, "SPOTIFY_FASTMCP_JWT_KEY": "env-prefixed-jwt-key"}
    assert secret("SPOTIFY_FASTMCP_JWT_KEY", both) == "env-prefixed-jwt-key"
    env = {**both, **_store(tmp_path, SPOTIFY_FASTMCP_JWT_KEY="file-jwt-key")}
    assert secret("SPOTIFY_FASTMCP_JWT_KEY", env) == "file-jwt-key"
    # the bare legacy name is an environment fallback only, never a credential file
    other = tmp_path / "other"
    other.mkdir()
    env = {**_store(other, FASTMCP_JWT_KEY="file-legacy-jwt-key")}
    assert secret("SPOTIFY_FASTMCP_JWT_KEY", env) == ""


def test_load_settings_from_environment_only():
    s = load_settings(ENV_OK)
    assert s.spotify_client_id == "client-id-from-env"
    assert s.spotify_client_secret == "env-not-a-real-secret"
    assert s.jwt_signing_key == "env-legacy-jwt-key"
    assert s.base_url == "https://mcp.test"
    assert s.port == 8080
    assert s.allowed_user_ids == frozenset({"alice", "bob"})


def test_load_settings_from_credential_store(tmp_path: Path):
    env = {
        "BASE_URL": "https://mcp.test",
        "PORT": "9090",
        **_store(
            tmp_path,
            SPOTIFY_CLIENT_ID="file-client-id",
            SPOTIFY_CLIENT_SECRET="file-not-a-real-secret",
            SPOTIFY_FASTMCP_JWT_KEY="file-jwt-key",
        ),
    }
    s = load_settings(env)
    assert (s.spotify_client_id, s.spotify_client_secret, s.jwt_signing_key) == (
        "file-client-id",
        "file-not-a-real-secret",
        "file-jwt-key",
    )
    assert s.port == 9090


def test_missing_credential_names_it_without_a_value():
    env = {**ENV_OK, "SPOTIFY_CLIENT_SECRET": "  "}
    with pytest.raises(SettingsError, match="SPOTIFY_CLIENT_SECRET") as ei:
        load_settings(env)
    assert "env-not-a-real-secret" not in str(ei.value)
    assert "env-legacy-jwt-key" not in str(ei.value)


def test_load_settings_validates_base_url_and_port():
    with pytest.raises(SettingsError, match="https://"):
        load_settings({**ENV_OK, "BASE_URL": "http://mcp.test"})
    with pytest.raises(SettingsError, match="PORT"):
        load_settings({**ENV_OK, "PORT": "x"})
