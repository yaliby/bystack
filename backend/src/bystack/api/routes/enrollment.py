"""The operator's side of agent trust.

Enrollment is a real workflow now, not a copied SSH key: issue a token,
install the agent, approve the host. ADR-0011 accepts that ceremony
deliberately, on the grounds that every step of it is short-lived and
revocable, which the key was not. This module is the three buttons.

It lives on the **browser-facing** port, which defaults to loopback -- these
are the operations that decide who joins the fleet, and they belong on the
side of the split an operator reaches, not the side agents dial. The agent
listener serves exactly two paths and neither of them is this one.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bystack import __version__
from bystack.api.deps import Context
from bystack.core.identity import engine_scope
from bystack.core.ports.provider import ProviderState
from bystack.infra.agentca import EnrolledAgent
from bystack.providers.agent.provider import AgentProvider

router = APIRouter(prefix="/agents", tags=["agents"])

#: What ADR-0011 sketches (`--ttl 15m`), as the default rather than as an
#: example. Long enough to walk to another machine and paste a command, short
#: enough that a token left in a scrollback is worth nothing by the time
#: anyone reads it.
DEFAULT_TTL_MINUTES = 15

#: A ceiling, because "single-use and short-lived" is the entire argument for
#: this credential existing. A token good for a week is a shared secret with
#: extra steps.
MAX_TTL_MINUTES = 24 * 60

#: Where the pasted command fetches `scripts/install-agent.sh` from.
#:
#: Pinned to this Controller's version rather than to a branch, so a host added
#: today gets the installer that shipped with the Controller it is joining. A
#: `main` URL would mean the command an operator copies changes underneath them
#: between one host and the next.
#:
#: **The Controller cannot serve this itself**, which is worth stating because
#: it looks like an obvious improvement. The port a new host can reach is the
#: fleet listener, and that one requires a client certificate the host does not
#: have yet -- that is the whole shape of ADR-0011. The browser-facing port is
#: on loopback by default. So the script comes from where releases come from,
#: and `--binary` is the escape hatch for a network with no egress.
INSTALLER_URL = (
    f"https://raw.githubusercontent.com/yaliby/bystack/v{__version__}/scripts/install-agent.sh"
)


class TokenIn(BaseModel):
    ttl_minutes: int = Field(default=DEFAULT_TTL_MINUTES, ge=1, le=MAX_TTL_MINUTES)


class TokenOut(BaseModel):
    token: str
    expires_at: int
    ca_fingerprint: str
    install: str
    """A command line to paste, with the token in it.

    Shown once and never retrievable, because the Controller keeps only a
    digest -- and because a token you can go back and read again is a token
    that is worth stealing for longer than it is alive.
    """

    manual: str
    """The same thing for a machine that already has the binary.

    Both are offered because they answer different questions. The first is for
    a host that has nothing on it, which is the case the dashboard's button
    exists for. This one is for a host where the agent is already installed --
    a re-enrolment after a rebuild, or a fleet that distributes binaries by
    configuration management and would not thank us for a curl to GitHub.
    """


class LocalAgentOut(BaseModel):
    """Whether this machine is managing itself, and why not when it is not.

    Reported here rather than on `/healthz` for the same reason the listener's
    posture is: a local agent an operator turned off is not a fault, and a
    machine with no Docker socket is not one either. What both are is the
    explanation for an empty canvas, and an empty canvas with no explanation is
    the first-run experience `docs/MIGRATION.md` §4 exists to fix.
    """

    state: str
    """`running`, `starting`, `disabled` or `unavailable`."""

    detail: str
    """What it is doing, or what is missing. Written to be shown verbatim."""


class EnrollmentOut(BaseModel):
    """Whether a host can join right now, and on what terms.

    Everything here is configuration rather than state, so it is fetched once
    rather than polled. It exists because the three routes below describe
    *agents* and none of them can answer the question an operator asks first:
    minting a token succeeds whether or not the listener is bound, so a UI
    without this would hand out a command that cannot connect and report no
    reason why.
    """

    enabled: bool
    """``agents.enabled``. False means nothing is listening and the pasted
    command will fail at the dial, not at the token."""

    auto_approve: bool
    """``agents.auto_approve``. Changes what an operator should expect to see
    after pasting: a pending row to approve, or a host already in the graph.
    Naming it is not endorsing it -- the UI still never approves anything."""

    listen: str
    """Where agents dial. Already inside every ``install`` string; repeated
    here so the disabled case can name the port that is not open."""

    upgrade: str
    """The command that replaces the agent on a host that already has one.

    Composed here rather than written down anywhere, for the same reason
    ``install`` is: it carries *this* Controller's version, so an operator who
    upgrades the Controller and then reads this line is told to install the
    matching agent instead of whichever number a document was last edited with.

    **It has no token, and that is the whole content of this field.** The
    obvious guess -- that upgrading is the install command again -- is wrong in
    a way that costs an afternoon: a token sitting beside a stored certificate
    makes the agent enrol a second time, so the host comes back as a stranger
    awaiting approval while the one you actually have goes quiet. Handing out
    the correct line is cheaper than documenting the trap.

    Constant across hosts, so it is here rather than on each row of
    ``GET /agents``. Nothing about it is a secret: it names a public installer
    and the address agents already dial, which is why it is served on a route
    that mints nothing and can be read before anything is behind.
    """

    local_agent: LocalAgentOut


#: The status of a host managed by the Controller's own agent.
#:
#: Not `approved`, which would be a lie about a record that does not exist:
#: nobody approved this host, and there is nothing to revoke. It is its own
#: word so a UI cannot accidentally offer the fleet's buttons for it.
LOCAL_STATUS = "local"


class AgentOut(BaseModel):
    engine_id: str
    status: str
    certificate_expires_at: int
    enrolled_at: int
    last_seen: int
    agent_version: str
    connected: bool
    """Whether an agent is on the stream *right now*.

    Deliberately separate from `status`: approved-but-offline and
    pending-but-connected are both ordinary, and collapsing them into one
    field is how a UI ends up telling an operator to approve a host that is
    already approved and simply asleep.
    """

    local: bool = False
    """Whether the session on that stream is the Controller's own child.

    A third fact, separate from the other two for the same reason they are
    separate from each other: a machine can be enrolled *and* currently managed
    locally. That is not a contrived case — it is what an operator gets by
    enrolling a host and later running the Controller on it — and the two
    records describe different things. `status` is what somebody decided about
    this host's certificate; this is which agent is actually attached.

    Folding it into `status` produced the bug that made this field exist: the
    host appeared twice, once as `local` and once as `approved`, and both rows
    claimed to be connected because `connected` is computed from the one
    provider they share.
    """

    @classmethod
    def of(cls, agent: EnrolledAgent, *, connected: bool, local: bool = False) -> AgentOut:
        return cls(
            engine_id=agent.engine_id,
            status=agent.status.value,
            certificate_expires_at=agent.not_after,
            enrolled_at=agent.enrolled_at,
            last_seen=agent.last_seen,
            agent_version=agent.agent_version,
            connected=connected,
            local=local,
        )

    @classmethod
    def unenrolled(cls, provider: AgentProvider) -> AgentOut:
        """The Controller's own host, in the ordinary case where it never enrolled.

        The certificate expiry is zero because there is no certificate, and
        that is honest where a fabricated date would not be: a panel counting
        down to a renewal that will never happen is worse than one with a
        field to hide.
        """
        return cls(
            engine_id=provider.id,
            status=LOCAL_STATUS,
            certificate_expires_at=0,
            enrolled_at=int(provider.connected_at),
            last_seen=int(provider.connected_at),
            agent_version=provider.agent_version,
            connected=True,
            local=True,
        )


def _dial_url(context: Context) -> str:
    """The address a host on the network types to reach this listener.

    On `AgentsConfig` rather than here since ADR-0019, because the deployment
    service composes the same address for the installer it runs itself -- and
    two spellings of it is how a host added one way reaches this listener and a
    host added the other way does not.
    """
    return context.settings.agents.dial_url


@router.post("/tokens", response_model=TokenOut, summary="Mint a join token")
async def mint_token(body: TokenIn, context: Context) -> TokenOut:
    """One token, one certificate, a few minutes.

    The token embeds the CA's fingerprint so the agent can authenticate the
    Controller *before* sending the secret. That is what makes enrollment an
    authenticated exchange rather than trust-on-first-use, and it is why the
    token is long: it is two credentials, not one.
    """
    minted = context.trust.mint_token(dt.timedelta(minutes=body.ttl_minutes))
    url = _dial_url(context)
    return TokenOut(
        token=minted.token,
        expires_at=minted.expires_at_unix,
        ca_fingerprint=context.trust.ca.fingerprint,
        install=(
            f"curl -fsSL {INSTALLER_URL} | sudo sh -s --"
            f" --controller {url} --token {minted.token}"
        ),
        manual=f"bystack-agent --controller {url} --token {minted.token}",
    )


@router.get("/enrollment", response_model=EnrollmentOut, summary="Whether hosts can join")
async def enrollment_terms(context: Context) -> EnrollmentOut:
    """The listener's posture, for a UI that would otherwise have to guess.

    Deliberately not on ``/healthz``: that route reports what is *wrong* with
    a running system, and a listener an operator chose to leave off is not a
    fault. It is also not on the agent list, which is a list -- and the answer
    is the same when the fleet is empty, which is exactly when it matters.
    """
    status = context.local_agent.status
    return EnrollmentOut(
        enabled=context.settings.agents.enabled,
        auto_approve=context.settings.agents.auto_approve,
        listen=context.settings.agents.listen,
        upgrade=f"curl -fsSL {INSTALLER_URL} | sudo sh -s -- --controller {_dial_url(context)}",
        local_agent=LocalAgentOut(state=status.state.value, detail=status.detail),
    )


@router.get("", response_model=list[AgentOut], summary="List enrolled agents")
async def list_agents(context: Context) -> list[AgentOut]:
    """Every host this Controller manages, however it came to manage it.

    **One row per host**, and that is the whole difficulty. The registry is the
    fleet, and it is not the whole answer: the agent the Controller spawned for
    its own machine is deliberately not enrolled (`runtime/trust.py`,
    `admit_local`), so a list read from the registry alone would show an
    operator an empty fleet next to a canvas full of their own containers.

    But the two sets *overlap*. A host enrolled as part of the fleet, on a
    machine the Controller is later run on, is in both -- and concatenating
    them listed it twice, with the second row claiming a connection belonging
    to the first, because `connected` is computed from the one provider they
    share. Merging on the engine id is what fixes that, and it is the right
    key for the same reason it is the partition key: under ADR-0011 "which
    host is this" and "which agent is this" are one question.

    The enrollment record wins the row where there is one, so a certificate
    that exists stays visible and revocable; `local` says which agent is
    actually attached. Only a host with no record at all is synthesized, which
    is why it disappears the moment that agent stops -- there is nothing
    durable behind it to go stale.
    """
    # A session exists, at whatever stage. `DEGRADED` is specifically the
    # state that means "we still show this host's topology and no agent is
    # attached", so it is the one that must not count as connected.
    live = {ProviderState.STARTING, ProviderState.SYNCING, ProviderState.READY}
    connected = {
        provider.id
        for provider in context.collector.providers.values()
        if provider.health().state in live
    }
    locals_ = {
        provider.id: provider
        for provider in context.collector.providers.values()
        if isinstance(provider, AgentProvider) and provider.local
    }

    rows = [
        AgentOut.of(
            agent,
            connected=agent.engine_id in connected,
            local=agent.engine_id in locals_,
        )
        for agent in context.trust.registry.all()
    ]
    enrolled_ids = {agent.engine_id for agent in context.trust.registry.all()}
    return [
        AgentOut.unenrolled(provider)
        for engine_id, provider in locals_.items()
        if engine_id not in enrolled_ids
    ] + rows


@router.post("/{engine_id}/approve", response_model=AgentOut, summary="Approve an agent")
async def approve(engine_id: str, context: Context) -> AgentOut:
    """Let an enrolled agent start contributing to the graph.

    Takes effect on the agent's next connection attempt, which is seconds
    away: a refused agent is reconnecting on a backoff, and "awaiting
    approval" is the one refusal it is told to keep retrying.
    """
    agent = context.trust.registry.approve(engine_scope(engine_id))
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no agent enrolled as {engine_id}")
    return AgentOut.of(agent, connected=False)


@router.post("/{engine_id}/revoke", response_model=AgentOut, summary="Revoke an agent")
async def revoke(engine_id: str, context: Context) -> AgentOut:
    """Stop trusting this host's certificate.

    Checked at connection time against the enrolled-agent list, which is why
    there is no CRL to publish and nothing to wait for. A connected agent
    keeps its current stream until it drops -- revocation is not a kill switch
    for a session, it is a refusal to open the next one.
    """
    scoped = engine_scope(engine_id)
    agent = context.trust.registry.revoke(scoped)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no agent enrolled as {engine_id}")
    return AgentOut.of(agent, connected=False)
