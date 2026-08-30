import pytest
import yaml

from hermes_mcp_gateway.config import ConfigError, load_config

SECRET_HASH = "sha256:" + "a" * 64


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def client_over(**overrides):
    base = {
        "client_id": "test-client",
        "secret_hash": SECRET_HASH,
        "scopes": ["task:run", "toolset:file"],
        "max_duration_s": 300,
    }
    base.update(overrides)
    return base


def test_defaults(tmp_path):
    path = write_config(tmp_path, {"clients": [client_over()]})
    cfg = load_config(path)

    assert cfg.server.bind == "127.0.0.1"
    assert cfg.server.port == 8778

    assert cfg.hermes.bin == "hermes"
    assert cfg.hermes.profile == "mcp-worker"
    assert cfg.hermes.timeout_s == 1800

    assert cfg.auth.issuer == "hermes-mcp-gateway"
    assert cfg.auth.signing_secret_env == "HERMES_MCP_GATEWAY_SIGNING_KEY"
    assert cfg.auth.token_ttl_s == 600

    client = cfg.clients[0]
    assert client.requires_approval is False
    assert client.max_concurrency == 1
    assert client.max_turns == 60
    assert client.workdirs == ["/home/rmax-10/src"]
    assert client.models == ["deepseek-v4-flash"]


def test_full_config(tmp_path):
    path = write_config(
        tmp_path,
        {
            "server": {"bind": "0.0.0.0", "port": 9000},
            "hermes": {"bin": "/usr/bin/hermes", "profile": "research", "timeout_s": 60},
            "auth": {"issuer": "gateway", "token_ttl_s": 30},
            "clients": [
                client_over(
                    requires_approval=True,
                    max_concurrency=3,
                    max_turns=10,
                    workdirs=["/tmp"],
                    models=["model-a", "model-b"],
                )
            ],
        },
    )
    cfg = load_config(path)

    assert cfg.server.bind == "0.0.0.0"
    assert cfg.server.port == 9000
    assert cfg.hermes.bin == "/usr/bin/hermes"
    assert cfg.hermes.profile == "research"
    assert cfg.auth.issuer == "gateway"
    assert cfg.clients[0].requires_approval is True
    assert cfg.clients[0].max_concurrency == 3
    assert cfg.clients[0].workdirs == ["/tmp"]
    assert cfg.clients[0].models == ["model-a", "model-b"]


@pytest.mark.parametrize("bad_id", ["", "has space", "a" * 65, "bad$id"])
def test_invalid_client_id(tmp_path, bad_id):
    path = write_config(tmp_path, {"clients": [client_over(client_id=bad_id)]})
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "bad_hash",
    ["not-a-hash", "sha256:short", "SHA256:" + "a" * 64, "sha256:" + "g" * 64],
)
def test_invalid_secret_hash(tmp_path, bad_hash):
    path = write_config(tmp_path, {"clients": [client_over(secret_hash=bad_hash)]})
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("bad_scopes", [[], None, "task:run", [""]])
def test_invalid_scopes(tmp_path, bad_scopes):
    path = write_config(tmp_path, {"clients": [client_over(scopes=bad_scopes)]})
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_required_max_duration(tmp_path):
    data = client_over()
    del data["max_duration_s"]
    path = write_config(tmp_path, {"clients": [data]})
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_top_level_key(tmp_path):
    path = write_config(tmp_path, {"clients": [client_over()], "nope": 1})
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_server_key(tmp_path):
    path = write_config(tmp_path, {"server": {"bogus": 1}, "clients": [client_over()]})
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_client_key(tmp_path):
    path = write_config(tmp_path, {"clients": [client_over(bogus=1)]})
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_clients(tmp_path):
    path = write_config(tmp_path, {"server": {}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_clients_must_be_list(tmp_path):
    path = write_config(tmp_path, {"clients": {}})
    with pytest.raises(ConfigError):
        load_config(path)
