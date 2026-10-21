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

from coppermind.settings import ProductSettings, SyncSettings, Wiring
from coppermind.statefiles import RevisionConflict, StateStore

FILE_TYPES = {
    "image": "Images",
    "audio": "Audio",
    "video": "Video",
    "pdf": "PDF",
    "unsupported": "All other files (including ICS and ZIP)",
}
CONFIG_TYPES = {
    "app": "App preferences",
    "appearance": "Appearance",
    "appearance-data": "Themes and snippets",
    "hotkey": "Hotkeys",
    "core-plugin": "Core plugin selection",
    "core-plugin-data": "Core plugin settings",
    "community-plugin": "Community plugin selection",
    "community-plugin-data": "Community plugin settings",
}


def configuration_form(product: ProductSettings, revision: int, status: dict) -> str:
    saved = product.sync
    live = status.get("client_status") if status.get("config_live") else None
    body = "<h2>Live vault configuration</h2>"
    if status.get("simulated"):
        body += '<p class="notice">Simulated client: this does not prove Obsidian delivery.</p>'
    if not status.get("configured"):
        body += "<p>Connect a vault to inspect its live configuration.</p>"
    elif status.get("control_busy"):
        body += (
            '<p class="notice">Sync control is busy. Connection information is cached; '
            "reload shortly for verified live settings.</p>"
        )
    elif not live:
        body += (
            '<p class="error">Live configuration unavailable. Saved settings are shown below.</p>'
        )
    else:
        if status.get("config_matches"):
            body += '<p class="success">Saved settings match the live client configuration.</p>'
        else:
            body += (
                '<p class="error">Saved settings do not match the live client configuration.</p>'
            )
        body += "<table><thead><tr><th>Setting</th><th>Live value</th></tr></thead><tbody>"
        for label, key in [
            ("Device", "deviceName"),
            ("Direction", "syncMode"),
            ("Conflict handling", "conflictStrategy"),
            ("File types", "fileTypes"),
            ("Excluded folders", "excludedFolders"),
            ("Settings categories", "configs"),
            ("Configuration directory", "configDir"),
        ]:
            value = live.get(key, "Unknown")
            if isinstance(value, list):
                value = ", ".join(value) or "None"
            body += f"<tr><th>{label}</th><td>{html.escape(str(value))}</td></tr>"
        body += "</tbody></table>"
    if status.get("config_error"):
        body += '<p class="error">' + html.escape(status["config_error"]) + "</p>"
    body += '<h2>Sync preferences</h2><form method="post" action="/admin/sync/configure">'
    body += f'<input type="hidden" name="revision" value="{revision}">'
    body += (
        '<label>Device name<input name="device_name" required value="'
        + html.escape(saved.device_name, quote=True)
        + '"></label>'
    )
    for name, label, values in [
        (
            "mode",
            "Direction",
            {
                "bidirectional": "Bidirectional (recommended)",
                "pull-only": "Download only",
                "mirror-remote": "Mirror remote (reverts local changes)",
            },
        ),
        (
            "conflict_strategy",
            "Conflict handling",
            {"merge": "Merge edits", "conflict": "Create conflict files"},
        ),
        ("plan", "Plan for displayed limits", {"standard": "Standard", "plus": "Plus"}),
    ]:
        body += f'<label>{label}<select name="{name}">'
        for value, title in values.items():
            selected = " selected" if getattr(saved, name) == value else ""
            body += f'<option value="{value}"{selected}>{title}</option>'
        body += "</select></label>"
    body += "<p>Bidirectional sync is required to send Coppermind edits back to your devices.</p>"
    body += (
        '<label><input type="checkbox" name="confirm_mirror" value="yes"> I '
        "authorize reverting local changes when switching to Mirror "
        "remote.</label>"
    )
    for prefix, title, choices, enabled in [
        ("file", "File types", FILE_TYPES, tuple(saved.file_types)),
        ("config", "Obsidian settings sync", CONFIG_TYPES, tuple(saved.sync_configs)),
    ]:
        body += f"<fieldset><legend>{title}</legend>"
        for value, label in choices.items():
            checked = " checked" if value in enabled else ""
            body += (
                f'<label><input type="checkbox" name="{prefix}_{value}" '
                f'value="yes"{checked}> {label}</label>'
            )
        body += "</fieldset>"
    body += (
        "<p>Markdown sync is always enabled. Select at least one attachment type; "
        "this client cannot disable all attachment types. Other files must also be enabled on "
        "the device uploading them. Show all files is a separate display "
        "preference in Obsidian.</p>"
    )
    body += (
        "<label>Excluded folders (one vault-relative path per line)<textarea "
        'name="excluded_folders" rows="3">'
        + html.escape("\n".join(saved.excluded_folders))
        + "</textarea></label>"
    )
    for name, label in [
        ("max_file_bytes", "File limit override (bytes)"),
        ("max_total_bytes", "Total limit override (bytes)"),
    ]:
        value = getattr(saved, name)
        body += (
            f'<label>{label}<input type="number" min="1" name="{name}" '
            f'value="{value if value is not None else ""}" placeholder="Use plan '
            f'limit"></label>'
        )
    body += (
        "<p>Plan and limits are local reference values. They do not change your "
        "Obsidian subscription or its enforced limits. Calendar intake retains "
        "its separate 10 MiB input limit.</p>"
    )
    body += (
        '<button name="preset" value="save">Save and apply</button> <button '
        'name="preset" value="all-files">Sync all file types</button></form>'
    )
    body += (
        "<p>Saving briefly restarts an active sync client. A paused client stays "
        "paused. Failed changes remain saved for automatic retry.</p>"
    )
    return body


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

    def settings() -> tuple[ProductSettings, int]:
        try:
            current = store.read("settings")
            return ProductSettings.model_validate(
                {k: v for k, v in current.body.items() if k != "revision"}
            ), current.revision
        except FileNotFoundError:
            return ProductSettings(), 0

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
            "legacy": "Sync preferences were not saved. Use the controls on this page.",
            "configured": "Sync preferences saved and verified against the client.",
            "pending": (
                "Sync preferences saved. Application is pending; check the live "
                "configuration below."
            ),
            "conflict": "Settings changed in another tab. Reload and reapply your choices.",
            "invalid_config": (
                "Invalid sync preferences. Check selections, folder paths, positive "
                "limits and mirror confirmation."
            ),
        }.get(request.query_params.get("result", ""), "")
        try:
            product, revision = settings()
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
        body += configuration_form(product, revision, status)
        body += '<p><a href="/admin">Admin overview</a></p>'
        return HTMLResponse(page("Sync", body), headers={"Cache-Control": "no-store"})

    @routes.post("/admin/sync/{action}")
    async def sync_action(action: str, request: Request) -> Response:
        from coppermind_admin.main import SubmissionTooLarge, submitted

        def redirect(result: str) -> Response:
            return RedirectResponse("/admin/sync?result=" + result, status_code=303)

        if action not in {"connect", "pause", "resume", "disconnect", "configure"}:
            return Response(status_code=404)
        if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            return redirect("invalid")
        try:
            form = await submitted(request)
        except SubmissionTooLarge:
            return redirect("invalid")
        if action == "configure":
            try:
                current = store.read("settings")
                document = current.body
                values = dict(document.get("sync", {}))
                for key in ("device_name", "mode", "conflict_strategy", "plan"):
                    values[key] = form[key]
                if (
                    values["mode"] == "mirror-remote"
                    and current.body.get("sync", {}).get("mode") != "mirror-remote"
                    and form.get("confirm_mirror") != "yes"
                ):
                    return redirect("invalid_config")
                values["file_types"] = (
                    list(FILE_TYPES)
                    if form.get("preset") == "all-files"
                    else [key for key in FILE_TYPES if form.get("file_" + key) == "yes"]
                )
                values["sync_configs"] = [
                    key for key in CONFIG_TYPES if form.get("config_" + key) == "yes"
                ]
                values["excluded_folders"] = [
                    line.strip() for line in form["excluded_folders"].splitlines() if line.strip()
                ]
                for key in ("max_file_bytes", "max_total_bytes"):
                    values[key] = int(form[key]) if form[key].strip() else None
                    if values[key] is not None and values[key] <= 0:
                        return redirect("invalid_config")
                document["sync"] = SyncSettings.model_validate(values).model_dump(mode="json")
                store.write("settings", document, if_revision=int(form["revision"]))
            except RevisionConflict:
                return redirect("conflict")
            except (ValueError, KeyError):
                return redirect("invalid_config")
            except OSError:
                return redirect("failed")
            try:
                result = await asyncio.to_thread(helper, "configure", {})
                return redirect("configured" if result.get("config_matches") else "pending")
            except (HTTPError, OSError, ValueError):
                return redirect("pending")
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
