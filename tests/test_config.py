from __future__ import annotations

import pytest
from pathlib import Path

from niuma.config import load_config, NiumaConfig, ConfigError


def test_load_config_from_file(config_file: Path) -> None:
    config = load_config(config_file)
    assert config.teams.trigger == "@niuma"
    assert config.teams.poll_interval == 60
    assert config.claude.max_concurrent == 5
    assert config.claude.session_timeout == 86400
    assert "testuser@nvidia.com" in config.security.allowed_users


def test_load_config_expands_home(tmp_dir: Path) -> None:
    import yaml

    raw = {
        "teams": {"chat_ids": ["x"], "trigger": "@niuma", "poll_interval": 60},
        "claude": {
            "dispatcher_model": "sonnet",
            "worker_model": "sonnet",
            "max_concurrent": 5,
            "session_timeout": 86400,
            "permission_mode": "auto",
            "default_cwd": "~",
        },
        "security": {"allowed_users": ["a@b.com"], "admin_users": ["a@b.com"]},
        "storage": {"db_path": "~/.niuma/test.db"},
        "logging": {"level": "INFO", "file": "~/.niuma/test.log"},
    }
    path = tmp_dir / "config.yaml"
    path.write_text(yaml.dump(raw))
    config = load_config(path)
    assert "~" not in config.storage.db_path
    assert "~" not in config.claude.default_cwd


def test_load_config_missing_file() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/config.yaml"))


def test_load_config_missing_required_field(tmp_dir: Path) -> None:
    import yaml

    path = tmp_dir / "bad.yaml"
    path.write_text(yaml.dump({"teams": {}}))
    with pytest.raises(ConfigError):
        load_config(path)
