"""Browser-capture endpoints (spec 010, tightened by spec 035).

A bookmarklet can only navigate a top-level GET: a plain navigation from an
HTTPS page to localhost is never blocked by mixed-content policy, unlike
fetch. That is why this route existed and why it answered GET, and it is also
why any page could fire it with an image tag and add a row to the tracker.

So the GET no longer changes anything. It renders a confirmation page holding
the captured fields and the local token, and a click posts them. The
bookmarklet still works and still takes one visible step; an image tag now
renders a page nobody asked for and changes nothing.

POST does the work, requires the token, and still serves scripts and curl
with JSON. Status contract unchanged: 200 added, 400 missing fields, 409
duplicate, 500 error.
"""

from __future__ import annotations

import html as html_lib
from typing import Annotated

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from harrier.capture import CaptureResult, add_captured_job
from harrier_api.deps import Conn
from harrier_api.localauth import load_or_create_token, require_token, token_matches

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


def _confirm_page(fields: dict[str, str], token: str) -> str:
    """The captured posting, and one button that actually adds it.

    Everything is escaped and nothing is executed: the values arrive in a
    query string from whatever page the operator was reading, so they are
    attacker-influenced text by definition.
    """
    rows = "".join(
        f"<tr><th>{html_lib.escape(name)}</th><td>{html_lib.escape(value)}</td></tr>"
        for name, value in fields.items()
        if value
    )
    hidden = "".join(
        f'<input type="hidden" name="{html_lib.escape(name, quote=True)}" '
        f'value="{html_lib.escape(value, quote=True)}">'
        for name, value in fields.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Add to job tracker</title>
<style>body{{font-family:system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 20px;}}
h1{{font-size:1.3rem;}} table{{border-collapse:collapse;width:100%;margin:1rem 0;}}
th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #ddd;vertical-align:top;}}
th{{width:8rem;color:#666;font-weight:500;}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:6px;
background:#0969da;color:#fff;cursor:pointer;}}</style>
</head>
<body>
<h1>Add this posting to the tracker?</h1>
<table>{rows}</table>
<form method="post" action="/capture/add-form">
{hidden}
<input type="hidden" name="token" value="{html_lib.escape(token, quote=True)}">
<button type="submit">Add to tracker</button>
</form>
<p style="font-size:.85rem;color:#888;margin-top:2rem">harrier capture</p>
</body></html>"""


@capture_router.get(
    "/capture/add",
    operation_id="captureJobConfirm",
    response_class=HTMLResponse,
    responses={200: {"content": {"text/html": {}}, "description": "confirmation page"}},
)
def capture_confirm(
    company: str = "",
    title: str = "",
    location: str = "",
    url: str = "",
    source: str = "manual",
    description: str = "",
) -> HTMLResponse:
    """Renders. Changes nothing.

    This used to add the row, which meant `<img src="http://localhost:8000/
    capture/add?company=x&title=y">` on any page the operator visited wrote to
    the tracker with no interaction at all (spec 035).
    """
    fields = {
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "source": source or "manual",
        "description": description,
    }
    return HTMLResponse(content=_confirm_page(fields, load_or_create_token()))


@capture_router.post(
    "/capture/add-form",
    operation_id="captureJobFromForm",
    response_class=HTMLResponse,
    responses={
        400: {"content": {"text/html": {}}, "description": "company and title are required"},
        403: {"content": {"text/html": {}}, "description": "missing or wrong local token"},
        409: {"content": {"text/html": {}}, "description": "already in tracker"},
    },
)
def capture_from_form(
    conn: Conn,
    company: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "manual",
    description: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The confirmation page's submit target.

    A form post cannot set a header, so the token travels as a field. A
    cross-origin page can post a form here, but it cannot read the token, so
    it cannot fill that field.
    """
    if not token_matches(token):
        return HTMLResponse(
            content=_result_page(
                CaptureResult(status="invalid", message="this form did not carry the local token"),
                url.strip(),
            ),
            status_code=403,
        )
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
    dependencies=[Depends(require_token)],
    responses={
        400: {"model": CaptureOut, "description": "company and title are required"},
        409: {"model": CaptureOut, "description": "already in tracker"},
        500: {"description": "unexpected error"},
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
