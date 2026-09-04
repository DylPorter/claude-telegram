"""Render a self-contained HTML board from tagged rows.

THIS FILE IS COPIED, NOT IMPORTED. Identical copies live in job-sift, hk-events
and the standalone hku-cedars-scraper. They are separate distributions with no
shared package between them, and the board has to open from a file:// URL on a
machine that has none of them installed — so a shared library would have to be
a fourth thing to install. Keep the copies byte-identical and change them
together; everything project-specific belongs in the caller that builds the
`Section`s, not in here.

THE RULE THIS FILE EXISTS TO ENFORCE
------------------------------------
A tag is advisory. It filters; it never gates. Concretely, three properties
that every change here must preserve:

1. A row with a missing, empty or unrecognised value for a facet is STILL IN
   THE DATA. It is reachable under the facet's "(untagged)" option and it is
   visible whenever that facet is set to "All" — which is the default. There is
   no code path in which an absent tag removes a row.
2. A missing value renders as "—". It is never filled in, guessed, or inferred
   from a sibling field.
3. Every view states "showing N of M". An over-narrow filter has to be legible
   as a filter rather than as an empty dataset — that ambiguity ("nothing
   there" vs "I could not look") is the bug this whole pipeline was rebuilt to
   delete, and it would be trivially easy to recreate in a UI.

Facet OPTIONS are derived from the rows at view time, not declared here. That
is what lets a different reader — one who wants design and art roles — get
useful dropdowns out of the same generator with no code change: the options are
whatever the data actually contains.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Shown in a facet dropdown for rows whose value is missing/empty, and as the
# cell text for any missing value. One token for both so the reader can connect
# the option to the cell without a legend.
UNTAGGED = "—"


@dataclass(frozen=True)
class Column:
    """One rendered column.

    `kind` chooses the cell renderer:
      "text"  — plain string
      "link"  — anchor; `href_key` names the row field holding the URL
      "date"  — ISO date string, rendered as-is plus a relative-days hint
      "tags"  — a list of small pills; the row value may be a list or a scalar
    """

    key: str
    label: str
    kind: str = "text"
    href_key: str | None = None


@dataclass(frozen=True)
class Facet:
    """One dropdown. `key` is the row field; options come from the data."""

    key: str
    label: str


@dataclass(frozen=True)
class Sort:
    """One sort option. `kind` is "date" (ISO strings) or "text"."""

    key: str
    label: str
    kind: str = "text"
    ascending: bool = True


@dataclass
class Section:
    """One tab.

    `available=False` is NOT the same as `rows=[]`, and conflating them is the
    exact failure this codebase keeps removing. An empty list means "I read the
    register and it had no rows"; unavailable means "I could not read it at
    all", and the tab says so instead of showing a confident zero. `note`
    carries the explanation either way.
    """

    key: str
    label: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)
    sorts: list[Sort] = field(default_factory=list)
    search_keys: list[str] = field(default_factory=list)
    empty_text: str = "Nothing here."
    note: str | None = None
    available: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "rows": self.rows if self.available else [],
            "columns": [
                {"key": c.key, "label": c.label, "kind": c.kind, "hrefKey": c.href_key}
                for c in self.columns
            ],
            "facets": [{"key": f.key, "label": f.label} for f in self.facets],
            "sorts": [
                {"key": s.key, "label": s.label, "kind": s.kind, "asc": s.ascending}
                for s in self.sorts
            ],
            "searchKeys": list(self.search_keys),
            "emptyText": self.empty_text,
            "note": self.note,
            "available": self.available,
        }


def _embed(payload: dict[str, Any]) -> str:
    """JSON, escaped so it cannot terminate the <script> element that holds it.

    `<`, `>` and `&` never occur in JSON syntax outside string literals, so
    replacing them with their \\u escapes leaves the document semantically
    identical while making `</script>` unrepresentable in the output.
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #16181d; --muted: #6b7280; --line: #e3e5ea;
  --chip: #f1f2f5; --accent: #2b5cd9; --card: #fbfbfc;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161a; --fg:#e8eaee; --muted:#9aa1ad; --line:#2a2e36;
          --chip:#22262e; --accent:#7aa2f7; --card:#1a1d23; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 20px 64px; background:var(--bg); color:var(--fg);
       font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
