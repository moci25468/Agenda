"""核心链路验证脚本：不依赖真实外部服务，全程使用隔离的临时数据目录。"""
import base64
import os
import sys
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="qut_verify_")
os.environ["CAMPUS_DATA_DIR"] = _TEST_DATA_DIR
sys.path.insert(0, ".")

from pathlib import Path
import time
import subprocess
import socket
from app.ai import (build_context, _request_headers, _sse_delta, SYSTEM_PROMPT,
                    weeks_mentioned, last_room_query, room_query_context,
                    free_sections, free_sections_context, _free_spans,
                    _config_status_lines, current_section_lines)
from app.providers.httpjson import courses_from_payload
from app.browser_login import (
    build_candidate, pick_best, _headers_for_config, _parse_body, _public_candidates,
    get_status, _mask_url,
)
from app.curl_import import analyze_payload, diagnose_url, parse_payload_text
from app.providers.httpjson import check_auth_expired, PortalProvider, _to_day
from app.providers.base import AuthExpiredError
from app.providers.zhengfang import zhengfang_configured
import httpx
from app.curl_import import parse_curl, guess_mapping
from app.config import load_config as _load_cfg, save_config as _save_cfg
from app import db as _db
from datetime import date, datetime, timedelta
from app.push import (push_all, send_wecom, _wecom_url, WECOM_API,
                      send_selected, available_channels, wecom_input_error)
from app.scheduler import (run_sync, format_changes, courses_on, section_time,
                           run_reminders, push_exam_reviews, TZ)
import app.scheduler as _sched
import app.weather as _weather
from app.weather import describe_code, weather_line
from app.study import advice_for, review_message
from app.user_context import current_user_id, set_current_user
from app.config import load_config, save_config
from app import db
from app.models import (
    Course, Event, Exam, apply_course_overrides, course_uid, diff_courses,
    current_week, parse_exam_time, parse_weeks,
)
import asyncio
import json

_db.init_db()   # 隔离目录是空的，先建表（含旧库迁移检测）
asyncio.run(run_sync(push=False))   # 生成一份演示快照，供后续筛选用例使用

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" +
          (f"  [{detail}]" if detail and not cond else ""))


# 1. diff：新增 / 取消 / 教室调整
old = [Course("数据结构", day=2, start=1, end=2, room="1-402", weeks=list(range(1, 17))),
       Course("大学英语", day=1, start=3, end=4, room="2-305", weeks=list(range(1, 17)))]
new = [Course("数据结构", day=2, start=1, end=2, room="1-501", weeks=list(range(1, 17))),  # 教室变更
       # 新增；大学英语被取消
       Course("新增课程", day=5, start=1, end=2, weeks=[1, 2, 3])]
d = diff_courses(old, new)
check("diff.新增检测", len(d["added"]) ==
      1 and d["added"][0]["name"] == "新增课程", str(d))
check("diff.取消检测", len(d["removed"]) ==
      1 and d["removed"][0]["name"] == "大学英语", str(d))
check("diff.教室调整检测", len(d["changed"]) == 1 and d["changed"]
      [0]["fields"]["room"]["after"] == "1-501", str(d))
check("diff.变更文本格式化", "教室" in format_changes(load_config(), d))

# 2. 周次调整（同课同节次，周次变化 → changed 而非 added+removed）
old2 = [Course("物理", day=3, start=5, end=6, weeks=list(range(1, 17)))]
new2 = [Course("物理", day=3, start=5, end=6, weeks=list(range(1, 9)))]
d2 = diff_courses(old2, new2)
check("diff.周次调整归为 changed", not d2["added"] and not d2["removed"] and len(
    d2["changed"]) == 1, str(d2))

# 3. 教学周计算（2026-2027-1 学期自 2026-08-31 开始）
check("周次计算", current_week("2026-08-31", date(2026, 11, 7)) == 10)
check("开学前为第0周", current_week("2026-08-31", date(2026, 8, 1)) == 0)

# 3a. 正方周次字符串解析（含单双周、第N周、中文分隔符）
check("周次.普通范围", parse_weeks("1-16周") == (list(range(1, 17)), ""))
check("周次.第N周", parse_weeks("第15周") == ([15], ""))
check("周次.连续单周", parse_weeks("1-15周(单)") == (list(range(1, 16, 2)), "单周"))
check("周次.混合单双", parse_weeks("1-3周(单),4-8周(双),9周")
      == ([1, 3, 4, 6, 8, 9], ""))
check("周次.全角括号", parse_weeks("1-15周（双）")[0] == list(range(2, 16, 2)))
check("周次.顿号分隔", parse_weeks("2周、5-7周(单)") == ([2, 5, 7], ""))
check("周次.双周段加连续段", parse_weeks("2-4周(双),5-10周")
      == ([2, 4, 5, 6, 7, 8, 9, 10], ""))
check("周次.正方实测第N周", parse_weeks("第15周") == ([15], ""))
check("周次.无法解析保留原文", parse_weeks("另行通知") == ([], "另行通知"))

# 3b. 手动调整叠加：隐藏 / 修改 / 新增，不改动原始快照
_auto = [Course("高数", day=1, start=1, end=2, room="1-101",
                weeks=[1, 2, 3], source_id="s1")]
_uid = course_uid(_auto[0])
_eff = apply_course_overrides(_auto, {})
check("课表.稳定 uid", len(_eff) == 1 and _eff[0].uid == _uid
      and course_uid(_eff[0]) == _uid)
check("课表.隐藏课程", apply_course_overrides(_auto, {"hidden": [_uid]}) == [])
_eff = apply_course_overrides(_auto, {"edited": {_uid: {
    "name": "高数 A", "room": "1-201"}}})
check("课表.修改保留原字段", len(_eff) == 1 and _eff[0].name == "高数 A"
      and _eff[0].room == "1-201" and _eff[0].day == 1 and _eff[0].weeks == [1, 2, 3],
      str(_eff[0].to_dict()))
_eff = apply_course_overrides(_auto, {"added": [{
    "name": "自习", "day": 7, "start": 9, "end": 10, "weeks": [4, 5]}]})
check("课表.新增课程", len(_eff) == 2 and _eff[1].name == "自习"
      and _eff[1].source_id.startswith("manual:")
      and _eff[1].uid == _eff[1].source_id, str(_eff[1].to_dict()))
check("课表.原始快照不变", _auto[0].name == "高数" and _auto[0].room == "1-101")

# 3b-2. 考试手动调整叠加：隐藏 / 修改 / 新增，不改动原始快照
from app.models import apply_exam_overrides, exam_uid  # noqa: E402
_auto_exam = [Exam.from_dict({"name": "高等数学", "time": "2026-11-12 09:00-11:00",
                              "room": "1-101", "seat": "12", "source_id": "e1"})]
_euid = exam_uid(_auto_exam[0])
_eff = apply_exam_overrides(_auto_exam, {})
check("考试.稳定 uid", len(_eff) == 1 and _eff[0].uid == _euid
      and exam_uid(_eff[0]) == _euid)
check("考试.隐藏考试", apply_exam_overrides(_auto_exam, {"hidden": [_euid]}) == [])
_eff = apply_exam_overrides(_auto_exam, {"edited": {_euid: {
    "room": "2-202", "date": "2026-11-13", "start": "14:00", "end": "16:00"}}})
check("考试.修改保留原字段", len(_eff) == 1 and _eff[0].room == "2-202"
      and _eff[0].date == "2026-11-13" and _eff[0].start == "14:00"
      and _eff[0].name == "高等数学" and _eff[0].seat == "12"
      and exam_uid(_eff[0]) == _euid, str(_eff[0].to_dict()))
_eff = apply_exam_overrides(_auto_exam, {"added": [{
    "name": "英语四级", "date": "2026-12-01", "start": "09:00", "end": "11:20"}]})
check("考试.新增考试", len(_eff) == 2 and _eff[1].name == "英语四级"
      and _eff[1].source_id.startswith("manual:")
      and _eff[1].uid == _eff[1].source_id, str(_eff[1].to_dict()))
check("考试.原始快照不变", _auto_exam[0].room == "1-101"
      and _auto_exam[0].date == "2026-11-12")

# 3b. 考试时间解析与状态
check("考试.标准时间解析",
      parse_exam_time("2026-11-12 09:00-11:00") == ("2026-11-12", "09:00", "11:00"))
check("考试.中文时间解析",
      parse_exam_time("2026年11月12日 9:00—11:00") == ("2026-11-12", "09:00", "11:00"))
check("考试.带括号时间解析",
      parse_exam_time("2026-11-12(14:00-16:00)") == ("2026-11-12", "14:00", "16:00"))
check("考试.无法解析时留空", parse_exam_time("另行通知") == ("", "", ""))
exam = Exam.from_dict({"name": "高等数学", "time": "2026-11-12 09:00-11:00",
                       "room": "1-201", "seat": "12"})
check("考试.模型回填日期节次", exam.date == "2026-11-12" and exam.start == "09:00"
      and exam.end == "11:00", str(exam.to_dict()))
check("考试.结束判定", exam.is_finished(datetime(2026, 11, 12, 12, 0))
      and not exam.is_finished(datetime(2026, 11, 12, 10, 0)))
check("考试.展示时间", exam.display_time() == "2026-11-12 09:00-11:00")
check("节次.开始与结束可分别覆盖",
      section_time({"section_times": {"1": "08:10"}, "section_ends": {"1": "08:55"}}, 1)
      == "08:10"
      and section_time({"section_times": {"1": "08:10"},
                        "section_ends": {"1": "08:55"}}, 1, end=True) == "08:55")
check("考试.正方凭证识别",
      zhengfang_configured({"provider": {"zf_base": "x", "zf_cookie": "c"}})
      and zhengfang_configured({"provider": {
          "zf_base": "x", "zf_username": "u", "zf_password": "p"}})
      and not zhengfang_configured({"provider": {"zf_base": "x"}}))

from app.providers.zhengfang import rsa_encrypt, _hidden_value, _page_message
_rsa_n = base64.b64encode(((1 << 1023) + 0x12345).to_bytes(128, "big")).decode()
_rsa_enc = base64.b64decode(rsa_encrypt("p@ss", _rsa_n, base64.b64encode(b"\x01\x00\x01").decode()))
check("正方.RSA 密文长度", len(_rsa_enc) == 128)
check("正方.登录页隐藏域解析",
      _hidden_value('<input id="csrftoken" name="csrftoken" value="a-b,c-d"/>', "csrftoken") == "a-b,c-d"
      and _hidden_value('<input type="hidden" name="mmsfjm" id="mmsfjm" value= 1>', "mmsfjm") == "1")
check("正方.登录失败提示解析",
      _page_message('<div class="alert" id="tips"><span>!</span>用户名或密码不正确</div>')
      == "用户名或密码不正确")

# 正方学期：学年归一化、当前学期推断、空学期自动切换
from app.providers.zhengfang import (normalize_year, default_term, term_label,
                                    TermEmptyError, ZhengfangProvider)

check("正方.学年归一化", normalize_year("2026-2027") == "2026"
      and normalize_year(" 2026 ") == "2026" and normalize_year("2026/2027") == "2026"
      and normalize_year("abc") == "" and normalize_year("26") == "")
check("正方.当前学期按日期推断", default_term(date(2026, 11, 9)) == ("2026", "3")
      and default_term(date(2027, 1, 20)) == ("2026", "3")
      and default_term(date(2027, 2, 15)) == ("2026", "12")
      and default_term(date(2027, 8, 1)) == ("2027", "3"))
check("正方.学期标签", term_label("2026", "3") == "2026-2027 学年 第一学期（秋季）"
      and term_label("2026", "12") == "2026-2027 学年 第二学期（春季）")


class _ZfResp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text = payload, text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _ZfClient:
    """按调用顺序返回预设响应的假 httpx 客户端。"""

    def __init__(self, *responses):
        self._responses = list(responses)

    async def post(self, *a, **k):
        return self._responses.pop(0)

    async def get(self, *a, **k):
        return self._responses.pop(0)


def _zf_cfg(**zf):
    conf = {"provider": {"zhengfang": {
        "base": "http://example.invalid/jwglxt", "username": "u", "password": "p",
        "year": "2026", "term": "3"}}}
    conf["provider"]["zhengfang"].update(zf)
    return conf


def _zf_index(year_sel: str, term_sel: str) -> str:
    """模拟课表页里教务默认选中的学年/学期。"""
    return (
        f'<select name="xnm" id="xnm"><option value="2027">2027-2028</option>'
        f'<option value="{year_sel}" selected>{year_sel}</option></select>'
        f'<select name="xqm" id="xqm"><option value="">请选择</option>'
        f'<option value="3"{" selected" if term_sel == "3" else ""}>1</option>'
        f'<option value="12"{" selected" if term_sel == "12" else ""}>2</option></select>'
    )


_zfp = ZhengfangProvider(_zf_cfg())
check("正方.学年为空按日期推断",
      (ZhengfangProvider(_zf_cfg(year="", term="")).year,
       ZhengfangProvider(_zf_cfg(year="", term="")).term) == default_term())
check("正方.学年支持 2026-2027 写法",
      ZhengfangProvider(_zf_cfg(year="2026-2027")).year == "2026")

try:
    asyncio.run(_zfp._fetch_courses(_ZfClient(_ZfResp({"kbList": []}))))
    _zf_empty = ""
except TermEmptyError as e:
    _zf_empty = str(e)
check("正方.空学期报错带学期标签",
      "2026-2027 学年 第一学期（秋季）" in _zf_empty and "没有排课数据" in _zf_empty,
      _zf_empty)
try:
    asyncio.run(_zfp._fetch_courses(_ZfClient(_ZfResp(None))))
    _zf_null = ""
except TermEmptyError as e:
    _zf_null = str(e)
check("正方.学年参数不被接受时报学期", "2026-2027" in _zf_null, _zf_null)

check("正方.识别教务当前学期",
      asyncio.run(_zfp.detect_current_term(
          _ZfClient(_ZfResp(text=_zf_index("2026", "3"))))) == ("2026", "3")
      and asyncio.run(_zfp.detect_current_term(
          _ZfClient(_ZfResp(text="<html></html>")))) is None)

