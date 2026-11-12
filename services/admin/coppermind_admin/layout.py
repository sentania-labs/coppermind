"""The one page frame every Admin screen is drawn in: menu, header, dialog.

Everything visual resolves through Lattice tokens. Lattice has no sidebar,
stepper, status row, tiles or dialog yet, so those are added in the local layer
below and are candidates to move into Lattice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path

LATTICE = (Path(__file__).parent / "lattice.css").read_text()

CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--font-sans)}
a{color:var(--link)}
:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
[hidden]{display:none!important}
.lat-banner{white-space:normal}
a.lat-btn{text-decoration:none;display:inline-block}
.lat-btn{white-space:nowrap}
.lat-select{font:inherit;font-size:13px;padding:6px 9px;border:1px solid var(--ink-subtle);
  border-radius:var(--radius-lg);background:var(--surface);color:var(--ink)}
form.act{display:inline}
.skip{position:absolute;left:8px;top:-100px;background:var(--surface);padding:8px;z-index:30}
.skip:focus{top:8px}
.frame{container-type:inline-size;container-name:app;background:var(--bg)}
.shell{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
.side{background:var(--surface);border-right:1px solid var(--line);padding:14px 10px;
  display:flex;flex-direction:column;gap:2px;position:sticky;top:0;align-self:start;
  height:100vh;overflow:auto}
.brand{font-size:17px;font-weight:650;letter-spacing:-.01em;padding:2px 10px 10px;
  color:var(--ink);text-decoration:none;display:block}
.brand small{display:block;font-size:11.5px;font-weight:400;color:var(--ink-subtle);
  letter-spacing:0}
.nav-grp{font-size:10.5px;font-weight:500;letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-subtle);padding:12px 10px 3px}
.nav a{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 10px;
  border-radius:var(--radius-lg);text-decoration:none;color:var(--ink);font-size:13.5px}
.nav a:hover{background:var(--surface-sunken)}
.nav a.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.count{display:inline-block;min-width:18px;text-align:center;border-radius:var(--radius-pill);
  padding:0 6px;font-size:11.5px;font-weight:600;background:var(--warn-soft);
  color:var(--warn-ink)}
.count--bad{background:var(--bad-soft);color:var(--bad-ink)}
.count--quiet{background:var(--surface-sunken);color:var(--ink-muted)}
.side-foot{margin-top:auto;padding:10px 10px 0;font-size:12px;color:var(--ink-subtle);
  display:flex;justify-content:space-between;align-items:center}
.topbar{display:none}
.main{padding:20px 28px 36px;max-width:880px;width:100%}
.back{font-size:12px;text-decoration:none;display:inline-block;margin-bottom:4px}
h1{font-size:17px;line-height:1.5;font-weight:650;letter-spacing:-.01em;margin:0}
.lede{color:var(--ink-muted);margin:2px 0 12px;max-width:80ch}
h2,h3{font-size:14px;line-height:1.4;font-weight:650;margin:0 0 8px}
p{margin:0 0 8px}
.stack>*+*{margin-top:12px}
.muted{color:var(--ink-muted)} .subtle{color:var(--ink-subtle);font-size:12px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ink-subtle);
  flex:none}
.dot--ok{background:var(--ok)} .dot--warn{background:var(--warn)} .dot--bad{background:var(--bad)}
.dot--off{background:var(--line)}
.status{display:flex;gap:12px;align-items:flex-start}
.status .dot{margin-top:7px;width:11px;height:11px}
.lat-textarea{width:100%;min-height:9em;line-height:1.5;resize:vertical}
ul.plain{margin:6px 0 0;padding-left:20px}ul.plain li{margin:4px 0}
.summary{white-space:pre-wrap;margin:2px 0 10px;line-height:1.5}
.status-list{margin-top:8px;border-top:1px solid var(--line-soft)}
.status-t{font-size:17px;font-weight:650;letter-spacing:-.01em}
.status-a{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.rows{display:grid}
.row{display:grid;grid-template-columns:minmax(120px,190px) minmax(0,1fr) auto;gap:4px 14px;
  align-items:center;padding:9px 0;border-top:1px solid var(--line-soft)}
.row:first-child{border-top:0}
.row-l{font-weight:600;font-size:13px}
.row-v{color:var(--ink-muted);font-size:13px;min-width:0}
.row-a{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
.rows--one .row{grid-template-columns:1fr}
.rows--one .row-a{justify-content:flex-start}
.item{display:flex;gap:12px;align-items:center;padding:10px 0;
  border-top:1px solid var(--line-soft)}
.item:first-child{border-top:0}
.item-b{flex:1;min-width:0}
.item-t{font-weight:600}
.item-t a{color:inherit;text-decoration:none}
.item-t a:hover{text-decoration:underline}
.item-d{color:var(--ink-muted);font-size:13px}
.item-a{display:flex;gap:6px;flex:none;align-items:center}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.tiles--5{grid-template-columns:repeat(5,minmax(0,1fr))}
.tile{display:block;text-decoration:none;color:inherit;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--radius-xl);padding:10px 12px}
.tile:hover{border-color:var(--ink-subtle)}
.tile-n{font-size:12px;color:var(--ink-muted)}
.tile-s{font-weight:650;display:flex;gap:7px;align-items:center}
.tile-f{font-size:12px;color:var(--ink-subtle)}
.steps{display:flex;gap:6px;list-style:none;padding:0;margin:0 0 12px;flex-wrap:wrap}
.steps li{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink-subtle);
  padding-right:10px}
.steps .n{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;
  background:var(--surface-sunken);font-size:11.5px;font-weight:600;color:var(--ink-muted)}
.steps .cur{color:var(--ink);font-weight:600}
.steps .cur .n{background:var(--accent);color:var(--on-accent)}
.steps .done .n{background:var(--ok-soft);color:var(--ok-ink)}
.field{margin-bottom:10px;max-width:440px}
.field .lat-label{font-size:13px;color:var(--ink);font-weight:600}
.field--wide{max-width:none}
.choice{display:flex;gap:9px;align-items:flex-start;padding:8px 10px;
  border:1px solid var(--line);border-radius:var(--radius-lg);cursor:pointer;margin-bottom:6px;
  max-width:520px}
.choice:hover{border-color:var(--ink-subtle)}
.choice:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.choice input{margin-top:3px}
.choice b{display:block;font-weight:600}
.choice span span{color:var(--ink-muted);font-size:12.5px}
.checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 16px;max-width:560px}
.checks--one{grid-template-columns:1fr}
.check{display:flex;gap:8px;align-items:flex-start;padding:4px 0;cursor:pointer}
.check input{margin-top:3px}
.check small{display:block;color:var(--ink-subtle);font-size:12px}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
.actions--top{margin-top:0}
.actions--end{justify-content:flex-end}
.actions--mid{justify-content:center}
.seg{display:inline-flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
.seg a{font-size:13px;padding:4px 11px;border-radius:var(--radius-pill);
  border:1px solid var(--line);background:var(--surface);color:var(--ink-muted);
  text-decoration:none}
.seg a.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink);
  font-weight:600}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.spread{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;
  align-items:flex-start}
.spread--end{align-items:flex-end}
.cmp{border:1px solid var(--line);border-radius:var(--radius-lg);padding:10px 12px}
.cmp .k{font-size:12px;color:var(--ink-subtle)}
.cmp mark{background:var(--warn-soft);color:var(--warn-ink);padding:1px 5px;
  border-radius:var(--radius-sm);font-weight:650;border:1px solid var(--warn)}
.pickgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 8px}
.pickgrid .choice{margin-bottom:6px;padding:6px 9px}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{display:grid;grid-template-columns:118px 1fr;gap:10px;padding:4px 0;font-size:13px}
.timeline time{color:var(--ink-subtle)}
.bar{display:flex;height:10px;border-radius:var(--radius-pill);overflow:hidden;
  background:var(--surface-sunken);margin:6px 0}
.bar i{display:block}
.legend{display:flex;gap:4px 16px;flex-wrap:wrap;font-size:12px;color:var(--ink-muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.use-a{background:var(--accent)} .use-b{background:var(--ok)} .use-c{background:var(--warn)}
.use-d{background:var(--ink-subtle)}
.keybox{font-family:var(--font-mono);font-size:12px;background:var(--surface-sunken);
  padding:10px 12px;border-radius:var(--radius-lg);overflow-wrap:anywhere}
.keybox--code{display:inline-block;font-size:16px;letter-spacing:.08em}
.empty{text-align:center;padding:26px 16px;color:var(--ink-muted)}
.empty b{display:block;color:var(--ink);font-size:14px;margin-bottom:2px}
.quote{border-left:3px solid var(--line);padding:2px 10px;color:var(--ink-muted);
  font-size:13px;margin:6px 0}
.tech{font-family:var(--font-mono);font-size:11px;color:var(--ink-subtle);margin-top:4px;
  overflow-wrap:anywhere}
.linkish{background:none;border:0;padding:0;font:inherit;font-size:12px;color:var(--link);
  cursor:pointer;text-decoration:underline}
details.support>summary{list-style:none;display:inline}
details.support>summary::-webkit-details-marker{display:none}
.overlay{position:fixed;inset:0;background:rgba(15,20,28,.45);display:grid;place-items:center;
  z-index:20;padding:16px}
.dialog{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-xl);
  padding:16px 18px;max-width:440px;width:100%;max-height:calc(100vh - 32px);overflow:auto}
.dialog h2{font-size:15px}
.public{min-height:100vh;display:grid;place-items:center;padding:16px}
.public .lat-card{width:min(440px,100%);padding:20px 22px}
.tzlist{border:1px solid var(--line);border-radius:var(--radius-lg);overflow:hidden;
  max-width:440px}
.tzlist button{display:block;width:100%;text-align:left;font:inherit;font-size:13px;
  padding:7px 10px;border:0;border-top:1px solid var(--line-soft);background:var(--surface);
  color:var(--ink);cursor:pointer}
.tzlist form:first-child button{border-top:0}
.tzlist button:hover{background:var(--surface-sunken)}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;gap:6px;align-items:center;background:var(--surface-sunken);
  border-radius:var(--radius-pill);padding:2px 4px 2px 10px;font-size:12.5px}
.chip button{border:0;background:none;color:var(--ink-subtle);cursor:pointer;font-size:14px;
  line-height:1;padding:2px 5px}
.inline{display:inline-block;width:auto}
.narrow{max-width:240px}
.hide-wide{display:none}
.steps-list{margin:0;padding-left:20px}
audio{height:30px;vertical-align:middle;max-width:220px}

@container app (max-width: 720px) {
  .shell{display:block}
  .side{display:none}
  .side.open{display:flex;position:absolute;inset:0;z-index:10;border-right:0;
    padding:12px 14px 20px;overflow:auto;height:auto;min-height:100%}
  .frame{position:relative}
  .topbar{display:flex;justify-content:space-between;align-items:center;padding:6px 14px;
    background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;
    z-index:5}
  .topbar .brand{padding:0}
  .main{padding:14px 16px 28px}
  .lat-btn{min-height:44px;padding:9px 14px;font-size:14px}
  .lat-btn--ghost{padding:9px 10px}
  .nav a{min-height:44px;font-size:15px}
  .lat-input,.lat-select{min-height:44px;font-size:16px}
  .seg a{min-height:40px;font-size:14px;display:inline-flex;align-items:center}
  .linkish{font-size:13px;padding:8px 0}
  .tiles,.tiles--5{grid-template-columns:1fr 1fr}
  .row{grid-template-columns:1fr}
  .row-a{justify-content:flex-start;flex-wrap:wrap}
  .row-a:empty{display:none}
  .item{align-items:flex-start}
  .item-a{flex-wrap:wrap;justify-content:flex-end;max-width:46%}
  .status{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px}
  .status-a{grid-column:2;margin:4px 0 0;justify-content:flex-start}
  .two{grid-template-columns:1fr}
  .checks{grid-template-columns:1fr}
  .timeline li{grid-template-columns:1fr;gap:0}
  .hide-narrow{display:none}
  .back{display:inline-flex;align-items:center;min-height:44px;font-size:14px;margin-bottom:0}
  .chip{padding:0 0 0 12px;font-size:14px}
  .chip button{min-width:44px;min-height:44px;font-size:18px}
  .check{min-height:44px;align-items:center}
  .check input,.choice input{width:20px;height:20px;margin-top:0}
  .choice{align-items:center;min-height:44px}
  .lede{margin-bottom:8px}
  .hide-wide{display:initial}
  .tcards thead{display:none}
  .tcards tr{display:block;padding:8px 0;border-bottom:1px solid var(--line-soft)}
  .tcards td{display:block;border:0;padding:1px 0}
  .tcards td:first-child{color:var(--ink);font-weight:600}
  .tcards td[data-l]:not(:empty)::before{content:attr(data-l) ": ";color:var(--ink-subtle)}
  .pickgrid{grid-template-columns:1fr}
}
"""

