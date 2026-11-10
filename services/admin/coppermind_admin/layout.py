"""Shared, responsive presentation for the operator pages."""

from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LATTICE = (Path(__file__).parent / "lattice.css").read_text()

CSS = """
[data-theme="light"] {color-scheme:light}
[data-theme="dark"] {color-scheme:dark}
:root {color-scheme:light dark;
  font-family:system-ui,sans-serif;
  background:var(--surface-sunken);
  color:var(--ink)}
* {box-sizing:border-box;min-width:0}
body {margin:0;min-height:100vh}
a {color:var(--link);text-underline-offset:3px}
button,input,select,textarea {font:inherit}
:focus-visible {outline:3px solid var(--warn);outline-offset:3px}
.skip {position:absolute;left:1rem;top:-10rem;background:var(--surface);padding:1rem;z-index:2}
.skip:focus {top:1rem}
.shell {display:grid;grid-template-columns:14rem minmax(0,1fr);min-height:100vh}
.sidebar {padding:1.5rem 1rem;background:var(--surface);border-right:1px solid var(--line)}
.brand {font:650 1.35rem var(--font-sans);text-decoration:none;color:var(--ink);
  display:block;margin:.5rem}
.brand small {display:block;
  font:normal .75rem system-ui,sans-serif;
  color:var(--ink-muted);
  margin-top:.6rem}
nav {display:grid;gap:.35rem;margin:2rem 0}
nav a {padding:.7rem .8rem;border-radius:.4rem;text-decoration:none}
nav a:hover {background:var(--accent-soft)}
nav a[aria-current=page] {background:var(--accent);color:var(--on-accent)}
.sidebar button {width:100%;background:transparent;color:var(--link);border:1px solid var(--line)}
.sidebar button:hover {background:var(--surface-sunken);color:var(--link)}
.workspace {padding:2rem;max-width:100rem;width:100%;margin:0 auto}
.breadcrumb {font-size:.8rem;color:var(--ink-muted);margin-bottom:1.5rem}
main {background:var(--surface);border:1px solid var(--line);border-radius:.8rem;padding:2rem}
.context-content {min-width:0;overflow-wrap:anywhere}
.attention-list {list-style:none;padding:0}
.attention-list > li {border:1px solid var(--line);border-radius:var(--radius-lg);
  padding:var(--space-7);margin-bottom:var(--space-6);background:var(--surface)}
.attention-list > li p {margin:.5rem 0}
.property-list {border:1px solid var(--line);border-radius:.5rem;overflow:hidden}
.property-row {display:grid;grid-template-columns:1fr .6fr 2fr auto;gap:1rem;padding:1rem;
  border-bottom:1px solid var(--line-soft);text-decoration:none;align-items:center}
.property-row small {color:var(--ink-muted)}
.review-grid {display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1.5rem}
@media(max-width:700px){.property-row{grid-template-columns:1fr 1fr}
.review-grid{grid-template-columns:1fr}}
.connection-card {border:1px solid var(--line);border-radius:.5rem;padding:1rem;margin:1rem 0}
.connection-card summary {cursor:pointer;font-weight:650;color:var(--link)}
.connection-card summary small {font-weight:400;color:var(--ink-muted);margin-left:.75rem}
.field-help {font-size:.9rem;color:var(--ink-muted);margin-top:.4rem}
.public {display:grid;place-items:center;padding:1rem;min-height:100vh}
.public main {width:min(30rem,100%);box-shadow:0 1rem 3rem transparent}
h1,h2,h3 {font-family:var(--font-sans);font-weight:500;overflow-wrap:anywhere}
h1 {font-size:2.2rem;margin:0 0 1rem;letter-spacing:-.04rem}
h2 {font-size:1.5rem;margin:1.5rem 0 1rem}
h3 {font-size:1.2rem} p,li {line-height:1.6}
label {display:block;font-weight:650;margin-top:1rem;line-height:1.5}
input,textarea,select {width:100%;margin-top:.4rem;padding:.7rem;border:1px solid var(--line);
  border-radius:.4rem;background:var(--surface);color:var(--ink)}
input[type=checkbox],input[type=radio] {width:auto;margin-right:.5rem}
input:disabled,select:disabled,textarea:disabled {background:var(--surface-sunken);
  color:var(--ink-muted)}
textarea {min-height:7rem;resize:vertical}
[aria-invalid=true] {border-color:var(--bad);outline:2px solid var(--bad)}
fieldset {margin:1.5rem 0;padding:1rem 1.3rem;border:1px solid var(--line);border-radius:.5rem}
legend {font-weight:650;padding:0 .4rem}
button,.button {display:inline-block;
  margin-top:1rem;
  border:1px solid var(--accent);
  border-radius:.4rem;

  padding:.7rem 1rem;background:var(--accent);color:var(--on-accent);font-weight:650;cursor:pointer;
  text-decoration:none}
button.danger {background:var(--bad-ink);border-color:var(--bad-ink)}
button.danger:hover {background:var(--bad-ink)}
button.secondary {background:var(--surface);color:var(--link);margin-left:.5rem}
button.secondary:hover {background:var(--ok-soft)}
button:hover,.button:hover {background:var(--accent-strong)} button:disabled {opacity:.55;
  cursor:default}
.error,.notice,.success {padding:1rem;border-radius:.3rem;line-height:1.6}
.error {background:var(--bad-soft);border-left:.25rem solid var(--bad)}
.notice {background:var(--warn-soft);border-left:.25rem solid var(--warn)}
.success {background:var(--ok-soft);border-left:.25rem solid var(--accent)}
.muted {color:var(--ink-muted);font-size:.9rem} pre,code {font-size:.85rem;overflow-wrap:anywhere}
pre {white-space:pre-wrap;
  line-height:1.7;
  padding:1rem;
  background:var(--surface-sunken);
  border-radius:.4rem}
table {width:100%;border-collapse:collapse;text-align:left;table-layout:fixed}
th,td {padding:.8rem;border-bottom:1px solid var(--line);overflow-wrap:anywhere;vertical-align:top}
table.artifacts td.nowrap,table.artifacts th {white-space:nowrap}
table.artifacts th,table.artifacts td {padding:.6rem .5rem}
table.artifacts small {font-size:.8rem}
table.artifacts small.state {color:var(--warn);font-size:.85rem}
table.artifacts a.button {display:inline-block;padding:.35rem .6rem;font-size:.85rem}
th {font-size:.8rem;
  color:var(--ink-muted)} dl {display:grid;
  grid-template-columns:1fr 2fr;
  gap:.8rem}
dt {font-weight:650} dd {margin:0;overflow-wrap:anywhere}
.metrics {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem;margin:1.5rem 0}
.metric {background:var(--surface-sunken);
  border:1px solid var(--line);
  padding:1.2rem;
  border-radius:.5rem}
.metric strong {display:block;font:2.3rem Georgia,serif;margin-top:.6rem}
.metric a {display:block;margin-top:.5rem}
.section-links {display:flex;flex-wrap:wrap;gap:.5rem;margin:1.5rem 0}
.section-links a {padding:.55rem .85rem;border:1px solid var(--line);border-radius:2rem;
  text-decoration:none;background:var(--surface-sunken)}
.section-links-heading {flex-basis:100%;margin:.5rem 0 -.25rem;color:var(--ink-muted)}
.section-links a[aria-current=page] {background:var(--accent);color:var(--on-accent);
  border-color:var(--accent)}
.settings-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 2rem}
.setting-help {margin:.6rem 0 1rem;font-size:.85rem;color:var(--ink-muted);line-height:1.6}
.setting-help code {font-size:.75rem}
.form-footer {border-top:1px solid var(--line);margin-top:1.5rem;padding-top:.5rem}
.field-definition {border:1px solid var(--line);border-radius:.5rem;margin:1rem 0;padding:1rem}
.field-definition fieldset {border:0;margin:0;padding:.5rem 0}
summary {cursor:pointer;font-weight:650;line-height:1.5}
[hidden] {display:none!important}
@media(max-width:900px) {
  .metrics {grid-template-columns:1fr}.settings-grid {grid-template-columns:1fr}
}
@media(min-width:651px) {.mobile-nav {display:none}}
@media(max-width:650px) {
  .desktop-nav,.desktop-controls {display:none}
  .shell {display:block}.sidebar {border-right:0;border-bottom:1px solid var(--line);padding:1rem}
  nav {display:grid;margin:1rem 0;gap:.2rem}nav a {padding:.5rem;font-size:.95rem}
  .sidebar form {display:inline-block}.sidebar button {margin:0;padding:.4rem .7rem}
  .workspace {padding:1rem}main {padding:1.2rem}h1 {font-size:1.8rem}
  fieldset {padding:.8rem}th,td {padding:.5rem;font-size:.85rem}dl {grid-template-columns:1fr}
}
"""

