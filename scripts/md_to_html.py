"""
Render a docs/*.md file to artifact-ready HTML.

Emits the same visual system as the published design doc: IBM Plex Sans for
headings, Source Serif 4 for prose, IBM Plex Mono for data, a teal/amber palette
derived from the subject's heating/cooling axis, and a sticky section rail built
from the document's H2s. Theme-aware for light, dark and system settings.

Handles the same Markdown subset as md_to_docx.py, so all three output formats
stay generated from one source.

Usage:
    python scripts/md_to_html.py docs/INTERNALS.md build/internals.html "Page Title"
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

STYLE = """
  :root {
    --ground:#F1F4F4; --surface:#FDFEFE; --surface-alt:#E7ECEC;
    --ink:#131B1E; --ink-muted:#566A6E; --ink-faint:#8B9C9F;
    --rule:#CBD6D7; --rule-strong:#A8B8BA;
    --accent:#0E5D6E; --accent-soft:#DCEAED;
    --warm:#A9541F; --warm-soft:#F4E4D8;
    --good:#2C6E4F; --critical:#98302E;
    --font-sans:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
    --font-serif:"Source Serif 4",Georgia,"Times New Roman",serif;
    --font-mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    --measure:70ch; --rail:250px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:#0D1315; --surface:#151E21; --surface-alt:#1D282B;
      --ink:#DFE7E7; --ink-muted:#9AACAF; --ink-faint:#6B7E81;
      --rule:#2A3739; --rule-strong:#3D4E51;
      --accent:#4EB2C5; --accent-soft:#12333B;
      --warm:#D2854C; --warm-soft:#332114;
      --good:#5FAF87; --critical:#D4726F;
    }
  }
  :root[data-theme="dark"] {
    --ground:#0D1315; --surface:#151E21; --surface-alt:#1D282B;
    --ink:#DFE7E7; --ink-muted:#9AACAF; --ink-faint:#6B7E81;
    --rule:#2A3739; --rule-strong:#3D4E51;
    --accent:#4EB2C5; --accent-soft:#12333B;
    --warm:#D2854C; --warm-soft:#332114;
    --good:#5FAF87; --critical:#D4726F;
  }
  *{box-sizing:border-box}
  body{background:var(--ground);color:var(--ink);font-family:var(--font-serif);
       font-size:17px;line-height:1.65;margin:0;-webkit-font-smoothing:antialiased}
  .shell{display:grid;grid-template-columns:var(--rail) minmax(0,1fr);
         max-width:1280px;margin:0 auto}
  .rail{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;
        padding:44px 26px 44px 32px;border-right:1px solid var(--rule);
        font-family:var(--font-sans);font-size:13px}
  .rail-mark{font-family:var(--font-mono);font-size:10.5px;letter-spacing:.14em;
             text-transform:uppercase;color:var(--accent);margin-bottom:6px}
  .rail-title{font-weight:600;font-size:15px;line-height:1.35;margin:0 0 24px}
  .rail nav ol{list-style:none;margin:0;padding:0;display:flex;
               flex-direction:column;gap:2px}
  .rail nav a{display:flex;gap:9px;padding:5px 0;color:var(--ink-muted);
              text-decoration:none}
  .rail nav a .n{font-family:var(--font-mono);font-size:10.5px;
                 color:var(--ink-faint);padding-top:2px;flex:none}
  .rail nav a:hover,.rail nav a:focus-visible{color:var(--accent)}
  main{padding:44px 8px 120px 48px;min-width:0}
  .col{max-width:var(--measure)}
  h1,h2,h3,h4{font-family:var(--font-sans);text-wrap:balance;margin:0}
  h1{font-size:clamp(28px,4vw,40px);font-weight:600;letter-spacing:-.02em;
     line-height:1.12;padding-bottom:18px;border-bottom:2px solid var(--ink)}
  h2{font-size:24px;font-weight:600;letter-spacing:-.012em;line-height:1.2;
     margin-top:58px;padding-top:8px;border-top:2px solid var(--ink);
     margin-bottom:10px}
  h3{font-size:17.5px;font-weight:600;line-height:1.3;margin-top:30px;
     margin-bottom:6px}
  h4{font-size:14.5px;font-weight:600;margin-top:22px;margin-bottom:4px;
     font-family:var(--font-mono);color:var(--accent)}
  p{margin:0 0 14px}
  a{color:var(--accent);text-underline-offset:2px}
  strong{font-weight:600}
  em{font-style:italic}
  code{font-family:var(--font-mono);font-size:.855em;background:var(--surface-alt);
       padding:1px 5px;border-radius:3px}
  ul,ol{margin:0 0 16px;padding-left:22px;max-width:var(--measure)}
  li{margin-bottom:7px}
  blockquote{margin:0 0 18px;max-width:var(--measure);
             border-left:3px solid var(--accent);background:var(--accent-soft);
             padding:15px 18px;font-size:16px}
  blockquote p:last-child{margin-bottom:0}
  blockquote strong:first-child{color:var(--ink)}
  pre{margin:0 0 18px;background:var(--surface);border:1px solid var(--rule);
      border-radius:4px;padding:16px 18px;overflow-x:auto;
      font-family:var(--font-mono);font-size:12.5px;line-height:1.6}
  pre code{background:none;padding:0;font-size:inherit}
  .tbl-wrap{overflow-x:auto;border:1px solid var(--rule);border-radius:4px;
            background:var(--surface);margin:0 0 20px;max-width:940px}
  table{border-collapse:collapse;width:100%;font-family:var(--font-sans);
        font-size:13.5px}
  th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--rule);
        vertical-align:top}
  thead th{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
           color:var(--ink-muted);font-weight:600;background:var(--surface-alt);
           white-space:nowrap}
  tbody tr:last-child td{border-bottom:0}
  td code,th code{font-size:12px}
  figure{margin:0 0 22px;background:var(--surface);border:1px solid var(--rule);
         border-radius:4px;padding:20px;overflow-x:auto;max-width:940px}
  figure img{display:block;max-width:100%;height:auto;margin:0 auto}
  figcaption{font-family:var(--font-sans);font-size:13px;line-height:1.45;
             color:var(--ink-muted);margin-top:14px;padding-top:12px;
             border-top:1px solid var(--rule)}
  hr{border:0;border-top:1px solid var(--rule);margin:34px 0}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;
    transition:none!important}}
  @media (max-width:900px){
    .shell{grid-template-columns:minmax(0,1fr)}
    .rail{position:static;height:auto;border-right:0;
          border-bottom:1px solid var(--rule);padding:26px 22px}
    .rail nav ol{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
                 gap:0 18px}
    main{padding:30px 22px 90px}
  }
