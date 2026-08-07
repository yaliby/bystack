"""Configuration.

Manual configuration exists only to tell the platform *where to look*. It
never describes topology -- that is discovered. The golden rule holds: a user
who has to hand-draw their infrastructure is using a diagramming tool, not a
control plane.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

#: What a `hosts:` block used to mean, and why it is now refused.
#:
#: `docs/MIGRATION.md` §6 asked for a hard error rather than a silent
#: migration, and the reason is the whole point: a config that quietly
#: degraded to "manage nothing" looks exactly like an empty cluster. An
#: operator upgrading with a `hosts:` block would see a Controller that
#: started cleanly, reported healthy, and discovered nothing at all.
_HOSTS_REMOVED = """\
`hosts:` is no longer supported. The Controller does not reach out to Docker
sockets any more -- agents dial in (ARCHITECTURE.md section 1).

Install an agent on each host listed there and enable the endpoint:

    agents:
      enabled: true

See docs/MIGRATION.md for what moved and why."""


class AgentsConfig(BaseModel):
    """Where agents dial in, and on what terms.

    Says far less than the ``hosts:`` block it will replace, because agents
    *arrive* rather than being reached. There is nothing here about addresses,
    credentials or transports -- that whole category of configuration is what
    ADR-0008 deleted.
    """

    enabled: bool = Field(
        default=False,
        description="Accept agent connections. Off until an operator opts in.",
    )

    auto_approve: bool = Field(
        default=False,
        description="Adopt an unknown agent on first connection instead of refusing it.",
    )
    """Off by default, and the default is the security control.

    Until mTLS enrollment lands (ADR-0011) the agent endpoint has no way to
    tell a real agent from anything else that can reach the port, so an
    unknown engine id is refused unless an operator has said otherwise. With
    ``auto_approve`` on, whatever connects first *becomes* a managed host --
    acceptable on a trusted network, never as a shipped default.
    """

    resync_interval: int = Field(
        default=900,
        ge=60,
        description="Seconds between agent-side resyncs; sent in HelloAck.",
    )
    """15 minutes, three times the agentless interval.

    Resync ran every 5 minutes because the event stream crossed an SSH tunnel
    that could drop silently. An agent reads a unix socket on the same kernel;
    there is no network to lose. It now defends only against agent-side hash
    map bugs, and at steady state it produces zero bytes on the wire because
    nothing changed and therefore nothing is sent.
    """


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    """Loopback by default. This service holds root-equivalent access to every
    managed engine; binding it to the world on first run is not a default any
    platform should ship."""

    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


class Settings(BaseModel):
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    read_only: bool = Field(
        default=True,
        description="Refuse all mutating operations. Secure default; opt out deliberately.",
    )
    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _refuse_removed_keys(cls, data: Any) -> Any:
        # Before-mode, so this fires on the raw mapping. Pydantic ignores
        # unknown keys by default, which is exactly the silent degradation
        # MIGRATION section 6 asked us not to ship.
        if isinstance(data, dict) and "hosts" in data:
            raise ValueError(_HOSTS_REMOVED)
        return data

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        """Load from YAML, failing loudly on a malformed file.

        No silent fallback to defaults: a typo in a config block that quietly
        degraded to "manage nothing" would look identical to an empty cluster,
        and the user would have no way to tell.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")

        raw: Any = yaml.safe_load(path.read_text()) or {}
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"invalid configuration in {path}:\n{exc}") from exc

    @classmethod
    def default(cls) -> Settings:
        """Zero-config startup: a Controller with nothing to manage yet.

        It used to mean "discover the local engine", which the Controller can
        no longer do -- it reaches no socket, local or remote. Hosts arrive by
        installing an agent, and the endpoint they dial stays off until an
        operator turns it on, because until mTLS lands (ADR-0011) it trusts
        whoever reaches it. Defaulting it to on would be a Controller that
        adopts the first thing to find the port.
        """
        return cls()
