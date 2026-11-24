"""核心链路验证脚本：不依赖真实外部服务，全程使用隔离的临时数据目录。"""
import base64
import os
import shutil
import sys
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="qut_verify_")
os.environ["CAMPUS_DATA_DIR"] = _TEST_DATA_DIR
# 必须在导入任何 app 模块之前覆盖生产环境，验证只使用进程内缓存和独立命名空间。
os.environ["REDIS_URL"] = ""
os.environ["AGENDA_CACHE_PREFIX"] = f"agenda:verify:{os.getpid()}"
# 验证脚本用演示源做测试夹具：生产默认关闭演示数据，这里显式开启以保留原有测试场景。
os.environ["AGENDA_ALLOW_DEMO"] = "1"
sys.path.insert(0, ".")

from pathlib import Path
import time
import subprocess
import socket
import threading
from app.providers.httpjson import courses_from_payload
from app.browser_login import (
    build_candidate, pick_best, _headers_for_config, _parse_body, _public_candidates,
    get_status, _mask_url, _lock as _bl_lock,
)
from app.curl_import import analyze_payload, diagnose_url, parse_payload_text
from app.providers.httpjson import check_auth_expired, PortalProvider, _to_day
from app.providers.base import AuthExpiredError
from app.providers.mock import MockProvider
from app.providers.zhengfang import zhengfang_configured
import httpx
from app.curl_import import parse_curl, guess_mapping
from app.config import load_config as _load_cfg, save_config as _save_cfg, MASK
from app import db as _db
from app import cache as _cache
from datetime import date, datetime, timedelta
from app.push import (push_all, send_wecom, _wecom_url, WECOM_API,
                      send_selected, wecom_input_error)
from app.scheduler import (run_sync, format_changes, format_score_changes,
                           courses_on, section_time,
                           free_sections, free_spans,
                           run_reminders, push_exam_reviews, TZ)
import app.scheduler as _sched
import app.weather as _weather
from app.weather import describe_code, weather_line
from app.study import advice_for, review_message
from app.user_context import current_user_id, set_current_user
from app.config import load_config, save_config
from app import db
from app.models import (
    Course, Event, Exam, Score, apply_course_overrides, apply_exam_overrides,
    course_uid, exam_uid, diff_courses,
    diff_scores, score_uid, current_week, parse_exam_time, parse_weeks,
)
import asyncio
import json

_db.init_db()   # 隔离目录是空的，先建表（含旧库迁移检测）
asyncio.run(run_sync(push=False))   # 生成一份演示快照，供后续筛选用例使用

passed = []
_db.create_user("verify-primary", "unused-test-hash")


def check(name, cond, detail=""):
    passed.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" +
          (f"  [{detail}]" if detail and not cond else ""))


# Redis 缓存层：无 Redis 时仍提供 TTL 限制的进程内降级缓存。
_cache.clear_local()
asyncio.run(_cache.set("verify:one", {"value": 1}, 30))
check("Redis缓存.本地降级读写", asyncio.run(_cache.get("verify:one")) == {"value": 1})
check("Redis缓存.凭证身份摘要不可逆", len(_cache.fingerprint("cookie-secret")) == 24
      and "cookie-secret" not in _cache.fingerprint("cookie-secret"))


# 配置与定时同步新回归：使用独立用户，避免污染主验证夹具。
_uid_migration_user = _db.create_user("verify-migration", "unused-test-hash")
_uid_migration_ctx = set_current_user(_uid_migration_user)
try:
    import hashlib as _hashlib
    _legacy_course = Course("旧课", day=2, start=3, end=4, weeks=[1, 3],
                            source_id="legacy-course", uid="old-explicit-uid")
    _canonical_course = Course("旧课", day=2, start=3, end=4, weeks=[1, 3],
                               source_id="legacy-course")
    _new_course_uid = course_uid(_canonical_course)
    _legacy_course_uid = _hashlib.sha1(
        "|".join([_legacy_course.source_id, _legacy_course.name, "2", "3", "4", "1,3"])
        .encode("utf-8")).hexdigest()[:16]
    _db.save_snapshot([_legacy_course.to_dict()], "migration-test")
    _db.save_user_config(_uid_migration_user, {
        "course_overrides": {
            "hidden": ["old-explicit-uid"],
            "edited": {
                "old-explicit-uid": {"room": "旧键覆盖"},
                _new_course_uid: {"room": "新键覆盖"},
            },
        },
    })
    _db.migrate_override_uids(_uid_migration_user)
    _migrated_snapshot = _db.get_snapshot_for(_uid_migration_user)
    _migrated_cfg = _db.get_user_config(_uid_migration_user)
    _migrated_overrides = _migrated_cfg["course_overrides"]
    check("迁移.快照显式 uid 与旧键同事务更新",
          _migrated_snapshot["courses"][0]["uid"] == _new_course_uid
          and _migrated_overrides["hidden"] == [_new_course_uid]
          and _legacy_course_uid not in _migrated_overrides["edited"],
          str((_migrated_snapshot, _migrated_overrides)))
    check("迁移.edited 新键优先",
          _migrated_overrides["edited"].get(_new_course_uid) == {"room": "新键覆盖"},
          str(_migrated_overrides["edited"]))
finally:
    current_user_id.reset(_uid_migration_ctx)

_provider_test_uid = _db.create_user("verify-provider", "unused-test-hash")
_db.save_user_config(_provider_test_uid, {"provider": {
    "http_url": "https://portal.example.invalid/courses",
    "http_headers": '{"cookie":"legacy-cookie"}',
    "zf_base": "https://zf.example.invalid",
    "zf_username": "legacy-user",
}})
_provider_patch_cfg = _db.patch_user_config(_provider_test_uid, {
    "provider": {"portal": {"headers": '{"cookie":"canonical-cookie"}'}}
})
check("配置.旧 provider 字段先规范化且新 headers 覆盖旧值",
      _provider_patch_cfg["provider"]["portal"]["headers"]
      == '{"cookie":"canonical-cookie"}'
      and _provider_patch_cfg["provider"]["zhengfang"]["username"] == "legacy-user"
      and "http_headers" not in _provider_patch_cfg["provider"]
      and "zf_username" not in _provider_patch_cfg["provider"],
      str(_provider_patch_cfg["provider"]))

_patch_uid = _db.create_user("verify-patch", "unused-test-hash")
_db.save_user_config(_patch_uid, {
    "calendar_subscribe_token": "preserve-subscribe-token",
    "course_overrides": {"hidden": ["old-hidden"], "edited": {}, "added": []},
    "exam_overrides": {"hidden": ["old-exam"], "edited": {}, "added": []},
})
_patch_result = _db.patch_user_config(_patch_uid, {"school": "patched-school"})
_empty_override_result = _db.patch_user_config(_patch_uid, {
    "course_overrides": {"hidden": [], "edited": {}, "added": []},
    "exam_overrides": {"hidden": [], "edited": {}, "added": []},
})
check("配置.patch保留订阅令牌并支持清空override",
      _patch_result.get("calendar_subscribe_token") == "preserve-subscribe-token"
      and _empty_override_result.get("calendar_subscribe_token") == "preserve-subscribe-token"
      and _empty_override_result.get("course_overrides")
      == {"hidden": [], "edited": {}, "added": []}
      and _empty_override_result.get("exam_overrides")
      == {"hidden": [], "edited": {}, "added": []},
      str(_empty_override_result))
_db.save_user_config(_patch_uid, {})

_retry_uid = 989
_retry_now = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)
_retry_saved = _sched._sync_retries.copy()
_sched._sync_retries.pop(_retry_uid, None)
_retry_expected = [15, 30, 60]
for _retry_attempt, _retry_minutes in enumerate(_retry_expected, 1):
    _sched._defer_sync_retry(_retry_uid, _retry_now)
    _retry_due, _retry_attempts = _sched._sync_retries[_retry_uid]
    check(f"定时同步.失败退避第{_retry_attempt}次",
          _retry_attempts == _retry_attempt
          and _retry_due == _retry_now + timedelta(
              minutes=_retry_minutes, seconds=_retry_uid % 60),
          str((_retry_due, _retry_attempts)))

_retry_originals = {
    "list_user_ids": _db.list_user_ids,
    "reminder_sent": _db.reminder_sent,
    "mark_reminder": _db.mark_reminder,
    "sync_slots": _sched.sync_slots,
    "run_sync": _sched.run_sync,
}
try:
    _db.list_user_ids = lambda: [_retry_uid]
    _db.reminder_sent = lambda *_args: False
    _marked_retries = []
    _db.mark_reminder = lambda *args: _marked_retries.append(args)
    _sched.sync_slots = lambda day=None: {
        _retry_uid: datetime(day.year, day.month, day.day, tzinfo=TZ)}
    _busy_retry = datetime(2000, 1, 1, tzinfo=TZ)
    _sched._sync_retries[_retry_uid] = (_busy_retry, 2)
    _sched.run_sync = lambda **_kwargs: asyncio.sleep(0, result={"ok": False, "busy": True})
    _sched._sync_check_wrapper()
    check("定时同步.busy 不退避", _sched._sync_retries.get(_retry_uid) == (_busy_retry, 2))
    _sched.run_sync = lambda **_kwargs: asyncio.sleep(0, result={"ok": True})
    _sched._sync_check_wrapper()
    check("定时同步.成功清理退避状态",
          _retry_uid not in _sched._sync_retries and len(_marked_retries) == 1,
          str((_sched._sync_retries.get(_retry_uid), _marked_retries)))