# Small progressive enhancements. Every page works without them: forms post, links
# navigate, and a dialog is an ordinary page with the question drawn over it.
SCRIPT = """
const pref=()=>{try{return localStorage.getItem("theme")||"system"}catch(e){return "system"}};
function theme(v){document.documentElement.dataset.theme=v==="system"
  ?(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"):v}
theme(pref());
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",()=>theme(pref()));
addEventListener("DOMContentLoaded",()=>{
  const $=(s,r)=>Array.from((r||document).querySelectorAll(s));
  $("select.theme").forEach(s=>{s.value=pref();s.addEventListener("change",()=>{
    try{localStorage.setItem("theme",s.value)}catch(e){}theme(s.value);
    const done=document.getElementById("theme-saved");if(done)done.hidden=false;});});
  $("form").forEach(f=>f.addEventListener("submit",e=>{
    const b=e.submitter||f.querySelector("button[data-busy]");
    if(b&&b.dataset.busy){setTimeout(()=>{b.disabled=true;b.textContent=b.dataset.busy},0)}}));
  $("[data-auto]").forEach(el=>{el.addEventListener("change",()=>el.form.requestSubmit());
    $("noscript,.no-js",el.form).forEach(n=>n.hidden=true);});
  $("input[data-live]").forEach(el=>{let t;el.addEventListener("input",()=>{clearTimeout(t);
    t=setTimeout(()=>el.form.requestSubmit(),350)});
    if(el.value&&el.hasAttribute("autofocus")){el.setSelectionRange(el.value.length,
      el.value.length)}});
  const menu=document.getElementById("menu"),side=document.getElementById("side");
  $("[data-menu]").forEach(b=>b.addEventListener("click",()=>side.classList.toggle("open")));
  $("[data-copy]").forEach(b=>b.addEventListener("click",()=>{
    const src=document.getElementById(b.dataset.copy);
    navigator.clipboard.writeText(src.textContent.trim()).then(()=>{
      const done=document.getElementById(b.dataset.copy+"-copied");if(done)done.hidden=false;
      b.classList.remove("lat-btn--primary");});}));
  const sync=()=>$("[data-if]").forEach(el=>{const [n,v]=el.dataset.if.split("=");
    const on=$('input[name="'+n+'"]:checked,select[name="'+n+'"]').some(i=>
      v.split("|").includes(i.value));
    el.hidden=!on;$("input,select,textarea",el).forEach(i=>i.disabled=!on);});
  $("input[type=radio],select").forEach(i=>i.addEventListener("change",sync));sync();
  const wait=document.body.dataset.refresh;
  if(wait)setTimeout(()=>location.reload(),Number(wait)*1000);
  const fresh=document.querySelector("[data-fresh]");
  if(fresh){const box=fresh.getBoundingClientRect();
    if(box.top<60||box.bottom>innerHeight)fresh.scrollIntoView({block:"center"});}
  const cancel=document.querySelector(".dialog [data-cancel]");
  if(cancel){addEventListener("keydown",e=>{if(e.key==="Escape")location.href=cancel.href});
    const first=document.querySelector(".dialog input:not([type=hidden]),.dialog button");
    if(first)first.focus();}
});
"""

