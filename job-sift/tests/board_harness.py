"""Drive the emitted board's JavaScript in Node and assert on what it RENDERS.

WHY THIS EXISTS. The board's three load-bearing invariants — an untagged row is
never hidden, a missing value renders "—", every view states "showing N of M" —
were previously asserted by grepping the emitted JavaScript for source
fragments. Those assertions pass if the JavaScript is syntactically broken,
which makes them a test of the string literal rather than of the behaviour, and
the behaviour is the entire deliverable: this file is the thing a reader opens.

A DOM shim in Node rather than a browser driver: the page uses no layout, no
CSS-dependent behaviour and no async work, so a few dozen lines of
createElement/appendChild are enough to run it faithfully, and it adds no test
dependency beyond `node` (skipped when absent).

`board_html.py` is byte-identical across job-sift, hk-events and the standalone
scraper, so this harness is copied to each of them and each suite protects its
own copy — the file is duplicated precisely because there is no shared package
to put it in.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_SHIM = textwrap.dedent(r"""
    const fs = require('fs');
    const html = fs.readFileSync(process.argv[2], 'utf8');
    const actions = JSON.parse(process.argv[3]);

    const dataMatch = html.match(/<script type="application\/json" id="board-data">([\s\S]*?)<\/script>/);
    if (!dataMatch) { console.error("no embedded data block"); process.exit(2); }
    const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
    if (!scriptMatch) { console.error("no behaviour script"); process.exit(2); }

    function mkNode(tag) {
      return {
        tagName: tag, className: '', children: [], attrs: {}, _text: '', style: {},
        hidden: false, id: '', value: '', type: '', htmlFor: '', href: '',
        placeholder: '', rel: '', target: '', _listeners: {},
        set textContent(v) { this._text = v; this.children = []; },
        get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
        appendChild(c) { this.children.push(c); return c; },
        addEventListener(kind, fn) { (this._listeners[kind] ||= []).push(fn); },
        fire(kind) { (this._listeners[kind] || []).forEach(fn => fn()); },
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k]; },
      };
    }
    const registry = {};
    const dataNode = mkNode('script'); dataNode._text = dataMatch[1];
    registry['board-data'] = dataNode;
    global.document = {
      createElement: mkNode,
      createTextNode: (t) => { const n = mkNode('#text'); n._text = t; return n; },
      getElementById: (id) => registry[id] || (registry[id] = mkNode('div')),
    };

    eval(scriptMatch[1]);

    function walk(n, out) { out.push(n); n.children.forEach(c => walk(c, out)); return out; }
    function panel(key) { return walk(registry['panel-' + key], []); }
    function find(key, pred) { return panel(key).filter(pred); }

    // Replay the requested interactions, then report what the DOM holds.
    actions.forEach(a => {
      const nodes = panel(a.section);
      if (a.type === 'select') {
        const sel = nodes.find(n => n.tagName === 'select' && n.id === a.section + '-' + a.key);
        if (!sel) { console.error('no such control: ' + a.key); process.exit(3); }
        sel.value = a.value;
        sel.fire('change');
      } else if (a.type === 'search') {
        const box = nodes.find(n => n.tagName === 'input' && n.id === a.section + '-q');
        box.value = a.value;
        box.fire('input');
      } else if (a.type === 'reset') {
        nodes.find(n => n.className === 'reset').fire('click');
      } else if (a.type === 'tab') {
        registry['tab-' + a.section].fire('click');
      }
    });

    const report = {};
    JSON.parse(dataMatch[1]).sections.forEach(s => {
      const nodes = panel(s.key);
      const bodyRows = nodes.filter(n => n.tagName === 'tr').length;
      report[s.key] = {
        count: (nodes.find(n => n.className === 'count') || {}).textContent || null,
        rows: Math.max(0, bodyRows - (bodyRows ? 1 : 0)),  // minus the header row
        cells: nodes.filter(n => n.tagName === 'td').map(n => n.textContent),
        pills: nodes.filter(n => n.className === 'pill').map(n => n.textContent),
        links: nodes.filter(n => n.tagName === 'a').map(n => ({text: n.textContent, href: n.href})),
        options: Object.fromEntries(
          nodes.filter(n => n.tagName === 'select')
               .map(n => [n.id.replace(s.key + '-', ''), n.children.map(o => o.textContent)])
        ),
        empty: (nodes.find(n => n.className === 'empty') || {}).textContent || null,
        note: (nodes.find(n => n.className === 'note') || {}).textContent || null,
        hidden: registry['panel-' + s.key].hidden,
      };
    });
    process.stdout.write(JSON.stringify(report));
""")


def render_in_node(tmp_path: Path, html: str, actions: list[dict] | None = None) -> dict:
    """Run the page's own JS over `html` and return what it put in the DOM."""
    page = tmp_path / "board.html"
    page.write_text(html)
    shim = tmp_path / "shim.js"
    shim.write_text(_SHIM)
    proc = subprocess.run(
        ["node", str(shim), str(page), json.dumps(actions or [])],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"the board's own JavaScript failed to run (exit {proc.returncode}):\n"
            f"{proc.stderr[:2000]}"
        )
    return json.loads(proc.stdout)
