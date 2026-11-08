"""AI 助手：OpenAI 兼容接口 + 课表上下文注入。

默认指向通义千问 DashScope 兼容端点，可换成任何 OpenAI 兼容服务
（DeepSeek、Moonshot、本地 Ollama/vLLM 等）。
"""
from __future__ import annotations

import json
from datetime import date

import httpx

from . import db
from .config import load_config
from .models import Course, Event, Exam, apply_course_overrides, current_week, event_line
from .scheduler import WEEKDAY_CN, course_line, courses_on


def exam_line(e: Exam) -> str:
    parts = [e.display_time(), e.name]
    if e.room:
        parts.append(f"@{e.room}")
    if e.seat:
        parts.append(f"座号 {e.seat}")
    if e.campus:
        parts.append(e.campus)
    if e.note:
        parts.append(e.note)
    return " ".join(p for p in parts if p)


def build_context(cfg: dict) -> str:
    today = date.today()
    week = current_week(cfg.get("semester_start", ""), today, cfg.get("total_weeks", 30))
    snap = db.get_snapshot()
    lines = [
        f"今天是 {today.strftime('%Y年%m月%d日')} {WEEKDAY_CN[today.isoweekday() - 1]}，当前教学周：第 {week} 周。",
        f"学校：{cfg.get('school', '青岛理工大学')}。",
    ]
    courses = apply_course_overrides(
        [Course.from_dict(d) for d in snap["courses"]] if snap else [],
        cfg.get("course_overrides"))
    this_week = [c for c in courses if c.in_week(week)]
    if this_week:
        if snap:
            lines.append(f"学生本周课表（数据来源：{snap['source']}，更新于 {snap['fetched_at']}）：")
        else:
            lines.append("学生本周课表（手动维护）：")
        for dow in range(1, 8):
            day_courses = sorted([c for c in this_week if c.day == dow], key=lambda c: c.start)
            if day_courses:
                lines.append(f"- {WEEKDAY_CN[dow - 1]}：" + "；".join(course_line(cfg, c) for c in day_courses))
            else:
                lines.append(f"- {WEEKDAY_CN[dow - 1]}：无课")
    elif snap:
        lines.append("本周没有课程安排。")
    else:
        lines.append("课表尚未同步，暂无课程数据。")
    if snap and snap["source"] == "mock":
        lines.append("注意：当前课表为演示数据，回答时请提醒用户这是示例课表。")

    exam_snap = db.get_exam_snapshot()
    if exam_snap is None:
        lines.append("考试数据尚未同步；被问到考试安排时说明需要先在设置中同步。")
    else:
        exams = [Exam.from_dict(d) for d in exam_snap["exams"]]
        upcoming = sorted(
            [e for e in exams if not e.is_finished()],
            key=lambda e: (e.date or "9999-12-31", e.start or "23:59"),
        )
        source = exam_snap["source"]
        if exam_snap["source"] == "mock":
            lines.append("注意：当前考试为演示数据，回答时请提醒用户这是示例。")
        if upcoming:
            lines.append(f"即将到来的考试（数据来源：{source}，更新于 {exam_snap['fetched_at']}）：")
            for e in upcoming[:12]:
                lines.append(f"- {exam_line(e)}")
        else:
            lines.append("暂无未结束的考试安排。")

    events = [Event.from_dict(d) for d in db.list_events()]
    upcoming_events = sorted(
        [e for e in events if not e.is_finished()],
        key=lambda e: (e.date, e.start or "00:00"),
    )
    if upcoming_events:
        lines.append("个人日程（用户手动添加的安排）：")
        for e in upcoming_events[:20]:
            lines.append(f"- {event_line(e)}")
    else:
        lines.append("暂无个人日程。")
    return "\n".join(lines)


SYSTEM_PROMPT = """你是「Agenda 知序」，青岛理工大学学生的校园 AI 助理。
你可以基于下方提供的实时课表、考试与个人日程上下文，回答学生的课程、教室、时间、教学周、考试与日程安排等问题。
规则：
1. 只依据上下文中的课表/考试/日程数据回答相关问题；数据中没有的信息明确说不知道，不要编造。
2. 回答简洁友好，用中文；涉及时间地点时给出具体日期、节次、时间、教室和座号。
3. 可以做简单规划建议（如哪天没课、考试要带什么），但提醒以教务系统为准。
4. 与课程、考试、日程无关的通用问题也可以正常回答。
5. 当用户明确要求添加/记录一条日程时（如「明天下午三点提醒我开会」），从对话提取信息，
   在回复末尾单独一行输出：
   [添加日程] {{"title": "...", "date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM", "location": "...", "note": "..."}}
   日期根据上下文中的今天推算（明天/后天/本周五等）；时间用 24 小时制；不确定的字段留空字符串；
   全天安排 start/end 都留空；一次最多输出一行。系统会把这行渲染成确认卡片，用户点击后才真正保存，
   所以回复里不要说「已添加」，可以说「确认后就会存入你的日程」。

课表、考试与日程上下文：
{context}"""


