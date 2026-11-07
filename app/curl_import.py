"""解析浏览器「Copy as cURL」文本，提取 URL / 方法 / 请求头 / Body。

用于门户课表接口的一键导入：用户在门户课表页抓包复制 cURL，
粘贴进设置即可，无需手工抄 Cookie 和字段。
"""
from __future__ import annotations

import json
import re
import shlex
from urllib.parse import parse_qsl

NO_ARG_FLAGS = {
    "--compressed", "-s", "--silent", "-k", "--insecure", "-L", "--location",
    "--globoff", "-i", "--include", "-v", "--verbose", "--http1.1", "--http2",
}
VALUE_FLAGS = {
    "-H": "header", "--header": "header",
    "-b": "cookie", "--cookie": "cookie",
    "-d": "body", "--data": "body", "--data-raw": "body", "--data-binary": "body",
    "--data-urlencode": "body",
    "-X": "method", "--request": "method",
    "--url": "url",
    "-A": "user-agent", "--user-agent": "user-agent",
    "-e": "referer", "--referer": "referer",
}


def parse_curl(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"\\\r?\n", " ", text)
    if not text:
        raise ValueError("cURL 文本为空")
    if not re.match(r"^curl\b", text):
        raise ValueError(
            "不是 cURL 命令：请在开发者工具 Network 中右键请求 → Copy → Copy as cURL")
    tokens = shlex.split(text)
    url, method, body, cookie = None, None, None, None
    headers: dict[str, str] = {}
    i = 1
    while i < len(tokens):
        t = tokens[i]
        kind = VALUE_FLAGS.get(t)
        if kind:
            i += 1
            if i >= len(tokens):
                raise ValueError(f"cURL 参数 {t} 缺少值")
            v = tokens[i]
            if kind == "header":
                k, _, val = v.partition(":")
                key = k.strip()
                if key.lower() == "cookie":
                    key = "Cookie"
                if key:
                    headers[key] = val.strip()
            elif kind == "cookie":
                cookie = v
            elif kind == "body":
                body = v
            elif kind == "method":
                method = v.upper()
            elif kind == "url":
                url = v
            elif kind == "user-agent":
                headers["User-Agent"] = v
            elif kind == "referer":
                headers["Referer"] = v
        elif t in NO_ARG_FLAGS or t.startswith("-"):
            pass  # 忽略压缩/静默等开关；带 = 的长参数自包含
        elif url is None:
            url = t
        i += 1
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("cURL 中未找到有效的 http(s) URL")
    if cookie:
        existing = headers.get("Cookie") or headers.get("cookie") or ""
        headers["Cookie"] = (
            existing + "; " + cookie).strip("; ") if existing else cookie
    if method is None:
        method = "POST" if body is not None else "GET"
    parsed_body = None
    if body is not None:
        try:
            parsed_body = json.loads(body)
        except json.JSONDecodeError:
            if "=" in body and "{" not in body:
                parsed_body = dict(parse_qsl(body))
            else:
                parsed_body = body
    return {"url": url, "method": method, "headers": headers, "body": parsed_body}


# 常见课表字段别名 → 标准字段（含青岛理工门户 datacenter/xskb 的下划线大写字段）
FIELD_SYNONYMS = {
    "name": ["name", "kcmc", "courseName", "course_name", "kcMc", "title", "lessonName"],
    "teacher": ["teacher", "xm", "jsxm", "teacherName", "jsMc", "skjs", "jsmc"],
    "room": ["room", "cdmc", "place", "classroom", "cdMc", "address", "skdd", "skddmc"],
    "day": ["day", "xqj", "xqjmc", "weekDay", "weekday", "dayOfWeek", "xingqi"],
    "start": ["start", "beginNumber", "startSection", "jcstart", "ksjc", "sectionStart", "ksj"],
    "end": ["end", "endSection", "jcend", "jsjc", "sectionEnd", "jsj"],
    "weeks": ["weeks", "zcd", "weekList", "weeksList", "zhouci", "skzc"],
    "source_id": ["id", "jxb_id", "kch_id", "lessonId", "courseId", "jxbh"],
}