LINKS = (
    ("/admin", "Home"),
    ("/admin/workflow?tab=intake", "Imports"),
    ("/admin/workflow?tab=review", "Review status"),
    ("/admin/context?tab=calendar", "Calendar"),
    ("/admin/workflow?tab=dossiers", "People"),
    ("/admin/settings", "Settings"),
)

SETTINGS_LINKS = (
    ("/admin/sync", "Devices"),
    ("/admin/workflow?tab=enrichment", "Suggestions"),
    ("/admin/properties", "Note properties"),
    ("/admin/settings?section=notes", "Folders and filing"),
    ("/admin/keys", "Connected apps"),
    ("/admin/search-index", "Search index"),
    ("/admin/field-index", "Field index"),
    ("/admin/note-history", "Note recovery"),
    ("/admin/artifacts", "Artifacts"),
    ("/admin/purge", "Retention purge"),
    ("/admin/workflow", "Workflow setup"),
    ("/admin/context?tab=review", "Suggestions and backlog"),
    ("/admin/context?tab=models", "Model connections"),
)


SETTINGS_PAGES = (
    "/admin/sync",
    "/admin/properties",
    "/admin/keys",
    "/admin/search-index",
    "/admin/field-index",
    "/admin/note-history",
    "/admin/artifacts",
    "/admin/purge",
)

