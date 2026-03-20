from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_config(tmp_dir: Path) -> dict[str, Any]:
    return {
        "teams": {
            "chat_ids": ["19:test-chat@thread.v2"],
            "trigger": "@niuma",
            "poll_interval": 60,
        },
        "claude": {
            "dispatcher_model": "sonnet",
            "worker_model": "sonnet",
            "max_concurrent": 5,
            "session_timeout": 86400,
            "permission_mode": "auto",
            "default_cwd": str(tmp_dir),
        },
        "security": {
            "allowed_users": ["testuser@nvidia.com"],
            "admin_users": ["admin@nvidia.com"],
        },
        "storage": {
            "db_path": str(tmp_dir / "test.db"),
        },
        "logging": {
            "level": "DEBUG",
            "file": str(tmp_dir / "test.log"),
        },
    }


@pytest.fixture
def config_file(tmp_dir: Path, sample_config: dict[str, Any]) -> Path:
    path = tmp_dir / "config.yaml"
    path.write_text(yaml.dump(sample_config))
    return path
