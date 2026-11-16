"""附件解析：把用户上传的文件转成可直接喂给模型的文本或图片数据。

设计取舍：
- 只用标准库解析 docx / pptx / xlsx（本质是 zip + XML），服务器不需要装一堆解析库；
- PDF 需要 pypdf（纯 Python，无编译依赖），缺失时给用户一句能看懂的提示；
- 图片不在这里做 OCR，而是把原图交给多模态模型直接看；
- 解析结果按会话用户暂存在内存里（带 TTL），既能被下一次请求取用，也不落盘留隐私。
"""
from __future__ import annotations

import base64
import html
import io
import re
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # 单个文件 20 MB
MAX_TEXT_CHARS = 60_000               # 单个文件注入模型的字符上限
MAX_TOTAL_CHARS = 120_000             # 一条消息里所有附件的总字符上限
MAX_FILES_PER_MESSAGE = 5             # 一条消息最多带几个附件
STORE_TTL = 6 * 3600                  # 解析结果保留 6 小时，够用户追问
MAX_STORE_PER_USER = 12               # 每人暂存上限，超出挤掉最早的

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"

TEXT_EXTS = {
    ".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".log", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".php", ".rb", ".sh", ".bash", ".sql",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".tex", ".r", ".m",
    ".swift", ".kt", ".vue", ".xml", ".srt", ".vtt", ".env", ".properties",
}
HTML_EXTS = {".html", ".htm", ".xhtml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
UNSUPPORTED = {
    ".doc": "旧版 Word（.doc）无法直接读取，请用 Word 另存为 .docx 再上传",
    ".ppt": "旧版 PPT（.ppt）无法直接读取，请另存为 .pptx 再上传",
    ".xls": "旧版 Excel（.xls）无法直接读取，请另存为 .xlsx 再上传",
    ".wps": "WPS 旧格式无法直接读取，请另存为 .docx / .xlsx 再上传",
    ".pages": "Apple Pages 文档无法直接读取，请导出为 .docx 或 PDF 再上传",
    ".key": "Keynote 文档无法直接读取，请导出为 .pptx 或 PDF 再上传",
    ".numbers": "Numbers 表格无法直接读取，请导出为 .xlsx 或 PDF 再上传",
    ".zip": "压缩包不支持直接读取，请解压后上传其中的文件",
    ".rar": "压缩包不支持直接读取，请解压后上传其中的文件",
    ".7z": "压缩包不支持直接读取，请解压后上传其中的文件",
}

SUPPORTED_HINT = ("支持纯文本 / Markdown / CSV / JSON / 代码、Word（.docx）、"
                  "PPT（.pptx）、Excel（.xlsx）、PDF 和图片")


class FileError(ValueError):
    """附件无法读取，消息可直接展示给用户。"""


# ---------- 文本解码 ----------

def _decode_text(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise FileError("这看起来是二进制文件，没法当文本读取")
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _html_text(raw: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u00a0\u3000]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ---------- Office（zip + XML） ----------

def _open_zip(data: bytes, kind: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise FileError(f"这个 {kind} 文件已损坏，或者后缀和真实格式不一致")


def _docx_text(data: bytes) -> str:
    with _open_zip(data, "Word") as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            raise FileError("这个 .docx 里没找到正文（可能不是真正的 Word 文档）")
    root = ET.fromstring(xml)
    lines = []
    for p in root.iter(_W + "p"):
        buf = []
        for node in p.iter():
            if node.tag == _W + "t":
                buf.append(node.text or "")
            elif node.tag == _W + "tab":
                buf.append("\t")
            elif node.tag in (_W + "br", _W + "cr"):
                buf.append("\n")
        lines.append("".join(buf).rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise FileError("这份 Word 里没读到文字（可能是纯图片的扫描件）")
    return text


def _pptx_text(data: bytes) -> str:
    with _open_zip(data, "PPT") as z:
        names = [n for n in z.namelist()
                 if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        if not names:
            raise FileError("这个 .pptx 里没找到幻灯片")
        names.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        blocks = []
        for i, name in enumerate(names, 1):
            try:
                root = ET.fromstring(z.read(name))
            except ET.ParseError:
                continue
            paras = []
            for p in root.iter(_A + "p"):
                line = "".join(t.text or "" for t in p.iter(_A + "t")).strip()
                if line:
                    paras.append(line)
            if paras:
                blocks.append(f"【第 {i} 页】\n" + "\n".join(paras))
    if not blocks:
        raise FileError("这份 PPT 里没读到文字（可能是纯图片的幻灯片）")
    return "\n\n".join(blocks)


def _xlsx_sheets(names: list[str]) -> list[str]:
    sheets = [n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    sheets.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))
    return sheets


def _xlsx_sheet_names(z: zipfile.ZipFile, names: list[str]) -> dict[str, str]:
    """把 xl/worksheets/sheetN.xml 映射成「工作表名」。"""
    mapping: dict[str, str] = {}
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        try:
            book = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rid2target = {}
            for rel in rels.iter(_PKG_R + "Relationship"):
                target = (rel.get("Target") or "").lstrip("/")
                if target and not target.startswith("xl/"):
                    target = "xl/" + target
                rid2target[rel.get("Id") or ""] = target
            for sheet in book.iter(_S + "sheet"):
                target = rid2target.get(sheet.get(_R + "id") or "", "")
                title = (sheet.get("name") or "").strip()
                if target and title:
                    mapping[target] = title
        except (ET.ParseError, KeyError):
            return {}
    return mapping


def _xlsx_text(data: bytes) -> str:
    with _open_zip(data, "Excel") as z:
        names = z.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in root.iter(_S + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
            except ET.ParseError:
                shared = []
        titles = _xlsx_sheet_names(z, names)
        blocks = []
        for idx, path in enumerate(_xlsx_sheets(names)[:8], 1):
            try:
                root = ET.fromstring(z.read(path))
            except (ET.ParseError, KeyError):
                continue
            rows = []
            for row in root.iter(_S + "row"):
                cells = []
                for c in row.iter(_S + "c"):
                    kind = c.get("t")
                    v = c.find(_S + "v")
                    if kind == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(_S + "t"))
                    elif kind == "s" and v is not None and (v.text or "").strip():
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = ""
                    else:
                        val = (v.text or "") if v is not None else ""
                    cells.append(re.sub(r"\s+", " ", val).strip())
                if any(cells):
                    rows.append(" | ".join(cells).rstrip(" |"))
                if len(rows) >= 400:
                    rows.append("…（这张表行数较多，只取了前 400 行）")
                    break
            if rows:
                title = titles.get(path) or f"Sheet{idx}"
                blocks.append(f"【工作表：{title}】\n" + "\n".join(rows))
    if not blocks:
        raise FileError("这份 Excel 里没读到内容（可能全是空的）")
    return "\n\n".join(blocks)


# ---------- PDF ----------

def _pdf_extract(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise FileError("服务器缺少 PDF 解析组件（pypdf），暂时读不了 PDF；"
                        "可以把它另存为 Word 或直接复制文字给我")
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise FileError("这份 PDF 有密码保护，请先解除加密再上传")
        pages = []
        for i, page in enumerate(reader.pages[:100], 1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001 - 个别页解析失败不影响整体
                text = ""
            if text:
                pages.append(f"【第 {i} 页】\n{text}")
    except FileError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FileError(f"PDF 解析失败：{e}")
    if not pages:
        raise FileError("这份 PDF 没有可提取的文字（大概是扫描件或整页图片），"
                        "可以把关键内容截图上传，或直接复制文字给我")
    return "\n\n".join(pages), len(reader.pages)


# ---------- 统一入口 ----------

def safe_name(raw: str) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return (name or "未命名文件")[:120]


def _extract(name: str, data: bytes) -> dict:
    """返回 {kind, text, data_url, note, pages}。"""
    ext = Path(name).suffix.lower()
    if ext in UNSUPPORTED:
        raise FileError(UNSUPPORTED[ext])
    if ext in IMAGE_EXTS:
        if not data:
            raise FileError("文件是空的")
        mime = IMAGE_MIME.get(ext, "image/png")
        return {"kind": "image", "text": "",
                "data_url": f"data:{mime};base64," + base64.b64encode(data).decode(),
                "note": "图片，由多模态模型直接查看", "pages": 0}
    if ext == ".pdf":
        text, pages = _pdf_extract(data)
        return {"kind": "text", "text": text, "note": f"PDF 共 {pages} 页", "pages": pages}
    if ext == ".docx":
        return {"kind": "text", "text": _docx_text(data), "note": "Word 文档", "pages": 0}
    if ext == ".pptx":
        return {"kind": "text", "text": _pptx_text(data), "note": "PPT", "pages": 0}
    if ext in (".xlsx", ".xlsm"):
        return {"kind": "text", "text": _xlsx_text(data), "note": "Excel 表格", "pages": 0}
    try:
        raw = _decode_text(data)
    except FileError:
        raise FileError(f"读不了这个文件（{ext or '无后缀'}）。{SUPPORTED_HINT}")
    text = _html_text(raw) if ext in HTML_EXTS else raw
    if not text.strip():
        raise FileError("文件里没有可读取的文字")
    return {"kind": "text", "text": text, "note": "纯文本", "pages": 0}


def _trim(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    cut = text[:MAX_TEXT_CHARS]
    return (cut + f"\n…（原文共 {len(text)} 字，这里只读了前 {MAX_TEXT_CHARS} 字）", True)


# ---------- 暂存区（内存，按用户隔离，带 TTL） ----------

_STORE: dict[str, dict] = {}


def _gc() -> None:
    now = time.time()
    for key, item in list(_STORE.items()):
        if now - item["created"] > STORE_TTL:
            _STORE.pop(key, None)


def _public(item: dict) -> dict:
    text = item.get("text") or ""
    return {
        "id": item["id"], "name": item["name"], "size": item["size"],
        "kind": item["kind"], "ext": item.get("ext", ""),
        "chars": len(text), "pages": item.get("pages") or 0,
        "truncated": bool(item.get("truncated")),
        "preview": re.sub(r"\s+", " ", text)[:160],
        "note": item.get("note", ""),
    }


def save(user_id: int, name: str, data: bytes) -> dict:
    _gc()
    if not data:
        raise FileError("文件是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise FileError(f"文件太大了（{len(data) / 1048576:.1f} MB），单文件上限 "
                        f"{MAX_UPLOAD_BYTES // 1048576} MB")
    clean = safe_name(name)
    parsed = _extract(clean, data)
    if parsed["kind"] == "text":
        parsed["text"], parsed["truncated"] = _trim(parsed["text"])
        if parsed.get("truncated"):
            parsed["note"] = (parsed.get("note", "") + " · 内容较长已截断").strip(" ·")

    mine = sorted((k for k, v in _STORE.items() if v["user_id"] == user_id),
                  key=lambda k: _STORE[k]["created"])
    while len(mine) >= MAX_STORE_PER_USER:
        _STORE.pop(mine.pop(0), None)

    aid = uuid.uuid4().hex[:12]
    item = {"id": aid, "user_id": user_id, "name": clean, "size": len(data),
            "ext": Path(clean).suffix.lower(), "created": time.time()}
    item.update(parsed)
    _STORE[aid] = item
    return _public(item)


def resolve(user_id: int, ids: list[str] | None) -> list[dict]:
    """取出属于该用户、未过期的附件；无效 id 直接忽略。"""
    out = []
    for aid in (ids or [])[:MAX_FILES_PER_MESSAGE]:
        item = _STORE.get(str(aid))
        if not item or item["user_id"] != user_id:
            continue
        if time.time() - item["created"] > STORE_TTL:
            continue
        out.append(item)
    return out


def drop(user_id: int, attach_id: str) -> bool:
    item = _STORE.get(str(attach_id))
    if not item or item["user_id"] != user_id:
        return False
    _STORE.pop(str(attach_id), None)
    return True


def build_blocks(items: list[dict]) -> tuple[list[str], list[dict], bool]:
    """把附件拆成「文本块」和「图片」，并卡住总字数。

    返回 (文本块列表, 图片列表, 是否因超长丢掉了部分内容)。
    """
    blocks, images = [], []
    used = 0
    dropped = False
    for i, item in enumerate(items, 1):
        if item["kind"] == "image":
            images.append(item)
            continue
        text = item.get("text") or ""
        if used >= MAX_TOTAL_CHARS:
            dropped = True
            break
        room = MAX_TOTAL_CHARS - used
        if len(text) > room:
            text = text[:room] + "\n…（附件总量已达上限，后面的内容没有读入）"
            dropped = True
        used += len(text)
        label = f"【附件 {i}：{item['name']}（约 {len(text)} 字）】"
        blocks.append(f"{label}\n{text}\n【附件 {i} 结束】")
    return blocks, images, dropped


def strip_ids(text: str) -> str:
    """历史消息里只保留附件名，去掉内部 id，别让模型看到。"""
    return re.sub(r"#[0-9a-f]{12}", "", text or "")