"""

HEAD = """<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>{style}</style>
"""

IMAGE_RE = re.compile(r"^!\[(?P<alt>.*?)\]\((?P<src>[^)]+)\)\s*$")
HEADING_RE = re.compile(r"^(?P<h>#{1,4})\s+(?P<text>.*)$")
BULLET_RE = re.compile(r"^[-*]\s+(?P<text>.*)$")
ORDERED_RE = re.compile(r"^\d+\.\s+(?P<text>.*)$")

_INLINE = re.compile(
    r"(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)]+\))"
)


def inline(text: str) -> str:
    """Converts inline Markdown to HTML, escaping everything else."""
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(html.escape(text[pos:m.start()]))
        tok = m.group(0)
        if tok.startswith("`"):
            out.append(f"<code>{html.escape(tok[1:-1])}</code>")
        elif tok.startswith("**"):
            out.append(f"<strong>{html.escape(tok[2:-2])}</strong>")
        elif tok.startswith("["):
            label = tok[1:tok.index("]")]
            href = tok[tok.index("](") + 2:-1]
            out.append(f'<a href="{html.escape(href)}">{html.escape(label)}</a>')
        else:
            out.append(f"<em>{html.escape(tok[1:-1])}</em>")
        pos = m.end()
    if pos < len(text):
        out.append(html.escape(text[pos:]))
    return "".join(out)


def slugify(text: str) -> str:
    plain = re.sub(r"[`*_\[\]()]", "", text).strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:60]


def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def convert(md_path: Path, out_path: Path, title: str | None) -> None:
    lines = md_path.read_text().split("\n")
    body: list[str] = []
    toc: list[tuple[str, str]] = []

    i = 0
    doc_title = title
    in_list: str | None = None

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            body.append(f"</{in_list}>")
            in_list = None

    while i < len(lines):
        line = lines[i].rstrip()

        if line.startswith("```"):
            close_list()
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(html.escape(lines[i].rstrip()))
                i += 1
            i += 1
            body.append("<pre><code>" + "\n".join(block) + "</code></pre>")
            continue

        if line.startswith(">"):
            close_list()
            block = []
            while i < len(lines) and lines[i].startswith(">"):
                block.append(lines[i].lstrip(">").strip())
                i += 1
            paras = [p.strip() for p in "\n".join(block).split("\n\n") if p.strip()]
            inner = "".join(
                f"<p>{inline(' '.join(p.split()))}</p>" for p in paras
            )
            body.append(f"<blockquote>{inner}</blockquote>")
            continue

        if (line.startswith("|") and i + 1 < len(lines)
                and "-" in lines[i + 1]
                and set(lines[i + 1].replace("|", "").replace(":", "").strip())
                <= {"-", " "}):
            close_list()
            header = split_row(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            thead = "".join(f"<th>{inline(c)}</th>" for c in header)
            tbody = "".join(
                "<tr>" + "".join(
                    f"<td>{inline(c)}</td>" for c in r[:len(header)]
                ) + "</tr>"
                for r in rows
            )
            body.append(
                f'<div class="tbl-wrap"><table><thead><tr>{thead}</tr></thead>'
                f"<tbody>{tbody}</tbody></table></div>"
            )
            continue

        m = IMAGE_RE.match(line)
        if m:
            close_list()
            alt = m.group("alt")
            src = m.group("src")
            body.append(
                f'<figure><img src="{html.escape(src)}" alt="{html.escape(alt)}">'
                f"<figcaption>{inline(alt)}</figcaption></figure>"
            )
            i += 1
            continue

        if line.strip() in ("---", "***", "___"):
            close_list()
            body.append("<hr>")
            i += 1
            continue

        m = HEADING_RE.match(line)
        if m:
            close_list()
            level = len(m.group("h"))
            text = m.group("text")
            if level == 1 and doc_title is None:
                doc_title = re.sub(r"[`*]", "", text)
            if level == 2:
                slug = slugify(text)
                toc.append((slug, re.sub(r"^\d+\.\s*", "", text)))
                body.append(f'<h2 id="{slug}">{inline(text)}</h2>')
            else:
                body.append(f"<h{level}>{inline(text)}</h{level}>")
            i += 1
            continue

        m = BULLET_RE.match(line) or ORDERED_RE.match(line)
        if m:
            ordered = ORDERED_RE.match(line) is not None
            want = "ol" if ordered else "ul"
            if in_list != want:
                close_list()
                body.append(f"<{want}>")
                in_list = want
            text = m.group("text")
            i += 1
            while (i < len(lines) and lines[i].strip()
                   and lines[i].startswith((" ", "\t"))
                   and not BULLET_RE.match(lines[i].strip())):
                text += " " + lines[i].strip()
                i += 1
            body.append(f"<li>{inline(text)}</li>")
            continue

        if not line.strip():
            close_list()
            i += 1
            continue

        block = [line.strip()]
        i += 1
        while (i < len(lines) and lines[i].strip()
               and not lines[i].startswith(("#", ">", "|", "```", "---", "!["))
               and not BULLET_RE.match(lines[i])
               and not ORDERED_RE.match(lines[i])):
            block.append(lines[i].strip())
            i += 1
        close_list()
        body.append(f"<p>{inline(' '.join(block))}</p>")

    close_list()

    nav = "".join(
        f'<li><a href="#{slug}"><span class="n">'
        f'{n:02d}</span><span>{html.escape(label)}</span></a></li>'
        for n, (slug, label) in enumerate(toc, 1)
    )

    page = HEAD.format(title=html.escape(doc_title or md_path.stem), style=STYLE)
    page += (
        '<div class="shell"><aside class="rail">'
        '<div class="rail-mark">Reference</div>'
        f'<p class="rail-title">{html.escape(doc_title or md_path.stem)}</p>'
        f'<nav aria-label="Contents"><ol>{nav}</ol></nav></aside>'
        '<main><div class="col">' + "\n".join(body) + "</div></main></div>"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)
    print(f"[SUCCESS] wrote {out_path}  ({len(toc)} sections)")


def main() -> None:
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        raise SystemExit(1)
    title = sys.argv[3] if len(sys.argv) == 4 else None
    convert(Path(sys.argv[1]), Path(sys.argv[2]), title)


if __name__ == "__main__":
    main()