DETAIL_PARENTS = (
    ("/admin/notes", "/admin/workflow?tab=review"),
    ("/admin/review", "/admin/workflow?tab=review"),
    ("/admin/context/jobs", "/admin/settings"),
    ("/admin/context/recordings", "/admin/settings"),
)


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base + "/")


def render(title: str, body: str, path: str) -> str:
    """Only authenticated requests supply a path and receive navigation."""
    if path:
        active = path.removeprefix("/v1")
        current = urlsplit(active)
        query = parse_qs(current.query)

        def matches(url: str) -> bool:
            target = urlsplit(url)
            target_query = parse_qs(target.query)
            return current.path == target.path and all(
                query.get(key) == value for key, value in target_query.items()
            )

        section = next(
            (parent for base, parent in DETAIL_PARENTS if _under(current.path, base)), ""
        )
        if not section and any(_under(current.path, base) for base in SETTINGS_PAGES):
            section = "/admin/settings"
        if not section and current.path == "/admin/context" and "tab" not in query:
            section = "/admin/context?tab=calendar"
        if not section:
            section = next((url for url, _ in LINKS if matches(url)), "")
        if not section and any(matches(url) for url, _ in SETTINGS_LINKS):
            section = "/admin/settings"

        links = "".join(
            f'<a href="{url}"'
            + (' aria-current="page"' if url == section else "")
            + f">{label}</a>"
            for url, label in LINKS
        )
        if section == "/admin/settings":
            settings_links = "".join(
                f'<li><a href="{url}">{label}</a></li>'
                for url, label in SETTINGS_LINKS
                if url != "/admin/settings"
            )
            settings_menu = (
                f"<details><summary>Devices, connections and maintenance</summary>"
                f"<ul>{settings_links}</ul></details>"
            )
            body = (
                body.replace("</h1>", "</h1>" + settings_menu, 1)
                if "</h1>" in body
                else settings_menu + body
            )
        controls = """<label>Appearance<select class="theme lat-select">
<option value="system">System</option><option value="light">Light</option>
<option value="dark">Dark</option></select></label>
<form method="post" action="/v1/admin/logout"><button class="lat-btn">Log out</button></form>"""
        attention_link = (
            '<p><a href="/admin#attention">Needs attention</a></p>'
            if current.path != "/admin"
            else ""
        )
        content = f"""<a class="skip" href="#content">Skip to content</a>
<div class="shell"><aside class="sidebar">
<a class="brand" href="/admin">Coppermind</a>
<nav class="desktop-nav" aria-label="Admin">{links}</nav>
<details class="mobile-nav"><summary>Menu</summary><nav aria-label="Admin">{links}</nav>
{controls}</details>
<div class="desktop-controls">{controls}</div>
</aside><div class="workspace"><div class="breadcrumb">Coppermind / {escape(title)}</div>
<main id="content">{attention_link}{body}</main></div></div>"""
    else:
        content = f'<div class="public"><main id="content">{body}</main></div>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} | Coppermind</title><style>{LATTICE}
{CSS}</style>
<script>const pref=localStorage.getItem("theme")||"system";

function theme(v){{
document.documentElement.dataset.theme=v==="system"
  ? (matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light") : v}}
theme(pref);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",
  ()=>theme(localStorage.getItem("theme")||"system"));

addEventListener("DOMContentLoaded",()=>{{
  document.querySelectorAll(".theme").forEach(s=>{{
    s.value=pref;
    s.addEventListener("change",()=>{{
      localStorage.setItem("theme",s.value);
      theme(s.value);
      document.querySelectorAll(".theme").forEach(other=>other.value=s.value);
    }});
  }});
}});
</script></head>
<body>{content}</body></html>"""