GROUPS = (
    ("", ("home", "setup")),
    ("Day to day", ("notes", "incoming", "calendar", "people")),
    ("Connections", ("sync", "services", "suggestions", "transcription", "apps")),
    ("Housekeeping", ("storage", "settings")),
)
LABELS = {
    "home": "Home",
    "setup": "Setup guide",
    "notes": "Notes",
    "incoming": "Recordings and imports",
    "calendar": "Calendar",
    "people": "People and voices",
    "sync": "Obsidian Sync",
    "services": "AI services",
    "suggestions": "Suggestions",
    "transcription": "Transcription",
    "apps": "Connected apps",
    "storage": "Storage",
    "settings": "Settings",
}


def href(entry: str) -> str:
    return "/admin" if entry == "home" else f"/admin/{entry}"


@dataclass
class Menu:
    """What the menu shows beside each entry. Every number here is one the pages show too."""

    attention: int = 0
    stopped: bool = False
    # (steps done, steps in all) while setup is unfinished; None once it is.
    setup: tuple[int, int] | None = None
    counts: dict[str, int] = field(default_factory=dict)
    # One word per switchable thing: none, on, paused, failing or signin.
    states: dict[str, str] = field(default_factory=dict)
    services_down: int = 0


def _count(value: int, *, bad: bool = False) -> str:
    if not value:
        return ""
    return f'<span class="count{" count--bad" if bad else ""}">{value}</span>'


