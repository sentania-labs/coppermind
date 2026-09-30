"""Guided Obsidian connection, with secrets used only in the submitted request."""

from __future__ import annotations

import asyncio
import html
import json
import os
from urllib.error import HTTPError
from urllib.request import Request as HTTPRequest
from urllib.request import urlopen

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.settings import ProductSettings, Wiring, read_settings
from coppermind.statefiles import RevisionConflict, StateStore


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()
    store = StateStore(wiring.state_dir)
    endpoint = os.environ.get("COPPERMIND_SYNC_URL", "http://obsidian-sync:8092").rstrip("/")

    def helper(action: str, body: dict[str, object] | None = None) -> dict:
        request = HTTPRequest(
            endpoint + "/" + action,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + wiring.read_internal_token(),
                "Content-Type": "application/json",
            },
            method="GET" if body is None else "POST",
        )
        # No exception detail, request body or upstream error body reaches HTML.
        with urlopen(request, timeout=300) as response:  # noqa: S310
            return json.load(response)

    def settings() -> ProductSettings:
        try:
            return read_settings(store)
        except FileNotFoundError:
            return ProductSettings()

    @routes.get("/admin/sync", response_class=HTMLResponse)
    async def sync_page(request: Request) -> Response:
        from coppermind_admin.main import page

        notice = {
            "invalid": "Check the required fields and matching encryption passwords.",
            "failed": (
                "Sync request failed. Check the helper status and retry. "
                "If creation succeeded, join that remote vault by name."
            ),
            "saved": "Sync request completed.",
        }.get(request.query_params.get("result", ""), "")
        try:
            product = settings()
        except (OSError, ValueError):
            return HTMLResponse(
                page("Sync", "<h1>Obsidian Sync</h1><p>Settings cannot be read.</p>"),
                status_code=503,
            )
        try:
            status = await asyncio.to_thread(helper, "status")
        except (OSError, ValueError):
            status = {"last_error": "Sync helper is unavailable"}
        rows = "".join(
            f"<dt>{label}</dt><dd>{html.escape(str(status.get(key) or 'Unknown'))}</dd>"
            for key, label in [
                ("state", "Connection"),
                ("vault_name", "Remote vault"),
                ("device_name", "Device"),
                ("last_sync_at", "Last sync"),
                ("last_error", "Last error"),
            ]
        )
        body = (
            f"<h1>Obsidian Sync</h1><p>{notice}</p><dl>{rows}</dl>"
            "<p>Liveness: child process only. "
            "A running process does not prove phone delivery.</p>"
        )
        body += (
            f"<p>Configured file limit: {product.sync.file_limit_bytes} bytes. "
            f"Total limit: {product.sync.total_limit_bytes} bytes.</p>"
        )
        if not status.get("configured"):
            device = html.escape(product.sync.device_name, quote=True)
            plans = "".join(
                f'<option value="{plan}"{" selected" if product.sync.plan == plan else ""}>'
                f"{plan.title()}</option>"
                for plan in ("standard", "plus")
            )
            body += f'''<form method="post" action="/admin/sync/connect" autocomplete="off">
<label>Obsidian email<input type="email" name="email" required></label>
<label>Password<input type="password" name="password" required></label>
<label>MFA code (optional)<input type="password" name="mfa_code"></label>
<label>Sync plan<select name="plan">{plans}</select></label>
<label>Remote vault name<input name="vault_name" required></label>
<label>Remote vault<select name="existing_vault">
<option value="false">Create new encrypted vault</option>
<option value="true">Join existing vault</option></select></label>
<p>Joining merges this notes filesystem with the selected remote vault.</p>
<label>Encryption password<input type="password" name="encryption_password" required></label>
<label>Confirm encryption password<input type="password" name="encryption_confirm" required></label>
<label>Device name<input name="device_name" value="{device}" required></label>
<button>Connect</button></form>'''
        else:
            for action in ("pause", "resume", "disconnect"):
                body += (
                    f'<form method="post" action="/admin/sync/{action}">'
                    f"<button>{action.title()}</button></form>"
                )
        body += '<p><a href="/admin">Admin overview</a></p>'
        return HTMLResponse(page("Sync", body), headers={"Cache-Control": "no-store"})

    @routes.post("/admin/sync/{action}")
    async def sync_action(action: str, request: Request) -> Response:
        from coppermind_admin.main import SubmissionTooLarge, submitted

        def redirect(result: str) -> Response:
            return RedirectResponse("/admin/sync?result=" + result, status_code=303)

        if action not in {"connect", "pause", "resume", "disconnect"}:
            return Response(status_code=404)
        if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            return redirect("invalid")
        try:
            form = await submitted(request)
        except SubmissionTooLarge:
            return redirect("invalid")
        body: dict[str, object] = {}
        if action == "connect":
            required = ("email", "password", "vault_name", "encryption_password", "device_name")
            if (
                any(not form.get(key, "").strip() for key in required)
                or form.get("encryption_password") != form.get("encryption_confirm")
                or form.get("plan") not in {"standard", "plus"}
                or form.get("existing_vault") not in {"true", "false"}
            ):
                return redirect("invalid")
            secrets = [
                form[key]
                for key in ("email", "password", "encryption_password", "mfa_code")
                if form.get(key)
            ]
            if any(
                secret in form[key] for key in ("device_name", "vault_name") for secret in secrets
            ):
                return redirect("invalid")
            if any(c in form["device_name"] for c in "\r\n\0"):
                return redirect("invalid")
            try:
                # Read the existing document so unrelated settings survive.
                try:
                    current = store.read("settings")
                    document, revision = current.body, current.revision
                except FileNotFoundError:
                    document, revision = ProductSettings().model_dump(), None
                document.setdefault("sync", {}).update(
                    plan=form["plan"], device_name=form["device_name"]
                )
                validated = dict(document)
                validated.pop("revision", None)
                ProductSettings.model_validate(validated)
                store.write("settings", document, if_revision=revision)
            except (OSError, ValueError, RevisionConflict):
                return redirect("failed")
            body = {key: form[key] for key in required}
            body["mfa_code"] = form.get("mfa_code", "")
            body["existing_vault"] = form["existing_vault"] == "true"
        try:
            await asyncio.to_thread(helper, action, body)
        except (HTTPError, OSError, ValueError):
            return redirect("failed")
        return redirect("saved")

    return routes
