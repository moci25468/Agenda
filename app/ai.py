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
from .models import Course, Exam, apply_course_overrides, current_week
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
    return "\n".join(lines)


SYSTEM_PROMPT = """你是「青理课表助手」，青岛理工大学学生的校园 AI 助理。
你可以基于下方提供的实时课表与考试上下文回答学生的课程、教室、时间、教学周、考试安排等问题。
规则：
1. 只依据上下文中的课表/考试数据回答相关问题；数据中没有的信息明确说不知道，不要编造。
2. 回答简洁友好，用中文；涉及时间地点时给出具体日期、节次、时间、教室和座号。
3. 可以做简单规划建议（如哪天没课、考试要带什么），但提醒以教务系统为准。
4. 与课程、考试无关的通用问题也可以正常回答。

课表与考试上下文：
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
    cfg = load_config()
    ai = cfg.get("ai", {})
    if not ai.get("enabled") or not ai.get("api_key"):
        raise ValueError("AI 尚未配置：请在设置中启用 AI 并填写 API Key（支持通义千问/DeepSeek/Ollama 等 OpenAI 兼容接口）")
    base_url = (ai.get("base_url") or "").rstrip("/")
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=build_context(cfg))}]
    for h in (history or [])[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:4000]})
    messages.append({"role": "user", "content": message[:4000]})
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