def _state(word: str) -> str:
    if word in {"failing", "signin"}:
        return '<span class="count count--bad">Stopped</span>'
    if word == "paused":
        return '<span class="count">Paused</span>'
    if word == "on":
        return '<span class="count count--quiet">On</span>'
    if word == "none":
        return '<span class="count count--quiet">Off</span>'
    return ""


def menu_html(menu: Menu, active: str) -> str:
    extras = {
        "home": _count(menu.attention, bad=menu.stopped),
        "setup": (
            f'<span class="count count--quiet">{menu.setup[0]} of {menu.setup[1]}</span>'
            if menu.setup
            else ""
        ),
        "notes": _count(menu.counts.get("notes", 0)),
        "incoming": _count(menu.counts.get("incoming", 0)),
        "calendar": _count(menu.counts.get("calendar", 0)),
        "sync": _state(menu.states.get("sync", "")),
        "services": _count(menu.services_down, bad=True),
        "suggestions": _state(menu.states.get("sugg", "")),
        "transcription": _state(menu.states.get("trans", "")),
        "storage": _count(menu.counts.get("storage", 0)),
    }
    out = ""
    for group, entries in GROUPS:
        if group:
            out += f'<div class="nav-grp">{group}</div>'
        for entry in entries:
            if entry == "setup" and menu.setup is None:
                continue
            current = ' class="on" aria-current="page"' if entry == active else ""
            out += f'<a href="{href(entry)}"{current}>{LABELS[entry]}{extras.get(entry, "")}</a>'
    return out