h1 { font-size:20px; margin:0 0 2px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:18px; }
.tabs { display:flex; gap:6px; border-bottom:1px solid var(--line); margin-bottom:14px; }
.tab { appearance:none; background:none; border:0; border-bottom:2px solid transparent;
       padding:8px 12px; font:inherit; color:var(--muted); cursor:pointer; }
.tab[aria-selected="true"] { color:var(--fg); border-bottom-color:var(--accent); font-weight:600; }
.controls { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; margin-bottom:10px; }
.ctl { display:flex; flex-direction:column; gap:3px; }
.ctl label { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
select, input[type=search] { font:inherit; padding:5px 8px; border:1px solid var(--line);
       border-radius:6px; background:var(--card); color:var(--fg); min-width:150px; }
button.reset { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:6px;
       background:var(--card); color:var(--fg); cursor:pointer; }
.count { color:var(--muted); font-size:13px; margin:6px 0 10px; }
.note { border-left:3px solid var(--accent); background:var(--card); padding:8px 12px;
        margin:10px 0; font-size:13px; color:var(--muted); border-radius:0 6px 6px 0; }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { position:sticky; top:0; background:var(--card); font-size:12px; text-transform:uppercase;
     letter-spacing:.04em; color:var(--muted); white-space:nowrap; }
tr:last-child td { border-bottom:0; }
td.t-title { min-width:260px; }
a { color:var(--accent); }
.pill { display:inline-block; background:var(--chip); border-radius:99px; padding:1px 8px;
        margin:1px 3px 1px 0; font-size:12px; color:var(--fg); white-space:nowrap; }
.muted { color:var(--muted); }
.empty { padding:22px; text-align:center; color:var(--muted); }
footer { margin-top:26px; color:var(--muted); font-size:12px; }
"""

# The whole interaction layer. Vanilla, no framework, no network.
_JS = r"""
(function () {
  var DATA = JSON.parse(document.getElementById("board-data").textContent);
  var UNTAGGED = DATA.untagged;
  var state = {};

  function txt(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "boolean") return v ? "yes" : "no";
    return String(v);
  }
  // The single definition of "this row has no value here". Empty string,
  // null and undefined all mean the same thing: nothing was recorded. They
  // must never mean "excluded".
  function missing(v) { return v === null || v === undefined || txt(v).trim() === ""; }
  function facetValue(row, key) { return missing(row[key]) ? UNTAGGED : txt(row[key]); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function optionsFor(rows, key) {
    // Derived from the DATA, never declared: a board built for someone with a
    // different tag vocabulary gets that vocabulary in its dropdowns.
    var seen = Object.create(null), out = [], hasUntagged = false;
    rows.forEach(function (r) {
      if (missing(r[key])) { hasUntagged = true; return; }
      var v = txt(r[key]);
      if (!seen[v]) { seen[v] = 1; out.push(v); }
    });
    out.sort(function (a, b) { return a.localeCompare(b); });
    if (hasUntagged) out.push(UNTAGGED);
    return out;
  }

  function matches(row, section, st) {
    for (var i = 0; i < section.facets.length; i++) {
      var f = section.facets[i], want = st.facets[f.key];
      if (!want) continue;               // "All" — never hides anything
      if (facetValue(row, f.key) !== want) return false;
    }
    var q = (st.q || "").trim().toLowerCase();
    if (!q) return true;
    for (var j = 0; j < section.searchKeys.length; j++) {
      if (txt(row[section.searchKeys[j]]).toLowerCase().indexOf(q) !== -1) return true;
    }
    return false;
  }

  function sortRows(rows, sort, dir) {
    if (!sort) return rows;
    var mult = dir === "desc" ? -1 : 1;
    return rows.slice().sort(function (a, b) {
      var av = a[sort.key], bv = b[sort.key];
      var am = missing(av), bm = missing(bv);
      // A missing sort key sinks to the bottom in BOTH directions. Reversing
      // the sort must not promote the rows we know least about to the top.
      if (am && bm) return 0;
      if (am) return 1;
      if (bm) return -1;
      var x = txt(av), y = txt(bv);
      if (sort.kind === "date") return (x < y ? -1 : x > y ? 1 : 0) * mult;
      return x.localeCompare(y) * mult;
    });
  }

  // Only these schemes may become a clickable href. Leading control
  // characters and whitespace are stripped first, because a browser ignores
  // them when resolving the URL and "\u0001javascript:x" would otherwise pass
  // a naive prefix test. A relative or scheme-less value is refused too: on a
  // file:// page it resolves against the local filesystem, not a job board.
  function safeHref(value) {
    var v = value.replace(/[\u0000-\u0020]/g, "").toLowerCase();
    return v.indexOf("http://") === 0 || v.indexOf("https://") === 0 || v.indexOf("mailto:") === 0;
  }

  function cell(row, col) {
    var td = el("td", "t-" + col.key);
    var raw = row[col.key];
    if (col.kind === "tags") {
      var vals = Array.isArray(raw) ? raw.filter(function (v) { return !missing(v); }) : (missing(raw) ? [] : [raw]);
      if (!vals.length) { td.appendChild(el("span", "muted", UNTAGGED)); return td; }
      vals.forEach(function (v) { td.appendChild(el("span", "pill", txt(v))); });
      return td;
    }
    if (missing(raw)) { td.appendChild(el("span", "muted", UNTAGGED)); return td; }
    if (col.kind === "link") {
      var href = col.hrefKey ? row[col.hrefKey] : raw;
      // SCHEME WHITELIST. Everything else on this page is textContent and the
      // JSON is escaped so it cannot close its own <script>, but an href is
      // the one place a value from the data becomes executable: a scraped
      // "javascript:..." apply_url would be a click away from running in a
      // file:// page, which is the most privileged context this file is ever
      // opened in. Anything not http/https/mailto renders as plain text, so
      // the row is still visible and still says what it says.
      if (missing(href) || !safeHref(txt(href))) {
        td.appendChild(document.createTextNode(txt(raw)));
        return td;
      }
      var a = el("a", null, txt(raw));
      a.href = txt(href); a.target = "_blank"; a.rel = "noopener noreferrer";
      td.appendChild(a);
      return td;
    }
    td.appendChild(document.createTextNode(txt(raw)));
    return td;
  }

  function render(section) {
    var st = state[section.key];
    var host = document.getElementById("panel-" + section.key);
    host.textContent = "";

    if (section.note) host.appendChild(el("div", "note", section.note));

    if (!section.available) {
      host.appendChild(el("div", "empty", "No data could be read for this tab."));
      return;
    }

    var controls = el("div", "controls");
    section.facets.forEach(function (f) {
      var box = el("div", "ctl");
      var lab = el("label", null, f.label);
      lab.htmlFor = section.key + "-" + f.key;
      var sel = el("select");
      sel.id = section.key + "-" + f.key;
      var opts = optionsFor(section.rows, f.key);
      var all = el("option", null, "All");
      all.value = "";
      sel.appendChild(all);
      opts.forEach(function (v) {
        var o = el("option", null, v === UNTAGGED ? UNTAGGED + " (untagged)" : v);
        o.value = v;
        sel.appendChild(o);
      });
      sel.value = st.facets[f.key] || "";
      sel.addEventListener("change", function () {
        st.facets[f.key] = sel.value;
        render(section);
      });
      box.appendChild(lab); box.appendChild(sel);
      controls.appendChild(box);
    });

    if (section.sorts.length) {
      var sbox = el("div", "ctl");
      var slab = el("label", null, "Sort by");
      slab.htmlFor = section.key + "-sort";
      var ssel = el("select");
      ssel.id = section.key + "-sort";
      section.sorts.forEach(function (s, i) {
        var o = el("option", null, s.label);
        o.value = String(i);
        ssel.appendChild(o);
      });
      ssel.value = String(st.sort);
      ssel.addEventListener("change", function () {
        st.sort = parseInt(ssel.value, 10);
        st.dir = section.sorts[st.sort].asc ? "asc" : "desc";
        render(section);
      });
      sbox.appendChild(slab); sbox.appendChild(ssel);
      controls.appendChild(sbox);

      var dbox = el("div", "ctl");
      var dlab = el("label", null, "Order");
      dlab.htmlFor = section.key + "-dir";
      var dsel = el("select");
      dsel.id = section.key + "-dir";
      [["asc", "Ascending"], ["desc", "Descending"]].forEach(function (p) {
        var o = el("option", null, p[1]); o.value = p[0]; dsel.appendChild(o);
      });
      dsel.value = st.dir;
      dsel.addEventListener("change", function () { st.dir = dsel.value; render(section); });
      dbox.appendChild(dlab); dbox.appendChild(dsel);
      controls.appendChild(dbox);
    }

    if (section.searchKeys.length) {
      var qbox = el("div", "ctl");
      var qlab = el("label", null, "Search");
      qlab.htmlFor = section.key + "-q";
      var q = el("input");
      q.type = "search"; q.id = section.key + "-q"; q.value = st.q || "";
      q.placeholder = "employer, title, anything";
      q.addEventListener("input", function () { st.q = q.value; render(section); });
      qbox.appendChild(qlab); qbox.appendChild(q);
      controls.appendChild(qbox);
    }

    var reset = el("button", "reset", "Reset filters");
    reset.type = "button";
    reset.addEventListener("click", function () {
      st.facets = {}; st.q = "";
      render(section);
    });
    controls.appendChild(reset);
    host.appendChild(controls);

    var shown = section.rows.filter(function (r) { return matches(r, section, st); });
    var sorted = sortRows(shown, section.sorts[st.sort], st.dir);

    // Always printed, including when the two numbers are equal. "showing 0 of
    // 233" and "showing 0 of 0" are different facts and the reader needs to be
    // able to tell them apart at a glance.
    host.appendChild(el("div", "count",
      "showing " + sorted.length + " of " + section.rows.length));

    if (!sorted.length) {
      host.appendChild(el("div", "empty",
        section.rows.length ? "No rows match these filters." : section.emptyText));
      return;
    }

    var wrap = el("div", "wrap");
    var table = el("table");
    var thead = el("thead"), htr = el("tr");
    section.columns.forEach(function (c) { htr.appendChild(el("th", null, c.label)); });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = el("tbody");
    sorted.forEach(function (row) {
      var tr = el("tr");
      section.columns.forEach(function (c) { tr.appendChild(cell(row, c)); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
  }

  function select(key) {
    DATA.sections.forEach(function (s) {
      var on = s.key === key;
      document.getElementById("tab-" + s.key).setAttribute("aria-selected", on ? "true" : "false");
      document.getElementById("panel-" + s.key).hidden = !on;
    });
  }

  DATA.sections.forEach(function (s) {
    state[s.key] = { facets: {}, q: "", sort: 0, dir: s.sorts.length && !s.sorts[0].asc ? "desc" : "asc" };
    document.getElementById("tab-" + s.key).addEventListener("click", function () { select(s.key); });
    render(s);
  });
  if (DATA.sections.length) select(DATA.sections[0].key);
})();
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_board(
    sections: list[Section],
    *,
    generated_on: date,
    title: str = "Board",
    subtitle: str = "",
    footer: str = "",
) -> str:
    """Return ONE self-contained HTML document. No CDN, no build step, no network."""
    payload = {
        "untagged": UNTAGGED,
        "sections": [s.to_payload() for s in sections],
    }
    tabs = "".join(
        f'<button class="tab" role="tab" id="tab-{_escape(s.key)}" '
        f'aria-selected="false" type="button">{_escape(s.label)}</button>'
        for s in sections
    )
    panels = "".join(
        f'<div class="panel" role="tabpanel" id="panel-{_escape(s.key)}" hidden></div>'
        for s in sections
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{_escape(title)}</h1>
<div class="sub">{_escape(subtitle)}Generated {generated_on.isoformat()} · filters are advisory — nothing is hidden because a tag is missing.</div>
<div class="tabs" role="tablist">{tabs}</div>
{panels}
<footer>{_escape(footer)}</footer>
<script type="application/json" id="board-data">{_embed(payload)}</script>
<script>{_JS}</script>
</body>
</html>
"""
