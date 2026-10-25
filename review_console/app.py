r"""
Review console API and page (FastAPI). Local use only: it binds to 127.0.0.1 and has no sign-in, so the
reviewer name typed in is recorded but not verified. A production deployment needs real staff authentication
in front of it (single sign-on) and should take the reviewer from that identity instead.

Uses the same stores and settings as the agent server: ACTION_STORE_DIR, CASE_STORE_PATH,
CUSTOMER_DATA_BACKEND (mock | databricks) and config/service_policy.hocon.

    .\.venv\Scripts\python.exe -m review_console            # http://127.0.0.1:8090
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from coded_tools.customer_service.action_store import ActionStore
from coded_tools.customer_service.case_store import CaseStore
from coded_tools.customer_service.policy import load_policy
from coded_tools.customer_service.review_service import ReviewError
from coded_tools.customer_service.review_service import ReviewService

PAGE = Path(__file__).with_name("page.html")

app = FastAPI(title="Support review console", docs_url=None, redoc_url=None)


class Approve(BaseModel):
    reviewer: str
    note: str = ""


class Reject(BaseModel):
    reviewer: str
    reason: str
    customer_message: str = ""


class Resolve(BaseModel):
    reviewer: str
    note: str
    customer_message: str = ""


def service() -> ReviewService:
    return ReviewService(ActionStore(), CaseStore(), None, load_policy())  # backend opened only on approve


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ReviewError as e:
        status = 404 if "not in the review queue" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@app.get("/api/queue")
def queue(all: bool = False) -> list[dict[str, Any]]:  # noqa: A002 - query parameter name
    return service().queue(include_decided=all)


@app.get("/api/items/{review_id}")
def item(review_id: str) -> dict[str, Any]:
    return _call(service().detail, review_id)


@app.post("/api/items/{review_id}/approve")
async def approve(review_id: str, body: Approve) -> dict[str, Any]:
    try:
        return await service().approve(review_id, reviewer=body.reviewer, note=body.note)
    except ReviewError as e:
        raise HTTPException(status_code=404 if "not in the review queue" in str(e) else 400, detail=str(e)) from e


@app.post("/api/items/{review_id}/reject")
def reject(review_id: str, body: Reject) -> dict[str, Any]:
    return _call(service().reject, review_id, reviewer=body.reviewer, reason=body.reason,
                 customer_message=body.customer_message)


@app.post("/api/items/{review_id}/resolve")
def resolve(review_id: str, body: Resolve) -> dict[str, Any]:
    return _call(service().resolve, review_id, reviewer=body.reviewer, note=body.note,
                 customer_message=body.customer_message)


@app.get("/api/outbox")
def outbox() -> list[dict[str, Any]]:
    return service().outbox()