def document(title: str, content: str, *, refresh: int = 0) -> str:
    wait = f' data-refresh="{refresh}"' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} | Coppermind</title><style>{LATTICE}
{CSS}</style>
<script>{SCRIPT}</script></head>
<body{wait}>{content}</body></html>"""


def public(title: str, body: str) -> str:
    """A page for someone who is not signed in: no menu, one card."""
    return document(
        title,
        f'<div class="frame"><div class="public"><main id="content">{body}</main></div></div>',
    )


def render(
    title: str, body: str, active: str, menu: Menu, *, dialog: str = "", refresh: int = 0
) -> str:
    """A signed-in page: the menu, the page, and a dialog drawn over it when one is open."""
    badge = _count(menu.attention, bad=menu.stopped)
    content = f"""<a class="skip" href="#content">Skip to content</a>
<div class="frame"><div class="topbar"><a class="brand" href="/admin">Coppermind</a>
<button type="button" class="lat-btn" data-menu aria-controls="side">Menu {badge}</button></div>
<div class="shell"><aside class="side" id="side">
<div class="spread"><a class="brand" href="/admin">Coppermind<small>Admin</small></a>
<span class="hide-wide"><button type="button" class="lat-btn" data-menu>Close</button></span></div>
<nav class="nav" aria-label="Admin">{menu_html(menu, active)}</nav>
<div class="side-foot"><span>Signed in</span><form class="act" method="post"
action="/admin/signout"><button class="lat-btn lat-btn--ghost">Sign out</button></form></div>
</aside><main class="main" id="content">{body}</main></div>{dialog}</div>"""
    return document(title, content, refresh=refresh)
