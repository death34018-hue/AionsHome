"""Isolated HTTP controls for browser security alerts."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from security_access import SecurityAccessService


class TrustCurrentDeviceRequest(BaseModel):
    label: str = ""


def create_security_access_router(service: SecurityAccessService) -> APIRouter:
    router = APIRouter(prefix="/api/security-access", tags=["security-access"])

    @router.get("/alerts/pending")
    async def pending_alerts(request: Request):
        observation = getattr(request.state, "security_access", None)
        if observation is None:
            raise HTTPException(status_code=503, detail="security audit unavailable")
        return {"alerts": service.pending_alerts_for(observation)}

    @router.post("/devices/trust")
    async def trust_current_device(
        request: Request,
        payload: TrustCurrentDeviceRequest,
    ):
        observation = getattr(request.state, "security_access", None)
        if observation is None:
            raise HTTPException(status_code=503, detail="security audit unavailable")
        label = str(payload.label or "").strip()[:40]
        service.trust_device(
            observation.device_id,
            observation.effective_ip,
            label,
        )
        service.acknowledge_alerts_for(observation)
        return {
            "trusted": True,
            "device": observation.device_fingerprint,
            "label": label,
        }

    @router.post("/alerts/{alert_id}/ack")
    async def acknowledge_alert(alert_id: str):
        return {"acknowledged": service.acknowledge_alert(alert_id)}

    @router.post("/alerts/{alert_id}/trust-source")
    async def trust_alert_source(
        alert_id: str,
        payload: TrustCurrentDeviceRequest,
    ):
        label = str(payload.label or "").strip()[:40]
        event = service.trust_alert_source(alert_id, label)
        if event is None:
            raise HTTPException(status_code=409, detail="alert source is no longer available")
        return {
            "trusted": True,
            "device": event["device"],
            "label": label,
        }

    @router.post("/alerts/{alert_id}/block-24h")
    async def block_alert_ip(alert_id: str):
        try:
            result = service.block_alert_ip(alert_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return result

    return router