def _norm_key(k) -> str:
    """字段名归一化：门户数据中心返回全大写（KCMC / XQJ / KSJC），别名表是小写驼峰。"""
    return str(k).lower().replace("_", "").replace("-", "")


def _find_course_list(obj, path="") -> tuple[str, list] | None:
    """递归找第一个「看起来像课程数组」的 list[dict]。"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            low = {_norm_key(k) for k in obj[0].keys()}
            if any(_norm_key(syn) in low for syns in FIELD_SYNONYMS.values() for syn in syns[:3]):
                return path, obj
        for idx, item in enumerate(obj):
            found = _find_course_list(
                item, f"{path}.{idx}" if path else str(idx))
            if found:
                return found
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found = _find_course_list(v, f"{path}.{k}" if path else k)
            if found:
                return found
    return None


def guess_mapping(payload) -> tuple[str, dict]:
    """根据响应结构猜测 list_path 与字段映射。返回 (list_path, mapping)。"""
    found = _find_course_list(payload)
    if not found:
        return "", {k: v[0] for k, v in FIELD_SYNONYMS.items()}
    path, items = found
    # 归一化查找，但映射里必须写回原始字段名，否则全大写字段取不到值
    lookup = {_norm_key(k): str(k) for k in items[0].keys()}
    mapping = {}
    for field, syns in FIELD_SYNONYMS.items():
        hit = next((lookup[_norm_key(s)] for s in syns if _norm_key(s) in lookup), None)
        if hit:
            mapping[field] = hit
    # showdoc 风格：time: [3,4] 表示节次
    time_key = lookup.get("time")
    if "start" not in mapping and time_key and isinstance(items[0].get(time_key), list):
        mapping["start"] = time_key + ".0"
        mapping["end"] = time_key + ".-1"
    if "end" not in mapping and "start" in mapping:
        mapping["end"] = mapping["start"]
    return path, mapping


# 门户「应用 / 资源 / 新闻」列表也有 title、category_name、daycount 之类字段，
# 用子串包含会把它们当成课表；下面改为归一化后的**精确**匹配 + 要素组合判定。
COURSE_NAME_HINTS = {"name", "kcmc", "coursename",
                     "kcname", "lessonname", "subjectname"}
COURSE_DAY_HINTS = {"day", "xqj", "xqjmc", "weekday", "dayofweek", "xingqi"}
COURSE_SECTION_HINTS = {
    "start", "end", "ksjc", "jsjc", "ksj", "jsj", "startsection", "endsection",
    "jcstart", "jcend", "sectionstart", "sectionend",
}
COURSE_META_HINTS = {
    "weeks", "zcd", "skzc", "weeklist", "room", "cdmc", "skdd", "place",
    "teacher", "jsxm", "skjs",
}
COURSE_FIELD_HINTS = sorted(COURSE_NAME_HINTS | COURSE_DAY_HINTS
                            | COURSE_SECTION_HINTS | COURSE_META_HINTS)
EVENT_FIELD_HINTS = [
    "date", "rq", "kssj", "jssj", "starttime", "start_time", "endtime", "end_time",
    "begintime", "startdate", "enddate", "sksj", "eventdate", "scheduledate", "kbrq",
]
WEEK_PARAM_RE = re.compile(
    r"[?&](week|weeks|zc|zhouci|skzc|date|rq|startdate|enddate)=", re.I)
TERM_PARAM_RE = re.compile(
    r"[?&](xn|xq|xqm|xnm|term|semester)=", re.I)


def _key_set(keys) -> set[str]:
    return {str(k).lower().replace("-", "").replace("_", "") for k in keys}


def _looks_like_course(keys) -> bool:
    """课程名 + （星期 或 节次）才算课表数组，避开门户应用/资源列表。"""
    low = _key_set(keys)
    if not low & COURSE_NAME_HINTS:
        return False
    if not low & (COURSE_DAY_HINTS | COURSE_SECTION_HINTS):
        return False
    return bool(low & COURSE_META_HINTS) or bool(low & COURSE_DAY_HINTS and low & COURSE_SECTION_HINTS)


def _looks_like_event(keys) -> bool:
    low = _key_set(keys)
    return any(h in low or any(h in k for k in low) for h in EVENT_FIELD_HINTS)


def collect_arrays(obj, path: str = "", depth: int = 0, out: list | None = None,
                   max_depth: int = 8) -> list[dict]:
    """枚举响应里所有「数组节点」，返回路径、元素数、字段名与样例，供人工挑路径。"""
    if out is None:
        out = []
    if depth > max_depth:
        return out
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts:
            keys = sorted({str(k) for x in dicts[:5] for k in x.keys()})
            out.append({
                "path": path or "(根数组)",
                "count": len(obj),
                "keys": keys[:24],
                "sample": {k: str(dicts[0].get(k))[:60] for k in keys[:8]},
                "course_like": _looks_like_course(keys),
                "event_like": _looks_like_event(keys),
            })
        for i, item in enumerate(obj[:5]):
            collect_arrays(item, f"{path}.{i}" if path else str(
                i), depth + 1, out, max_depth)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            collect_arrays(v, f"{path}.{k}" if path else k,
                           depth + 1, out, max_depth)
    return out


def parse_payload_text(text: str):
    """解析用户从浏览器 Response 面板粘贴的 JSON 文本。"""
    t = (text or "").strip()
    if not t:
        raise ValueError("响应内容为空：请在 Network → Response 里全选复制 JSON")
    # 允许前面带 「复制为」前缀或代码块围栏
    t = t.strip("`")
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start > 0:
        t = t[start:]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"不是合法 JSON（{e.msg}）。请确认复制的是 XHR 的 Response 内容，而不是页面 HTML 或登录页。")


def diagnose_url(url: str) -> list[str]:
    """区分「写死周次」与「写死学年学期」两类接口参数，给出对应提醒。"""
    warnings = []
    if WEEK_PARAM_RE.search(url or ""):
        warnings.append(
            "接口 URL 里带固定周次 / 日期参数，每日同步会一直拉到同一周的数据；"
            "请优先找不带这些参数的课表接口。"
        )
    if TERM_PARAM_RE.search(url or ""):
        warnings.append(
            "接口 URL 里写死了学年 / 学期参数（如 XN、XQ），换学期后需要重新登录或更新地址，"
            "否则拉到的还是旧学期课表。"
        )
    return warnings


def analyze_payload(payload) -> dict:
    """离线诊断响应结构：列出候选数组、判断是周课表还是日程事件、给出建议映射。"""
    arrays = collect_arrays(payload)
    top_keys = list(payload.keys())[:20] if isinstance(payload, dict) else \
        [f"(根数组，{len(payload)} 个元素)"]
    path, mapping = guess_mapping(payload)
    course_arrays = [a for a in arrays if a["course_like"]]
    event_arrays = [a for a in arrays if a["event_like"]]
    if event_arrays and not course_arrays:
        kind = "events"
        hint = ("这看起来是「按日期的日程 / 事件流」，不是每周循环课表。"
                "现有课表模型只表达周期性课程，无法直接承载一次性停课、改教室、补课。"
                "请把这份响应预览（不含 Cookie）发给助手，按实际字段写事件适配器。")
    elif course_arrays or path:
        kind = "courses"
        hint = "识别到周课表数组，可用建议路径与字段映射；保存后点「保存并立即同步」验证。"
    elif arrays:
        kind = "unknown"
        hint = ("找到数组但没认出课程字段。请从下方候选路径里挑一个填进「课程数组路径」，"
                "或把响应预览发给助手调整字段映射。")
    else:
        kind = "empty"
        hint = "响应里没有数组节点，可能复制到了错误请求（如页面 HTML）。请重新确认抓的是返回课程列表的 XHR。"
    return {
        "top_keys": top_keys,
        "arrays": arrays[:12],
        "suggested_path": path,
        "mapping": mapping,
        "kind": kind,
        "hint": hint,
    }