finally:
    _db.list_user_ids = _retry_originals["list_user_ids"]
    _db.reminder_sent = _retry_originals["reminder_sent"]
    _db.mark_reminder = _retry_originals["mark_reminder"]
    _sched.sync_slots = _retry_originals["sync_slots"]
    _sched.run_sync = _retry_originals["run_sync"]
    _sched._sync_retries.clear()
    _sched._sync_retries.update(_retry_saved)


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
check("周三开学后周一为第2周",
      current_week("2026-09-02", date(2026, 9, 7)) == 2)

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
_t0 = time.perf_counter()
try:
    parse_weeks("1-999999999999999999999999999999999999")
    _huge_weeks_rejected = False
except ValueError:
    _huge_weeks_rejected = True
check("周次.超大范围快速拒绝且不展开",
      _huge_weeks_rejected and time.perf_counter() - _t0 < 1.0)

# source_id 不变时，课表周次 / 考试考场变化后仍用旧 uid 应用手动覆盖。
_course_source_old = Course("来源课程", day=2, start=3, end=4,
                            weeks=[1, 2, 3], source_id="course-source-1")
_course_source_new = Course("来源课程", day=2, start=3, end=4,
                            weeks=[4, 5, 6], source_id="course-source-1")
_course_source_uid = course_uid(_course_source_old)
_course_hidden = apply_course_overrides(
    [_course_source_new], {"hidden": [_course_source_uid]})
_course_edited = apply_course_overrides(
    [_course_source_new], {"edited": {_course_source_uid: {"room": "新教室"}}})
check("课表.source_id 周次变化仍应用旧隐藏/修改",
      not _course_hidden and len(_course_edited) == 1
      and _course_edited[0].room == "新教室"
      and course_uid(_course_source_new) == _course_source_uid)

_exam_source_old = Exam.from_dict({"name": "来源考试", "date": "2026-12-01",
                                   "start": "09:00", "end": "11:00",
                                   "room": "旧考场", "seat": "1",
                                   "source_id": "exam-source-1"})
_exam_source_new = Exam.from_dict({"name": "来源考试", "date": "2026-12-01",
                                   "start": "09:00", "end": "11:00",
                                   "room": "新考场", "seat": "9",
                                   "source_id": "exam-source-1"})
_exam_source_uid = exam_uid(_exam_source_old)
_exam_hidden = apply_exam_overrides(
    [_exam_source_new], {"hidden": [_exam_source_uid]})
_exam_edited = apply_exam_overrides(
    [_exam_source_new], {"edited": {_exam_source_uid: {"note": "保留覆盖"}}})
check("考试.source_id 考场座位变化仍应用旧隐藏/修改",
      not _exam_hidden and len(_exam_edited) == 1
      and _exam_edited[0].room == "新考场" and _exam_edited[0].seat == "9"
      and _exam_edited[0].note == "保留覆盖"
      and exam_uid(_exam_source_new) == _exam_source_uid)

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

# 数据源解析：配了真实凭证后，即使 type 还是默认 mock，也绝不能再返回演示数据
from app.config import resolve_course_source as _resolve_src, DEFAULT_CONFIG as _def_cfg
import json as _json
check("数据源.配正方后不再回退 mock",
      _resolve_src({**_json.loads(_json.dumps(_def_cfg)),
                    "provider": {**_def_cfg["provider"],
                                 "zhengfang": {"base": "https://x", "username": "u", "password": "p"}}})
      == "zhengfang")
check("数据源.配门户后不再回退 mock",
      _resolve_src({**_json.loads(_json.dumps(_def_cfg)),
                    "provider": {**_def_cfg["provider"],
                                 "portal": {"url": "https://portal.qut.edu.cn/x",
                                            "headers": '{"cookie": "c"}'}}})
      == "portal")
check("数据源.未配置任何真实来源才回退 mock",
      _resolve_src(_json.loads(_json.dumps(_def_cfg))) == "mock")
_both = {**_json.loads(_json.dumps(_def_cfg)), "provider": {**_def_cfg["provider"],
         "zhengfang": {"base": "https://x", "username": "u", "password": "p"},
         "portal": {"url": "https://portal.qut.edu.cn/x",
                    "headers": '{"cookie": "c"}'}}}
check("数据源.auto 双源时门户优先", _resolve_src(_both) == "portal")
check("数据源.显式选正方时不被门户抢回",
      _resolve_src({**_both, "provider": {**_both["provider"], "primary": "zhengfang"}})
      == "zhengfang")

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
except ValueError as e:
    _zf_null = str(e)
check("正方.空响应不伪装成空学期", "格式异常" in _zf_null, _zf_null)

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

# 成绩：模型 / diff / 解析 / 快照落库
_s1 = Score("数据结构", score="88", credit="4", year="2025", term="3", source_id="sc1")
_s2 = Score("高等数学", score="92", credit="5", year="2025", term="3", source_id="sc2")
check("成绩.学期标签", _s1.term_label() == "2025-2026 学年 第一学期", _s1.term_label())
check("成绩.稳定标识", score_uid(_s1) == score_uid(Score.from_dict(_s1.to_dict())))
_sd = diff_scores([_s1], [_s1, _s2])
check("成绩.diff新出现", len(_sd["added"]) == 1 and _sd["added"][0]["name"] == "高等数学"
      and _sd["has_change"], str(_sd)[:160])
_sd2 = diff_scores(
    [Score("数据结构", score="88", source_id="sc1")],
    [Score("数据结构", score="95", source_id="sc1")])
check("成绩.diff分数更新", len(_sd2["changed"]) == 1
      and _sd2["changed"][0]["fields"]["score"]["after"] == "95", str(_sd2)[:160])
check("成绩.diff无变化", not diff_scores([_s1], [_s1])["has_change"])
check("成绩.变更文本格式化", "新成绩" in format_score_changes(_sd))
# 正方成绩解析：多字段兜底 + 学期码归一化
_zf_scores = asyncio.run(_zfp._fetch_scores(_ZfClient(_ZfResp({"items": [
    {"kcmc": "数据结构", "cj": 88, "xf": "4", "jd": "3.7", "xnm": "2025", "xqm": "3",
     "kclbmc": "必修", "ksxz": "正常考试", "id": "A1"},
    {"kcmc": "英语", "score": "良好", "xnm": "2025", "xqm": "12", "id": "A2"},
    {"bz": "无意义行"},
]}))))
check("正方.成绩解析多字段兜底", len(_zf_scores) == 2
      and _zf_scores[0].name == "数据结构" and _zf_scores[0].score == "88"
      and _zf_scores[0].gpa == "3.7" and _zf_scores[0].term == "3"
      and _zf_scores[1].score == "良好" and _zf_scores[1].term == "12",
      str([s.to_dict() for s in _zf_scores])[:200])
# 异常响应必须抛错，不能静默返回 [] 清空历史快照（否则接口恢复后会全量误推）
_zf_bad = ""
try:
    asyncio.run(_zfp._fetch_scores(_ZfClient(_ZfResp(None))))
except ValueError:
    _zf_bad = "raised"
check("正方.成绩空响应抛错而非清空", _zf_bad == "raised")
_zf_missing = ""
try:
    asyncio.run(_zfp._fetch_scores(_ZfClient(_ZfResp({"msg": "无权限"}))))
except ValueError:
    _zf_missing = "raised"
check("正方.成绩缺items字段抛错", _zf_missing == "raised")
# 全学期遍历：合并去重；任一学期失败则整体失败，不保存「成功的一部分」
_orig_years = _zfp.score_years
_zfp.score_years = 2
try:
    def _score_resp(name):
        return _ZfResp({"items": [{"kcmc": name, "cj": 90, "id": name}]})
    _multi = asyncio.run(_zfp._fetch_all_scores(_ZfClient(
        _score_resp("A"), _score_resp("B"), _score_resp("C"), _score_resp("A"))))
    check("正方.全学期成绩保留跨学期同ID", len(_multi) == 4 and len({s.key() for s in _multi}) == 4,
          str([s.name for s in _multi])[:160])
    _partial = ""
    try:
        asyncio.run(_zfp._fetch_all_scores(_ZfClient(
            _score_resp("A"), _score_resp("B"), _ZfResp(None), _score_resp("C"))))
    except RuntimeError:
        _partial = "raised"
    check("正方.部分学期失败整体失败", _partial == "raised")
finally:
    _zfp.score_years = _orig_years
# 会话缓存复用：fetch_bundle 抓过之后不再开第二次登录
_zfp._scores_fetched, _zfp.last_scores, _zfp.last_score_error = True, [_s1], ""
_cached_scores = asyncio.run(_zfp.fetch_scores())
check("正方.成绩会话缓存复用", len(_cached_scores) == 1
      and _cached_scores[0].name == "数据结构")
_zfp._scores_fetched = False
# 成绩接口路径/菜单号可配置覆盖
_override_cfg = load_config()
_override_cfg.setdefault("provider", {}).setdefault("zhengfang", {})
_override_cfg["provider"]["zhengfang"].update(
    score_path="/cjcx/custom.html", score_gnmk="N999999")
_zp_override = ZhengfangProvider(_override_cfg)
check("正方.成绩路径可配置覆盖", _zp_override.score_path == "/cjcx/custom.html"
      and _zp_override.score_gnmk == "N999999")
from app.config import normalize_provider
_score_cfg = normalize_provider({"zhengfang": {"score_years": 6,
                                                "score_path": "/score",
                                                "score_gnmk": "N123"}})
