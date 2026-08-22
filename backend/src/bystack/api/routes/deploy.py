"""Installing agents on machines the operator named (ADR-0019).

Two routes: start a run, and read how far it has got. No state is held here —
everything an operator sees is the service's own record of a run that is
either in flight or finished, which is the same discipline the fleet rollout
and the self-update follow.

**This is the one route in the product that takes a secret in.** Three things
follow from that and are worth stating where they can be read beside the code:

* It is served on the **browser-facing** port, which defaults to loopback and
  has no login (ADR-0014). That is why INSTALL.md tells operators to reach the
  dashboard over `ssh -L` — over that, this request crosses the network inside
  their own SSH session.
* **Nothing echoes it.** `DeployIn` is the only model that carries a
  credential, no response model has a field for one, and `Credential.__repr__`
  prints the *kind* of authentication rather than the secret — so a traceback
  from anywhere below cannot contain it.
* **Nothing stores it.** The service takes it as an argument and holds it in a
  local variable for the length of the run (`runtime/deploy.py`). There is no
  code that writes it down, which is a stronger guarantee than a policy that
  says it must not be written down.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bystack.api.deps import Context
from bystack.infra.ssh import Credential
from bystack.runtime.deploy import DeployError, HostState, RunState, parse_hosts

router = APIRouter(prefix="/agents/deploy", tags=["agents"])


class HostOut(BaseModel):
    """One machine's row."""

    host: str
    port: int
    phase: str
    """`waiting`, `connecting`, `preparing`, `installing`, `enrolling`,
    `done`, `skipped` or `failed`.

    `skipped` is not a failure and the dashboard must not draw it as one: a
    machine that already runs an agent is a correct outcome for "make this
    machine managed".
    """

    detail: str
    engine_id: str = ""
    fingerprint: str = ""
    """The host key that address answered with, in the `SHA256:` spelling
    `ssh-keygen -l` prints. Shown so it can be compared against the machine
    itself, which is the only thing that makes trust-on-first-use mean
    anything."""

    @classmethod
    def of(cls, state: HostState) -> HostOut:
        return cls(
            host=state.host,
            port=state.port,
            phase=state.phase.value,
            detail=state.detail,
            engine_id=state.engine_id,
            fingerprint=state.fingerprint,
        )


class RunOut(BaseModel):
    """A deployment, in flight or finished."""

    running: bool
    started_at: int
    finished_at: int = 0
    error: str = ""
    """Why the run stopped, when it did. A run stops at the first host that
    fails, so every host after it is still `waiting` — which is the honest
    word for not attempted."""

    hosts: list[HostOut]

    @classmethod
    def of(cls, run: RunState) -> RunOut:
        return cls(
            running=run.running,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error=run.error,
            hosts=[HostOut.of(host) for host in run.hosts],
        )


class DeployIn(BaseModel):
    """Where to install, and how to get in.

    The credential fields are the only ones in the API that carry a secret,
    and they are write-only in the strict sense: there is no response model
    with a matching field, and no route that could return one.
    """

    hosts: str = Field(
        description=(
            "Addresses, separated by newlines, commas or spaces. `host:port` is "
            "honoured; an IPv6 address with a port is written `[::1]:22`."
        )
    )
    user: str = Field(default="root", description="The account to log in as.")
    password: str = Field(default="", description="Its password, if that is the way in.")
    private_key: str = Field(
        default="",
        description="A private key in PEM, whole, beginning with -----BEGIN.",
    )
    passphrase: str = Field(default="", description="For the key, if it has one.")


@router.post("", response_model=RunOut, summary="Install agents on these machines")
async def deploy(body: DeployIn, context: Context) -> RunOut:
    """Start a run and return its first state. Does not wait for it.

    409 for everything that makes a run impossible — read-only, one already
    going, an unparseable address, no credential. Each is a state rather than a
    malformed request, and each carries the sentence that says what to do about
    it.
    """
    try:
        targets = parse_hosts(body.hosts)
        run = context.deploy.start(
            targets,
            Credential(
                user=body.user.strip(),
                password=body.password,
                private_key=body.private_key,
                passphrase=body.passphrase,
            ),
        )
    except DeployError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunOut.of(run)


@router.get("", response_model=RunOut | None, summary="How far the deployment got")
async def status(context: Context) -> RunOut | None:
    """`null` until this Controller has ever run one. Polled while one is going."""
    run = context.deploy.status()
    return RunOut.of(run) if run is not None else None
