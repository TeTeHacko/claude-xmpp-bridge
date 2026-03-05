"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def credentials_file(tmp_path):
    """Create a temporary credentials file with correct permissions."""
    cred = tmp_path / "credentials"
    cred.write_text("test-password")
    cred.chmod(0o600)
    return cred


@pytest.fixture
def db_path(tmp_path):
    """Temporary database path."""
    return tmp_path / "test.db"


@pytest.fixture
def socket_path(tmp_path):
    """Temporary socket path."""
    return tmp_path / "test.sock"
