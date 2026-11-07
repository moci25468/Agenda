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
from app.ai import build_context, _request_headers
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
from datetime import date, datetime
from app.push import push_all
from app.scheduler import run_sync, format_changes, courses_on, section_time
from app.config import load_config, save_config
from app import db
from app.models import (
    Course, Exam, apply_course_overrides, course_uid, diff_courses,
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

db.save_exam_snapshot([exam.to_dict()], "test")
esnap = db.get_exam_snapshot()
check("考试.快照落库", bool(esnap) and esnap["source"] == "test"
      and esnap["exams"][0]["name"] == "高等数学", str(esnap)[:160])
ctx = build_context(load_config())
check("考试.AI 上下文可回答教室座号",
      "高等数学" in ctx and "@1-201" in ctx and "座号 12" in ctx, ctx[-220:])

# 4. 明日课表筛选（2026-11-08 周日 → 无课；周一有课）
cfg = load_config()
snap = db.get_snapshot()
courses = [Course.from_dict(c) for c in snap["courses"]]
sun = courses_on(cfg, courses, date(2026, 11, 8))
mon = courses_on(cfg, courses, date(2026, 11, 9))
check("周日无课筛选", len(sun) == 0)
check("周一有课筛选", bool(mon) and all(
    c.day == 1 for c in mon), str([c.name for c in mon]))


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

    # 7. 日志落库
    logs = db.get_logs(10)
    check("同步日志落库", len(logs["sync"]) >= 2)
    check("推送日志落库", len(logs["push"]) >= 2)

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