_score_provider = ZhengfangProvider({"provider": _score_cfg})
check("正方.成绩normalize保留配置",
      _score_cfg["zhengfang"].get("score_years") == 6
      and _score_provider.score_years == 6
      and _score_provider.score_path == "/score"
      and _score_provider.score_gnmk == "N123",
      str(_score_cfg["zhengfang"]))
db.save_score_snapshot([_s1.to_dict(), _s2.to_dict()], "test")
_ssnap = db.get_score_snapshot()
check("成绩.快照落库", bool(_ssnap) and _ssnap["source"] == "test"
      and len(_ssnap["scores"]) == 2, str(_ssnap)[:160])
# 还原：演示源不产生成绩快照，清掉本段测试写入的数据
with db.get_conn() as _c:
    _c.execute("DELETE FROM score_snapshot WHERE user_id = 1")

db.save_exam_snapshot([exam.to_dict()], "test")
esnap = db.get_exam_snapshot()
check("考试.快照落库", bool(esnap) and esnap["source"] == "test"
      and esnap["exams"][0]["name"] == "高等数学", str(esnap)[:160])
# 个人日程：模型、落库
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
db.delete_event(_saved_ev["id"])
check("日程.删除后不可读", db.get_event(_saved_ev["id"]) is None)

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

# 默认校区：仅用于周课表空教室查询
check("默认校区.配置含默认项",
      "default_campus" in load_config().get("rooms", {}), "")

# 空闲节次：按课表算出没课的连续区间
check("空闲节次.区间合并", free_spans({1, 2, 5, 6}) == [(3, 4), (7, 12)]
      and free_spans(set()) == [(1, 12)]
      and free_spans(set(range(1, 13))) == [], "")
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
if _fs_orig:
    db.save_snapshot(_fs_orig["courses"], _fs_orig["source"])

# 天气按天摘要
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

_weather.fetch_hourly = _orig_fetch2

# 天气接口抖动时不能把整块天气变空：先重试，再回落上次成功的数据
class _BoomClient:
    """模拟 Open-Meteo 不可达（超时/断网）。"""
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        _BoomClient.calls += 1
        raise RuntimeError("boom")


class _OkClient:
    """模拟正常返回。"""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"hourly": {"time": [f"{_today_s}T08:00"],
                                   "temperature_2m": [13.4],
                                   "weather_code": [2],
                                   "precipitation_probability": [7]}}
        return _R()


_weather.clear_cache()
_orig_client = _weather.httpx.AsyncClient
_weather.httpx.AsyncClient = _OkClient
_ok_h = asyncio.run(_weather.fetch_hourly(35.96, 120.20))
check("天气.正常抓取并缓存",
      _ok_h.get(f"{_today_s}T08:00", {}).get("temp") == 13.4, str(_ok_h)[:120])
_weather.httpx.AsyncClient = _BoomClient
_weather._cache.clear()          # 仅清短期缓存，保留「上次成功」
_weather.shared_cache.clear_local("weather:")
_BoomClient.calls = 0
_stale = asyncio.run(_weather.fetch_hourly(35.96, 120.20))
check("天气.抓取失败沿用上次成功数据",
      _stale == _ok_h, str(_stale)[:120])
# 主源失败会重试一次，随后还会尝试备用源 wttr.in 一次，共 3 次请求
check("天气.失败会重试并尝试备用源", _BoomClient.calls == 3, str(_BoomClient.calls))
# 从未成功过的地方，失败后返回空字典（而不是抛错）
_weather.clear_cache()
_weather.httpx.AsyncClient = _BoomClient
_empty = asyncio.run(_weather.fetch_hourly(10.0, 100.0))
check("天气.无历史数据时返回空表", _empty == {}, str(_empty)[:120])
_weather.httpx.AsyncClient = _orig_client
_weather.clear_cache()

# 主源熔断：境内 VPS 连不通 Open-Meteo 会干等满超时，失败后 15 分钟内直接走备用源
_weather.clear_cache()
_weather._primary_dead_until = time.monotonic() + 60
_wttr_hits = {"n": 0}
_orig_wttr_fetch = _weather._fetch_from_wttr
async def _fake_wttr_fetch(lat=None, lon=None):
    _wttr_hits["n"] += 1
    return {f"{_today_s}T08:00": {"temp": 9, "code": 0, "pop": 0}}
class _NoNetClient:
    def __init__(self, *a, **k):
        raise AssertionError("熔断期间不应再请求主源")

_weather._fetch_from_wttr = _fake_wttr_fetch
_weather.httpx.AsyncClient = _NoNetClient
_cb_hourly = asyncio.run(_weather.fetch_hourly(35.96, 120.20))
_weather.httpx.AsyncClient = _orig_client
_weather._fetch_from_wttr = _orig_wttr_fetch
_weather.clear_cache()
check("天气.主源熔断后直接走备用源",
      _wttr_hits["n"] == 1 and _cb_hourly.get(f"{_today_s}T08:00", {}).get("temp") == 9,
      f"wttr={_wttr_hits['n']} {str(_cb_hourly)[:80]}")

# 4. 明日课表筛选（2026-11-08 周日 → 无课；周一有课）
cfg = load_config()
snap = db.get_snapshot()
courses = [Course.from_dict(c) for c in snap["courses"]]
sun = courses_on(cfg, courses, date(2026, 11, 8))
mon = courses_on(cfg, courses, date(2026, 11, 9))
check("周日无课筛选", len(sun) == 0)
check("周一有课筛选", bool(mon) and all(
    c.day == 1 for c in mon), str([c.name for c in mon]))

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
_orig_ssnap = db.get_score_snapshot()
_orig_cfg = load_config()

# 新成绩推送：无课表变更、关闭明日课后，只有成绩段进入推送；首次同步建基线不推送
_push_calls.clear()
_scfg = load_config()
_scfg["push_next_day"] = False
_scfg["push"] = dict(_scfg.get("push") or {}, new_score_enabled=True,
                     bark_enabled=True, bark_key="test")
_cur_courses = [Course.from_dict(d) for d in _orig_snap["courses"]]
db.save_score_snapshot([_s1.to_dict()], "test")  # 基线：只有数据结构
_res = asyncio.run(_sched.apply_snapshot(
    _scfg, _cur_courses, "test", push=True,
    scores=[_s1, _s2], score_source="test"))  # 新增高等数学
check("成绩.新成绩触发推送", any("【考试成绩】" in b and "新成绩" in b and "高等数学" in b
                          for _, b in _push_calls), str(_push_calls)[:300])
# 课程快照已存在、但成绩快照为空：第一次拉成绩仍应建基线，不能借课程的 first_sync
with db.get_conn() as _c:
    _c.execute("DELETE FROM score_snapshot WHERE user_id = 1")
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(
    _scfg, _cur_courses, "test", push=True,
    scores=[_s1, _s2], score_source="test"))
check("成绩.已有课程快照时首同步不刷屏",
      not any("【考试成绩】" in b for _, b in _push_calls), str(_push_calls)[:300])
# 首次同步（课程与成绩快照都为空）同样只建基线、不推成绩段
with db.get_conn() as _c:
    _c.execute("DELETE FROM score_snapshot WHERE user_id = 1")
    _c.execute("DELETE FROM snapshot WHERE user_id = 1")
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(
    _scfg, _cur_courses, "test", push=True,
    scores=[_s1, _s2], score_source="test"))
check("成绩.首次同步不刷屏", not any("【考试成绩】" in b for _, b in _push_calls),
      str(_push_calls)[:300])
# 还原课程 + 成绩现场（演示模式下没有成绩快照，清掉即可）
db.save_snapshot(_orig_snap["courses"], _orig_snap["source"])
if _orig_ssnap:
    _db.save_score_snapshot(_orig_ssnap["scores"], _orig_ssnap["source"])
else:
    with _db.get_conn() as _c:
        _c.execute("DELETE FROM score_snapshot WHERE user_id = 1")
_push_calls.clear()   # 成绩推送测试不应污染后续提醒用例的初始状态


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

# 用户隔离：独立真实用户的配置 / 快照 / 去重记录独立于默认用户
_uid_isolated = _db.create_user("verify-reminder-isolation", "unused-test-hash")
_ctx_isolated = set_current_user(_uid_isolated)
try:
    _cfg_isolated = load_config()
    _cfg_isolated["semester_start"] = "2026-08-31"
    _cfg_isolated["push"] = {**_cfg_isolated.get("push", {}), "bark_enabled": True, "bark_key": "t",
                             "reminder_enabled": True, "reminder_advance": 15}
    save_config(_cfg_isolated)
    db.save_snapshot([Course("周五隔离课", day=5, start=1, end=2,
                             weeks=[11]).to_dict()], "test")
    _push_calls.clear()
    asyncio.run(run_reminders(now=_at("2026-11-13T07:50:00")))
    check("提醒.按用户独立推送", len(_push_calls) == 1
          and "周五隔离课" in _push_calls[0][1], str(_push_calls))
    check("提醒.去重记录落当前用户",
          db.reminder_sent("2026-11-13", "course:周五隔离课:1:"))
finally:
    current_user_id.reset(_ctx_isolated)
_push_calls.clear()
asyncio.run(run_reminders(now=_at("2026-11-13T07:50:00")))
check("提醒.用户间互不串扰", not _push_calls
      and not db.reminder_sent("2026-11-13", "course:周五隔离课:1:"), str(_push_calls))

