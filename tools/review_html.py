"""Render plan/review.md as a self-contained, readable HTML page (plan/review.html).

Usage: py tools/review_html.py [--src plan/review.md] [--out plan/review.html]
No dependencies; the converter handles only the Markdown subset review.md uses.
"""
import argparse
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/suzyvaque/ms-fabric-energy-lab/blob/main/"

FIELDS = {  # label prefix -> (css class, display label); dict order = display order in each card
    "Task": ("task", "Task"),
    "Goal": ("task", "Task"),
    "Actions": ("actions", "Actions"),
    "Blockers": ("blockers", "Blockers → solutions"),
    "Results": ("results", "Results"),
    "End state": ("results", "Results"),
    "Insight": ("insights", "Insights"),
    "Check in UI": ("ui", "Check in UI"),
    "Check in repo": ("repo", "Check in repo"),
    "Concepts to understand": ("concepts", "Concepts"),
}


def slug(text):
    s = re.sub(r"<[^>]+>", "", text).lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s", "-", s.strip())


def link_target(url):
    if url.startswith(("http://", "https://", "#", "mailto:")):
        return url
    path, _, anchor = url.partition("#")
    resolved = (ROOT / "plan" / path).resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return url
    if rel in ("plan/review.md", "plan/review.html"):
        return "#" + anchor if anchor else "#"
    return REPO_URL + rel + ("#" + anchor if anchor else "")


