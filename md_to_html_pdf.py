# -*- coding: utf-8 -*-
"""
Markdown → 手机可直接查看的 HTML + 可打印/分享的 PDF

用法：
    python md_to_html_pdf.py <markdown文件路径> [--no-pdf]

产物：与源文件同目录、同名，扩展名为 .html / .pdf
原理：markdown 库转 HTML → 套打印友好样式（A4 + 移动端自适应）→ Edge 无头模式打印 PDF
注意：中文路径需先复制到 ASCII 临时目录再交给 Edge，否则可能打印失败
"""

import sys
import subprocess
import tempfile
from pathlib import Path

import markdown

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
  @page { size: A4; margin: 16mm 15mm 14mm 15mm; }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; padding: 28px 20px 40px; background: #eef1f6;
    font-family: "Microsoft YaHei", "PingFang SC", "Source Han Sans SC", sans-serif;
    color: #1f2733; font-size: 15.5px; line-height: 1.85;
  }
  .sheet {
    max-width: 760px; margin: 0 auto; background: #fff; border-radius: 4px;
    box-shadow: 0 2px 18px rgba(24,39,66,.12); padding: 40px 44px 46px;
  }
  h1 {
    font-size: 24px; margin: 0 0 6px; color: #1f4e79; letter-spacing: .02em;
    border-bottom: 3px solid #1f4e79; padding-bottom: 14px;
  }
  h2 {
    font-size: 18px; margin: 34px 0 12px; color: #1f4e79;
    border-left: 4px solid #1f4e79; padding-left: 10px;
  }
  h3 { font-size: 16px; margin: 24px 0 10px; color: #2c5f8a; }
  p { margin: 0 0 12px; }
  blockquote {
    margin: 0 0 20px; padding: 12px 16px; background: #f4f7fb;
    border-left: 4px solid #9db8d2; color: #4a5568; font-size: 14px;
  }
  blockquote p { margin: 0; }
  strong { color: #14385c; }
  hr { border: none; border-top: 1px dashed #c7d0da; margin: 28px 0; }
  ul, ol { margin: 0 0 14px; padding-left: 24px; }
  li { margin-bottom: 6px; }
  table { border-collapse: collapse; width: 100%; margin: 14px 0 18px; font-size: 14px; }
  th, td { border: 1px solid #d3dae3; padding: 8px 10px; text-align: left; vertical-align: top; }
  th { background: #eaf1f9; color: #1f4e79; font-weight: 600; }
  code { background: #f0f2f5; padding: 1px 5px; border-radius: 3px; font-size: 13px; }
  .foot-meta { margin-top: 34px; padding-top: 12px; border-top: 1px solid #e1e6ec;
               font-size: 12px; color: #8a94a0; text-align: right; }

  @media (max-width: 640px) {
    body { padding: 16px 10px 28px; font-size: 15px; }
    .sheet { padding: 22px 18px 26px; box-shadow: none; }
    h1 { font-size: 20px; }
    h2 { font-size: 17px; margin-top: 26px; }
    table { font-size: 13px; display: block; overflow-x: auto; white-space: nowrap; }
  }
  @media print {
    body { padding: 0; background: #fff; font-size: 11pt; }
    .sheet { max-width: none; box-shadow: none; padding: 0; }
    h2 { page-break-after: avoid; }
    table { page-break-inside: avoid; }
  }
"""


def build_html(md_path: Path, out_html: Path) -> None:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["extra", "sane_lists"])
    title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("# ")),
                 md_path.stem)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="sheet">
{body}
</div>
</body>
</html>"""
    out_html.write_text(doc, encoding="utf-8")
    print(f"[ok] HTML -> {out_html}")


def build_pdf(html: Path, out_pdf: Path) -> None:
    """Edge 无头打印。中文路径先搬到 ASCII 临时目录。"""
    tmp_dir = Path(tempfile.gettempdir()) / "md2pdf_ascii"
    tmp_dir.mkdir(exist_ok=True)
    tmp_html = tmp_dir / "doc.html"
    tmp_pdf = tmp_dir / "doc.pdf"
    tmp_html.write_text(html.read_text(encoding="utf-8"), encoding="utf-8")
    if tmp_pdf.exists():
        tmp_pdf.unlink()

    subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
         "--virtual-time-budget=8000",
         f"--print-to-pdf={tmp_pdf}", f"file:///{tmp_html}"],
        capture_output=True, timeout=120,
    )
    if not tmp_pdf.exists():
        print("[warn] PDF 未生成，仅保留 HTML")
        return
    out_pdf.write_bytes(tmp_pdf.read_bytes())
    print(f"[ok] PDF  -> {out_pdf}  ({out_pdf.stat().st_size // 1024} KB)")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("用法: python md_to_html_pdf.py <markdown文件>")
        raise SystemExit(1)
    md_path = Path(args[0]).resolve()
    out_html = md_path.with_suffix(".html")
    build_html(md_path, out_html)
    if "--no-pdf" not in sys.argv:
        build_pdf(out_html, md_path.with_suffix(".pdf"))


if __name__ == "__main__":
    main()
