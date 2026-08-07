"""Configuration loading.

The transport half of this file went with the agentless path
(`docs/MIGRATION.md` §6). What it protected did not: a config that quietly
degrades to "manage nothing" is indistinguishable from an empty cluster, and
that is now more likely rather than less, because a Controller with no agents
connected is a *legitimate* state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bystack.config import Settings


def test_zero_config_manages_nothing_and_listens_for_nobody() -> None:
    settings = Settings.default()

    # It used to discover the local engine. The Controller reaches no socket
    # at all now -- hosts arrive by installing an agent.
    assert settings.agents.enabled is False


def test_secure_defaults() -> None:
    settings = Settings.default()

    # Mutation is opt-in.
    assert settings.read_only is True
    # This service holds root-equivalent access to every managed engine;
    # binding it to the world on first run is not a shippable default.
    assert settings.api.host == "127.0.0.1"
    # Until mTLS lands the agent endpoint trusts whoever reaches it, and an
    # unknown engine id is refused unless an operator says otherwise.
    assert settings.agents.auto_approve is False


def test_a_hosts_block_is_a_hard_error_naming_the_migration(tmp_path: Path) -> None:
    # The single most likely upgrade path: an operator who had `hosts:`
    # configured. Pydantic ignores unknown keys, so without this the
    # Controller would start cleanly, report healthy, and discover nothing --
    # which looks exactly like a cluster that is simply empty.
    config = tmp_path / "bystack.yaml"
    config.write_text(
        """
hosts:
  - id: lab-01
    transport:
      type: ssh_tunnel
      host: 10.0.0.5
"""
    )

    with pytest.raises(ValueError, match="no longer supported"):
        Settings.load(config)


def test_the_hosts_error_says_what_to_do_instead(tmp_path: Path) -> None:
    config = tmp_path / "bystack.yaml"
    config.write_text("hosts: []\n")

    with pytest.raises(ValueError) as excinfo:
        Settings.load(config)

    # An empty list is refused too: it is still a config written against the
    # old model, and silence would be the failure this error exists to avoid.
    message = str(excinfo.value)
    assert "agents:" in message
    assert "MIGRATION" in message


def test_agents_block_still_loads(tmp_path: Path) -> None:
    config = tmp_path / "bystack.yaml"
    config.write_text("agents:\n  enabled: true\n  resync_interval: 600\nread_only: false\n")

    settings = Settings.load(config)

    assert settings.agents.enabled is True
    assert settings.agents.resync_interval == 600
    assert settings.read_only is False


def test_malformed_config_fails_loudly(tmp_path: Path) -> None:
    config = tmp_path / "bystack.yaml"
    config.write_text("agents:\n  resync_interval: 5\n")  # below the ge=60 floor

    with pytest.raises(ValueError):
        Settings.load(config)


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Settings.load(tmp_path / "nope.yaml")
