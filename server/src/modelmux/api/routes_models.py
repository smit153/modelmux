"""GET /v1/models: the active driver's allowlist."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from modelmux.api.auth import require_api_key
from modelmux.api.schemas import ModelCard, ModelList
from modelmux.core.pipeline import Pipeline

router = APIRouter()

_STARTED = int(time.time())


@router.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models(request: Request) -> JSONResponse:
    pipeline: Pipeline = request.app.state.pipeline
    owner = f"modelmux-{pipeline.driver.name}"
    cards = [
        ModelCard(id=model_id, created=_STARTED, owned_by=owner) for model_id in pipeline.models
    ]
    return JSONResponse(ModelList(data=cards).model_dump())