# 同步后推送逻辑：今天有剩余课 → 推「今日剩余课程」；今天没课 → 不提前推
# 明日课表、不占 next_day 去重键（2026-11-18 线上修复：中午同步提前推会占用
# 去重键，导致配置了 22:30 等自定义时间用户当晚收不到定时推送）。
# 明日课表统一由每晚定时任务按各用户 daily_time（默认 22:00）推送。
_sync_cfg = load_config()
_sync_cfg["push"] = {**_sync_cfg.get("push", {}), "bark_enabled": True, "bark_key": "t",
                     "push_next_day": True, "reminder_enabled": False}
# 把第 10 节的结束时间固定到当天最晚，让「今日剩余课程」不依赖真实时钟，
# 否则晚上跑测试时该分支永远为空
_orig_section_ends = _sync_cfg.get("section_ends")
_sync_cfg["section_ends"] = {**(_orig_section_ends or {}), "10": "23:59"}
save_config(_sync_cfg)
# 清空快照与提醒记录，避免前面测试的课程/去重记录干扰
db.save_snapshot([], "test")
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")

# 1) 今天有剩余课（用真实 today 判断）→ 推「今日剩余课程」
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

# 2) 今天没课 → 不提前推明日课表、不占去重键（回归 2026-11-18 问题）
_tmr_dow = (_today + timedelta(days=1)).isoweekday()
_tmr_s = (_today + timedelta(days=1)).isoformat()
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(_sync_cfg,
      [Course("明日课", day=_tmr_dow, start=1, end=2, weeks=list(range(1, 20)))],
      "test", push=True))
check("同步推送.没课不提前占键",
      len(_push_calls) == 1 and "明日课表" not in _push_calls[0][1]
      and not db.reminder_sent(_tmr_s, "next_day"), str(_push_calls))

# 3) 无可用推送通道的用户：同样不推也不标记，避免白占去重位
_nc_cfg = load_config()
_nc_cfg["push"] = {**_nc_cfg.get("push", {}), "bark_enabled": False,
                   "wecom_enabled": False, "push_next_day": True,
                   "reminder_enabled": False}
save_config(_nc_cfg)
_push_calls.clear()
asyncio.run(_sched.apply_snapshot(_nc_cfg,
      [Course("明日课", day=_tmr_dow, start=1, end=2, weeks=list(range(1, 20)))],
      "test", push=True))
check("同步推送.无通道不占去重键",
      not _push_calls and not db.reminder_sent(_tmr_s, "next_day"), str(_push_calls))
save_config(_sync_cfg)  # 恢复有通道配置

# 4) 隔离前置考试夹具；本组仅验证课程摘要，混合内容另有测试。
db.save_exam_snapshot([], "test")
# 每晚定时任务：未推过 → 按默认 22:00 推；已推过（被标记）→ 跳过
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T22:00:00")))
check("定时推送.未推则推送明日", len(_push_calls) == 1
      and "明日课表" in _push_calls[0][0], str(_push_calls))
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T22:00:00")))
check("定时推送.明日已推则跳过", not _push_calls, str(_push_calls))

# 5) 用户设定的推送时间（如 22:30）能命中，当晚照常推（回归 2026-11-18 问题）
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_dt_cfg = load_config()
_dt_cfg["push"] = {**_dt_cfg.get("push", {}), "bark_enabled": True, "bark_key": "t",
                   "push_next_day": True, "reminder_enabled": False, "daily_time": "22:30"}
save_config(_dt_cfg)
_push_calls.clear()
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T22:30:00")))
check("定时推送.设定时间照常推送", len(_push_calls) == 1
      and "明日课表" in _push_calls[0][0], str(_push_calls))
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")

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
asyncio.run(_sched.push_next_day_summary(now=_at(_today.isoformat() + "T21:29:00")))
check("每日推送.配置时间前不推", not _push_calls, str(_push_calls))
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
db.delete_user(_uid_isolated)   # 清掉隔离用户的配置 / 快照 / 提醒记录
with db.get_conn() as conn:
    conn.execute("DELETE FROM reminders WHERE user_id = 1")
_sched.push_all = _orig_push
_sched.weather_line = _orig_weather
if _orig_snap:
    db.save_snapshot(_orig_snap["courses"], _orig_snap["source"])
if _orig_esnap:
    db.save_exam_snapshot(_orig_esnap["exams"], _orig_esnap["source"])
if _orig_ssnap:
    db.save_score_snapshot(_orig_ssnap["scores"], _orig_ssnap["source"])
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

# 企业微信 / 钉钉群机器人：离线 mock 传输（负载 / 失败 / 日志）
import app.push as _pushmod
_wecom_hits: list[tuple[str, dict]] = []


def _wecom_mock(request):
    _wecom_hits.append((str(request.url), json.loads(request.content or b"{}")))
    data = ({"errcode": 93000, "errmsg": "invalid webhook url"}
            if "deadbeef" in str(request.url)
            else {"errcode": 0, "errmsg": "ok"})
    return httpx.Response(200, headers={"Content-Type": "application/json"}, json=data)


_wecom_transport = httpx.MockTransport(_wecom_mock)
_orig_wecom_guard = _pushmod.assert_safe_outbound_url
_orig_wecom_transport = _pushmod.SafeAsyncHTTPTransport
_pushmod.assert_safe_outbound_url = lambda url: url
_pushmod.SafeAsyncHTTPTransport = lambda: _wecom_transport
WECOM_OK = WECOM_API + "242cbb53-1ae9-a13e-a360-4a2819a0af87"
WECOM_FAIL = WECOM_API + "deadbeef-1ae9-a13e-a360-4a2819a0af87"


async def main():
    # 5. 手动同步（无推送通道 → skip 日志，不报错）
    r = await run_sync(push=True)
    check("手动同步成功", r["ok"], r["message"])
    check("无通道时同步不报错不推送", r["ok"] and not r["pushed"], str(r["pushed"]))

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
    # 按通道发送：测试可绕过启用开关
    _scfg = {"push": {"bark_enabled": False, "bark_key": "",
                      "wecom_enabled": True, "wecom_webhook": WECOM_OK}}
    _sr = await send_selected(_scfg, "标题", "正文", ["wecom"])
    check("推送.按通道发送", _sr[0]["ok"] is True and _wecom_hits
          and _wecom_hits[-1][1]["text"]["content"].endswith("正文"), str(_sr))
    _sr = await send_selected(_scfg, "t", "b", ["bark"])
    check("推送.未配置通道友好失败",
          _sr[0]["ok"] is False and "未配置" in _sr[0]["error"], str(_sr))
    _sr = await send_selected(_scfg, "t", "b", ["sms"])
    check("推送.未知通道拒绝",
          _sr[0]["ok"] is False and "不支持" in _sr[0]["error"], str(_sr))

    _pushmod.assert_safe_outbound_url = _orig_wecom_guard
    _pushmod.SafeAsyncHTTPTransport = _orig_wecom_transport

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
    cfg3["push"]["bark_key"] = "sk-test-secret"
    save_config(cfg3)
    from app.config import masked_config
    masked = masked_config(load_config())
    check("密钥掩码", masked["push"]["bark_key"] == "********")
    save_config({"push": {"bark_key": "********"}})
    check("掩码提交保留原密钥", load_config()["push"]["bark_key"] == "sk-test-secret")
    # 清理测试密钥
    save_config({"push": {"bark_key": ""}})

asyncio.run(main())