_zfp_spring = ZhengfangProvider(_zf_cfg(term="12"))
_kb = {"kbList": [{"xqj": "1", "jcs": "1-2", "kcmc": "测试课", "zcd": "1-8周"}]}
_courses, _switched = asyncio.run(_zfp_spring.fetch_courses_with_term_fix(
    _ZfClient(_ZfResp({"kbList": []}), _ZfResp(text=_zf_index("2026", "3")),
              _ZfResp(_kb))))
check("正方.空学期自动切到教务当前学期",
      _switched == ("2026", "3") and _zfp_spring.term == "3"
      and len(_courses) == 1 and _courses[0].name == "测试课", str(_switched))
try:
    asyncio.run(ZhengfangProvider(_zf_cfg()).fetch_courses_with_term_fix(
        _ZfClient(_ZfResp({"kbList": []}), _ZfResp(text=_zf_index("2026", "3")))))
    _zf_same = ""
except TermEmptyError as e:
    _zf_same = str(e)
check("正方.当前学期也无排课时不切换", "没有排课数据" in _zf_same, _zf_same)

db.save_exam_snapshot([exam.to_dict()], "test")
esnap = db.get_exam_snapshot()
check("考试.快照落库", bool(esnap) and esnap["source"] == "test"
      and esnap["exams"][0]["name"] == "高等数学", str(esnap)[:160])
# AI 上下文只展示尚未结束的考试：固定日期会过期，这里临时换成明天的同一场考试
_future_day = (date.today() + timedelta(days=1)).isoformat()
_future_exam = Exam.from_dict({"name": "高等数学",
                               "time": f"{_future_day} 09:00-11:00",
                               "room": "1-201", "seat": "12"})
db.save_exam_snapshot([_future_exam.to_dict()], "test")
ctx = build_context(load_config())
check("考试.AI 上下文可回答教室座号",
      "高等数学" in ctx and "@1-201" in ctx and "座号 12" in ctx, ctx[-220:])
db.save_exam_snapshot(esnap["exams"], esnap["source"])  # 恢复，避免影响后续用例

# 个人日程：模型、落库、AI 上下文
_ev = Event.from_dict({"title": "社团例会", "date": "2026-11-09",
                       "start": "19:00", "end": "20:30", "location": "活动中心"})
check("日程.时间描述", _ev.display_time() == "2026-11-09 19:00-20:30")
check("日程.全天描述", Event.from_dict(
    {"title": "休息", "date": "2026-11-09"}).display_time() == "2026-11-09 全天")
check("日程.结束判定", _ev.is_finished(datetime(2026, 11, 9, 21, 0))
      and not _ev.is_finished(datetime(2026, 11, 9, 18, 0))
      and not _ev.is_finished(datetime(2026, 11, 8, 23, 0)))
_saved_ev = db.add_event({"title": "社团例会", "date": "2099-12-31",
                          "start": "19:00", "end": "20:30", "location": "活动中心"})
check("日程.落库与读取", bool(_saved_ev["id"])
      and db.get_event(_saved_ev["id"])["title"] == "社团例会", str(_saved_ev)[:160])
_ctx_ev = build_context(load_config())
check("日程.AI 上下文包含", "社团例会" in _ctx_ev and "@活动中心" in _ctx_ev, _ctx_ev[-200:])
_prompt = SYSTEM_PROMPT.format(context="占位上下文")
check("日程.提示词模板可渲染", "占位上下文" in _prompt and "[添加日程]" in _prompt
      and '"title"' in _prompt)
check("AI.推荐追问标记可渲染", "[追问]" in _prompt and "2-3 个" in _prompt)
db.delete_event(_saved_ev["id"])
check("日程.删除后不可读", db.get_event(_saved_ev["id"]) is None)

# 长期记忆：落库、去重、注入 AI 上下文、删除
_mem = db.add_memory("我喜欢坐前排")
check("记忆.落库", bool(_mem["id"]) and not _mem["duplicate"], str(_mem)[:160])
_mem2 = db.add_memory("我喜欢坐前排")
check("记忆.重复去重", _mem2["duplicate"] and _mem2["id"] == _mem["id"])
_ctx_mem = build_context(load_config())
check("记忆.AI 上下文包含", "我喜欢坐前排" in _ctx_mem and "长期记忆" in _ctx_mem,
      _ctx_mem[-200:])
_prompt_mem = SYSTEM_PROMPT.format(context="占位上下文")
check("记忆.提示词模板可渲染", "[记住]" in _prompt_mem and '"content"' in _prompt_mem)
check("记忆.删除与幂等", db.delete_memory(_mem["id"])
      and not db.delete_memory(_mem["id"]))
check("记忆.删除后上下文不含", "我喜欢坐前排" not in build_context(load_config()))

# 对话短期记忆：整体替换（编辑截断）、读取、清空
db.replace_chat_history([{"role": "user", "content": "你好"},
                         {"role": "assistant", "content": "你好呀"}])
