"""Browser-capture endpoints (spec 010 port of the old job_server.py contract).

GET returns a small HTML result page: bookmarklets navigate a new tab here,
and a plain navigation from an HTTPS page to localhost is never blocked by
mixed-content policy, unlike fetch. POST serves scripts and curl with JSON.
Status contract: 200 added, 400 missing fields, 409 duplicate, 500 error.
"""

from __future__ import annotations

import html as html_lib

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from harrier.capture import CaptureResult, add_captured_job
from harrier_api.deps import Conn

capture_router = APIRouter()

_STATUS_CODES = {"added": 200, "invalid": 400, "duplicate": 409}


class CaptureIn(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    url: str = ""
    source: str = "manual"
    description: str = ""


class CaptureOut(BaseModel):
    ok: bool
    message: str


def _result_page(result: CaptureResult, job_url: str) -> str:
    ok = result.status == "added"
    icon = "&#9989;" if ok else "&#10060;"
    color = "#1a7f37" if ok else "#cf222e"
    safe_message = html_lib.escape(result.message)
    safe_url = html_lib.escape(job_url, quote=True)
    back_link = (
        f'<p><a href="{safe_url}" style="color:#0969da">&#8592; back to job posting</a></p>'
        if job_url
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Job tracker</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:60px auto;padding:0 20px;}}
h1{{font-size:1.4rem;color:{color};}} p{{color:#444;line-height:1.5;}}</style>
</head>
<body>
<h1>{icon} {safe_message}</h1>
{back_link}
<p style="font-size:.85rem;color:#888;margin-top:2rem">harrier capture</p>
</body></html>"""


@capture_router.get("/capture/add", operation_id="captureJobViaGet", response_class=HTMLResponse)
def capture_via_get(
    conn: Conn,
    company: str = "",
    title: str = "",
    location: str = "",
    url: str = "",
    source: str = "manual",
    description: str = "",
) -> HTMLResponse:
    result = add_captured_job(
        conn,
        company=company,
        title=title,
        location=location,
        url=url,
        source=source or "manual",
        description=description,
    )
    return HTMLResponse(
        content=_result_page(result, url.strip()), status_code=_STATUS_CODES[result.status]
    )


@capture_router.post(
    "/capture/add",
    operation_id="captureJob",
    responses={
        400: {"model": CaptureOut, "description": "company and title are required"},
        409: {"model": CaptureOut, "description": "already in tracker"},
    },
)
def capture_via_post(body: CaptureIn, conn: Conn) -> CaptureOut:
    result = add_captured_job(
        conn,
        company=body.company,
        title=body.title,
        location=body.location,
        url=body.url,
        source=body.source or "manual",
        description=body.description,
    )
    payload = CaptureOut(ok=result.status == "added", message=result.message)
    status_code = _STATUS_CODES[result.status]
    if status_code == 200:
        return payload
    return JSONResponse(  # pyright: ignore[reportReturnType]
        status_code=status_code, content=payload.model_dump()
    )