def _request_headers(ai: dict) -> dict:
    """基础 Bearer 鉴权 + 用户自定义请求头（可覆盖 Authorization，适配各种网关）。"""
    raw = ai.get("headers") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            raise ValueError("AI 自定义请求头不是合法 JSON 对象")
    if not isinstance(raw, dict):
        raise ValueError("AI 自定义请求头必须是 JSON 对象，如 {\"X-API-Key\": \"xxx\"}")
    headers = {"Authorization": f"Bearer {ai.get('api_key', '')}"}
    headers.update({str(k): str(v) for k, v in raw.items()})
    return headers


async def list_models() -> list[str]:
    """探测当前服务可用的模型（GET /models）。"""
    ai = load_config().get("ai", {})
    base_url = (ai.get("base_url") or "").rstrip("/")
    if not base_url or not ai.get("api_key"):
        raise ValueError("请先填写接口地址与 API Key")
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.get(f"{base_url}/models", headers=_request_headers(ai))
    if r.status_code != 200:
        raise ValueError(f"模型列表接口返回 {r.status_code}：{r.text[:200]}")
    try:
        data = r.json()
    except ValueError:
        raise ValueError("模型列表接口未返回 JSON")
    return [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


async def test_connection() -> dict:
    """用当前配置发一条极短请求，验证模型服务是否可用。"""
    ai = load_config().get("ai", {})
    base_url = (ai.get("base_url") or "").rstrip("/")
    if not base_url or not ai.get("api_key"):
        raise ValueError("请先填写接口地址与 API Key")
    async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
        r = await client.post(
            f"{base_url}/chat/completions",
            headers=_request_headers(ai),
            json={"model": ai.get("model") or "qwen-plus",
                  "messages": [{"role": "user", "content": "只回复两个字：正常"}],
                  "temperature": 0},
        )
    if r.status_code != 200:
        raise ValueError(f"接口返回 {r.status_code}：{r.text[:200]}")
    data = r.json()
    reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"ok": True, "model": ai.get("model"), "reply": str(reply)[:100]}


async def chat(message: str, history: list[dict] | None = None) -> str:
    ai, base_url, messages = _prepare(message, history)
    headers = _request_headers(ai)
    async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
        r = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={"model": ai.get("model", "qwen-plus"), "messages": messages, "temperature": 0.4},
        )
        if r.status_code != 200:
            raise ValueError(f"AI 接口返回 {r.status_code}: {r.text[:200]}")
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise ValueError(f"AI 响应格式异常: {json.dumps(data, ensure_ascii=False)[:200]}")


def check_ready():
    """流式接口在开始响应前先校验配置，这样未配置能直接返回 400。"""
    cfg = load_config()
    ai = cfg.get("ai", {})
    if not ai.get("enabled") or not ai.get("api_key"):
        raise ValueError("AI 尚未配置：请在设置中启用 AI 并填写 API Key"
                         "（支持通义千问/DeepSeek/Ollama 等 OpenAI 兼容接口）")
    if not (ai.get("base_url") or "").strip():
        raise ValueError("AI 接口地址为空，请在设置中填写")


def _prepare(message: str, history: list[dict] | None):
    check_ready()
    cfg = load_config()
    ai = cfg.get("ai", {})
    base_url = (ai.get("base_url") or "").rstrip("/")
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=build_context(cfg))}]
    for h in (history or [])[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:4000]})
    messages.append({"role": "user", "content": message[:4000]})
    return ai, base_url, messages


def _sse_delta(line: str) -> str | None:
    """解析一行 SSE，返回增量文本；非 data 行、空 delta、[DONE] 返回 None。

    兼容标准 OpenAI（delta）和少数忽略 stream 参数、直接回 message 的服务。
    """
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    for key in ("delta", "message"):
        part = choices[0].get(key)
        if isinstance(part, dict) and part.get("content"):
            return str(part["content"])
    return None


async def chat_stream(message: str, history: list[dict] | None = None):
    """逐段产出模型回复，供 SSE 转发。"""
    ai, base_url, messages = _prepare(message, history)
    timeout = httpx.Timeout(120, connect=20)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions",
            headers=_request_headers(ai),
            json={"model": ai.get("model", "qwen-plus"), "messages": messages,
                  "temperature": 0.4, "stream": True},
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")[:200]
                raise ValueError(f"AI 接口返回 {r.status_code}: {body}")
            if "text/event-stream" not in r.headers.get("content-type", ""):
                # 服务端忽略了 stream 参数，按普通 JSON 一次性返回
                data = json.loads((await r.aread()).decode("utf-8", "replace"))
                try:
                    yield data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    raise ValueError(f"AI 响应格式异常: {json.dumps(data, ensure_ascii=False)[:200]}")
                return
            async for line in r.aiter_lines():
                chunk = _sse_delta(line)
                if chunk:
                    yield chunk