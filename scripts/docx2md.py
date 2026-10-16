#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把需求文档 .docx 转为 Markdown —— 纯 Python 标准库实现（不依赖 pandoc / python-docx）。

用法:
    python3 docx2md.py <文件.docx> [<文件2.docx> ...]

输出:
    同目录下同名 .md 文件；被引用的图片提取到 <文件名>.assets/media/，
    并尽力在 <文件名>.assets/preview/ 生成缩略图（可选：Pillow / macOS sips / ImageMagick，
    任一可用即可；都没有则跳过，不影响转换）。

实现:
    直接把 .docx 当作 ZIP 解包，解析 word/document.xml、word/_rels/document.xml.rels、
    word/numbering.xml、word/media/*，只用 zipfile + xml.etree。

覆盖:
    标题（Heading 样式 / outlineLvl）、加粗 / 斜体 / 删除线、项目符号与编号列表、
    GFM 表格、超链接、图片、软换行；修订的插入予以保留、删除渲染为 ~~删除线~~。
    只做确定性结构转换，不做智能重排或内容判断。

说明:
    旧版二进制 .doc 不支持（请先另存为 .docx）。
    解析用户提供的 XML 时 ElementTree 默认不解析外部实体，满足一般使用；如需更强防护，
    可在受控环境中改用 defusedxml。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
V = "{urn:schemas-microsoft-com:vml}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
OO = "{urn:schemas-microsoft-com:office:office}"

PREVIEW_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}


def _truthy(elem) -> bool:
    """<w:b/> 或无 val 视为开；<w:b w:val="0|false|off"/> 视为关。"""
    if elem is None:
        return False
    val = elem.get(W + "val")
    if val is None:
        return True
    return str(val).strip().lower() not in ("0", "false", "off", "no")


class Context:
    def __init__(self, zipf: zipfile.ZipFile, md_dir: Path, base: str):
        self.zip = zipf
        self.md_dir = md_dir
        self.assets_rel = f"{base}.assets"
        self.media_dir = md_dir / f"{base}.assets" / "media"
        self.preview_dir = md_dir / f"{base}.assets" / "preview"
        self.rels = {}          # rid -> (target, mode)
        self.numbering = {}     # (numId, ilvl) -> numFmt
        self._extracted = {}    # zip 内路径 -> 相对 md 的链接路径
        self.extracted_files = []
        self.n = 0

    # ---------- 关系与编号 ----------
    def load_rels(self, part="word/document.xml"):
        d = Path(part).parent
        rels_name = d / "_rels" / (Path(part).name + ".rels")
        try:
            data = self.zip.read(rels_name.as_posix())
        except KeyError:
            return
        root = ET.fromstring(data)
        for r in root:
            rid, tgt, mode = r.get("Id"), r.get("Target"), r.get("TargetMode")
            if rid and tgt:
                self.rels[rid] = (tgt, mode)

    def load_numbering(self):
        try:
            data = self.zip.read("word/numbering.xml")
        except KeyError:
            return
        root = ET.fromstring(data)
        abstract = {}
        for an in root.findall(W + "abstractNum"):
            aid = an.get(W + "abstractNumId")
            lvls = {}
            for lvl in an.findall(W + "lvl"):
                ilvl = lvl.get(W + "ilvl")
                fmt = lvl.find(W + "numFmt")
                lvls[ilvl] = fmt.get(W + "val") if fmt is not None else "decimal"
            abstract[aid] = lvls
        for num in root.findall(W + "num"):
            nid = num.get(W + "numId")
            aref = num.find(W + "abstractNumId")
            aid = aref.get(W + "val") if aref is not None else None
            if aid in abstract:
                for ilvl, fmt in abstract[aid].items():
                    self.numbering[(nid, ilvl)] = fmt

    # ---------- 图片 ----------
    def image(self, rid: str | None, alt: str) -> str:
        if not rid or rid not in self.rels:
            return ""
        target, mode = self.rels[rid]
        if mode == "External" or re.match(r"^[a-z]+://", target, re.I):
            return f"![{alt}]({target})"
        zip_path = target[1:] if target.startswith("/") else "word/" + target
        if zip_path not in self._extracted:
            try:
                data = self.zip.read(zip_path)
            except KeyError:
                return ""
            name = Path(zip_path).name
            self.media_dir.mkdir(parents=True, exist_ok=True)
            dest = self.media_dir / name
            i = 1
            while dest.exists() and dest.read_bytes() != data:
                dest = self.media_dir / f"{Path(name).stem}-{i}{Path(name).suffix}"
                i += 1
            dest.write_bytes(data)
            self._extracted[zip_path] = f"{self.assets_rel}/media/{dest.name}"
            self.extracted_files.append(dest)
        return f"![{alt}]({self._extracted[zip_path]})"


# ---------------- 内联渲染 ----------------

def render_run(run, ctx: Context) -> str:
    parts = []
    for node in run:
        t = node.tag
        if t == W + "t" or t == W + "delText":
            parts.append(node.text or "")
        elif t == W + "tab":
            parts.append("\t")
        elif t == W + "br":
            parts.append("\n")
        elif t == W + "noBreakHyphen":
            parts.append("-")
        elif t == W + "drawing":
            parts.append(render_drawing(node, ctx))
        elif t == W + "pict":
            parts.append(render_pict(node, ctx))
    s = "".join(parts)
    if not s or "![" in s:
        return s
    rpr = run.find(W + "rPr")
    if rpr is not None:
        if _truthy(rpr.find(W + "b")):
            s = f"**{s}**"
        if _truthy(rpr.find(W + "i")):
            s = f"*{s}*"
        if _truthy(rpr.find(W + "strike")):
            s = f"~~{s}~~"
    return s


def render_hyperlink(node, ctx: Context) -> str:
    text = "".join(render_run(c, ctx) for c in node if c.tag == W + "r")
    rid = node.get(REL + "id")
    if rid and rid in ctx.rels:
        target, mode = ctx.rels[rid]
        if mode == "External" or re.match(r"^[a-z]+://", target, re.I):
            return f"[{text}]({target})" if text else ""
    return text


def render_inline(parent, ctx: Context) -> str:
    parts = []
    for c in parent:
        t = c.tag
        if t == W + "r":
            parts.append(render_run(c, ctx))
        elif t == W + "hyperlink":
            parts.append(render_hyperlink(c, ctx))
        elif t == W + "ins":
            parts.append(render_inline(c, ctx))
        elif t == W + "del":
            parts.append(f"~~{render_inline(c, ctx)}~~")
        elif t == W + "sdt":
            sc = c.find(W + "sdtContent")
            if sc is not None:
                parts.append(render_inline(sc, ctx))
        elif t in (W + "smartTag", W + "fldSimple", W + "moveFrom", W + "moveTo", W + "customXml"):
            parts.append(render_inline(c, ctx))
        elif t == MC + "AlternateContent":
            choice = c.find(MC + "Choice")
            node = choice if choice is not None else c.find(MC + "Fallback")
            if node is not None:
                parts.append(render_inline(node, ctx))
        elif t == W + "br":
            parts.append("\n")
        elif t == W + "tab":
            parts.append("\t")
    return "".join(parts)


def _alt_text(drawing) -> str:
    docpr = drawing.find(".//" + WP + "docPr")
    if docpr is not None:
        return (docpr.get("descr") or docpr.get("name") or "").strip()
    return ""


def render_drawing(drawing, ctx: Context) -> str:
    blip = drawing.find(".//" + A + "blip")
    if blip is None:
        return ""
    rid = blip.get(REL + "embed") or blip.get(REL + "link")
    ctx.n += 1
    return ctx.image(rid, _alt_text(drawing) or f"图{ctx.n}")


def render_pict(pict, ctx: Context) -> str:
    im = pict.find(".//" + V + "imagedata")
    if im is None:
        return ""
    rid = im.get(REL + "id")
    alt = im.get(OO + "title") or ""
    ctx.n += 1
    return ctx.image(rid, alt.strip() or f"图{ctx.n}")


# ---------------- 块级渲染 ----------------

def heading_level(p) -> int | None:
    ppr = p.find(W + "pPr")
    style = None
    outline = None
    if ppr is not None:
        ps = ppr.find(W + "pStyle")
        if ps is not None:
            style = ps.get(W + "val")
        ol = ppr.find(W + "outlineLvl")
        if ol is not None:
            try:
                outline = int(ol.get(W + "val"))
            except (TypeError, ValueError):
                outline = None
    if style:
        s = style.strip()
        low = s.lower()
        m = re.search(r"(\d+)\s*$", s)
        if "heading" in low or "标题" in s:
            return min(int(m.group(1)), 6) if m else 1
        if low in ("title",) or s == "标题":
            return 1
    if outline is not None:
        return max(1, min(outline + 1, 6))
    return None


def list_prefix(p, ctx: Context) -> str | None:
    ppr = p.find(W + "pPr")
    if ppr is None:
        return None
    npr = ppr.find(W + "numPr")
    if npr is None:
        return None
    ilvl = npr.find(W + "ilvl")
    numid = npr.find(W + "numId")
    if numid is None:
        return None
    level = 0
    if ilvl is not None:
        try:
            level = int(ilvl.get(W + "val"))
        except (TypeError, ValueError):
            level = 0
    fmt = ctx.numbering.get((numid.get(W + "val"), str(level)), "bullet")
    marker = "- " if fmt == "bullet" else "1. "
    return "  " * level + marker


def paragraph_md(p, ctx: Context) -> str:
    text = render_inline(p, ctx)
    lvl = heading_level(p)
    if lvl:
        return ("#" * lvl) + " " + text.strip()
    prefix = list_prefix(p, ctx)
    if prefix is not None:
        return prefix + text.strip()
    return text.rstrip()


def table_md(tbl, ctx: Context) -> str:
    rows = []
    for tr in tbl.findall(W + "tr"):
        cells = []
        for tc in tr.findall(W + "tc"):
            cell = "<br>".join(
                t for p in tc.findall(W + "p") if (t := render_inline(p, ctx).strip())
            )
            cells.append(cell)
        rows.append(cells)
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)

    def fmt(cells):
        cells = (cells + [""] * ncol)[:ncol]
        return "| " + " | ".join(c.replace("|", "\\|").replace("\n", "<br>") for c in cells) + " |"

    out = [fmt(rows[0]), "|" + "|".join([":--"] * ncol) + "|"]
    out += [fmt(r) for r in rows[1:]]
    return "\n".join(out)


def iter_blocks(body):
    for child in list(body):
        if child.tag == W + "p":
            yield "p", child
        elif child.tag == W + "tbl":
            yield "tbl", child
        elif child.tag in (W + "sdt", W + "customXml"):
            sc = child.find(W + "sdtContent")
            if sc is None:
                sc = child
            yield from iter_blocks(sc)


# ---------------- 转换与预览 ----------------

def _is_imagemagick(exe: str) -> bool:
    """确认这个 convert 是 ImageMagick，而不是 Windows 自带的 convert.exe。"""
    try:
        r = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, check=False)
    except OSError:
        return False
    return b"ImageMagick" in (r.stdout or b"")


def _thumb_backends() -> list[str]:
    """按优先级返回可用的缩略图后端——跨平台，任一可用即可；都不可用就跳过缩略图。

    Pillow（跨平台，可选依赖）→ macOS sips → ImageMagick（IM7 `magick` / IM6 `convert`）。
    缩略图只是给评审/人眼快速看图的便利，不参与正文转换，缺后端不影响 .docx → Markdown。
    """
    backends: list[str] = []
    try:
        import PIL.Image  # noqa: F401
        backends.append("pillow")
    except Exception:
        pass
    if shutil.which("sips"):
        backends.append("sips")
    if shutil.which("magick"):
        backends.append("magick")
    elif shutil.which("convert") and _is_imagemagick("convert"):
        backends.append("convert")
    return backends


def _thumb_one(backend: str, src: Path, out: Path) -> bool:
    """用指定后端把 src 缩到最长边 1280 的 JPEG；返回是否成功产出 out。"""
    try:
        if backend == "pillow":
            from PIL import Image
            im = Image.open(src).convert("RGB")
            im.thumbnail((1280, 1280))
            im.save(out, "JPEG", quality=80)
        elif backend == "sips":
            subprocess.run(
                ["sips", "-Z", "1280", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                 str(src), "--out", str(out)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        elif backend == "magick":
            subprocess.run(
                ["magick", str(src), "-resize", "1280x1280>", "-quality", "80", str(out)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:  # convert（ImageMagick 6）
            subprocess.run(
                ["convert", str(src), "-resize", "1280x1280>", "-quality", "80", str(out)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        return False
    return out.exists()


def make_previews(ctx: Context) -> int:
    backends = _thumb_backends()
    if not backends:
        return 0  # 没有任何可用后端：跳过缩略图，正常完成转换
    ctx.preview_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in ctx.extracted_files:
        if f.suffix.lower() not in PREVIEW_EXT:
            continue
        out = ctx.preview_dir / (f.stem + ".jpg")
        for backend in backends:
            if _thumb_one(backend, f, out):
                n += 1
                break
    return n


def convert(path: Path) -> int:
    md_dir = path.parent
    base = path.stem
    out_md = md_dir / f"{base}.md"
    with zipfile.ZipFile(path) as z:
        ctx = Context(z, md_dir, base)
        ctx.load_rels()
        ctx.load_numbering()
        try:
            root = ET.fromstring(z.read("word/document.xml"))
        except KeyError:
            print(f"跳过（不是有效的 .docx，缺 word/document.xml）：{path}", file=sys.stderr)
            return 1
        body = root.find(W + "body")
        blocks = []
        if body is not None:
            for kind, el in iter_blocks(body):
                if kind == "p":
                    md = paragraph_md(el, ctx)
                    if md.strip():
                        blocks.append(md)
                else:
                    md = table_md(el, ctx)
                    if md.strip():
                        blocks.append(md)
    text = "\n\n".join(blocks)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out_md.write_text(text + "\n", encoding="utf-8")

    n_prev = make_previews(ctx)
    msg = f"已生成：{out_md}（纯标准库）"
    if ctx.extracted_files:
        msg += f"；图片 {len(ctx.extracted_files)} 张 → {ctx.assets_rel}/media/"
        if n_prev:
            msg += f"，缩略图 {n_prev} 张 → {ctx.assets_rel}/preview/"
    print(msg)
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    rc = 0
    for arg in argv:
        p = Path(arg)
        if not p.is_file():
            print(f"跳过（文件不存在）：{arg}", file=sys.stderr)
            rc = 1
            continue
        if p.suffix.lower() != ".docx":
            print(f"跳过（仅支持 .docx，.doc 请先另存为 .docx）：{arg}", file=sys.stderr)
            rc = 1
            continue
        try:
            rc |= convert(p)
        except zipfile.BadZipFile:
            print(f"跳过（无法解压，可能不是 .docx）：{arg}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
