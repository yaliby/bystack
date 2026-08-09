"""Health and introspection."""

from __future__ import annotations

from fastapi import APIRouter

from bystack import __version__
from bystack.api.deps import Context
from bystack.api.schemas import HealthOut, ProviderHealthOut
from bystack.core.ports.provider import ProviderState

router = APIRouter(tags=["system"])


@router.get("/healthz", response_model=HealthOut, summary="Service and provider health")
async def healthz(context: Context) -> HealthOut:
    """Report per-provider state alongside graph size.

    ``degraded`` rather than a failure status when a provider is unreachable:
    the service is working, part of the infrastructure is not, and conflating
    the two would have an orchestrator restart a healthy control plane
    because one managed host is powered off.
    """
    snapshot = context.store.snapshot()
    providers = context.collector.providers
    healths = context.collector.health()

    degraded = any(
        health.state in (ProviderState.DEGRADED, ProviderState.FAILED)
        for health in healths.values()
    )

    return HealthOut(
        status="degraded" if degraded else "ok",
        version=__version__,
        seq=snapshot.seq,
        node_count=len(snapshot.nodes),
        edge_count=len(snapshot.edges),
        read_only=context.settings.read_only,
        providers=[
            ProviderHealthOut.of(pid, providers[pid].kind, health)
            for pid, health in healths.items()
        ],
    )
