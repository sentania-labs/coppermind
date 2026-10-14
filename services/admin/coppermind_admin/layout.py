"""Shared, responsive presentation for the operator pages."""

from html import escape

CSS = """
:root {color-scheme:light;font-family:system-ui,sans-serif;background:#f4f1e8;color:#19231d}
* {box-sizing:border-box;min-width:0}
body {margin:0;min-height:100vh}
a {color:#245c3b;text-underline-offset:3px}
button,input,select,textarea {font:inherit}
:focus-visible {outline:3px solid #a67524;outline-offset:3px}
.skip {position:absolute;left:1rem;top:-10rem;background:white;padding:1rem;z-index:2}
.skip:focus {top:1rem}
.shell {display:grid;grid-template-columns:14rem minmax(0,1fr);min-height:100vh}
.sidebar {padding:1.5rem 1rem;background:#eeebdf;border-right:1px solid #d3cdbd}
.brand {font:1.7rem Georgia,serif;text-decoration:none;color:#19231d;display:block;margin:.5rem}
.brand small {display:block;font:normal .75rem system-ui,sans-serif;color:#59655d;margin-top:.6rem}
nav {display:grid;gap:.35rem;margin:2rem 0}
nav a {padding:.7rem .8rem;border-radius:.4rem;text-decoration:none}
nav a:hover {background:#e0e6da}
nav a[aria-current=page] {background:#245c3b;color:white}
.sidebar button {width:100%;background:transparent;color:#245c3b;border:1px solid #aebbad}
.workspace {padding:2rem;max-width:100rem;width:100%;margin:0 auto}
.breadcrumb {font-size:.8rem;color:#59655d;margin-bottom:1.5rem}
main {background:white;border:1px solid #d3cdbd;border-radius:.8rem;padding:2rem}
.public {display:grid;place-items:center;padding:1rem;min-height:100vh}
.public main {width:min(30rem,100%);box-shadow:0 1rem 3rem #283a2a18}
h1,h2,h3 {font-family:Georgia,serif;font-weight:500;overflow-wrap:anywhere}
h1 {font-size:2.2rem;margin:0 0 1rem;letter-spacing:-.04rem}
h2 {font-size:1.5rem;margin:1.5rem 0 1rem}
h3 {font-size:1.2rem} p,li {line-height:1.6}
label {display:block;font-weight:650;margin-top:1rem;line-height:1.5}
input,textarea,select {width:100%;margin-top:.4rem;padding:.7rem;border:1px solid #8a918b;
  border-radius:.4rem;background:white;color:#19231d}
input[type=checkbox],input[type=radio] {width:auto;margin-right:.5rem}
input:disabled,select:disabled,textarea:disabled {background:#f4f1e8;color:#59655d}
textarea {min-height:7rem;resize:vertical}
fieldset {margin:1.5rem 0;padding:1rem 1.3rem;border:1px solid #d3cdbd;border-radius:.5rem}
legend {font-weight:650;padding:0 .4rem}
button,.button {display:inline-block;margin-top:1rem;border:1px solid #245c3b;border-radius:.4rem;
  padding:.7rem 1rem;background:#245c3b;color:white;font-weight:650;cursor:pointer;
  text-decoration:none}
button:hover,.button:hover {background:#19442b} button:disabled {opacity:.55;cursor:default}
.error,.notice,.success {padding:1rem;border-radius:.3rem;line-height:1.6}
.error {background:#fde8e4;border-left:.25rem solid #a93624}
.notice {background:#fff1d6;border-left:.25rem solid #a67524}
.success {background:#e9f0e9;border-left:.25rem solid #245c3b}
.muted {color:#59655d;font-size:.9rem} pre,code {font-size:.85rem;overflow-wrap:anywhere}
pre {white-space:pre-wrap;line-height:1.7;padding:1rem;background:#f4f1e8;border-radius:.4rem}
table {width:100%;border-collapse:collapse;text-align:left;table-layout:fixed}
th,td {padding:.8rem;border-bottom:1px solid #d3cdbd;overflow-wrap:anywhere;vertical-align:top}
th {font-size:.8rem;color:#59655d} dl {display:grid;grid-template-columns:1fr 2fr;gap:.8rem}
dt {font-weight:650} dd {margin:0;overflow-wrap:anywhere}
.metrics {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem;margin:1.5rem 0}
.metric {background:#f4f1e8;border:1px solid #d3cdbd;padding:1.2rem;border-radius:.5rem}
.metric strong {display:block;font:2.3rem Georgia,serif;margin-top:.6rem}
.metric a {display:block;margin-top:.5rem}
.section-links {display:flex;flex-wrap:wrap;gap:.5rem;margin:1.5rem 0}
.section-links a {padding:.55rem .85rem;border:1px solid #d3cdbd;border-radius:2rem;
  text-decoration:none;background:#f4f1e8}
.section-links a[aria-current=page] {background:#245c3b;color:white;border-color:#245c3b}
.settings-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 2rem}
.setting-help {margin:.6rem 0 1rem;font-size:.85rem;color:#59655d;line-height:1.6}
.setting-help code {font-size:.75rem}
.form-footer {border-top:1px solid #d3cdbd;margin-top:1.5rem;padding-top:.5rem}
.field-definition {border:1px solid #d3cdbd;border-radius:.5rem;margin:1rem 0;padding:1rem}
.field-definition fieldset {border:0;margin:0;padding:.5rem 0}
summary {cursor:pointer;font-weight:650;line-height:1.5}
[hidden] {display:none!important}
@media(max-width:900px) {
  .metrics {grid-template-columns:1fr}.settings-grid {grid-template-columns:1fr}
}
@media(max-width:650px) {
  .shell {display:block}.sidebar {border-right:0;border-bottom:1px solid #d3cdbd;padding:1rem}
  nav {display:flex;flex-wrap:wrap;margin:1rem 0;gap:.2rem}nav a {padding:.5rem;font-size:.85rem}
  .sidebar form {display:inline-block}.sidebar button {margin:0;padding:.4rem .7rem}
  .workspace {padding:1rem}main {padding:1.2rem}h1 {font-size:1.8rem}
  fieldset {padding:.8rem}th,td {padding:.5rem;font-size:.85rem}dl {grid-template-columns:1fr}
}
"""

LINKS = (
    ("/admin", "Overview"),
    ("/admin/problems", "Problems"),
    ("/admin/fields", "Fields and tags"),
    ("/admin/settings", "Settings"),
    ("/admin/keys", "API Keys"),
    ("/admin/sync", "Obsidian Sync"),
    ("/admin/search-index", "Search index"),
    ("/admin/note-history", "Note recovery"),
)


def render(title: str, body: str, path: str) -> str:
    """Only authenticated requests supply a path and receive navigation."""
    if path:
        active = path.removeprefix("/v1")
        links = "".join(
            f'<a href="{url}"'
            + (
                ' aria-current="page"'
                if active == url or (url != "/admin" and active.startswith(url + "/"))
                else ""
            )
            + f">{label}</a>"
            for url, label in LINKS
        )
        content = f"""<a class="skip" href="#content">Skip to content</a>
<div class="shell"><aside class="sidebar">
<a class="brand" href="/admin">Coppermind</a>
<nav aria-label="Admin">{links}</nav>
<form method="post" action="/v1/admin/logout"><button>Log out</button></form>
</aside><div class="workspace"><div class="breadcrumb">Coppermind / {escape(title)}</div>
<main id="content">{body}</main></div></div>"""
    else:
        content = f'<div class="public"><main id="content">{body}</main></div>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} | Coppermind</title><style>{CSS}</style></head>
<body>{content}</body></html>"""