def inline(text):
    codes = []

    def keep(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  lambda m: f'<a href="{html.escape(link_target(m.group(2)))}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def render_list(items, ordered=False):
    """items: list of (text, children[])"""
    tag = "ol" if ordered else "ul"
    out = [f"<{tag}>"]
    for text, children in items:
        sub = render_list([(c, []) for c in children]) if children else ""
        out.append(f"<li>{inline(text)}{sub}</li>")
    out.append(f"</{tag}>")
    return "".join(out)


def parse_list(lines, i):
    """Parse a top-level list starting at lines[i]; return (items, ordered, next_i)."""
    ordered = bool(re.match(r"\d+\. ", lines[i]))
    items = []
    while i < len(lines):
        line = lines[i]
        m = re.match(r"(?:- |\d+\. )(.*)", line)
        if m:
            items.append((m.group(1), []))
        elif line.startswith("  ") and line.strip().startswith("- ") and items:
            items[-1][1].append(line.strip()[2:])
        else:
            break
        i += 1
    return items, ordered, i


def render_table(rows, kind_col=None):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], cells[2:]
    out = ['<div class="table-wrap"><table><thead><tr>']
    out += [f"<th>{inline(h)}</th>" for h in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>")
        for j, c in enumerate(r):
            if j == kind_col:
                out.append(f'<td><span class="chip kind-{re.match(r"[A-Za-z]*", c).group(0).lower()}">{inline(c)}</span></td>')
            elif j == len(r) - 1 and kind_col is not None:
                out.append(f'<td class="nowrap">{inline(c)}</td>')
            else:
                out.append(f"<td>{inline(c)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_blocks(lines):
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip() == "---":
            i += 1
        elif line.startswith("```"):
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            out.append(f'<pre class="diagram">{html.escape(chr(10).join(lines[i + 1:j]))}</pre>')
            i = j + 1
        elif line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            head = [c.strip() for c in lines[i].strip("|").split("|")]
            out.append(render_table(lines[i:j], head.index("Kind") if "Kind" in head else None))
            i = j
        elif re.match(r"(- |\d+\. )", line):
            items, ordered, i = parse_list(lines, i)
            out.append(render_list(items, ordered))
        else:
            out.append(f"<p>{inline(line)}</p>")
            i += 1
    return "\n".join(out)


def field_key(label):
    for k in FIELDS:
        if label.startswith(k):
            return k
    return None


def render_step(title, lines):
    m = re.match(r"(Step \w+): (.*)", title)
    num, rest = m.group(1), m.group(2)
    parts = [p.strip() for p in rest.split("·")]
    name, meta = parts[0], parts[1:]
    added = "(added" in name
    name = re.sub(r"\s*\(added[^)]*\)", "", name)
    chips = "".join(f'<span class="chip meta">{inline(p)}</span>' for p in meta)
    if added:
        chips = '<span class="chip added">added step</span>' + chips
    sid = slug(title)
    out = [f'<section class="step" id="{sid}"><header><span class="step-num">{num.replace("Step ", "")}</span>'
           f'<div><h3>{inline(name)}</h3><div class="chips">{chips}</div></div></header>']
    items, note, i = [], [], 0
    while i < len(lines):
        if lines[i].startswith("- "):
            lst, _, i = parse_list(lines, i)
            items += lst
        else:
            if lines[i].strip():
                note.append(lines[i])
            i += 1
    if note:
        out.append(f'<p class="note">{inline(" ".join(note))}</p>')
    out.append('<dl class="fields">')
    order = list(dict.fromkeys(c for c, _ in FIELDS.values()))

    def rank(item):
        fm = re.match(r"\*\*([^*]+?):\*\*", item[0])
        key = field_key(fm.group(1)) if fm else None
        return order.index(FIELDS[key][0]) if key else len(order)

    for text, children in sorted(items, key=rank):
        fm = re.match(r"\*\*([^*]+?):\*\*\s*(.*)", text)
        key = field_key(fm.group(1)) if fm else None
        if not key:
            out.append(f"<dt></dt><dd>{inline(text)}</dd>")
            continue
        cls, label = FIELDS[key]
        body = fm.group(2)
        body = body[:1].upper() + body[1:]
        if key == "Concepts to understand":
            content = '<div class="concepts">' + "".join(
                f'<span class="concept">{inline(c.strip().rstrip("."))}</span>' for c in body.split(" · ")) + "</div>"
        else:
            content = (f"<p>{inline(body)}</p>" if body else "") + (render_list([(c, []) for c in children]) if children else "")
        out.append(f'<dt class="f-{cls}">{label}</dt><dd class="f-{cls}">{content}</dd>')
    out.append("</dl></section>")
    first_task = next((re.sub(r"\*\*[^*]+:\*\*\s*", "", t) for t, _ in items if field_key(t[2:].split(":")[0]) in ("Task", "Goal")), "")
    first_task = first_task[:1].upper() + first_task[1:]
    return "\n".join(out), (num, name, meta, sid, first_task, added)


def build(src):
    md = src.read_text(encoding="utf-8").splitlines()
    title = md[0].lstrip("# ").strip()
    cur = ["__intro__", []]
    sections = [cur]
    for line in md[1:]:
        if line.startswith("## "):
            cur = [line[3:].strip(), []]
            sections.append(cur)
        else:
            cur[1].append(line)

    nav, body, overview_rows = [], [], []
    paras = [l for l in sections[0][1] if l.strip() and not l.startswith("Contents:") and l.strip() != "---"]
    intro_html = "".join(
        f'<p class="{"disclaimer" if p.startswith("Sample") else "lead"}">{inline(p)}</p>' for p in paras)

    for name, lines in sections:
        if name == "__intro__":
            continue
        sid = slug(name)
        if name == "Step by step":
            steps, curstep = [], None
            for line in lines:
                if line.startswith("### "):
                    curstep = [line[4:].strip(), []]
                    steps.append(curstep)
                elif curstep:
                    curstep[1].append(line)
            cards, sub = [], []
            for st_title, st_lines in steps:
                card, info = render_step(st_title, st_lines)
                cards.append(card)
                num, sname, meta, ssid, task, added = info
                sub.append(f'<li><a href="#{ssid}" title="{html.escape(sname)}"><span class="n">{num.replace("Step ", "")}</span><span class="t">{inline(sname)}</span></a></li>')
                added_chip = ' <span class="chip added">added</span>' if added else ""
                overview_rows.append(
                    f'<tr><td><a href="#{ssid}"><strong>{num.replace("Step ", "")}</strong></a></td><td>{inline(sname)}'
                    f'{added_chip}</td>'
                    f'<td class="muted">{inline(" · ".join(meta))}</td><td>{inline(task)}</td></tr>')
            nav.append(f'<li><a href="#{sid}">Steps 0–12</a><ul class="sub">{"".join(sub)}</ul></li>')
            overview = ('<div class="table-wrap"><table class="overview"><thead><tr><th>Step</th><th>Name</th>'
                        f'<th>Tracking</th><th>Task</th></tr></thead><tbody>{"".join(overview_rows)}</tbody></table></div>')
            legend = "".join(f'<span class="legend f-{c}">{l}</span>'
                             for c, l in dict.fromkeys(FIELDS.values()))
            body.append(f'<section id="{sid}"><h2>Step by step</h2><h4 class="kicker">Overview</h4>{overview}'
                        f'<div class="legend-row">Each step card: {legend}</div>{"".join(cards)}</section>')
        else:
            nav.append(f'<li><a href="#{sid}">{inline(name)}</a></li>')
            body.append(f'<section id="{sid}"><h2>{inline(name)}</h2>{render_blocks(lines)}</section>')

    return TEMPLATE.format(title=html.escape(title), intro=intro_html, nav="".join(nav), body="\n".join(body))


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<!-- Generated from plan/review.md by tools/review_html.py; edit the Markdown, then regenerate. -->
<style>
:root {{
  --bg:#f6f7f9; --card:#fff; --ink:#1f2328; --muted:#5b6470; --line:#e3e6ea; --accent:#0f6cbd;
  --task:#0f6cbd; --actions:#5b6470; --blockers:#c4314b; --results:#107c41; --insights:#8a3ffc;
  --ui:#0e7a8a; --repo:#a15c00; --concepts:#3b4cc0;
}}
* {{ box-sizing:border-box; }}
html {{ scroll-behavior:smooth; scroll-padding-top:16px; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 "Segoe UI",system-ui,-apple-system,sans-serif; }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
code {{ font:0.86em/1.4 Consolas,"Cascadia Mono",monospace; background:#eef1f4; padding:1px 5px; border-radius:4px; word-break:break-word; }}
.layout {{ display:grid; grid-template-columns:270px minmax(0,1fr); max-width:1400px; margin:0 auto; }}
nav {{ position:sticky; top:0; height:100vh; overflow-y:auto; padding:24px 16px; border-right:1px solid var(--line); background:#fbfcfd; font-size:14px; }}
nav .brand {{ font-weight:600; font-size:15px; margin-bottom:16px; }}
nav ul {{ list-style:none; margin:0; padding:0; }}
nav li {{ margin:2px 0; }}
nav a {{ display:block; padding:4px 8px; border-radius:6px; color:var(--ink); }}
nav a:hover {{ background:#eef1f4; text-decoration:none; }}
nav ul.sub a {{ display:flex; gap:8px; padding-left:16px; color:var(--muted); font-size:13px; }}
nav ul.sub .n {{ min-width:22px; font-weight:600; color:var(--accent); }}
main {{ padding:32px 40px 80px; }}
h1 {{ font-size:28px; margin:0 0 12px; }}
h2 {{ font-size:22px; margin:48px 0 16px; padding-bottom:8px; border-bottom:2px solid var(--line); }}
h3 {{ font-size:18px; margin:0; }}
.kicker {{ text-transform:uppercase; letter-spacing:.06em; font-size:12px; color:var(--muted); margin:0 0 8px; }}
.lead {{ font-size:16px; color:var(--muted); max-width:900px; }}
.disclaimer {{ border-left:4px solid #d29200; background:#fff8e6; padding:10px 14px; border-radius:6px; margin:16px 0; max-width:900px; }}
.table-wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; margin:12px 0 20px; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th, td {{ text-align:left; vertical-align:top; padding:8px 12px; border-bottom:1px solid var(--line); }}
th {{ background:#f0f2f5; font-weight:600; position:sticky; top:0; }}
tbody tr:nth-child(even) {{ background:#fafbfc; }}
tbody tr:last-child td {{ border-bottom:none; }}
td.muted {{ color:var(--muted); white-space:nowrap; font-size:13px; }}
pre.diagram {{ background:#1f2328; color:#e6edf3; padding:16px; border-radius:10px; overflow-x:auto; font:13px/1.45 Consolas,"Cascadia Mono",monospace; }}
section > ul, section > ol, section > p {{ max-width:1000px; }}
section > ol > li, section > ul > li {{ margin:6px 0; }}
.step {{ background:var(--card); border:1px solid var(--line); border-radius:12px; margin:20px 0; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
.step header {{ display:flex; gap:14px; align-items:center; padding:14px 20px; border-bottom:1px solid var(--line); background:linear-gradient(#fbfcfd,#f5f7f9); border-radius:12px 12px 0 0; }}
.step-num {{ flex:none; display:grid; place-items:center; min-width:44px; height:44px; padding:0 8px; border-radius:10px; background:var(--accent); color:#fff; font-weight:700; font-size:17px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }}
.chip {{ display:inline-block; font-size:12px; padding:1px 8px; border-radius:999px; background:#eef1f4; color:var(--muted); white-space:nowrap; }}
.chip.added {{ background:#efe6ff; color:#6b2fd6; }}
.note {{ margin:12px 20px 0; padding:8px 12px; background:#f3f6fb; border-radius:6px; color:var(--muted); font-size:14px; }}
dl.fields {{ display:grid; grid-template-columns:170px minmax(0,1fr); margin:0; padding:8px 20px 16px; }}
dl.fields dt {{ font-weight:600; font-size:13px; padding:12px 12px 12px 0; border-top:1px solid var(--line); }}
dl.fields dd {{ margin:0; padding:10px 0; border-top:1px solid var(--line); }}
dl.fields dt:first-of-type, dl.fields dd:first-of-type {{ border-top:none; }}
dl.fields dt::before {{ content:""; display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px; background:currentColor; }}
dl.fields dd p {{ margin:2px 0; }}
dl.fields dd ul {{ margin:4px 0; padding-left:20px; }}
dl.fields dd li {{ margin:3px 0; }}
.f-task {{ color:var(--task); }} .f-actions {{ color:var(--actions); }} .f-blockers {{ color:var(--blockers); }}
.f-results {{ color:var(--results); }} .f-insights {{ color:var(--insights); }} .f-ui {{ color:var(--ui); }}
.f-repo {{ color:var(--repo); }} .f-concepts {{ color:var(--concepts); }}
dl.fields dd {{ color:var(--ink); }}
dd.f-blockers, dd.f-insights {{ padding-left:12px; border-radius:6px; }}
dd.f-blockers {{ background:#fdf5f6; }}
dd.f-insights {{ background:#f8f4ff; }}
.concepts {{ display:flex; flex-wrap:wrap; gap:6px; }}
.concept {{ font-size:13px; padding:3px 10px; border-radius:6px; background:#eef0fb; border:1px solid #dde1f6; }}
.legend-row {{ font-size:13px; color:var(--muted); margin:8px 0 4px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
.legend {{ font-weight:600; }} .legend::before {{ content:"● "; }}
.kind-tenant, .kind-network, .kind-identity {{ background:#fff4e0; color:#8a5300; }}
.kind-product, .kind-undocumented {{ background:#e8f1fb; color:#0f5aa0; }}
.kind-our {{ background:#fdeaea; color:#a4262c; }}
.kind-capacity {{ background:#e9f6ee; color:#0b6a32; }}
.kind-licensing, .kind-platform {{ background:#f1eafd; color:#6b2fd6; }}
td.nowrap {{ white-space:nowrap; }}
.kind-environment {{ background:#eef1f4; color:#5b6470; }}
footer {{ margin-top:48px; color:var(--muted); font-size:13px; }}
@media (max-width:900px) {{
  .layout {{ grid-template-columns:minmax(0,1fr); }} nav {{ position:static; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
  nav ul.sub {{ display:flex; flex-wrap:wrap; gap:2px; }} nav ul.sub a {{ padding:2px 6px; }} nav ul.sub a span + .t {{ display:none; }}
  nav > ul {{ display:flex; flex-wrap:wrap; gap:4px; }} nav > ul > li {{ flex:1 1 100%; }}
  main {{ padding:20px; }} dl.fields {{ grid-template-columns:1fr; }} dl.fields dd {{ border-top:none; padding-top:0; }}
}}
@media print {{
  nav {{ display:none; }} .layout {{ display:block; }} body {{ background:#fff; }}
  .step {{ break-inside:avoid-page; box-shadow:none; }} a {{ color:inherit; }}
}}
</style>
</head>
<body>
<div class="layout">
<nav><div class="brand">Contoso Energy · Fabric lab review</div><ul>{nav}</ul></nav>
<main>
<h1>{title}</h1>
{intro}
{body}
<footer>Generated from <code>plan/review.md</code> by <code>tools/review_html.py</code>. Sample, not production guidance. Synthetic data.</footer>
</main>
</div>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=str(ROOT / "plan" / "review.md"))
    ap.add_argument("--out", default=str(ROOT / "plan" / "review.html"))
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.write_text(build(Path(a.src)), encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out.name}")


if __name__ == "__main__":
    main()