# 9. cURL 解析（Chrome Copy as cURL 风格）
curl_text = """curl 'https://portal.qut.edu.cn/portal/api/timetable' \\
  -H 'accept: application/json' \\
  -H 'content-type: application/json' \\
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
    {"url": "https://portal.qut.edu.cn/portal/api/kb", "headers": {}},
    "A=1").get("Cookie") == "A=1")
check("一键登录.POST body 解析", _parse_body('{"week":9}') == {"week": 9})
check("一键登录.状态不泄露 Cookie", "SESSION" not in str(_public_candidates([cand])))
masked = _mask_url("https://portal.qut.edu.cn/api/kb?token=abc123&week=9")
check("一键登录.URL 票据脱敏", "token=***" in masked and "week=9" in masked, masked)
check("一键登录.初始状态", get_status()["status"] == "idle")
# 回归 2026-11-19 线上事故：start() 在 with _lock 块内调用 _set()（内部再加锁），
# 普通 threading.Lock 不可重入 → 事件循环自我死锁、全站无响应。锁必须可重入。
check("一键登录.状态锁可重入", isinstance(_bl_lock, type(threading.RLock())),
      type(_bl_lock).__name__)

# 11d. App 化预埋：共用课表解析函数
cs = courses_from_payload(
    {"courses": [{"name": "数学", "day": 1, "start": 1, "end": 2, "room": "A1"}]},
    "courses", {})
check("预埋.课表解析可复用", len(cs) == 1 and cs[0].name == "数学" and cs[0].room == "A1")
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


def _stop_process(proc) -> None:
    """终止临时子进程，超时后强制结束。"""
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


_http_data_dir = None


def _start_temp_server():
    global _http_data_dir
    import httpx as _hx
    port = _free_port()
    _http_data_dir = tempfile.mkdtemp(prefix="qut_verify_http_")
    server_log = open("/tmp/qut_verify_server.log", "w")
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
            cwd=str(Path(__file__).resolve().parent),
            env={**os.environ, "CAMPUS_DATA_DIR": _http_data_dir,
                 "REDIS_URL": "", "AGENDA_SKIP_REGISTER_SYNC": "1"},
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
    except BaseException:
        _stop_process(proc)
        raise
    finally:
        server_log.close()


async def http_tests(port: int):
    """端到端：会话鉴权、多用户隔离、cURL 导入、数据注入、跨源 token 与 CORS。"""
    import httpx as _hx

    base = f"http://127.0.0.1:{port}"
    today = date.today()
    subscription_exam_date = (today + timedelta(days=30)).isoformat()
    manual_exam_date = (today + timedelta(days=45)).isoformat()
    manual_exam_updated_date = (today + timedelta(days=46)).isoformat()
    reg_counter = 0

    def reg_headers():
        nonlocal reg_counter
        reg_counter += 1
        return {"X-Forwarded-For": f"198.51.100.{reg_counter}"}

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
        "username": "admin", "password": "AdminPass123", "display_name": "管理员"},
        headers=reg_headers())
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
        check("import-curl.本地地址被 SSRF 拦截", d.get("ok") is False
              and "不允许访问本机/内网地址" in (d.get("detail", "") + d.get("error", "")),
              str(d)[:300])

        tt = (await ca.get(f"{base}/api/timetable")).json()
        await ca.post(f"{base}/api/settings", json={"weather": {"enabled": False}})
        w = (await ca.get(f"{base}/api/weather")).json()
        check("天气.接口关闭返回空表", w.get("ok") is True
              and w.get("enabled") is False and w.get("hours") == {}, str(w)[:200])
        await ca.post(f"{base}/api/settings", json={"weather": {"enabled": True}})
        await ca.post(f"{base}/api/settings", json={"rooms": {
            "default_campus": "05", "default_campus_name": "黄岛校区", "default_building": "B1"}})
        st = (await ca.get(f"{base}/api/settings")).json()
        check("默认校区.设置接口保存并回读",
              (st.get("rooms") or {}).get("default_campus") == "05"
              and st["rooms"].get("default_campus_name") == "黄岛校区"
              and st["rooms"].get("default_building") == "B1",
              str(st.get("rooms"))[:160])
        cleared = await ca.post(f"{base}/api/settings", json={"rooms": {
            "default_campus": "", "default_campus_name": ""}})
        check("默认教学楼.清空校区同步清空教学楼", cleared.json()["rooms"]["default_building"] == "")
        # 推送：企业微信单通道测试（离线子进程不发送真实网络请求）
        wecom_http_fixture = "http://127.0.0.1:9/webhook"
        await ca.post(f"{base}/api/settings", json={"push": {
            "bark_enabled": False, "bark_key": "",
            "wecom_enabled": False, "wecom_webhook": wecom_http_fixture}})
        r = (await ca.post(f"{base}/api/push/test", json={"channel": "wecom"})).json()
        check("推送.企业微信单通道测试", r["results"][0]["channel"] == "wecom"
              and r["results"][0]["ok"] is False
              and "不安全" in r["results"][0]["error"], str(r)[:200])
        await ca.post(f"{base}/api/settings", json={"push": {"wecom_enabled": True}})
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
        # webhook 现在按密钥脱敏回传（MASK），只要求仍是原值或掩码、且没被非法输入覆盖
        _keep_hook = keep.get("wecom_webhook")
        check("推送.被拒后配置未被覆盖",
              _keep_hook in (wecom_http_fixture, MASK) and "work.weixin" not in str(_keep_hook),
              str(_keep_hook)[:80])
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
            "username": "bob", "password": "BobPass123"}, headers=reg_headers())
        check("注册.无邀请码被拒", rb.status_code == 400, str(rb.status_code))
        rb = await ca.post(f"{base}/api/auth/register", json={
            "username": "bob", "password": "BobPass123", "invite": code},
            headers=reg_headers())
        check("注册.凭邀请码成功", rb.status_code == 200, str(rb.text)[:200])
        token_b = rb.cookies.get("qut_session")

        # 注册正方凭据使用 auto，后续门户接入可以自动优先；
        # 临时服务子进程已显式设置 AGENDA_SKIP_REGISTER_SYNC=1，不会触发真实网络同步。
        inv_zf = (await ca.post(f"{base}/api/auth/invites")).json().get("code")
        rz = await _hx.AsyncClient(timeout=30, trust_env=False).post(
            f"{base}/api/auth/register", json={
                "username": "zfnew", "password": "ZfnewPass123", "invite": inv_zf,
                "student_id": "20239999", "zhengfang_password": "secret-pwd"},
            headers=reg_headers())
        check("注册.带正方凭证成功", rz.status_code == 200, str(rz.text)[:200])
        token_zf = rz.cookies.get("qut_session")
        async with _hx.AsyncClient(timeout=30, trust_env=False,
                                   cookies={"qut_session": token_zf}) as cz:
            sz = (await cz.get(f"{base}/api/settings")).json()
            zp = sz.get("provider", {})
            zfg = zp.get("zhengfang", zp)
            check("注册.正方学号写入本人配置",
                  zfg.get("username") == "20239999", str(zfg)[:200])
            check("注册.自动主源允许后续门户优先",
                  zp.get("primary") == "auto" and _resolve_src(sz) == "zhengfang", str(zp)[:200])
        sb_after = (await ca.get(f"{base}/api/settings")).json()
        check("注册.正方凭证不污染管理员配置",
              "20239999" not in json.dumps(sb_after, ensure_ascii=False))
        # 清掉测试账号，避免影响后面「管理员注销转让权限」用例
        _zf_users = (await ca.get(f"{base}/api/auth/users")).json().get("users", [])
        for _u in _zf_users:
            if _u["username"] == "zfnew":
                await ca.delete(f"{base}/api/auth/users/{_u['id']}")

        # 一次性码用过即废
        again = await ca.post(f"{base}/api/auth/register", json={
            "username": "bob2", "password": "BobPass123", "invite": code},
            headers=reg_headers())
        check("邀请码.一次性码用后失效", again.status_code == 400, str(again.status_code))

        # 邀请码可复用次数：max_uses=2 的码可以被两个人用
        multi = (await ca.post(f"{base}/api/auth/invites",
                               json={"max_uses": 2})).json()
        mcode = multi.get("code")
        check("邀请码.可指定复用次数", bool(mcode) and multi.get("max_uses") == 2,
              str(multi)[:160])
        rr = await ca.post(f"{base}/api/auth/register", json={
            "username": "dave", "password": "DavePass123", "invite": mcode},
            headers=reg_headers())
        check("邀请码.多次码第 1 次可用", rr.status_code == 200, str(rr.status_code))
        rr = await ca.post(f"{base}/api/auth/register", json={
            "username": "erin", "password": "ErinPass123", "invite": mcode},
            headers=reg_headers())
        check("邀请码.多次码第 2 次可用", rr.status_code == 200, str(rr.status_code))
        rr = await ca.post(f"{base}/api/auth/register", json={
            "username": "frank", "password": "FrankPass123", "invite": mcode},
            headers=reg_headers())
        check("邀请码.次数用尽后被拒", rr.status_code == 400, str(rr.status_code))
        invites = (await ca.get(f"{base}/api/auth/invites")).json().get("invites", [])
        row = next((x for x in invites if x["code"] == mcode), {})
        check("邀请码.列表显示已用/剩余次数",
              row.get("used_count") == 2 and row.get("max_uses") == 2
              and row.get("remaining") == 0, str(row)[:200])
        check("邀请码.使用人可追溯",
              set(row.get("used_by_names") or []) == {"dave", "erin"}, str(row)[:200])
        # 用户列表附带两个数据源配置状态（供管理页双标签上色），只给布尔不泄露凭据
        users_now = (await ca.get(f"{base}/api/auth/users")).json().get("users", [])
        check("用户列表.附带双源配置状态",
              bool(users_now) and all(
                  isinstance(u.get("portal_configured"), bool)
                  and isinstance(u.get("zhengfang_configured"), bool)
                  for u in users_now), str(users_now)[:300])
        check("用户列表.不泄露凭据内容",
              all("password" not in u and "cookie" not in u and "headers" not in u
                  for u in users_now), str(users_now)[:200])
        # 清掉测试账号，避免影响后面「管理员注销转让权限」用例
        for u in users_now:
            if u["username"] in ("dave", "erin"):
                await ca.delete(f"{base}/api/auth/users/{u['id']}")
        invites = (await ca.get(f"{base}/api/auth/invites")).json().get("invites", [])
        row = next((x for x in invites if x["code"] == mcode), {})
        check("邀请码.注销账号后收回对应次数",
              row.get("used_count") == 0 and row.get("remaining") == 2, str(row)[:200])

        # 自定义邀请码：管理员自己指定码内容
        cus = (await ca.post(f"{base}/api/auth/invites",
                             json={"code": "QUT2026-class", "max_uses": 2})).json()
        check("邀请码.可自定义内容",
              cus.get("code") == "QUT2026-class" and cus.get("custom") is True
              and cus.get("max_uses") == 2, str(cus)[:160])
        dup = await ca.post(f"{base}/api/auth/invites", json={"code": "QUT2026-class"})
        check("邀请码.自定义重名被拒", dup.status_code == 400, str(dup.status_code))
        dup = await ca.post(f"{base}/api/auth/invites", json={"code": "qut2026-CLASS"})
        check("邀请码.自定义重名不区分大小写", dup.status_code == 400,
              str(dup.status_code))
        short = await ca.post(f"{base}/api/auth/invites", json={"code": "ab"})
        check("邀请码.自定义太短被拒", short.status_code == 400, str(short.status_code))
        bad = await ca.post(f"{base}/api/auth/invites", json={"code": "班级 2026"})
        check("邀请码.自定义非法字符被拒", bad.status_code == 400, str(bad.status_code))
        # 中文邀请码：允许汉字，全角自动归一化成半角
        cn = (await ca.post(f"{base}/api/auth/invites",
                            json={"code": "计科2301班级群", "max_uses": 2})).json()
        check("邀请码.中文码可创建", cn.get("code") == "计科2301班级群", str(cn)[:160])
        fw = (await ca.post(f"{base}/api/auth/invites",
                            json={"code": "测试２０２６"})).json()
        check("邀请码.全角自动转半角", fw.get("code") == "测试2026", str(fw)[:160])
        emo = await ca.post(f"{base}/api/auth/invites", json={"code": "开学啦🎉🎉"})
        check("邀请码.表情被拒", emo.status_code == 400, str(emo.status_code))
        rcn = await ca.post(f"{base}/api/auth/register", json={
            "username": "hana", "password": "HanaPass123", "invite": "计科2301班级群"},
            headers=reg_headers())
        check("邀请码.中文码可注册", rcn.status_code == 200, str(rcn.text)[:160])
        rfw = await ca.post(f"{base}/api/auth/register", json={
            "username": "iris", "password": "IrisPass123", "invite": "测试２０２６"},
            headers=reg_headers())
        check("邀请码.注册时全角自动归一化", rfw.status_code == 200,
              str(rfw.text)[:160])
        users_now = (await ca.get(f"{base}/api/auth/users")).json().get("users", [])
        for u in users_now:
            if u["username"] in ("hana", "iris"):
                await ca.delete(f"{base}/api/auth/users/{u['id']}")
        bad = await ca.post(f"{base}/api/auth/invites/batch",
                            json={"count": 3, "code": "batch-code"})
        check("邀请码.批量不支持自定义", bad.status_code == 400, str(bad.status_code))
        rc = await ca.post(f"{base}/api/auth/register", json={
            "username": "gina", "password": "GinaPass123", "invite": "QUT2026-class"},
            headers=reg_headers())
        check("邀请码.自定义码可用于注册", rc.status_code == 200, str(rc.status_code))
        users_now = (await ca.get(f"{base}/api/auth/users")).json().get("users", [])
        for u in users_now:
            if u["username"] == "gina":
                await ca.delete(f"{base}/api/auth/users/{u['id']}")

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
                "exam": {"name": "订阅测试考试", "date": subscription_exam_date,
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
        # （真实凭证会优先于 mock，必须先清空 portal/zhengfang 才能落到演示数据）
        await ca.post(f"{base}/api/settings", json={"provider": {
            "type": "mock", "portal": {"url": "", "headers": ""},
            "zhengfang": {"username": "", "password": "", "cookie": ""}}})
        sr = (await ca.post(f"{base}/api/sync")).json()
        gr = (await ca.get(f"{base}/api/grades")).json()
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
        _today_d = today
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
        # 演示源不产生成绩：直接检查同步后的成绩接口，绝不能用假数据填充。
        check("成绩.演示同步不产生成绩", not gr.get("scores")
              and not gr.get("supported"), str(gr)[:260])
        # 兼容旧版遗留的演示快照：库里即使有 mock 成绩也必须返回空
        db.save_score_snapshot([_s1.to_dict()], "mock")
        gr_demo = (await ca.get(f"{base}/api/grades")).json()
        check("成绩.旧演示快照不展示", not gr_demo.get("scores"), str(gr_demo)[:200])
        check("成绩.空快照也提供当前学期选项",
              (gr_demo.get("current_term") or {}).get("key")
              == f"{default_term()[0]}|{default_term()[1]}"
              and len(gr_demo.get("terms", [])) >= 1, str(gr_demo)[:200])
        with db.get_conn() as _c:
            _c.execute("DELETE FROM score_snapshot WHERE user_id = 1")
        # 写入真实成绩快照后，验证分组 / 统计 / 稳定标识
        db.save_score_snapshot([_s1.to_dict(), _s2.to_dict()], "zhengfang")
        gr = (await ca.get(f"{base}/api/grades")).json()
        check("成绩.接口返回真实成绩", len(gr.get("scores", [])) >= 2
              and all(x.get("uid") and x.get("term_label") for x in gr["scores"]),
              str(gr)[:260])
        check("成绩.分组与统计", len(gr.get("groups", [])) >= 1
              and gr.get("stats", {}).get("count", 0) >= 2
              and gr["stats"].get("avg") is not None,
              str(gr.get("stats"))[:160])
        check("成绩.分组带学期均分", all("avg" in g for g in gr.get("groups", [])),
              str(gr.get("groups", []))[:200])
        _cur_y, _cur_t = default_term()
        _cur_key = f"{_cur_y}|{_cur_t}"
        check("成绩.默认学期元数据", gr.get("current_term", {}).get("key") == _cur_key
              and any(t.get("key") == _cur_key and t.get("is_current")
                      for t in gr.get("terms", [])),
              str(gr.get("current_term"))[:200])
        check("成绩.学期选项含历史成绩", any(t.get("year") == "2025"
              and t.get("term") == "3" and t.get("count") == 2
              for t in gr.get("terms", [])),
              str(gr.get("terms"))[:240])
        # 错误透传：写一条失败日志后接口应带 error，测完清理避免污染日志用例
        db.add_sync_log("fail", "成绩拉取失败（不影响课表）：测试用接口错误")
        gr_err = (await ca.get(f"{base}/api/grades")).json()
        check("成绩.接口透传同步错误", "测试用接口错误" in (gr_err.get("error") or ""),
              str(gr_err.get("error")))
        with db.get_conn() as _c:
            _c.execute("DELETE FROM sync_log WHERE message LIKE '%测试用接口错误%'")
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
            "action": "add", "exam": {"name": "英语四级", "date": manual_exam_date,
                                       "start": "09:00", "end": "11:20",
                                       "room": "3-101", "seat": "8"}})).json()
        lst = (await ca.get(f"{base}/api/exams")).json()
        manual_ex = next((e for e in lst["exams"] if e["name"] == "英语四级"), None)
        st_e = (await ca.get(f"{base}/api/status")).json()
        check("考试.手动添加生效", add.get("ok") and manual_ex is not None
              and manual_ex["manual"] and len(lst["exams"]) == ex_base + 1
              and st_e.get("exam_count") == ex_base + 1
              and manual_ex["display_time"] == f"{manual_exam_date} 09:00-11:20",
              str(manual_ex)[:200])
        upd = (await ca.post(f"{base}/api/exams", json={
            "action": "update", "uid": manual_ex["uid"],
            "exam": {"name": "英语四级（改）", "date": manual_exam_updated_date,
                     "start": "14:00", "end": "16:00", "room": "3-102"}})).json()
        lst = (await ca.get(f"{base}/api/exams")).json()
        check("考试.修改手动考试", upd.get("ok") and any(
            e["name"] == "英语四级（改）" and e["date"] == manual_exam_updated_date
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
        today_iso = today.isoformat()
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
                (bob_id, today_iso, "course:注销清理课:1:", db.now_str()))
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
            n_rem = conn.execute("SELECT COUNT(*) FROM reminders WHERE user_id = ?",
                                 (bob_id,)).fetchone()[0]
        check("注销.个人日程一并删除", n_ev == 0, str(n_ev))
        check("注销.提醒记录一并删除", n_rem == 0, str(n_rem))
        # 管理员注销时把权限交给最早注册的其他账号
        code2 = (await ca2.post(f"{base}/api/auth/invites")).json().get("code")
        r = await ca2.post(f"{base}/api/auth/register", json={
            "username": "carol", "password": "CarolPass123", "invite": code2},
            headers=reg_headers())
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
_main_db_path = db.DB_PATH
try:
    server_proc, port = _start_temp_server()
    db.DB_PATH = Path(_http_data_dir) / "campus.db"
    asyncio.run(http_tests(port))
except Exception as e:  # noqa: BLE001
    check("端到端用例", False, f"临时服务或用例异常：{e}")
finally:
    _stop_process(server_proc)
    db.DB_PATH = _main_db_path
    if _http_data_dir:
        shutil.rmtree(_http_data_dir, ignore_errors=True)


# 会话原子性：放在 E2E（首个用户=admin 已注册）之后跑，避免抢占演示所挂的 id=1。
_session_uid = _db.create_user("sessionrace", "current-hash", role="user")
check("会话.旧密码哈希拒绝创建", not _db.checked_create_session(
    _session_uid, "stale-session", 60, "old-hash"))
check("会话.当前密码哈希允许创建", _db.checked_create_session(
    _session_uid, "fresh-session", 60, "current-hash")
      and _db.get_session_user("fresh-session")["user_id"] == _session_uid)
_db.delete_session("fresh-session")
_db.delete_user(_session_uid)

# ---- UID 迁移、旧来源配置合并与定时同步退避 ----
from hashlib import sha1
from unittest.mock import AsyncMock, patch

with tempfile.TemporaryDirectory(prefix="qut_verify_regressions_") as _reg_dir:
    with patch.object(db, "DB_PATH", Path(_reg_dir) / "campus.db"):
        db.init_db()
        _reg_uid = db.create_user("regressions", "unused-test-hash")
        _reg_ctx = set_current_user(_reg_uid)
        try:
            _reg_course = Course("迁移课程", day=2, weeks=[1, 3], source_id="course-42")
            _reg_exam = Exam("迁移考试", date="2026-12-01", start="09:00", end="11:00",
                             room="A101", seat="07", source_id="exam-42")
            _new_course_uid = course_uid(_reg_course)
            _new_exam_uid = exam_uid(_reg_exam)
            _old_course_uid = sha1("course-42|迁移课程|2|1|2|1,3".encode()).hexdigest()[:16]
            _old_exam_uid = sha1(
                "exam-42|迁移考试|2026-12-01|09:00|11:00|A101|07".encode()).hexdigest()[:16]
            _reg_course.uid = _old_course_uid
            _reg_exam.uid = _old_exam_uid
            db.save_snapshot([_reg_course.to_dict()], "portal")
            db.save_exam_snapshot([_reg_exam.to_dict()], "zhengfang")
            db.save_user_config(_reg_uid, {
                "course_overrides": {"hidden": [_old_course_uid, _new_course_uid],
                                     "edited": {_new_course_uid: {"room": "新调整"},
                                                _old_course_uid: {"room": "旧调整"}}},
                "exam_overrides": {"edited": {_old_exam_uid: {"seat": "08"}}},
                "provider": {"http_url": "https://example.invalid/courses",
                             "http_headers": {"Cookie": "session=legacy"},
                             "http_mapping": {"name": "old_name", "room": "classroom"},
                             "http_list_path": "result.rows", "zf_username": "legacy-user"},
            })
            _reg_cfg = load_config()
            check("UID迁移.旧快照同步改用新标识",
                  db.get_snapshot()["courses"][0]["uid"] == _new_course_uid
                  and db.get_exam_snapshot()["exams"][0]["uid"] == _new_exam_uid)
            check("UID迁移.去重并保留已有新键调整",
                  _reg_cfg["course_overrides"]["hidden"] == [_new_course_uid]
                  and _reg_cfg["course_overrides"]["edited"] == {_new_course_uid: {"room": "新调整"}}
                  and _reg_cfg["exam_overrides"]["edited"] == {_new_exam_uid: {"seat": "08"}})
            _reg_stored = db.get_user_config(_reg_uid)
            load_config()
            check("UID迁移.版本落库且重复加载幂等",
                  _reg_stored.get("override_uid_version") == 2
                  and db.get_user_config(_reg_uid) == _reg_stored)

            save_config({"provider": {"portal": {"headers": {"Cookie": "session=new"},
                                                "mapping": {"name": "new_name"}}}})
            _reg_provider = db.get_user_config(_reg_uid)["provider"]
            _reg_portal = load_config()["provider"]["portal"]
            check("旧来源配置.新 Cookie 和映射持久生效",
                  not any(k.startswith(("http_", "zf_")) for k in _reg_provider)
                  and _reg_portal["headers"] == {"Cookie": "session=new"}
                  and _reg_portal["mapping"]["name"] == "new_name")
            check("旧来源配置.保留未修改的门户和正方字段",
                  _reg_portal["url"] == "https://example.invalid/courses"
                  and _reg_portal["list_path"] == "result.rows"
                  and _reg_portal["mapping"]["room"] == "classroom"
                  and _reg_provider["zhengfang"]["username"] == "legacy-user")
        finally:
            current_user_id.reset(_reg_ctx)

        class _SyncClock(datetime):
            moment = datetime(2026, 11, 23, 7, tzinfo=_sched.TZ)

            @classmethod
            def now(cls, tz=None):
                return cls.moment.astimezone(tz) if tz else cls.moment.replace(tzinfo=None)

        async def _long_sync_failure(*, push):
            _SyncClock.moment += timedelta(minutes=20)
            return {"ok": False}

        _retry_jitter = timedelta(seconds=_reg_uid % 60)
        _retry_state = {}
        _sync_stub = AsyncMock(side_effect=[_long_sync_failure, {"ok": False, "busy": True},
                                           RuntimeError("模拟请求失败"), {"ok": True}])

        async def _sync_attempt(*, push):
            result = await _sync_stub(push=push)
            return await result(push=push) if callable(result) else result

        with (patch.object(_sched, "datetime", _SyncClock),
              patch.object(_sched, "run_sync", _sync_attempt),
              patch.object(_sched, "_sync_retries", _retry_state)):
            _sync_day = _SyncClock.moment.date().isoformat()
            _sched._sync_check_wrapper()
            _first_retry = _SyncClock.moment + timedelta(minutes=15) + _retry_jitter
            check("定时同步.长请求从结束时间算首次退避",
                  _retry_state.get(_reg_uid) == (_first_retry, 1))
            _sched._sync_check_wrapper()
            check("定时同步.退避期内跳过下一轮", _sync_stub.await_count == 1)
            _SyncClock.moment = _first_retry
            _sched._sync_check_wrapper()
            check("定时同步.忙碌可重试且不增加退避次数",
                  _sync_stub.await_count == 2 and _retry_state.get(_reg_uid) == (_first_retry, 1))
            _sched._sync_check_wrapper()
            _second_retry = _SyncClock.moment + timedelta(minutes=30) + _retry_jitter
            check("定时同步.同步异常进入第二次退避",
                  _sync_stub.await_count == 3 and _retry_state.get(_reg_uid) == (_second_retry, 2))
            _SyncClock.moment = _second_retry
            _sched._sync_check_wrapper()
            _sched._sync_check_wrapper()
            _reg_ctx = set_current_user(_reg_uid)
            try:
                check("定时同步.成功清除退避并标记当天完成",
                      _sync_stub.await_count == 4 and _reg_uid not in _retry_state
                      and db.reminder_sent(_sync_day, "daily_sync"))
            finally:
                current_user_id.reset(_reg_ctx)

# ---- 共享空教室：假教务和本地缓存，不使用真实凭证或网络 ----
async def _verify_shared_rooms():
    import httpx
    import app.main as main
    from app import room_cache as rc
    from app.user_context import get_current_user

    class Clock(datetime):
        moment = datetime(2026, 11, 23, 0, 30, tzinfo=rc.TZ)

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment.replace(tzinfo=None)

    env = {"AGENDA_ROOMS_SHARED_COOKIE": "fixture=shared",
           "AGENDA_ROOMS_SHARED_SEMESTER_START": "2026-08-31", "REDIS_URL": "",
           "AGENDA_ROOMS_SHARE_ON_DEMAND": "0"}
    campus = [{"id": "A", "name": "A 校区"}]
    source = main.ZhengfangProvider
    meta = AsyncMock(return_value={"campuses": campus, "campus": "A", "buildings": [
        {"id": "B1", "name": "一号楼"}, {"id": "B2", "name": "二号楼"}]})

    async def free(self, week, weekday, start, end, campus, building="", *, strict=False):
        if strict:
            assert self.cookie == "fixture=shared" and self._shared_rooms
            return [] if start == 3 else [{"id": "shared-room", "building": "一号楼"}]
        return [{"id": self.username}]

    cfg = {"semester_start": "2026-08-31", "provider": {"zhengfang": {
        "base": rc.BASE, "year": "2026", "term": "3"}}}
    provider = source(cfg)
    params = {"date_s": "2026-11-23", "campus": "A"}

    def request_config():
        uid = get_current_user()
        return {**cfg, "rooms": {"default_campus": "A", "default_campus_name": f"user-{uid}",
                                 "default_building": "B2" if uid == 1 else ""}}

    def session(token):
        return {"user_id": int(token)} if token in ("1", "2") else None

    with (patch.dict(os.environ, env), patch.object(rc, "datetime", Clock),
          patch.object(source, "fetch_room_meta", meta),
          patch.object(source, "fetch_free_rooms", autospec=True, side_effect=free) as fetch,
          patch.object(rc.asyncio, "sleep", new_callable=AsyncMock)):
        _cache.clear_local("rooms:shared:")
        await rc.refresh()
        check("共享空教室.单校区覆盖本周42个时段", fetch.await_count == 42
              and {c.args[2] for c in fetch.await_args_list} == set(range(1, 8))
              and all(c.kwargs == {"strict": True} for c in fetch.await_args_list))
        await rc.refresh()
        check("共享空教室.同一刷新周期内重启不重复回源", fetch.await_count == 42)
        Clock.moment += timedelta(days=1, minutes=-1)
        await rc.refresh()
        check("共享空教室.凌晨00:30前仍处于原刷新周期", fetch.await_count == 42)
        Clock.moment += timedelta(minutes=1)
        with patch.object(source, "fetch_free_rooms", new_callable=AsyncMock,
                          side_effect=ValueError("fixture failure")) as broken:
            await rc.refresh()
        old = await rc.get_rooms(provider, Clock.moment.date() - timedelta(days=1), "A", 1, 2)
        check("共享空教室.刷新连续失败三次停止且保留旧快照", broken.await_count == 3
              and old is not None and old["updated_at"] == "2026-11-23T00:30:00+08:00")
        await rc.refresh()
        check("共享空教室.进入新周期刷新全部时段", fetch.await_count == 84)
        with (patch.object(main, "load_config", request_config),
              patch.object(db, "get_session_user", session)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url="http://test") as client:
                a, b = {"Cookie": "qut_session=1"}, {"Cookie": "qut_session=2"}
                unauth = await client.get("/api/rooms/free", params=params)
                check("共享空教室.游客不能读取快照", unauth.status_code == 401)
                all_rooms = {**params, "building": ""}
                first = await client.get("/api/rooms/free", params=all_rooms, headers=a)
                second = await client.get("/api/rooms/free", params=all_rooms, headers=b)
                check("共享空教室.两个账号无个人凭证命中同一快照", first.status_code == 200
                      and first.json() == second.json() and first.json().get("cache_source") == "shared_week"
                      and first.json().get("updated_at") == "2026-11-24T00:30:00+08:00")
                preferred = await client.get("/api/rooms/free", params=params, headers=a)
                override = await client.get("/api/rooms/free", params={**params, "building": "B1"}, headers=a)
                check("共享空教室.默认楼生效且可显式改选", preferred.status_code == 200
                      and preferred.json().get("rooms") == []
                      and preferred.json().get("cache_source") == "shared_week"
                      and override.json().get("rooms") == first.json().get("rooms"))
                empty = await client.get("/api/rooms/free", params={**params, "start": 3, "end": 4}, headers=b)
                check("共享空教室.合法空结果命中不回源", empty.status_code == 200
                      and empty.json().get("rooms") == [] and fetch.await_count == 84)
                ma = (await client.get("/api/rooms/meta", headers=a)).json()
                mb = (await client.get("/api/rooms/meta", headers=b)).json()
                shared_meta = await rc.get_meta(provider, "")
                check("共享空教室.元数据不写入用户默认设置", ma.get("default_campus_name") == "user-1"
                      and mb.get("default_campus_name") == "user-2" and "default_campus_name" not in shared_meta)

                async def personal(uid, extra):
                    rc._on_demand_user_last.clear()
                    rc._on_demand_key_last.clear()
                    return await client.get("/api/rooms/free", params={**params, **extra},
                                            headers={"Cookie": f"qut_session={uid}"})

                def configured():
                    return {**cfg, "provider": {"zhengfang": {**cfg["provider"]["zhengfang"],
                            "username": f"person-{get_current_user()}", "password": "fixture"}}}

                with patch.object(main, "load_config", configured):
                    for extra in ({"building": "B"}, {"start": 2}, {"campus": "missing"}):
                        one = await personal(1, extra)
                        two = await personal(2, extra)
                        check(f"共享空教室.未覆盖条件回源且隔离 {extra}",
                              one.json().get("rooms") == [{"id": "person-1"}]
                              and two.json().get("rooms") == [{"id": "person-2"}])
                with (patch.dict(os.environ, {"AGENDA_ROOMS_SHARE_ON_DEMAND": "1",
                                              "AGENDA_ROOMS_SHARED_COOKIE": ""}),
                      patch.object(main, "load_config", configured),
                      patch.object(source, "fetch_free_rooms", new_callable=AsyncMock,
                                   side_effect=[[{"id": "room-A"}], [], ValueError("bad upstream"),
                                                [{"id": "room-past"}]]) as live,
                      patch.object(_cache, "set", wraps=_cache.set) as store):
                    day = Clock.moment.date()
                    query = {"date_s": (day + timedelta(days=7)).isoformat(), "building": "B1"}
                    first = await personal(1, query)
                    second = await personal(2, query)
                    check("按需共享.跨用户命中且只回源一次", first.status_code == 200
                          and first.json() == second.json() and live.await_count == 1
                          and live.await_args.kwargs == {"strict": True})
                    ttl = store.await_args.args[2]
                    check("按需共享.前后各七天允许写入且有效期固定七天", ttl == 7 * 86400
                          and rc.on_demand_write_eligible(day)
                          and rc.on_demand_write_eligible(day - timedelta(days=7))
                          and not rc.on_demand_write_eligible(day - timedelta(days=8))
                          and not rc.on_demand_write_eligible(day + timedelta(days=8)))
                    other_filter = await personal(2, {**query, "building": "B2"})
                    empty_hit = await personal(1, {**query, "building": "B2"})
                    check("按需共享.楼号隔离且缓存合法空列表", live.await_count == 2
                          and other_filter.json() == empty_hit.json() and empty_hit.json().get("rooms") == [])
                    bad = await personal(1, {**query, "start": 2})
                    check("按需共享.上游异常不写共享", bad.status_code == 400 and store.await_count == 2)
                    past_day = day - timedelta(days=7)
                    past_query = {**query, "date_s": past_day.isoformat()}
                    past = await personal(1, past_query)
                    past_hit = await personal(2, past_query)
                    check("按需共享.过去第七天同样写入七天", past.status_code == 200
                          and past.json() == past_hit.json() and live.await_count == 4
                          and store.await_args.args[2] == 7 * 86400)
                    Clock.moment += timedelta(days=1)
                    outside_hit = await personal(2, past_query)
                    check("按需共享.滑出写入窗口仍可命中且不续期", not rc.on_demand_write_eligible(past_day)
                          and outside_hit.json() == past.json() and live.await_count == 4
                          and store.await_count == 3)
                    with patch.object(source, "fetch_free_rooms", new_callable=AsyncMock,
                                      return_value=[{"id": "personal"}]) as outside_live:
                        outside_query = {**past_query, "building": "not-cached"}
                        miss = await personal(1, outside_query)
                        hit = await personal(1, outside_query)
                        check("按需共享.窗口外未命中沿用个人短缓存", miss.status_code == 200
                              and miss.json() == hit.json() and outside_live.await_count == 1
                              and store.await_args.args[2] == 60
                              and not store.await_args.args[0].startswith("rooms:on-demand:"))
                    unauth = await client.get("/api/rooms/free", params={**params, **query})
                    check("按需共享.游客不能读已有缓存", unauth.status_code == 401)
                    _cache.clear_local("rooms:on-demand:")
        other = source({"provider": {"zhengfang": {"base": "https://example.invalid", "year": "2026", "term": "3"}}})
        check("共享空教室.非同源或不同开学日不能读取", not rc.eligible(other, cfg["semester_start"])
              and not rc.eligible(provider, "2026-09-01"))
        _cache.clear_local("rooms:shared:")

    started = asyncio.Event()
    shutdown_order = []

    async def run_stub():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            shutdown_order.append("room task cancelled")

    async def close_cache():
        shutdown_order.append("cache closed")

    with (patch.dict(os.environ, env), patch.object(rc, "run", run_stub),
          patch.object(db, "init_db"), patch.object(db, "get_snapshot", return_value={}),
          patch.object(main, "load_config", return_value=cfg),
          patch.object(main, "start_scheduler"), patch.object(main, "shutdown_scheduler"),
          patch.object(main, "_warm_weather", new_callable=AsyncMock),
          patch.object(_cache, "start", new_callable=AsyncMock),
          patch.object(_cache, "close", close_cache)):
        async with main.lifespan(main.app):
            await asyncio.wait_for(started.wait(), timeout=1)
        check("共享空教室.应用关闭时先取消预取再关闭缓存", shutdown_order == [
            "room task cancelled", "cache closed"])


asyncio.run(_verify_shared_rooms())

# ---- 生产模式：未配置数据源时绝不展示演示数据 ----
# 演示开关只在上面夹具段开启；这里关闭后模拟「库里有遗留 mock 假快照」，
# 验证各 API 一律当空数据，且同步给出「去配置」引导。
os.environ["AGENDA_ALLOW_DEMO"] = "0"
_prod_dir = tempfile.mkdtemp(prefix="qut_verify_prod_")
os.environ["CAMPUS_DATA_DIR"] = _prod_dir
try:
    from fastapi.testclient import TestClient as _TC
    import app.config as _cfgmod
    _cfgmod.DATA_DIR = Path(_prod_dir)
    import app.db as _dbmod
    _dbmod.DB_PATH = Path(_prod_dir) / "campus.db"
    _dbmod.init_db()
    from app.main import app as _app
    from app.models import Course as _C, Exam as _E
    with _TC(_app) as _pc:
        _pc.post("/api/auth/register", json={"username": "admin", "password": "AdminPass123"})
        _dbmod.save_snapshot([_C("假高等数学", day=1, start=1, end=2).to_dict()], "mock")
        _dbmod.save_exam_snapshot([_E("假考试", date="2026-12-01", start="09:00", end="11:00").to_dict()], "mock")
        _dbmod.save_score_snapshot([], "mock")
        _st = _pc.get("/api/status").json()
        _tt = _pc.get("/api/timetable").json()
        _ex = _pc.get("/api/exams").json()
        _gr = _pc.get("/api/grades").json()
        check("无演示.状态无假数据", _st.get("is_demo") is False
              and _st.get("has_source") is False and _st.get("course_count") == 0,
              str(_st)[:160])
        check("无演示.课表无假数据", not _tt.get("courses")
              and _tt.get("is_demo") is False, str(_tt)[:160])
        check("无演示.考试无假数据且提示配置", not _ex.get("exams")
              and _ex.get("supported") is False, str(_ex)[:160])
        check("无演示.成绩无假数据", not _gr.get("scores")
              and _gr.get("is_demo") is False, str(_gr)[:160])
        _sr = _pc.post("/api/sync").json()
        check("无演示.同步引导去配置", _sr.get("ok") is False
              and _sr.get("no_source") is True and "设置" in _sr.get("message", ""),
              str(_sr)[:160])
        # 前端也不允许再出现「演示数据」：数据为空就显示空状态 + 去设置配置
        _appjs = Path("static/js/app.js").read_text(encoding="utf-8")
        _html = Path("static/index.html").read_text(encoding="utf-8")
        check("无演示.前端无演示文案与徽标", "演示数据" not in _appjs
              and "演示数据" not in _html and "demoBadge" not in _html
              and "data-go-source" in _appjs and "has_source" in _appjs,
              "app.js/index.html 仍含演示文案或缺少空状态入口")
finally:
    os.environ["AGENDA_ALLOW_DEMO"] = "1"
    os.environ["CAMPUS_DATA_DIR"] = _TEST_DATA_DIR
    shutil.rmtree(_prod_dir, ignore_errors=True)

shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)   # 清理隔离数据目录，全程不碰真实 config/DB

fails = [n for n, ok in passed if not ok]
print(f"\n{len(passed) - len(fails)}/{len(passed)} passed")
sys.exit(1 if fails else 0)