check("对话.落库读取", db.get_chat_history() == [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好呀"}], str(db.get_chat_history())[:160])
db.replace_chat_history([{"role": "user", "content": "截断后"}])
check("对话.整体替换即截断", db.get_chat_history() == [
    {"role": "user", "content": "截断后"}])
db.clear_chat_history()
check("对话.清空", db.get_chat_history() == [])

# 空教室：位掩码参数与响应解析（2026-11-08 正方实测协议）
from app.providers.zhengfang import room_query_data, parse_room
_rq = room_query_data("2026", "3", 10, 1, 1, 2, "05", "5011")
check("空教室.周次节次位掩码",
      _rq["zcd"] == "512" and _rq["jcd"] == "3" and _rq["xqj"] == "1"
      and _rq["jyfs"] == "0" and _rq["lh"] == "5011", str(_rq)[:200])
_rq2 = room_query_data("2026", "3", 1, 7, 9, 11, "", "")
check("空教室.多节位掩码", _rq2["zcd"] == "1"
      and _rq2["jcd"] == str(2**8 + 2**9 + 2**10), str(_rq2)[:200])
_room = parse_room({"cd_id": "x", "cdbh": " C2-4-114", "cdmc": "虚拟仿真教室",
                    "jxlmc": "环境实验楼", "xqmc": "黄岛校区", "cdlbmc": "实验室",
                    "zws": "100", "kszws1": "80"})
check("空教室.响应解析", _room["code"] == "C2-4-114" and _room["seats"] == 100
      and _room["building"] == "环境实验楼" and _room["campus"] == "黄岛校区",
      str(_room)[:200])

# 空教室 × AI：提示词规则 7 可渲染（JSON  braces 已转义）、上下文按配置状态提示
_prompt_room = SYSTEM_PROMPT.format(context="占位上下文")
check("空教室.AI 提示词模板可渲染", "[查空教室]" in _prompt_room
      and '"date"' in _prompt_room and '"campus"' in _prompt_room)
_ctx_room = build_context(load_config())
check("空教室.AI 上下文未配置提示", "空教室查询：未配置" in _ctx_room, _ctx_room[-160:])
_cfg_room = load_config()
_cfg_room["provider"] = {**_cfg_room.get("provider", {}), "zhengfang": {
    **_cfg_room.get("provider", {}).get("zhengfang", {}),
    "username": "20260001", "password": "dummy"}}
save_config(_cfg_room)
check("空教室.AI 上下文已就绪提示",
      "空教室查询：已就绪" in build_context(load_config()))
# 空教室默认校区：配置项存在，设置后注入 AI 上下文与配置现状，未设置则不出现
check("默认校区.配置含默认项",
      "default_campus" in load_config().get("rooms", {}), "")
_cfg_room["rooms"] = {"default_campus": "05", "default_campus_name": "黄岛校区"}
save_config(_cfg_room)
_ctx_def = build_context(load_config())
check("默认校区.已注入 AI 上下文",
      "用户默认校区：黄岛校区" in _ctx_def, _ctx_def[-300:])
check("默认校区.配置现状含默认校区",
      "空教室默认校区：黄岛校区" in _ctx_def, _ctx_def[-500:])
check("默认校区.提示词要求按默认查询",
      "用户默认校区" in SYSTEM_PROMPT, "")
_cfg_room["rooms"] = {"default_campus": "", "default_campus_name": ""}
save_config(_cfg_room)
check("默认校区.未设置不注入",
      "用户默认校区：" not in build_context(load_config()), "")
_cfg_room["provider"]["zhengfang"]["username"] = ""
_cfg_room["provider"]["zhengfang"]["password"] = ""
save_config(_cfg_room)

# 空教室多轮追问：沿用最近一次查询的日期/节次/校区，不切回今天
_room_hist = [
    {"role": "user", "content": "明天我想找一个空教室"},
    {"role": "assistant", "content":
     '好的，先查明天下午。\n[查空教室] {"date": "2026-11-10", "start": 5, "end": 8, '
     '"campus": "黄岛校区", "building": ""}\n[追问] ["上午三四节呢", "换到市北校区"]'},
    {"role": "assistant", "content":
     '已查询。\n[空教室结果] {"date": "2026-11-10", "start": 5, "end": 8, '
     '"campus": "黄岛校区", "building": "", "count": 3, "groups": []}'},
    {"role": "user", "content": "我想找一个小一点的教室"},
]
_q_last = last_room_query(_room_hist)
check("空教室.追问沿用上次条件", bool(_q_last)
      and _q_last["date"] == "2026-11-10" and _q_last["start"] == 5
      and _q_last["end"] == 8 and _q_last["campus"] == "黄岛校区", str(_q_last))
_room_ctx = room_query_context(_room_hist)
check("空教室.上下文写明确切日期",
      "最近一次空教室查询条件" in _room_ctx and "2026-11-10" in _room_ctx
      and "第 5–8 节" in _room_ctx and "不要切回今天" in _room_ctx, _room_ctx)
_room_hist2 = _room_hist + [
    {"role": "assistant", "content":
     '[空教室结果] {"date": "2026-11-11", "start": 1, "end": 2, '
     '"campus": "市北校区", "building": "一号教学楼"}'}]
check("空教室.取最近一次查询", last_room_query(_room_hist2)["date"] == "2026-11-11"
      and "一号教学楼" in room_query_context(_room_hist2), "")
check("空教室.无历史不注入",
      last_room_query([]) is None and room_query_context(None) == "", "")
check("空教室.失败结果不算条件",
      last_room_query([{"role": "assistant", "content":
                        '[空教室结果] {"error": "超时", "date": "2026-11-10", '
                        '"start": 5, "end": 8}'}]) is None, "")
check("空教室.提示词含追问规则", "10.1 空教室多轮追问" in SYSTEM_PROMPT, "")

# 空闲节次：后端按课表算好注入 AI，问「我没课的时候」不再靠模型自己数课表
check("空闲节次.区间合并", _free_spans({1, 2, 5, 6}) == [(3, 4), (7, 12)]
      and _free_spans(set()) == [(1, 12)]
      and _free_spans(set(range(1, 13))) == [], "")
_fs_orig = db.get_snapshot()
db.save_snapshot([
    Course("占用A", day=2, start=1, end=2, weeks=list(range(1, 31))).to_dict(),
    Course("占用B", day=2, start=5, end=6, weeks=list(range(1, 31))).to_dict(),
], "test")
_fs_cfg = load_config()
_fs_cfg["semester_start"] = "2026-08-31"
check("空闲节次.按课表算空闲",
      free_sections(_fs_cfg, date(2026, 11, 10)) == [(3, 4), (7, 12)],
      str(free_sections(_fs_cfg, date(2026, 11, 10))))
_fs_ctx = free_sections_context(_fs_cfg, [
    {"role": "assistant", "content":
     '[空教室结果] {"date": "2026-11-12", "start": 1, "end": 2}'}])
check("空闲节次.注入含查询日期", "用户空闲节次" in _fs_ctx
      and "2026-11-12" in _fs_ctx and "当天无课" in _fs_ctx, _fs_ctx)
if _fs_orig:
    db.save_snapshot(_fs_orig["courses"], _fs_orig["source"])
check("空闲节次.提示词引用", "用户空闲节次" in SYSTEM_PROMPT, "")

# 当前节次：AI 回答「现在去哪自习」不能漏掉正在上的这一节
from datetime import datetime as _dt
_cs_cfg = load_config()
_cs_1430 = current_section_lines(_cs_cfg, _dt(2026, 11, 9, 14, 30))
check("当前节次.正在上的一节",
      "正在上第 5 节" in _cs_1430 and "必须包含第 5 节" in _cs_1430, _cs_1430)
_cs_0955 = current_section_lines(_cs_cfg, _dt(2026, 11, 9, 9, 55))
check("当前节次.课间给最近一节",
      "不在上课时段" in _cs_0955 and "第 3 节" in _cs_0955, _cs_0955)
_cs_2300 = current_section_lines(_cs_cfg, _dt(2026, 11, 9, 23, 0))
check("当前节次.今天已放学", "都已结束" in _cs_2300, _cs_2300)
check("当前节次.注入AI上下文", "当前节次" in build_context(_fs_cfg), "")
check("当前节次.提示词含现在规则",
      "正在上的这一节" in SYSTEM_PROMPT and "现在" in SYSTEM_PROMPT, "")

# 应用辅导 + 配置状态注入 AI 上下文
_ctx_cfg = build_context(load_config())
check("辅导.AI 上下文含配置现状",
      "应用配置现状" in _ctx_cfg and "推送" in _ctx_cfg and "日历订阅" in _ctx_cfg
      and "Bark" in _ctx_cfg, _ctx_cfg[-400:])
_prompt_guide = SYSTEM_PROMPT.format(context="占位上下文")
check("辅导.提示词含应用使用说明",
      "应用使用辅导" in _prompt_guide and "Bark" in _prompt_guide
      and "一键订阅" in _prompt_guide, "")
check("推送.提示词含推送标记规则",
      "[推送]" in _prompt_guide and '"channels"' in _prompt_guide
      and "企业微信" in _prompt_guide and "12. 用户明确要求" in _prompt_guide, "")
# 可用通道：只列已开启且已配置的通道，且不泄露具体 Key / webhook
_pcfg = load_config()
_pcfg["push"] = {**_pcfg.get("push", {}), "bark_enabled": True,
                 "bark_key": "SECRETKEY123", "wecom_enabled": True,
                 "wecom_webhook": "http://127.0.0.1:9/hook"}
_plines = _config_status_lines(_pcfg)
check("推送.上下文列出可用通道",
      "可用于主动推送的通道" in _plines and '"bark"' in _plines
      and '"wecom"' in _plines and "SECRETKEY123" not in _plines
      and "127.0.0.1" not in _plines, _plines)
_pcfg["push"]["bark_enabled"] = False
_pcfg["push"]["wecom_enabled"] = False
_plines_off = _config_status_lines(_pcfg)
check("推送.未开启时不列通道",
      "无（不能主动推送" in _plines_off, _plines_off)
check("推送.可用通道函数与开关一致",
      available_channels(_pcfg) == [], str(available_channels(_pcfg)))
# 敏感值不泄露：配置了 Bark Key 后，上下文不应出现具体 Key
_cfg_sens = load_config()
_cfg_sens["push"] = {**_cfg_sens.get("push", {}),
                     "bark_enabled": True, "bark_key": "SECRETKEY123"}
save_config(_cfg_sens)
_ctx_sens = build_context(load_config())
check("辅导.上下文不泄露 Bark Key",
      "SECRETKEY123" not in _ctx_sens and "已配置" in _ctx_sens, _ctx_sens[-400:])
_cfg_sens["push"]["bark_enabled"] = False
_cfg_sens["push"]["bark_key"] = ""
save_config(_cfg_sens)

# 天气按天摘要 + 注入 AI 上下文
_orig_fetch2 = _weather.fetch_hourly
from datetime import date as _date
_today_s = _date.today().isoformat()
_tomorrow_s = (_date.today() + timedelta(days=1)).isoformat()
async def _fake_hourly2(lat, lon):
    return {
        f"{_today_s}T08:00": {"temp": 12, "code": 61, "pop": 40},
        f"{_today_s}T12:00": {"temp": 18, "code": 1, "pop": 10},
        f"{_today_s}T20:00": {"temp": 14, "code": 3, "pop": 20},
        f"{_tomorrow_s}T08:00": {"temp": 10, "code": 71, "pop": 30},
    }
_weather.fetch_hourly = _fake_hourly2
from app.weather import daily_summary
_ws = asyncio.run(daily_summary(load_config(), _today_s))
check("天气.按天摘要", "12~18℃" in _ws and "小雨" in _ws, _ws)
_ws2 = asyncio.run(daily_summary(load_config(), _tomorrow_s))
check("天气.次日摘要", "10℃" in _ws2 and "小雪" in _ws2, _ws2)
# 未开启天气时摘要为空
_wcfg_off = load_config()
_wcfg_off["weather"] = {**_wcfg_off.get("weather", {}), "enabled": False}
save_config(_wcfg_off)
check("天气.关闭后摘要为空",
      asyncio.run(daily_summary(load_config(), _today_s)) == "")
_wcfg_off["weather"]["enabled"] = True
save_config(_wcfg_off)

# AI 上下文注入天气（异步 _weather_context，仍用 fake 数据）
from app.ai import _weather_context
_ctx_w = asyncio.run(_weather_context(load_config()))
check("天气.AI 上下文注入", "今日天气" in _ctx_w and "明日天气" in _ctx_w, _ctx_w)
_weather.fetch_hourly = _orig_fetch2

# 流式回复的 SSE 行解析（OpenAI 兼容格式）
check("流式.SSE 解析增量",
      _sse_delta('data: {"choices":[{"delta":{"content":"你好"}}]}') == "你好")
check("流式.忽略空增量与结束标记",
      _sse_delta('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None
      and _sse_delta("data: [DONE]") is None)
check("流式.兼容一次性 message",
      _sse_delta('data: {"choices":[{"message":{"content":"完整"}}]}') == "完整")
check("流式.坏行安全跳过",
      _sse_delta("data: not-json") is None and _sse_delta("event: ping") is None)

# 4. 明日课表筛选（2026-11-08 周日 → 无课；周一有课）
cfg = load_config()
snap = db.get_snapshot()
courses = [Course.from_dict(c) for c in snap["courses"]]
sun = courses_on(cfg, courses, date(2026, 11, 8))
mon = courses_on(cfg, courses, date(2026, 11, 9))
check("周日无课筛选", len(sun) == 0)
check("周一有课筛选", bool(mon) and all(
    c.day == 1 for c in mon), str([c.name for c in mon]))

# 4b. AI 上下文按周展开：问「下周的课」时必须能看到下周课程，且不能误答「课表未同步」
_w_now = current_week(cfg.get("semester_start", ""), date.today(), cfg.get("total_weeks", 30))
_w_total = cfg.get("total_weeks", 30) or 30
# 学期最后一周没有「下周」，改用上周，保证用例在任何日期都成立
_w_next = _w_now + 1 if _w_now < _w_total else max(1, _w_now - 1)
_w_orig = db.get_snapshot()
db.save_snapshot([
    {"name": "相邻周专属课", "day": 1, "start": 3, "end": 4, "room": "C301",
     "weeks": [_w_next]},
    {"name": "本周常规课", "day": 2, "start": 1, "end": 2, "room": "A101",
     "weeks": [_w_now]},
], "portal")
_ctx_weeks = build_context(cfg)
check("AI 上下文.下周课程可见", "相邻周专属课" in _ctx_weeks, _ctx_weeks[:300])
check("AI 上下文.本周课程可见", "本周常规课" in _ctx_weeks)
check("AI 上下文.按周标注", f"第 {_w_next} 周" in _ctx_weeks and "下周" in _ctx_weeks)
check("AI 上下文.已同步不误报", "课表已同步" in _ctx_weeks
      and "课表尚未同步" not in _ctx_weeks)
check("AI 上下文.范围外有说明", "上面只展开以下周次" in _ctx_weeks)
check("AI 上下文.提取指定周次", weeks_mentioned("我第 20 周有什么课？") == [20],
      str(weeks_mentioned("我第 20 周有什么课？")))
_ctx_far = build_context(cfg, extra_weeks=[20])
check("AI 上下文.按需展开指定周", "第20周" in _ctx_far and "第 20 周" in _ctx_far,
      _ctx_far[:200])
# 每周都上的课在 4 个周次里内容相同，应压缩成「与第 N 周完全相同」
db.save_snapshot([
    {"name": "每周固定课", "day": 3, "start": 1, "end": 2, "room": "B101", "weeks": []},
], "portal")
_ctx_dup = build_context(cfg)
check("AI 上下文.重复周课表去重", _ctx_dup.count("与第") >= 3, _ctx_dup[:300])
db.save_snapshot(_w_orig["courses"], _w_orig["source"])

# 课前提醒：固定 now + mock 推送，验证窗口 / 去重 / 开关 / 提前量 / 用户隔离
_orig_push = _sched.push_all
_push_calls = []


async def _fake_push(cfg, title, body):
    _push_calls.append((title, body))
    return [{"channel": "bark", "ok": True, "error": ""}]


_sched.push_all = _fake_push
_orig_weather = _sched.weather_line


async def _fake_weather(cfg, day_s, hhmm):
    return "上课时天气（青岛黄岛区）：多云 15℃"


_sched.weather_line = _fake_weather   # 提醒用例不访问真实天气接口
_orig_snap = db.get_snapshot()
_orig_esnap = db.get_exam_snapshot()
_orig_cfg = load_config()


def _at(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


_rcfg = load_config()
_rcfg["semester_start"] = "2026-08-31"   # 2026-11-09 起为第 11 周周一~周五
_rcfg["push"] = {**_rcfg.get("push", {}), "bark_enabled": True, "bark_key": "test",
                 "reminder_enabled": True, "reminder_advance": 15}
save_config(_rcfg)
db.save_snapshot([c.to_dict() for c in [
    Course("提醒测试课", day=1, start=1, end=2, room="1-101", weeks=[11]),
    Course("已开始课", day=1, start=2, end=2, weeks=[11]),
    Course("周二课", day=2, start=1, end=2, weeks=[11]),
    Course("周三课", day=3, start=1, end=2, weeks=[11]),
    Course("周四课", day=4, start=1, end=2, weeks=[11]),
    Course("连堂第一课", day=6, start=1, end=2, weeks=[11]),
    Course("连堂第二课", day=6, start=3, end=4, weeks=[11]),
]], "test")
_rem_ev = db.add_event({"title": "提醒测试日程", "date": "2026-11-09",
                        "start": "12:00", "end": "13:00", "location": "活动中心"})
db.save_exam_snapshot([{"name": "提醒测试考试", "time": "2026-11-09 14:00-16:00",
                        "room": "1-201", "seat": "12"}], "test")

asyncio.run(run_reminders(now=_at("2026-11-09T07:44:00")))
check("提醒.窗口外不推", not _push_calls, str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T07:50:00")))
check("提醒.课前15分钟内推送", len(_push_calls) == 1 and _push_calls[0][0] == "上课提醒"
      and "还有 10 分钟" in _push_calls[0][1] and "提醒测试课" in _push_calls[0][1]
      and "@1-101" in _push_calls[0][1], str(_push_calls))
check("提醒.上课附当堂天气", len(_push_calls) == 1
      and "上课时天气（青岛黄岛区）" in _push_calls[0][1], str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T07:51:00")))
check("提醒.同一事项只推一次", not _push_calls, str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T09:00:00")))
check("提醒.已开始不推", not _push_calls, str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T11:50:00")))
check("提醒.日程开始前推送", len(_push_calls) == 1 and _push_calls[0][0] == "日程提醒"
      and "提醒测试日程" in _push_calls[0][1] and "还有 10 分钟" in _push_calls[0][1],
      str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T13:50:00")))
check("提醒.考试开始前推送", len(_push_calls) == 1 and _push_calls[0][0] == "考试提醒"
      and "提醒测试考试" in _push_calls[0][1] and "座位 12" in _push_calls[0][1],
      str(_push_calls))

_push_calls.clear()
_rcfg = load_config()
_rcfg["push"]["reminder_enabled"] = False
save_config(_rcfg)
asyncio.run(run_reminders(now=_at("2026-11-10T07:50:00")))
check("提醒.关闭开关不推", not _push_calls, str(_push_calls))
_rcfg = load_config()
_rcfg["push"]["reminder_enabled"] = True
save_config(_rcfg)
asyncio.run(run_reminders(now=_at("2026-11-10T07:50:00")))
check("提醒.重新开启后推送", len(_push_calls) == 1
      and "周二课" in _push_calls[0][1], str(_push_calls))

_push_calls.clear()
_rcfg = load_config()
_rcfg["push"]["reminder_advance"] = 5
save_config(_rcfg)
asyncio.run(run_reminders(now=_at("2026-11-11T07:50:00")))
check("提醒.自定义提前量窗口外不推", not _push_calls, str(_push_calls))
asyncio.run(run_reminders(now=_at("2026-11-11T07:56:00")))
check("提醒.自定义提前量生效", len(_push_calls) == 1
      and "还有 4 分钟" in _push_calls[0][1], str(_push_calls))

# 连堂课只在第一节提醒里带天气
_rcfg = load_config()
_rcfg["push"]["reminder_advance"] = 15
save_config(_rcfg)
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-14T07:50:00")))
check("提醒.连堂第一节带天气", len(_push_calls) == 1
      and "连堂第一课" in _push_calls[0][1] and "天气" in _push_calls[0][1],
      str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-14T09:56:00")))
check("提醒.连堂第二节不带天气", len(_push_calls) == 1
      and "连堂第二课" in _push_calls[0][1] and "天气" not in _push_calls[0][1],
      str(_push_calls))

# 考试复习提醒：考前 15 天推一次，按考试名称给建议
db.save_exam_snapshot([{"name": "高等数学（上）", "time": "2026-11-24 09:00-11:00",
                        "room": "1-201", "seat": "8"}], "test")
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T07:50:00")))
check("复习.早八点前不推", not _push_calls, str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T09:00:00")))
check("复习.考前15天推送", len(_push_calls) == 1
      and _push_calls[0][0] == "考试复习提醒"
      and "高等数学（上）" in _push_calls[0][1]
      and "还有 15 天" in _push_calls[0][1]
      and "公式" in _push_calls[0][1], str(_push_calls))
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-09T09:01:00")))
check("复习.同一场只推一次", not _push_calls, str(_push_calls))
check("复习.数学建议命中", "公式" in advice_for("高等数学"))
check("复习.代码建议命中", "代码" in advice_for("数据结构"))
check("复习.陌生科目有通用建议", advice_for("神秘学导论").strip() != "")

# 用户隔离：999 的配置 / 快照 / 去重记录独立于默认用户
_ctx999 = set_current_user(999)
try:
    _cfg999 = load_config()
    _cfg999["semester_start"] = "2026-08-31"
    _cfg999["push"] = {**_cfg999.get("push", {}), "bark_enabled": True, "bark_key": "t",
                       "reminder_enabled": True, "reminder_advance": 15}
    save_config(_cfg999)
    db.save_snapshot([Course("周五隔离课", day=5, start=1, end=2,
                             weeks=[11]).to_dict()], "test")
    _push_calls.clear()
    asyncio.run(run_reminders(now=_at("2026-11-13T07:50:00")))
    check("提醒.按用户独立推送", len(_push_calls) == 1
          and "周五隔离课" in _push_calls[0][1], str(_push_calls))
    check("提醒.去重记录落当前用户",
          db.reminder_sent("2026-11-13", "course:周五隔离课:1:"))
finally:
    current_user_id.reset(_ctx999)
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-13T07:50:00")))
check("提醒.用户间互不串扰", not _push_calls
      and not db.reminder_sent("2026-11-13", "course:周五隔离课:1:"), str(_push_calls))

# 同步后推送逻辑：今天有剩余课 → 只推今日剩余；今天没课 → 推明日课表
# 明日课表按天去重（同步兜底推 / 每晚 22:00 定时推，同一天只推一次）
_sync_cfg = load_config()
_sync_cfg["push"] = {**_sync_cfg.get("push", {}), "bark_enabled": True, "bark_key": "t",
                     "push_next_day": True, "reminder_enabled": False}
# 把第 10 节的结束时间固定到当天最晚，让「今日剩余课程」不依赖真实时钟，
# 否则晚上跑测试时该分支永远为空，还会误标记明日课表去重导致后面连锁失败
_orig_section_ends = _sync_cfg.get("section_ends")
_sync_cfg["section_ends"] = {**(_orig_section_ends or {}), "10": "23:59"}
save_config(_sync_cfg)
# 清空快照与提醒记录，避免前面测试的课程/去重记录干扰
db.save_snapshot([], "test")
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")

# 1) 今天有剩余课（周一 day=1，用真实 today 判断）→ 只推「今日剩余课程」
_today = date.today()
_today_dow = _today.isoweekday()
_today_course = [Course("今日课", day=_today_dow, start=9, end=10,
                        weeks=list(range(1, 20)))]
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(_sync_cfg, _today_course, "test", push=True))
check("同步推送.今天有课只推今日剩余",
      len(_push_calls) == 1 and "今日剩余课程" in _push_calls[0][1]
      and "今日课" in _push_calls[0][1] and "明日课表" not in _push_calls[0][1],
      str(_push_calls))

# 2) 今天没课 → 推「明日课表」
_tmr_dow = (_today + timedelta(days=1)).isoweekday()
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(_sync_cfg,
      [Course("明日课", day=_tmr_dow, start=1, end=2, weeks=list(range(1, 20)))],
      "test", push=True))
check("同步推送.今天没课推明日课表",
      len(_push_calls) == 1 and "明日课表" in _push_calls[0][1]
      and "明日课" in _push_calls[0][1], str(_push_calls))

# 3) 明日课表当天已推过 → 再次同步不重复推
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(_sync_cfg,
      [Course("明日课", day=_tmr_dow, start=1, end=2, weeks=list(range(1, 20)))],
      "test", push=True))
check("同步推送.明日已推不重复", not _push_calls, str(_push_calls))

# 4) 每晚 22:00 定时任务按天去重：已推过则不再推
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T22:00:00")))
check("定时推送.明日已推则跳过", not _push_calls, str(_push_calls))

# 清理去重记录，验证定时任务在「未推过」时能推
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T22:00:00")))
check("定时推送.未推则推送明日", len(_push_calls) == 1
      and "明日课表" in _push_calls[0][0], str(_push_calls))

# 清理本段测试的去重记录，并恢复节次时间，避免影响后续
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
if _orig_section_ends is None:
    _sync_cfg.pop("section_ends", None)
else:
    _sync_cfg["section_ends"] = _orig_section_ends
save_config(_sync_cfg)

# 每日推送：时间可配置 + 内容可选（课表 / 日程考试 / 天气）
_dc = load_config()
_dc["push"] = {**_dc.get("push", {}), "daily_time": "21:30",
               "daily_courses": True, "daily_events": True, "daily_weather": False}
save_config(_dc)
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T22:00:00")))
check("每日推送.非配置时间不推", not _push_calls, str(_push_calls))
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T21:30:00")))
check("每日推送.命中配置时间推送", len(_push_calls) == 1
      and "明日课表" in _push_calls[0][1], str(_push_calls))
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_dc["push"] = {**_dc["push"], "daily_courses": False, "daily_events": False,
               "daily_weather": True}
save_config(_dc)
_orig_daily = _sched.daily_summary


async def _fake_daily(cfg, day_s):
    return "多云，12~18℃，最高降水概率 40%"


_sched.daily_summary = _fake_daily
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T21:30:00")))
check("每日推送.内容可选只推天气", len(_push_calls) == 1
      and "明日天气" in _push_calls[0][1]
      and "明日课表" not in _push_calls[0][1], str(_push_calls))
_sched.daily_summary = _orig_daily
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_dc["push"] = {**_dc["push"], "daily_weather": False}
save_config(_dc)
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T21:30:00")))
check("每日推送.内容全关不推", not _push_calls, str(_push_calls))

# 凭据失效主动提醒：登录过期触发、按数据源每天去重、普通错误与开关关闭不打扰
from app.providers.base import AuthExpiredError
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_ac = load_config()
_ac["alert_on_fail"] = True
save_config(_ac)
_push_calls.clear()
asyncio.run(_sched._alert_auth_expired(_ac, "portal", AuthExpiredError("门户登录已过期")))
check("凭据提醒.过期推送", len(_push_calls) == 1
      and _push_calls[0][0] == "门户登录已过期", str(_push_calls))
_push_calls.clear()
asyncio.run(_sched._alert_auth_expired(_ac, "portal", AuthExpiredError("门户登录已过期")))
check("凭据提醒.当天去重", not _push_calls, str(_push_calls))
_push_calls.clear()
asyncio.run(_sched._alert_auth_expired(_ac, "zhengfang", AuthExpiredError("教务会话已过期")))
check("凭据提醒.正方独立去重", len(_push_calls) == 1
      and _push_calls[0][0] == "正方教务登录失效", str(_push_calls))
_push_calls.clear()
asyncio.run(_sched._alert_auth_expired(_ac, "portal", ValueError("网络超时")))
check("凭据提醒.普通错误不打扰", not _push_calls, str(_push_calls))
_ac["alert_on_fail"] = False
save_config(_ac)
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_push_calls.clear()
asyncio.run(_sched._alert_auth_expired(_ac, "portal", AuthExpiredError("门户登录已过期")))
check("凭据提醒.开关关闭不推", not _push_calls, str(_push_calls))

_push_calls.clear()
_rcfg = load_config()
_rcfg["push"]["bark_enabled"] = False
save_config(_rcfg)
asyncio.run(run_reminders(now=_at("2026-11-12T07:50:00")))
check("提醒.Bark 关闭不推", not _push_calls, str(_push_calls))

# 还原现场：快照 / 考试 / 配置 / 推送函数，清理提醒测试数据
db.delete_event(_rem_ev["id"])
db.delete_user(999)   # 清掉隔离用户的配置 / 快照 / 提醒记录
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_sched.push_all = _orig_push
_sched.weather_line = _orig_weather
if _orig_snap:
    db.save_snapshot(_orig_snap["courses"], _orig_snap["source"])
if _orig_esnap:
    db.save_exam_snapshot(_orig_esnap["exams"], _orig_esnap["source"])
save_config(_orig_cfg)

# 天气模块：WMO 中文、逐小时取数、关闭开关（不走真实网络）
check("天气.WMO 中文映射", describe_code(0) == "晴" and describe_code(61) == "小雨"
      and describe_code(95) == "雷阵雨")
_wp = _weather._params(35.96, 120.20)
check("天气.经纬度参数顺序", _wp["latitude"] == 35.96 and _wp["longitude"] == 120.2,
      str(_wp))
_orig_fetch = _weather.fetch_hourly


async def _fake_hourly(lat, lon):
    return {"2026-11-09T08:00": {"temp": 14.6, "code": 2, "pop": 40}}


_weather.fetch_hourly = _fake_hourly
_wcfg = load_config()
_wline = asyncio.run(weather_line(_wcfg, "2026-11-09", "08:00"))
check("天气.生成上课天气行", "青岛黄岛区" in _wline and "多云" in _wline
      and "15℃" in _wline and "降水概率 40%" in _wline, _wline)
_wcfg["weather"]["enabled"] = False
check("天气.关闭后不取数",
      asyncio.run(weather_line(_wcfg, "2026-11-09", "08:00")) == "")
_wcfg["weather"]["enabled"] = True
_hm = asyncio.run(_weather.hours_map(_wcfg))
check("天气.逐小时数据可用",
      _hm.get("2026-11-09T08:00") == {"text": "多云", "temp": 15, "pop": 40},
      str(_hm))
_wcfg["weather"]["enabled"] = False
check("天气.关闭后逐小时为空", asyncio.run(_weather.hours_map(_wcfg)) == {})
_weather.fetch_hourly = _orig_fetch

# 日历导出逐节天气映射
_orig_fetch3 = _weather.fetch_hourly


async def _fake_hourly3(lat, lon):
    return {
        "2026-11-09T08:00": {"temp": 14.6, "code": 2, "pop": 40},
        "2026-11-09T10:00": {"temp": 16.0, "code": 0, "pop": 10},
    }


_weather.fetch_hourly = _fake_hourly3
_wcfg["weather"]["enabled"] = True
_lwm = asyncio.run(_weather.lesson_weather_map(_wcfg))
check("天气.逐节天气映射结构",
      _lwm.get("2026-11-09", {}).get("08:00") == "多云 15℃，降水概率 40%"
      and _lwm.get("2026-11-09", {}).get("10:00") == "晴 16℃",
      str(_lwm))
_wcfg["weather"]["enabled"] = False
check("天气.关闭后逐节映射为空",
      asyncio.run(_weather.lesson_weather_map(_wcfg)) == {})
_wcfg["weather"]["enabled"] = True
_weather.fetch_hourly = _orig_fetch3

# 企业微信 / 钉钉群机器人：本地 mock webhook（负载 / 失败 / 日志）
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
_wecom_hits: list[tuple[str, dict]] = []


class _WecomMock(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _wecom_hits.append((self.path, json.loads(raw or b"{}")))
        data = ({"errcode": 93000, "errmsg": "invalid webhook url"} if "fail" in self.path
                else {"errcode": 0, "errmsg": "ok"})
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_wecom_srv = ThreadingHTTPServer(("127.0.0.1", 0), _WecomMock)
threading.Thread(target=_wecom_srv.serve_forever, daemon=True).start()
WECOM_OK = f"http://127.0.0.1:{_wecom_srv.server_address[1]}/wecom"
WECOM_FAIL = f"http://127.0.0.1:{_wecom_srv.server_address[1]}/fail"


async def main():
    # 5. 手动同步（无推送通道 → skip 日志，不报错）
    r = await run_sync(push=True)
    check("手动同步成功", r["ok"], r["message"])
    check("无通道时推送记录为 skip", any(x["channel"] == "none" for x in r["pushed"]))

    # 6. Bark 未配置 Key 的失败路径（不发真实网络请求）
    cfg2 = load_config()
    cfg2["push"]["bark_enabled"] = True
    cfg2["push"]["bark_key"] = ""
    results = await push_all(cfg2, "测试", "内容")
    check("Bark 缺 Key 优雅失败",
          results[0]["ok"] is False and "未配置" in results[0]["error"], str(results))

    # 6a. 企业微信 / 钉钉群机器人：URL 补全、发送负载、失败反馈、通道日志
    check("群机器人.key 自动补全", _wecom_url("abc-123") == WECOM_API + "abc-123"
          and _wecom_url(WECOM_OK) == WECOM_OK)
    ok, err = await send_wecom({"push": {"wecom_webhook": WECOM_OK}}, "标题", "正文")
    check("群机器人.发送与负载", ok and _wecom_hits
          and _wecom_hits[-1][1]["msgtype"] == "text"
          and "标题" in _wecom_hits[-1][1]["text"]["content"]
          and "正文" in _wecom_hits[-1][1]["text"]["content"], str(_wecom_hits[-1:]))
    ok, err = await send_wecom({"push": {"wecom_webhook": WECOM_FAIL}}, "t", "b")
    check("群机器人.失败反馈翻译", ok is False and "Webhook 无效" in err
          and "93000" in err, err)
    # 地址校验：后台网页、裸 hex、缺 key 都要在保存前挡住
    check("群机器人.管理后台地址被识别",
          "管理后台" in wecom_input_error(
              "https://work.weixin.qq.com/wework_admin/common/openBotProfile/242cbb"))
    check("群机器人.裸 hex 不是 key",
          "既不是完整地址也不是 key" in wecom_input_error("242cbb531ae9a13ea3604a2819a0af87"))
    check("群机器人.合法 key 通过",
          wecom_input_error("242cbb53-1ae9-a13e-a360-4a2819a0af87") == ""
          and wecom_input_error("a" * 64) == "")
    check("群机器人.合法地址通过",
          wecom_input_error(WECOM_API + "242cbb53-1ae9-a13e-a360-4a2819a0af87") == ""
          and wecom_input_error("") == "")
    check("群机器人.缺 key 的接口地址被识别",
          "缺少 key" in wecom_input_error("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"))
    _bad = await send_wecom({"push": {"wecom_webhook": "242cbb531ae9a13ea3604a2819a0af87"}},
                            "t", "b")
    check("群机器人.非法值不发请求", _bad[0] is False and "key" in _bad[1], str(_bad))
    check("群机器人.非法值不算已配置",
          available_channels({"push": {"wecom_enabled": True,
                                       "wecom_webhook": "2" * 34}}) == [])
    check("群机器人.未配置优雅失败",
          (await send_wecom({"push": {}}, "t", "b"))[0] is False)
    _cw = load_config()
    _cw["push"] = {**_cw.get("push", {}), "bark_enabled": False,
                   "wecom_enabled": True, "wecom_webhook": WECOM_OK}
    _wr = await push_all(_cw, "群推送", "正文")
    check("群机器人.push_all 通道", any(x["channel"] == "wecom" and x["ok"] for x in _wr),
          str(_wr))
    _wlogs = db.get_logs(5)["push"]
    check("群机器人.推送日志留痕",
          any(x["channel"] == "wecom" and x["status"] == "ok" for x in _wlogs))
    check("群机器人.默认关闭", load_config()["push"]["wecom_enabled"] is False
          and load_config()["push"]["wecom_webhook"] == "")
    # 按通道发送：测试可绕过启用开关，AI 主动推送要求已启用
    _scfg = {"push": {"bark_enabled": False, "bark_key": "",
                      "wecom_enabled": True, "wecom_webhook": WECOM_OK}}
    check("推送.可用通道只列已启用",
          available_channels(_scfg) == ["wecom"], str(available_channels(_scfg)))
    _sr = await send_selected(_scfg, "标题", "正文", ["wecom"])
    check("推送.按通道发送", _sr[0]["ok"] is True and _wecom_hits
          and _wecom_hits[-1][1]["text"]["content"].endswith("正文"), str(_sr))
    _sr = await send_selected(_scfg, "t", "b", ["bark"])
    check("推送.未配置通道友好失败",
          _sr[0]["ok"] is False and "未配置" in _sr[0]["error"], str(_sr))
    _sr = await send_selected(_scfg, "t", "b", ["sms"])
    check("推送.未知通道拒绝",
          _sr[0]["ok"] is False and "不支持" in _sr[0]["error"], str(_sr))
    _sr = await send_selected({**_scfg, "push": {**_scfg["push"],
                                                 "wecom_enabled": False}},
                              "t", "b", ["wecom"], need_enabled=True)
    check("推送.未启用时拒绝 AI 发送",
          _sr[0]["ok"] is False and "未启用" in _sr[0]["error"], str(_sr))
    _sr = await send_selected(_scfg, "t", "b", ["wecom"], need_enabled=True)
    check("推送.已启用时允许 AI 发送", _sr[0]["ok"] is True, str(_sr))

    # 7. 日志落库
    logs = db.get_logs(10)
    check("同步日志落库", len(logs["sync"]) >= 2)
    check("推送日志落库", len(logs["push"]) >= 2)
    check("推送.最近记录接口字段", db.list_push_logs(3)
          and {"ts", "channel", "status", "error"} <= set(db.list_push_logs(3)[0]))

    # 7a. 每日备份：在线快照可读、权限收紧、过期文件被清理
    import sqlite3 as _sq

    from app.backup import BACKUP_DIR as _bk_dir, backup_now as _backup_now
    _bk = _backup_now()
    check("备份.生成当日快照", _bk.exists() and _bk.stat().st_size > 0, str(_bk))
    with _sq.connect(_bk) as _bconn:
        check("备份.副本完整可读",
              _bconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
              and _bconn.execute(
                  "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0)
    check("备份.文件权限 600", oct(_bk.stat().st_mode)[-3:] == "600",
          oct(_bk.stat().st_mode))
    _old_bk = _bk_dir / "campus-19990101.db"
    _old_bk.write_text("old")
    _old_ts = time.time() - 9 * 86400
    os.utime(_old_bk, (_old_ts, _old_ts))
    _backup_now()
    check("备份.过期文件被清理", not _old_bk.exists())

    # 8. 配置保存 + 掩码保留密钥
    cfg3 = load_config()
    cfg3["ai"]["api_key"] = "sk-test-secret"
    save_config(cfg3)
    from app.config import masked_config
    masked = masked_config(load_config())
    check("密钥掩码", masked["ai"]["api_key"] == "********")
    save_config({"ai": {"api_key": "********"}})
    check("掩码提交保留原密钥", load_config()["ai"]["api_key"] == "sk-test-secret")
    # 清理测试密钥
    save_config({"ai": {"api_key": ""}})

asyncio.run(main())

# 9. cURL 解析（Chrome Copy as cURL 风格）
curl_text = """curl 'https://portal.qut.edu.cn/portal/api/timetable' \\
  -H 'accept: application/json' \\
  -H 'cookie: SESSION=abc123; route=xyz' \\
  --data-raw '{"week":9}' \\
  --compressed"""
pc = parse_curl(curl_text)
check("cURL.url", pc["url"] ==
      "https://portal.qut.edu.cn/portal/api/timetable")
check("cURL.method=POST(有body)", pc["method"] == "POST")
check("cURL.cookie 进 headers", pc["headers"].get(
    "Cookie") == "SESSION=abc123; route=xyz")
check("cURL.body 解析", pc["body"] == {"week": 9})

# 10. 字段映射猜测（嵌套 + time 数组风格）
payload = {"code": 200, "data": {"courses": [
    [{"id": 1, "name": "电磁场", "day": 2, "time": [3, 4], "place": "新安学堂512"}]]}}
path, mapping = guess_mapping(payload)
check("guess.嵌套数组路径", path == "data.courses.0", path)
check("guess.time 数组映射", mapping.get("start") ==
      "time.0" and mapping.get("end") == "time.-1", str(mapping))
check("guess.place→room", mapping.get("room") == "place", str(mapping))

# 11. SSO 过期检测
resp_sso = httpx.Response(200, headers={"content-type": "text/html"}, text="<title>青岛理工大学统一身份认证</title>",
                          request=httpx.Request("GET", "https://sso.qut.edu.cn/sso/login?x=1"))
try:
    check_auth_expired(resp_sso)
    check("SSO 过期检测", False)
except AuthExpiredError:
    check("SSO 过期检测", True)
resp_ok = httpx.Response(200, headers={"content-type": "application/json"}, text='{"courses":[]}',
                         request=httpx.Request("GET", "https://portal.qut.edu.cn/portal/api/timetable"))
try:
    check_auth_expired(resp_ok)
    check("正常响应不误报", True)
except AuthExpiredError:
    check("正常响应不误报", False)
check("星期中文归一", _to_day("星期三") == 3 and _to_day("7") == 7)
try:
    PortalProvider({"provider": {"http_url": "https://portal.qut.edu.cn/x"}})
    check("门户无 Cookie 报错", False)
except ValueError as e:
    check("门户无 Cookie 报错", "cURL" in str(e))
resp_401 = httpx.Response(200, headers={"content-type": "application/json"},
                          text='{"code":401,"msg":"未登录"}',
                          request=httpx.Request("GET", "https://portal.qut.edu.cn/portal/api/kb"))
try:
    check_auth_expired(resp_401)
    check("JSON 未登录检测", False)
except AuthExpiredError:
    check("JSON 未登录检测", True)
PortalProvider({"provider": {"http_url": "https://portal.qut.edu.cn/x",
                             "http_headers": {"Authorization": "Bearer t"}}})
check("门户 token 头也算登录态", True)

# 11b. 离线响应诊断（粘贴 Response JSON，无需登录态）
ev = {"code": 200, "data": {"events": [
    {"title": "高等数学", "date": "2026-11-09", "startTime": "08:00", "place": "1-302"}]}}
a = analyze_payload(ev)
check("诊断.识别日程事件流", a["kind"] == "events", str(a["kind"]))
check("诊断.列出候选数组路径", any(x["path"] == "data.events" for x in a["arrays"]))
kb = {"data": {"courses": [
    {"kcmc": "数据结构", "xqj": 2, "ksjc": 1, "jsjc": 2, "cdmc": "1-402"}]}}
check("诊断.识别周课表", analyze_payload(kb)["kind"] == "courses")
check("诊断.固定周次参数告警", len(diagnose_url("https://portal.qut.edu.cn/api/kb?week=9")) > 0)
check("诊断.普通 URL 不误报", not diagnose_url("https://portal.qut.edu.cn/api/kb"))
check("诊断.粘贴文本解析", parse_payload_text('```json\n{"a":1}\n```') == {"a": 1})
try:
    parse_payload_text("<html>login</html>")
    check("诊断.非 JSON 明确报错", False)
except ValueError as e:
    check("诊断.非 JSON 明确报错", "合法 JSON" in str(e))

# 11c. 一键登录的接口自动发现（纯离线，不启动浏览器）
kb_payload = {"code": 200, "data": {"courses": [
    {"kcmc": "数据结构", "xqj": 2, "ksjc": 1, "jsjc": 2, "cdmc": "1-402", "zcd": "1-16周"}]}}
cand = build_candidate("https://portal.qut.edu.cn/portal/api/kb", "GET", 200,
                       {"cookie": "SESSION=abc",
                           "host": "portal.qut.edu.cn", ":authority": "x"},
                       None, kb_payload)
check("一键登录.识别课表接口",
      cand is not None and cand["kind"] == "courses", str(cand)[:200])
check("一键登录.自动 list_path", bool(cand) and cand["list_path"] == "data.courses",
      str(cand.get("list_path") if cand else None))
noise = build_candidate("https://portal.qut.edu.cn/portal/api/user", "GET", 200, {},
                        None, {"code": 200, "data": {"name": "张三", "role": "student"}})
check("一键登录.过滤无关接口", noise is None)
ev_cand = build_candidate("https://portal.qut.edu.cn/portal/api/events", "GET", 200, {},
                          None, {"data": {"events": [{"title": "高数", "date": "2026-11-09"}]}})
check("一键登录.事件接口不写入配置", bool(ev_cand) and ev_cand["kind"] == "events"
      and pick_best([ev_cand]) is None)
check("一键登录.优先选课表接口", pick_best([ev_cand, cand])["url"] == cand["url"])
h = _headers_for_config(cand, "SESSION=fallback")
check("一键登录.保留 Cookie 去掉 host", h.get("cookie") == "SESSION=abc"
      and "host" not in {k.lower() for k in h}, str(h))
check("一键登录.无 Cookie 时补齐", _headers_for_config(
    {"headers": {}}, "A=1").get("Cookie") == "A=1")
check("一键登录.POST body 解析", _parse_body('{"week":9}') == {"week": 9})
check("一键登录.状态不泄露 Cookie", "SESSION" not in str(_public_candidates([cand])))
masked = _mask_url("https://portal.qut.edu.cn/api/kb?token=abc123&week=9")
check("一键登录.URL 票据脱敏", "token=***" in masked and "week=9" in masked, masked)
check("一键登录.初始状态", get_status()["status"] == "idle")

# 11d. App 化预埋：共用解析函数与可插拔模型请求头
cs = courses_from_payload(
    {"courses": [{"name": "数学", "day": 1, "start": 1, "end": 2, "room": "A1"}]},
    "courses", {})
check("预埋.课表解析可复用", len(cs) == 1 and cs[0].name == "数学" and cs[0].room == "A1")
check("预埋.AI 自定义请求头", _request_headers({"api_key": "k", "headers": {"X-API-Key": "x"}})
      == {"Authorization": "Bearer k", "X-API-Key": "x"})
check("预埋.AI 请求头覆盖鉴权", _request_headers({"api_key": "k", "headers": '{"Authorization": "Bearer other"}'})
      ["Authorization"] == "Bearer other")
try:
    _request_headers({"api_key": "k", "headers": "{bad"})
    check("预埋.AI 非法请求头报错", False)
except ValueError as e:
    check("预埋.AI 非法请求头报错", "合法 JSON" in str(e))

# 11e. 门户 xskb 全大写字段与应用列表误判（真实故障回归）
xskb_payload = {"results": {"data": {"Rows": [
    {"KCMC": "形势与政策5", "XQJ": "1", "KSJC": "5", "JSJC": "6",
     "SKDD": "2楼西报告厅", "SKZC": "7,8,9", "JXBH": "a1"},
    {"KCMC": "大学物理A(下)", "XQJ": "4", "KSJC": "9", "JSJC": "10",
     "SKDD": "B129", "SKZC": "1,3,5,7,9", "JXBH": "a2"},
]}}}
xp, xm = guess_mapping(xskb_payload)
check("大写字段.自动定位 Rows", xp == "results.data.Rows", xp)
check("大写字段.映射回原始键名", xm.get("name") == "KCMC" and xm.get("weeks") == "SKZC"
      and xm.get("room") == "SKDD", str(xm))
xc = courses_from_payload(xskb_payload, xp, xm)
check("大写字段.解析真实课表", len(xc) == 2 and xc[0].name == "形势与政策5"
      and xc[0].room == "2楼西报告厅" and xc[0].weeks == [7, 8, 9], str(xc[0].to_dict())[:200])
app_payload = {"results": [
    {"title": "青岛理工大学官方网站", "id": "125", "daycount": "1",
     "category_name": "x", "created": "x", "uuid": "x"} for _ in range(3)]}
ca = build_candidate("https://portal.qut.edu.cn/portal/api/v2/app?_format=json&type=ziyuan&items_per_page=100",
                     "GET", 200, {}, None, app_payload)
ck = build_candidate("https://portal.qut.edu.cn/portal/api/v2/datacenter/get/xskb?XN=2026-2027&XQ=1",
                     "GET", 200, {}, None, xskb_payload)
check("接口挑选.过滤门户应用列表", ca is None or ca["kind"] != "courses", str(ca)[:160])
check("接口挑选.选中 xskb 课表接口",
      bool(ck) and "xskb" in (
          pick_best([x for x in (ca, ck) if x]) or {}).get("url", ""),
      str(ck)[:160] if ck else "None")
check("告警.学期参数单独提示", any("换学期" in w for w in diagnose_url(
    "https://portal.qut.edu.cn/portal/api/v2/datacenter/get/xskb?XN=2026-2027&XQ=1")))

# 12. 导入管线端到端：自启临时服务当"外部接口"，跑完自动关闭。
#     不再依赖 8300 常驻服务，单独执行 python verify.py 即可全绿。


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_temp_server():
    import httpx as _hx
    port = _free_port()
    server_log = open("/tmp/qut_verify_server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(Path(__file__).resolve().parent),
        stdout=server_log, stderr=subprocess.STDOUT,
    )
    with _hx.Client(trust_env=False) as probe:
        for _ in range(40):
            if proc.poll() is not None:
                raise RuntimeError("临时服务进程已退出（可用 --log-level error 手动排查）")
            try:
                if probe.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                    print(f"（端到端用例自启临时服务 127.0.0.1:{port}，验证结束自动关闭）")
                    return proc, port
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
    raise RuntimeError("临时服务 10 秒内未就绪")


async def http_tests(port: int):
    """端到端：会话鉴权、多用户隔离、cURL 导入、数据注入、跨源 token 与 CORS。"""
    import httpx as _hx

    base = f"http://127.0.0.1:{port}"
    anon = _hx.AsyncClient(timeout=30, trust_env=False)
    check("鉴权.未登录访问被拒",
          (await anon.get(f"{base}/api/status")).status_code == 401)
    check("鉴权.天气接口未登录被拒",
          (await anon.get(f"{base}/api/weather")).status_code == 401)
    mf = await anon.get(f"{base}/manifest.webmanifest")
    check("PWA.manifest 可访问", mf.status_code == 200
          and "manifest" in mf.headers.get("content-type", "")
          and mf.json().get("display") == "standalone", str(mf.status_code))
    sw = await anon.get(f"{base}/sw.js")
    check("PWA.SW 根作用域", sw.status_code == 200
          and sw.headers.get("service-worker-allowed") == "/"
          and "fetch" in sw.text, str(sw.headers.get("service-worker-allowed")))
    ic = await anon.get(f"{base}/static/icons/icon-192.png")
    check("PWA.图标可访问", ic.status_code == 200
          and ic.headers.get("content-type", "").startswith("image/png"),
          str(ic.status_code))
    idx = await anon.get(f"{base}/")
    check("PWA.首页引用 manifest", 'rel="manifest"' in idx.text
          and "apple-touch-icon" in idx.text, "")
    st = (await anon.get(f"{base}/api/auth/status")).json()
    check("注册.首次需初始化", st.get("needs_setup") is True
          and st.get("invite_required") is False, str(st))
    r = await anon.post(f"{base}/api/auth/register", json={
        "username": "admin", "password": "AdminPass123", "display_name": "管理员"})
    check("注册.首个账号为管理员", r.status_code == 200
          and r.json().get("user", {}).get("role") == "admin", str(r.text)[:200])
    token_a = r.cookies.get("qut_session")
    check("注册.下发会话 Cookie", bool(token_a), str(r.headers.get("set-cookie"))[:120])
    await anon.aclose()

    async with _hx.AsyncClient(timeout=30, trust_env=False,
                               cookies={"qut_session": token_a}) as ca:
        r = await ca.post(f"{base}/api/provider/import-curl", json={
            "curl": f"curl '{base}/api/timetable' -H 'cookie: qut_session={token_a}'"})
        d = r.json()
        check("import-curl 端到端", r.status_code == 200 and d.get("ok")
              and d.get("course_count", 0) > 0, str(d)[:300])
        check("import.自动 list_path", d.get("list_path") == "courses",
              str(d.get("list_path")))

        tt = (await ca.get(f"{base}/api/timetable")).json()
        await ca.post(f"{base}/api/settings", json={"weather": {"enabled": False}})
        w = (await ca.get(f"{base}/api/weather")).json()
        check("天气.接口关闭返回空表", w.get("ok") is True
              and w.get("enabled") is False and w.get("hours") == {}, str(w)[:200])
        await ca.post(f"{base}/api/settings", json={"weather": {"enabled": True}})
        await ca.post(f"{base}/api/settings", json={"rooms": {
            "default_campus": "05", "default_campus_name": "黄岛校区"}})
        st = (await ca.get(f"{base}/api/settings")).json()
        check("默认校区.设置接口保存并回读",
              (st.get("rooms") or {}).get("default_campus") == "05"
              and st["rooms"].get("default_campus_name") == "黄岛校区",
              str(st.get("rooms"))[:160])
        await ca.post(f"{base}/api/settings", json={"rooms": {
            "default_campus": "", "default_campus_name": ""}})
        # 推送：企业微信单通道测试（免启用开关）与 AI 主动推送（需已启用）
        await ca.post(f"{base}/api/settings", json={"push": {
            "bark_enabled": False, "bark_key": "",
            "wecom_enabled": False, "wecom_webhook": WECOM_OK}})
        r = (await ca.post(f"{base}/api/push/test", json={"channel": "wecom"})).json()
        check("推送.企业微信单通道测试", r["results"][0]["channel"] == "wecom"
              and r["results"][0]["ok"] is True, str(r)[:200])
        r = (await ca.post(f"{base}/api/push/send", json={
            "title": "AI 推送", "body": "明天 08:00 上课",
            "channels": ["wecom"]})).json()
        check("推送.AI 发送未启用被拒", r["results"][0]["ok"] is False
              and "未启用" in r["results"][0]["error"], str(r)[:200])
        await ca.post(f"{base}/api/settings", json={"push": {"wecom_enabled": True}})
        r = (await ca.post(f"{base}/api/push/send", json={
            "title": "AI 推送", "body": "明天 08:00 上课",
            "channels": ["wecom"]})).json()
        check("推送.AI 发送成功", r["results"][0]["ok"] is True
              and "明天 08:00 上课" in _wecom_hits[-1][1]["text"]["content"],
              str(r)[:200])
        d = (await ca.post(f"{base}/api/push/send", json={
            "title": "自动通道", "body": "内容"})).json()
        check("推送.未指定通道用已启用通道",
              d["results"][0]["channel"] == "wecom", str(d)[:200])
        r = await ca.post(f"{base}/api/push/send", json={"title": "x", "body": "  "})
        check("推送.空内容被拒", r.status_code == 400, str(r.status_code))
        st_push = (await ca.get(f"{base}/api/status")).json()
        check("推送.状态列出企业微信通道",
              "wecom" in st_push.get("push_channels", []),
              str(st_push.get("push_channels")))
        lg = (await ca.get(f"{base}/api/push/log?limit=5")).json().get("logs") or []
        check("推送.最近记录可读", lg and {"ts", "channel", "status"} <= set(lg[0]),
              str(lg[:1])[:200])
        # 非法地址在保存阶段就被拦下，不会存进配置
        r = await ca.post(f"{base}/api/settings", json={"push": {
            "wecom_webhook": "https://work.weixin.qq.com/wework_admin/common/openBotProfile/242cbb"}})
        check("推送.管理后台地址保存被拒", r.status_code == 400
              and "管理后台" in r.text, str(r.text)[:200])
        r = await ca.post(f"{base}/api/settings", json={"push": {
            "wecom_webhook": "242cbb531ae9a13ea3604a2819a0af87"}})
        check("推送.裸 hex 保存被拒", r.status_code == 400 and "key" in r.text,
              str(r.text)[:200])
        keep = (await ca.get(f"{base}/api/settings")).json().get("push", {})
        check("推送.被拒后配置未被覆盖", keep.get("wecom_webhook") == WECOM_OK,
              str(keep.get("wecom_webhook"))[:80])
        await ca.post(f"{base}/api/settings", json={"push": {
            "wecom_enabled": False, "wecom_webhook": ""}})
        r = await ca.post(f"{base}/api/ingest",
                          json={"payload": {"code": 200, "data": {"courses": tt["courses"]}},
                                "push": False, "source": "test"})
        d = r.json()
        check("ingest.解析并写入快照", r.status_code == 200
              and d.get("ok") is True, str(d)[:250])
        check("ingest.自动定位数组", d.get("list_path") == "data.courses",
              str(d.get("list_path")))
        ev = (await ca.post(f"{base}/api/ingest", json={
            "payload": {"data": {"events": [{"title": "高数", "date": "2026-11-09"}]}}
        })).json()
        check("ingest.事件流明确拒绝", ev.get("ok") is False
              and "事件适配器" in ev.get("message", ""), str(ev)[:200])

        inv = (await ca.post(f"{base}/api/auth/invites")).json()
        code = inv.get("code")
        check("邀请码.管理员可生成", bool(code), str(inv)[:160])
        rb = await ca.post(f"{base}/api/auth/register", json={
            "username": "bob", "password": "BobPass123"})
        check("注册.无邀请码被拒", rb.status_code == 400, str(rb.status_code))
        rb = await ca.post(f"{base}/api/auth/register", json={
            "username": "bob", "password": "BobPass123", "invite": code})
        check("注册.凭邀请码成功", rb.status_code == 200, str(rb.text)[:200])
        token_b = rb.cookies.get("qut_session")

        async with _hx.AsyncClient(timeout=30, trust_env=False,
                                   cookies={"qut_session": token_b}) as cb:
            check("权限.普通用户不能看用户列表",
                  (await cb.get(f"{base}/api/auth/users")).status_code == 403)
            await ca.post(f"{base}/api/settings", json={"school": "A 的学校"})
            sa = (await ca.get(f"{base}/api/settings")).json()
            sb = (await cb.get(f"{base}/api/settings")).json()
            check("隔离.配置互不串扰", sa["school"] == "A 的学校"
                  and sb["school"] != "A 的学校", f"A={sa['school']} B={sb['school']}")
            pa = {"courses": [{"name": "A 专属课", "day": 1,
                               "start": 1, "end": 2, "weeks": []}]}
            pb = {"courses": [{"name": "B 专属课", "day": 2,
                               "start": 3, "end": 4, "weeks": []}]}
            await ca.post(f"{base}/api/ingest", json={"payload": pa, "source": "a"})
            await cb.post(f"{base}/api/ingest", json={"payload": pb, "source": "b"})
            na = [c["name"] for c in (await ca.get(
                f"{base}/api/timetable")).json()["courses"]]
            nb = [c["name"] for c in (await cb.get(
                f"{base}/api/timetable")).json()["courses"]]
            check("隔离.课表互不串扰", "A 专属课" in na and "B 专属课" in nb
                  and "B 专属课" not in na and "A 专属课" not in nb,
                  f"A={na} B={nb}")

            # 日历订阅：私有 token 定位用户，webcal 拉取无需 Cookie
            sinfo = (await ca.get(f"{base}/api/export/subscribe")).json()
            check("订阅.返回 token 与链接", bool(sinfo.get("token"))
                  and sinfo.get("webcal", "").startswith("webcal://")
                  and sinfo.get("https", "").startswith("http")
                  and "google" in sinfo, str(sinfo)[:200])
            stok = sinfo["token"]
            # 匿名（无 Cookie）用 token 拉订阅，应返回 A 的课表
            async with _hx.AsyncClient(timeout=30, trust_env=False) as anon_sub:
                ics_anon = await anon_sub.get(
                    f"{base}/api/export/calendar.ics?token={stok}")
                check("订阅.无 Cookie 凭 token 可取", ics_anon.status_code == 200
                      and "BEGIN:VCALENDAR" in ics_anon.text
                      and "A 专属课" in ics_anon.text, str(ics_anon.status_code))
                # 无 token 也无 Cookie：必须 401，不得回落全局模板泄露他人课表
                bare = await anon_sub.get(f"{base}/api/export/calendar.ics")
                check("订阅.无 token 无 Cookie 拒绝", bare.status_code == 401,
                      str(bare.status_code))
                bad = await anon_sub.get(
                    f"{base}/api/export/calendar.ics?token=wrong")
                check("订阅.错误 token 被拒", bad.status_code == 404, str(bad.status_code))

            # 考试也进订阅日历：手动添加一场考试，凭 token 拉取应包含该考试事件
            await ca.post(f"{base}/api/exams", json={
                "action": "add",
                "exam": {"name": "订阅测试考试", "date": "2026-11-20",
                         "start": "14:00", "end": "16:00", "room": "3-101",
                         "seat": "8"}})
            async with _hx.AsyncClient(timeout=30, trust_env=False) as anon_exam:
                ics_exam = await anon_exam.get(
                    f"{base}/api/export/calendar.ics?token={stok}")
                check("订阅.考试进入日历", ics_exam.status_code == 200
                      and "订阅测试考试" in ics_exam.text
                      and "考试：订阅测试考试" in ics_exam.text,
                      str(ics_exam.status_code))
            # 清理手动添加的考试，避免影响后续用例
            await ca.post(f"{base}/api/exams", json={"action": "reset"})

            # 重新生成后旧 token 失效
            await ca.post(f"{base}/api/export/subscribe/regenerate")
            sinfo2 = (await ca.get(f"{base}/api/export/subscribe")).json()
            check("订阅.重新生成换新 token", sinfo2.get("token")
                  and sinfo2["token"] != stok, str(sinfo2.get("token")))
            async with _hx.AsyncClient(timeout=30, trust_env=False) as anon_sub2:
                old_again = await anon_sub2.get(
                    f"{base}/api/export/calendar.ics?token={stok}")
                check("订阅.旧 token 立即失效", old_again.status_code == 404,
                      str(old_again.status_code))

        await ca.post(f"{base}/api/settings", json={"security": {
            "enabled": True, "token": "test-token-123", "allowed_origins": []}})
        cross = {"Origin": "capacitor://localhost"}
        r3 = await ca.get(f"{base}/api/status", headers=cross)
        check("鉴权.跨源无 token 被拒", r3.status_code == 401, str(r3.status_code))
        r4 = await ca.get(f"{base}/api/status",
                          headers={**cross, "X-Local-Token": "test-token-123"})
        check("鉴权.带 token 放行", r4.status_code == 200
              and r4.headers.get("access-control-allow-origin") == "capacitor://localhost",
              str(r4.status_code))
        r5 = await ca.get(f"{base}/api/status")
        check("鉴权.同源无需 token", r5.status_code == 200, str(r5.status_code))
        r6 = await ca.options(f"{base}/api/status",
                              headers={**cross, "Access-Control-Request-Method": "GET"})
        check("鉴权.预检返回 CORS",
              r6.headers.get("access-control-allow-origin") == "capacitor://localhost",
              str(dict(r6.headers))[:160])
        await ca.post(f"{base}/api/settings", json={"security": {
            "enabled": False, "token": "", "allowed_origins": []}})

        # 考试链路：切到演示源同步，验证快照 → API → 分组字段
        await ca.post(f"{base}/api/settings", json={"provider": {"type": "mock"}})
        sr = (await ca.post(f"{base}/api/sync")).json()
        check("考试.同步带考试数据", sr.get("ok") is True
              and "考试" in sr.get("message", ""), str(sr)[:220])
        ex = (await ca.get(f"{base}/api/exams")).json()
        check("考试.接口返回演示考试", ex.get("is_demo") is True
              and len(ex.get("exams", [])) >= 2, str(ex)[:220])
        check("考试.分组与展示字段完整", bool(ex.get("upcoming"))
              and all(e.get("date") and e.get("display_time") for e in ex["exams"]),
              str(ex)[:220])
        st_ex = (await ca.get(f"{base}/api/status")).json()
        check("考试.状态栏计数", st_ex.get("exam_count", 0) >= 2
              and st_ex.get("upcoming_exam_count", 0) >= 2
              and bool(st_ex.get("next_exam")), str(st_ex.get("next_exam"))[:160])
        # 今日页合并日程：今日/明日考试随课表接口返回，供时间线展示
        _today_d = date.today()
        _tmr_d = _today_d + timedelta(days=1)
        await ca.post(f"{base}/api/exams", json={"action": "add", "exam": {
            "name": "今日日程考试", "date": _today_d.isoformat(),
            "start": "23:00", "end": "23:59", "room": "1-101"}})
        await ca.post(f"{base}/api/exams", json={"action": "add", "exam": {
            "name": "明日日程考试", "date": _tmr_d.isoformat(),
            "start": "09:00", "end": "11:00", "room": "2-202"}})
        tt_d = (await ca.get(f"{base}/api/timetable")).json()
        check("日程.今日考试进时间线",
              any(e.get("name") == "今日日程考试"
                  for e in tt_d.get("today_exams", [])),
              str(tt_d.get("today_exams"))[:200])
        check("日程.明日考试进明日安排",
              any(e.get("name") == "明日日程考试"
                  for e in tt_d.get("tomorrow_exams", [])),
              str(tt_d.get("tomorrow_exams"))[:200])
        await ca.post(f"{base}/api/exams", json={"action": "reset"})
        # 手动改课表：添加 → 修改 → 隐藏 → 恢复 → 修改同步课 → 重置
        base_list = (await ca.get(f"{base}/api/courses")).json()
        check("课表.列表带稳定标识", base_list.get("count", 0) > 0
              and all(c.get("uid") for c in base_list.get("courses", [])),
              str(base_list)[:180])
        base_count = base_list["count"]
        auto = base_list["courses"][0]
        bad = await ca.post(f"{base}/api/courses", json={
            "action": "add", "course": {"name": "  "}})
        check("课表.空名称被拒", bad.status_code == 400, str(bad.status_code))
        add = (await ca.post(f"{base}/api/courses", json={
            "action": "add", "course": {"name": "手动自习", "day": 7, "start": 11,
                                         "end": 12, "weeks": "1-16",
                                         "room": "图书馆"}})).json()
        lst = (await ca.get(f"{base}/api/courses")).json()
        manual = next((c for c in lst["courses"] if c["name"] == "手动自习"), None)
        st_c = (await ca.get(f"{base}/api/status")).json()
        check("课表.手动添加生效", add.get("ok") and manual is not None
              and manual["manual"] and lst["count"] == base_count + 1
              and st_c.get("course_count") == base_count + 1
              and manual["weeks"] == list(range(1, 17)), str(manual)[:200])
        upd = (await ca.post(f"{base}/api/courses", json={
            "action": "update", "uid": manual["uid"],
            "course": {"name": "手动自习（改）", "day": 6, "start": 9, "end": 10,
                       "weeks": "3,5,7", "room": "图书馆 2"}})).json()
        lst = (await ca.get(f"{base}/api/courses")).json()
        check("课表.修改手动课程", upd.get("ok") and any(
            c["name"] == "手动自习（改）" and c["day"] == 6
            and c["weeks"] == [3, 5, 7] for c in lst["courses"]),
            str(lst.get("courses"))[:200])
        await ca.post(f"{base}/api/courses", json={
            "action": "delete", "uid": auto["uid"]})
        lst = (await ca.get(f"{base}/api/courses")).json()
        check("课表.隐藏同步课程", all(c["uid"] != auto["uid"]
                                   for c in lst["courses"])
              and lst["hidden_count"] == 1 and lst["count"] == base_count,
              str(lst)[:200])
        await ca.post(f"{base}/api/courses", json={"action": "restore_hidden"})
        lst = (await ca.get(f"{base}/api/courses")).json()
        check("课表.恢复隐藏课程", any(c["uid"] == auto["uid"]
                                   for c in lst["courses"])
              and lst["hidden_count"] == 0, str(lst)[:160])
        upd2 = (await ca.post(f"{base}/api/courses", json={
            "action": "update", "uid": auto["uid"],
            "course": {"name": auto["name"], "day": auto["day"],
                       "start": auto["start"], "end": auto["end"],
                       "room": "改后教室",
                       "weeks": ",".join(str(w) for w in auto["weeks"])}})).json()
        lst = (await ca.get(f"{base}/api/courses")).json()
        edited = next((c for c in lst["courses"] if c["uid"] == auto["uid"]), None)
        check("课表.修改同步课程", upd2.get("ok") and edited is not None
              and edited["room"] == "改后教室"
              and edited["teacher"] == auto["teacher"],
              f"upd={upd2} edited={edited} auto={auto}")
        tt = (await ca.get(f"{base}/api/timetable")).json()
        check("课表.周视图读取调整", all(
            c.get("room") != auto.get("room") or c["uid"] != auto["uid"]
            for c in tt["courses"]), str(tt.get("courses"))[:160])
        await ca.post(f"{base}/api/courses", json={"action": "reset"})
        fin = (await ca.get(f"{base}/api/courses")).json()
        check("课表.恢复自动课表", fin["count"] == base_count
              and not fin["has_overrides"]
              and not any(c["name"] == "手动自习（改）" for c in fin["courses"]),
              str(fin)[:200])

        # 手动改考试：校验 → 添加 → 修改 → 隐藏 → 恢复 → 改同步考试 → 重置
        ex_list = (await ca.get(f"{base}/api/exams")).json()
        check("考试.列表带稳定标识", len(ex_list.get("exams", [])) >= 2
              and all(e.get("uid") for e in ex_list.get("exams", [])),
              str(ex_list)[:180])
        ex_base = len(ex_list["exams"])
        sync_exam = next(e for e in ex_list["exams"] if not e["finished"])
        bad = await ca.post(f"{base}/api/exams", json={
            "action": "add", "exam": {"name": "  ", "date": "2026-12-01"}})
        check("考试.空名称被拒", bad.status_code == 400, str(bad.status_code))
        bad = await ca.post(f"{base}/api/exams", json={
            "action": "add", "exam": {"name": "测试", "date": "2026-13-40"}})
        check("考试.非法日期被拒", bad.status_code == 400, str(bad.status_code))
        bad = await ca.post(f"{base}/api/exams", json={
            "action": "add", "exam": {"name": "测试", "date": "2026-12-01",
                                       "start": "10:00", "end": "09:00"}})
        check("考试.结束早于开始被拒", bad.status_code == 400, str(bad.status_code))
        add = (await ca.post(f"{base}/api/exams", json={
            "action": "add", "exam": {"name": "英语四级", "date": "2026-12-14",
                                       "start": "09:00", "end": "11:20",
                                       "room": "3-101", "seat": "8"}})).json()
        lst = (await ca.get(f"{base}/api/exams")).json()
        manual_ex = next((e for e in lst["exams"] if e["name"] == "英语四级"), None)
        st_e = (await ca.get(f"{base}/api/status")).json()
        check("考试.手动添加生效", add.get("ok") and manual_ex is not None
              and manual_ex["manual"] and len(lst["exams"]) == ex_base + 1
              and st_e.get("exam_count") == ex_base + 1
              and manual_ex["display_time"] == "2026-12-14 09:00-11:20",
              str(manual_ex)[:200])
        upd = (await ca.post(f"{base}/api/exams", json={
            "action": "update", "uid": manual_ex["uid"],
            "exam": {"name": "英语四级（改）", "date": "2026-12-15",
                     "start": "14:00", "end": "16:00", "room": "3-102"}})).json()
        lst = (await ca.get(f"{base}/api/exams")).json()
        check("考试.修改手动考试", upd.get("ok") and any(
            e["name"] == "英语四级（改）" and e["date"] == "2026-12-15"
            and e["room"] == "3-102" for e in lst["exams"]),
            str(lst.get("exams"))[:200])
        await ca.post(f"{base}/api/exams", json={
            "action": "delete", "uid": sync_exam["uid"]})
        lst = (await ca.get(f"{base}/api/exams")).json()
        check("考试.隐藏同步考试", all(e["uid"] != sync_exam["uid"]
                                   for e in lst["exams"])
              and lst["hidden_count"] == 1, str(lst)[:200])
        await ca.post(f"{base}/api/exams", json={"action": "restore_hidden"})
        lst = (await ca.get(f"{base}/api/exams")).json()
        check("考试.恢复隐藏考试", any(e["uid"] == sync_exam["uid"]
                                   for e in lst["exams"])
              and lst["hidden_count"] == 0, str(lst)[:160])
        upd2 = (await ca.post(f"{base}/api/exams", json={
            "action": "update", "uid": sync_exam["uid"],
            "exam": {"name": sync_exam["name"], "date": sync_exam["date"],
                     "start": sync_exam.get("start", ""),
                     "end": sync_exam.get("end", ""),
                     "room": "改后考场"}})).json()
        lst = (await ca.get(f"{base}/api/exams")).json()
        edited_ex = next((e for e in lst["exams"] if e["uid"] == sync_exam["uid"]),
                         None)
        check("考试.修改同步考试保留时间", upd2.get("ok") and edited_ex is not None
              and edited_ex["room"] == "改后考场"
              and edited_ex["start"] == sync_exam.get("start"),
              f"upd={upd2} edited={edited_ex}")
        await ca.post(f"{base}/api/exams", json={"action": "reset"})
        fin = (await ca.get(f"{base}/api/exams")).json()
        check("考试.恢复自动考试", len(fin["exams"]) == ex_base
              and not fin["has_overrides"]
              and not any(e["room"] == "改后考场" for e in fin["exams"]),
              str(fin)[:200])

        # 个人日程：校验 → 增改查 → 今日汇总 → 状态计数 → 多用户隔离 → 删除
        today_iso = date.today().isoformat()
        bad = await ca.post(f"{base}/api/events", json={
            "action": "add", "event": {"title": "  ", "date": today_iso}})
        check("日程.空标题被拒", bad.status_code == 400, str(bad.status_code))
        bad = await ca.post(f"{base}/api/events", json={
            "action": "add", "event": {"title": "x", "date": "11/09"}})
        check("日程.非法日期被拒", bad.status_code == 400, str(bad.status_code))
        bad = await ca.post(f"{base}/api/events", json={
            "action": "add", "event": {"title": "x", "date": today_iso, "start": "25:00"}})
        check("日程.非法时间被拒", bad.status_code == 400, str(bad.status_code))
        bad = await ca.post(f"{base}/api/events", json={
            "action": "add", "event": {"title": "x", "date": today_iso,
                                       "start": "20:00", "end": "19:00"}})
        check("日程.结束早于开始被拒", bad.status_code == 400, str(bad.status_code))
        # 无结束时间的今日日程在任何时刻都算未结束，用例不受运行时间影响
        r = await ca.post(f"{base}/api/events", json={
            "action": "add", "event": {"title": "社团例会", "date": today_iso,
                                       "start": "19:00", "location": "活动中心"}})
        d = r.json()
        check("日程.添加", r.status_code == 200 and d.get("ok")
              and bool(d.get("event", {}).get("id")), str(d)[:200])
        ev_id = d["event"]["id"]
        lst = (await ca.get(f"{base}/api/events")).json()
        check("日程.列表与展示字段", lst.get("count") == 1
              and lst["events"][0]["display_time"] == f"{today_iso} 19:00",
              str(lst)[:200])
        tt = (await ca.get(f"{base}/api/timetable")).json()
        check("日程.并入今日安排", any(e["id"] == ev_id
                                  for e in tt.get("today_events", [])),
              str(tt.get("today_events"))[:200])
        st_ev = (await ca.get(f"{base}/api/status")).json()
        check("日程.状态栏计数", st_ev.get("upcoming_event_count") == 1,
              str(st_ev)[:160])
        upd = (await ca.post(f"{base}/api/events", json={
            "action": "update", "id": ev_id,
            "event": {"title": "社团例会（改）", "location": "活动中心 2"}})).json()
        check("日程.修改保留未填字段", upd.get("ok")
              and upd["event"]["title"] == "社团例会（改）"
              and upd["event"]["start"] == "19:00"
              and upd["event"]["date"] == today_iso, str(upd)[:200])
        bob_id = db.get_user_by_username("bob")["id"]
        async with _hx.AsyncClient(timeout=30, trust_env=False,
                                   cookies={"qut_session": token_b}) as cb_ev:
            lst_b = (await cb_ev.get(f"{base}/api/events")).json()
            check("日程.隔离.他人不可见", lst_b.get("count") == 0, str(lst_b)[:160])
            r = await cb_ev.post(f"{base}/api/events", json={
                "action": "add", "event": {"title": "B 的日程", "date": today_iso}})
            check("日程.隔离.各自可添加", r.status_code == 200, str(r.text)[:160])
            bad = await cb_ev.post(f"{base}/api/events", json={
                "action": "update", "id": ev_id, "event": {"title": "hack"}})
            check("日程.隔离.不能改他人日程", bad.status_code == 404, str(bad.status_code))
            bad = await cb_ev.post(f"{base}/api/events", json={
                "action": "delete", "id": ev_id})
            check("日程.隔离.不能删他人日程", bad.status_code == 404, str(bad.status_code))
        d = (await ca.post(f"{base}/api/events",
                           json={"action": "delete", "id": ev_id})).json()
        lst = (await ca.get(f"{base}/api/events")).json()
        check("日程.删除", d.get("ok") and lst.get("count") == 0, str(lst)[:160])

        # 全局搜索：课程 / 个人日程 / 空结果，返回所在周次供前端跳转
        sr = (await ca.get(f"{base}/api/search", params={"q": "数据结构"})).json()
        check("搜索.命中课程", any(x["kind"] == "course" and "数据结构" in x["title"]
                                   for x in sr["results"]), str(sr)[:200])
        check("搜索.课程带周次", all(isinstance(x.get("week"), int) for x in sr["results"])
              and any("第" in x["meta"] for x in sr["results"]), str(sr)[:200])
        await ca.post(f"{base}/api/events", json={
            "action": "add", "event": {"title": "搜索验证日程", "date": today_iso,
                                       "start": "10:00", "location": "图书馆"}})
        sr = (await ca.get(f"{base}/api/search", params={"q": "搜索验证"})).json()
        check("搜索.命中个人日程", any(x["kind"] == "event" and x["title"] == "搜索验证日程"
                                     and x["date"] == today_iso for x in sr["results"]),
              str(sr)[:200])
        # db.list_events() 的字段名是 start/end（不是数据库列名 start_time），别再显示成"全天"
        check("搜索.日程带起止时间", any(x["kind"] == "event" and "10:00" in x["sub"]
                                     and "全天" not in x["sub"] for x in sr["results"]),
              str(sr)[:200])
        sr = (await ca.get(f"{base}/api/search", params={"q": "图书馆"})).json()
        check("搜索.按地点命中", any(x["kind"] == "event" for x in sr["results"]), str(sr)[:200])
        sr = (await ca.get(f"{base}/api/search", params={"q": "不存在xyz"})).json()
        check("搜索.无结果为空", sr["results"] == [], str(sr)[:120])
        sr = (await ca.get(f"{base}/api/search", params={"q": ""})).json()
        check("搜索.空关键词为空", sr["results"] == [], str(sr)[:120])
        _se = (await ca.get(f"{base}/api/events")).json()["events"]
        await ca.post(f"{base}/api/events", json={
            "action": "delete", "id": [e["id"] for e in _se if e["title"] == "搜索验证日程"][0]})
        async with _hx.AsyncClient(timeout=10, trust_env=False) as _an2:
            check("搜索.未登录被拒",
                  (await _an2.get(f"{base}/api/search?q=x")).status_code == 401)

        # 空教室：未配置正方 → 400；参数校验先于数据源检查
        r = await ca.get(f"{base}/api/rooms/meta")
        check("空教室.未配置正方被拒", r.status_code == 400
              and "正方" in r.json().get("detail", ""), str(r.text)[:160])
        r = await ca.get(f"{base}/api/rooms/free", params={"date_s": today_iso})
        check("空教室.查询需正方凭证", r.status_code == 400, str(r.text)[:160])
        r = await ca.get(f"{base}/api/rooms/free", params={"date_s": "11/09"})
        check("空教室.非法日期被拒", r.status_code == 400, str(r.status_code))
        r = await ca.get(f"{base}/api/rooms/free",
                         params={"date_s": today_iso, "start": 5, "end": 2})
        check("空教室.节次倒置被拒", r.status_code == 400, str(r.status_code))
        r = await ca.get(f"{base}/api/rooms/free", params={"date_s": "2020-01-01"})
        check("空教室.开学前被拒", r.status_code == 400, str(r.status_code))
        r = await ca.get(f"{base}/api/rooms/my-free", params={"date_s": today_iso})
        j = r.json()
        _fs_expected = free_sections(load_config(), date.fromisoformat(today_iso))
        check("空教室.空闲节次接口", r.status_code == 200
              and j.get("date") == today_iso
              and ((j.get("spans") is None and _fs_expected is None)
                   or (j.get("spans") is not None and _fs_expected is not None
                       and [(s["start"], s["end"]) for s in j["spans"]] == _fs_expected)),
              str(r.text)[:160])
        r = await ca.get(f"{base}/api/rooms/my-free", params={"date_s": "11/09"})
        check("空教室.空闲节次非法日期", r.status_code == 400, str(r.status_code))

        # 对话落库：读取 → 回存（过滤非法角色）→ 截断式回存 → 清空
        h = (await ca.get(f"{base}/api/chat/history")).json()
        check("对话.初始为空", h.get("messages") == [], str(h)[:160])
        r = await ca.post(f"{base}/api/chat/history", json={"messages": [
            {"role": "user", "content": "明天有什么课"},
            {"role": "assistant", "content": "明天有高数"},
            {"role": "system", "content": "应被过滤"},
            {"role": "user", "content": "   "}]})
        check("对话.回存过滤非法角色", r.status_code == 200
              and r.json().get("count") == 2, str(r.text)[:160])
        h = (await ca.get(f"{base}/api/chat/history")).json()
        check("对话.刷新后恢复", [m["content"] for m in h.get("messages", [])]
              == ["明天有什么课", "明天有高数"], str(h)[:200])
        await ca.post(f"{base}/api/chat/history", json={"messages": [
            {"role": "user", "content": "明天有什么课"}]})
        h = (await ca.get(f"{base}/api/chat/history")).json()
        check("对话.编辑截断回存", len(h.get("messages", [])) == 1, str(h)[:160])
        await ca.post(f"{base}/api/chat/clear")
        h = (await ca.get(f"{base}/api/chat/history")).json()
        check("对话.清空", h.get("messages") == [], str(h)[:160])

        # 长期记忆：添加 → 去重 → 列表 → 校验 → 多用户隔离 → 删除
        r = await ca.post(f"{base}/api/memories",
                          json={"action": "add", "content": "我喜欢坐前排"})
        mem_id = r.json().get("memory", {}).get("id")
        check("记忆.添加", r.status_code == 200 and bool(mem_id), str(r.text)[:160])
        r = await ca.post(f"{base}/api/memories",
                          json={"action": "add", "content": "我喜欢坐前排"})
        check("记忆.接口去重", r.json().get("memory", {}).get("duplicate") is True
              and r.json().get("memory", {}).get("id") == mem_id, str(r.text)[:160])
        m = (await ca.get(f"{base}/api/memories")).json()
        check("记忆.列表", len(m.get("memories", [])) == 1, str(m)[:160])
        r = await ca.post(f"{base}/api/memories", json={"action": "add", "content": " "})
        check("记忆.空内容被拒", r.status_code == 400, str(r.status_code))
        r = await ca.post(f"{base}/api/memories",
                          json={"action": "add", "content": "字" * 201})
        check("记忆.超长被拒", r.status_code == 400, str(r.status_code))
        async with _hx.AsyncClient(timeout=30, trust_env=False,
                                   cookies={"qut_session": token_b}) as cb_mem:
            m = (await cb_mem.get(f"{base}/api/memories")).json()
            check("记忆.隔离.他人不可见", m.get("memories") == [], str(m)[:160])
            r = await cb_mem.post(f"{base}/api/memories",
                                  json={"action": "delete", "id": mem_id})
            check("记忆.隔离.不能删他人记忆", r.status_code == 404, str(r.status_code))
            r = await cb_mem.post(f"{base}/api/memories",
                                  json={"action": "add", "content": "B 的记忆"})
            check("记忆.隔离.各自可添加", r.status_code == 200, str(r.text)[:160])
            await cb_mem.post(f"{base}/api/chat/history", json={"messages": [
                {"role": "user", "content": "B 的对话"}]})
        r = await ca.post(f"{base}/api/memories",
                          json={"action": "delete", "id": mem_id})
        m = (await ca.get(f"{base}/api/memories")).json()
        check("记忆.删除", r.status_code == 200 and m.get("memories") == [],
              str(m)[:160])
        r = await ca.post(f"{base}/api/memories",
                          json={"action": "delete", "id": mem_id})
        check("记忆.重复删除404", r.status_code == 404, str(r.status_code))

        # 正方保存：密码留空沿用旧密码，学年归一化，测试失败不改配置
        r = await ca.post(f"{base}/api/provider/zhengfang", json={
            "username": "20230001", "password": "", "test": False})
        _d = r.json()
        check("正方.首次保存缺密码被拒", _d.get("ok") is False
              and "沿用已保存的密码" in _d.get("message", ""), str(_d)[:160])
        r = await ca.post(f"{base}/api/provider/zhengfang", json={
            "username": "20230001", "password": "demo-pass",
            "year": "2026-2027", "term": "12", "test": False})
        _d = r.json()
        check("正方.未测试保存归一化学年", _d.get("ok") is True
              and _d.get("year") == "2026" and _d.get("term") == "12", str(_d)[:160])
        await ca.post(f"{base}/api/provider/zhengfang", json={
            "username": "20230001", "password": "",
            "year": "2026", "term": "3", "test": False})
        _src = (await ca.get(f"{base}/api/provider/sources")).json()["zhengfang"]
        check("正方.密码留空不清空已存密码",
              _src.get("has_login") is True and _src.get("year") == "2026"
              and _src.get("term") == "3", str(_src)[:200])

        # 门户课表 + 正方考试：不切数据源也能识别考试能力
        await ca.post(f"{base}/api/settings", json={"provider": {
            "type": "portal", "zf_base": "http://example.invalid/jwglxt",
            "zf_cookie": "dummy", "http_url": f"{base}/api/timetable"}})
        ex_portal = (await ca.get(f"{base}/api/exams")).json()
        check("考试.门户+正方凭证可用", ex_portal.get("supported") is True
              and ex_portal.get("provider") == "portal", str(ex_portal)[:180])

        # AI 流式：未配置直接 400；上游不可达时用 SSE error 事件兜底
        st = await ca.post(f"{base}/api/chat/stream", json={"message": "你好"})
        check("流式.未配置被拒", st.status_code == 400, str(st.status_code))
        await ca.post(f"{base}/api/settings", json={"ai": {
            "enabled": True, "api_key": "dummy", "model": "x",
            "base_url": "http://127.0.0.1:9/v1"}})
        rr = await ca.post(f"{base}/api/chat/stream", json={"message": "你好"})
        check("流式.返回事件流", rr.status_code == 200
              and "text/event-stream" in rr.headers.get("content-type", ""),
              str(rr.headers.get("content-type")))
        check("流式.上游失败写入 error 事件", '"error"' in rr.text, rr.text[:200])

    async with _hx.AsyncClient(timeout=30, trust_env=False,
                               cookies={"qut_session": token_b}) as cb:
        await cb.post(f"{base}/api/auth/logout")
        check("鉴权.登出后失效",
              (await cb.get(f"{base}/api/status")).status_code == 401)

    async with _hx.AsyncClient(timeout=30, trust_env=False,
                               cookies={"qut_session": token_a}) as ca2, \
            _hx.AsyncClient(timeout=30, trust_env=False) as cb2:
        # 上一步已登出，重新登录拿到新会话再测注销
        relogin = await cb2.post(f"{base}/api/auth/login",
                                 json={"username": "bob", "password": "BobPass123"})
        check("注销.重新登录成功", relogin.status_code == 200,
              str(relogin.status_code))
        # 注销账号：用户名 + 密码双重确认，删除后会话立即失效
        bad = await cb2.post(f"{base}/api/auth/delete-account",
                             json={"username": "bob", "password": "WrongPass123"})
        check("注销.密码错误被拒", bad.status_code == 400, str(bad.status_code))
        bad = await cb2.post(f"{base}/api/auth/delete-account",
                             json={"username": "admin", "password": "BobPass123"})
        check("注销.用户名不一致被拒", bad.status_code == 400, str(bad.status_code))
        with db.get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO reminders (user_id, day, item_key, sent_at) VALUES (?, ?, ?, ?)",
                (bob_id, "2026-11-09", "course:注销清理课:1:", db.now_str()))
        ok = await cb2.post(f"{base}/api/auth/delete-account",
                            json={"username": "bob", "password": "BobPass123"})
        check("注销.确认后删除账号",
              ok.status_code == 200 and ok.json().get("ok") is True, str(ok.text)[:160])
        check("注销.原会话立即失效",
              (await cb2.get(f"{base}/api/status")).status_code == 401)
        users = (await ca2.get(f"{base}/api/auth/users")).json().get("users", [])
        check("注销.用户从列表移除",
              all(u["username"] != "bob" for u in users), str(users)[:200])
        with db.get_conn() as conn:
            n_ev = conn.execute("SELECT COUNT(*) FROM events WHERE user_id = ?",
                                (bob_id,)).fetchone()[0]
            n_chat = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE user_id = ?",
                                  (bob_id,)).fetchone()[0]
            n_mem = conn.execute("SELECT COUNT(*) FROM user_memories WHERE user_id = ?",
                                 (bob_id,)).fetchone()[0]
            n_rem = conn.execute("SELECT COUNT(*) FROM reminders WHERE user_id = ?",
                                 (bob_id,)).fetchone()[0]
        check("注销.个人日程一并删除", n_ev == 0, str(n_ev))
        check("注销.对话与记忆一并删除", n_chat == 0 and n_mem == 0,
              f"chat={n_chat} mem={n_mem}")
        check("注销.提醒记录一并删除", n_rem == 0, str(n_rem))
        # 管理员注销时把权限交给最早注册的其他账号
        code2 = (await ca2.post(f"{base}/api/auth/invites")).json().get("code")
        r = await ca2.post(f"{base}/api/auth/register", json={
            "username": "carol", "password": "CarolPass123", "invite": code2})
        check("注销.新账号可继续注册", r.status_code == 200, str(r.status_code))
        token_c = r.cookies.get("qut_session")
        ok = await ca2.post(f"{base}/api/auth/delete-account",
                            json={"username": "admin", "password": "AdminPass123"})
        check("注销.管理员注销并转让权限", ok.status_code == 200
              and ok.json().get("promoted") == "carol", str(ok.text)[:160])
        async with _hx.AsyncClient(timeout=30, trust_env=False,
                                   cookies={"qut_session": token_c}) as cc:
            st = (await cc.get(f"{base}/api/auth/status")).json()
            check("注销.接任管理员生效",
                  st.get("user", {}).get("role") == "admin"
                  and bool(st.get("user", {}).get("created_at"))
                  and (await cc.get(f"{base}/api/auth/users")).status_code == 200,
                  str(st)[:160])


server_proc = None
try:
    server_proc, port = _start_temp_server()
    asyncio.run(http_tests(port))
except Exception as e:  # noqa: BLE001
    check("端到端用例", False, f"临时服务或用例异常：{e}")
finally:
    if server_proc is not None:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            try:
                server_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

import shutil
shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)   # 清理隔离数据目录，全程不碰真实 config/DB

fails = [n for n, ok in passed if not ok]
print(f"\n{len(passed) - len(fails)}/{len(passed)} passed")
sys.exit(1 if fails else 0)
