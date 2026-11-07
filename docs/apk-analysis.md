# QutCampusAssistant v1.3.2 拆解报告与「青理课表助手」提升建议

> 样本：`/Users/moci/Downloads/QutCampusAssistant-v1.3.2.apk`
> SHA-256：`512bc23bc6e8ec06b703c80144688aa947f166206219ba42edc7e575b5226544`
> 包名：`cn.edu.qut.campus`，版本名 1.3.2，versionCode 6，minSdk 26，targetSdk 35
> 工具：apktool 2.11.1 + jadx 1.5.3；应用自身代码经 R8 混淆，类名大多被重命名为 `A`、`y1`、`z.t` 等，Manifest 组件与数据模型仍保留可读名
>
> 用途说明：本文只做互操作与产品分析，用于改进你自己的项目。上线前请确认学校对第三方客户端、账号代登录和个人信息处理的要求；不要绕过验证码、MFA 或访问控制。

---

## 0. 结论先行

这个 App 值得看的不是界面，而是三件事：

1. **它把你「尚未实测」的正方教务链路完整写出来了**：RSA 加密登录、`csrftoken`、跳转判定、课表/考试/成绩/学业四个接口的参数和字段都能对上。你现在的 `app/providers/zhengfang.py` 路径、参数、登录方式都不对，实际是跑不通的。
2. **它把「课表助手」做成了「教务数据聚合」**：课表之外还有考试（含座号）、成绩（重修去重）、学业审核（GPA/学分/模块）、系统日历、桌面小组件。这些正好填你 v1.0 的功能空白。
3. **它的工程取舍和你相反**：单机、端侧直连、离线优先、无后端、无统计 SDK；你的优势是服务端定时同步、多用户、Bark/邮件、AI、跨平台 Web。不要为了抄功能丢掉这些优势。

最值得做的五件事，按投入产出排序：

| 优先级 | 事项 | 收益 | 参考工作量 |
|---|---|---|---|
| P0 | 按 APK 协议重写正方直连登录与课表拉取 | 让「账号密码直连」真正可用，不再只能靠 cURL | 0.5–1 天 |
| P0 | 校正青岛理工作息节次与校历默认值 | 今日课表、提醒、AI 回答的时间不再偏 | 10 分钟 |
| P1 | 新增考试 + 成绩两个模块 | 从「课表」升级为「教务助手」，且可推送 | 1–2 天 |
| P1 | 提供 ICS 日历订阅（替代原生写系统日历） | 手机原生提醒、锁屏/负一屏可见，零 App | 0.5–1 天 |
| P1 | 课前提醒 + 课程冲突检测 | 比「每天中午一条」更贴近真实使用 | 0.5–1 天 |

学业审核（GPA/学分模块）放在 P2：数据要解析教务页面 HTML，维护成本最高，收益相对窄。

---

## 1. 技术画像

### 1.1 构建与依赖

| 项 | 值 | 说明 |
|---|---|---|
| 语言 | Kotlin 2.0.21 | `kotlin-tooling-metadata.json` |
| 构建 | Gradle 8.11.1，AGP 8.11.1，JVM 17 | 同上 |
| UI | Jetpack Compose + Material3 | 单 Activity，`MainActivity` 承载全部页面 |
| 本地库 | Room、SharedPreferences、Android Keystore | 离线缓存、偏好、凭证加密 |
| 网络 | OkHttp（含 CookieJar 会话） | 直连 `jxgl.qut.edu.cn` |
| 协程/序列化 | kotlinx.coroutines，JSON 解析 | Repository 层全部 suspend |
| 系统集成 | AppWidget、CalendarProvider、POST_NOTIFICATIONS | 桌面小组件、系统日历、通知权限 |
| 后台 | 依赖中未见 WorkManager / JobScheduler 任务声明 | 更像启动或手动同步，提醒交给系统日历（混淆可能掩盖个别实现） |
| 统计/广告 | 未发现 Firebase、友盟、腾讯、Facebook、Sentry 等常见 SDK | 未做完整依赖树审计，但包名层面很干净 |
| 混淆 | R8，应用类被重命名为 `A`、`B0`、`y1` 等 | Manifest 组件与数据模型保留原名，其余反编译可读性一般 |

Manifest 权限只有：`INTERNET`、`ACCESS_NETWORK_STATE`、`READ/WRITE_CALENDAR`、`POST_NOTIFICATIONS`、`VIBRATE`。没有读取通讯录、定位、存储、设备标识等权限，和它「本地直连学校、不上传第三方」的隐私文案基本一致。

`network_security_config.xml` 只对 `qut.edu.cn` 及其子域放行明文 HTTP，其他域名强制 TLS。这个细节值得学：**学校域名兼容旧 HTTP，外部 API 一律 HTTPS**。你项目里 `zf_base` 默认还是 `http://`，应改成 `https://`。

### 1.2 架构

```
Compose UI (MainActivity)
      │
      ▼
Repository (z1.o) ──────────────► Room (课程/考试/成绩/学业缓存)
      │                                   │
      ▼                                   ▼
数据源 (y1.n)                        AppWidget (RemoteViews)
      │
      ├─ OkHttp + CookieJar ──► https://jxgl.qut.edu.cn/jwglxt/...
      ├─ RSA 加密登录
      └─ CalendarProvider ──► 系统日历（独立日历本 + 提醒）
```

数据源类对应关系（混淆后的名字）：

| 类 | 职责 |
|---|---|
| `y1.n` | 数据源门面 / 同步编排，持有 OkHttpClient 与 JSON 解析器 |
| `y1.k` | 正方教务账号密码直连登录 |
| `y1.m` | 统一身份认证（SSO / CAS）直连登录 |
| `y1.i` | 课表拉取与解析（`kbList` + `sjkList`） |
| `y1.e` | 考试安排拉取 |
| `y1.g` | 成绩拉取 |
| `y1.c` | 学业审核拉取 |
| `z1.o` | Repository：登录态、全量同步、当前周计算 |
| `A1.g` / `A1.c` / `A1.f` | 系统日历：建日历本、清空、逐周写事件与提醒 |
| `D1.*` | Compose 页面与 ViewModel（课表/考试/成绩/学业/我的） |

---

## 2. 功能地图

底部 5 个 Tab：**课表、考试、成绩、学业、我的**。登录页有两个入口：**统一身份认证（SSO）** 和 **教务直接登录（正方）**。

### 2.1 登录

- 首次进入先选登录方式，输入学号/密码；密码框可显隐。
- 隐私说明明确：账号密码只用于直连学校系统，不上传第三方；密码本机加密保存；退出登录会清除账号、密码和缓存。
- 登录成功后立即触发一次「全量同步」：课表 + 考试 + 成绩 + 学业。
- 登录态保存在本地，后续启动自动恢复；教务会话过期时提示重新登录。

### 2.2 课表

- 日视图 / 周视图切换，周次横向 chip（当前周高亮）。
- 课程卡片展示：课程名、教室、教师、节次、学分、开课周次；重修课程有「重修」徽标。
- 实践/线上环节单独归类，提示「以学院通知为准」。
- **冲突检测**：同一周内时间重叠的课程，顶部横幅提示「第 N 周检测到 X 门课程时间重叠」，卡片标「冲突」。
- 课程颜色按课程名哈希从固定 10 色盘取色，保证同一门课颜色稳定。
- 空状态文案区分「今天没有课」「该日无课程安排」。

### 2.3 考试

- 展示未结束考试：课程、时间、考场、座号、校区。
- 已结束考试默认折叠，可展开查看。
- 空状态区分「本次同步成功但没有考试」和「同步失败」。
- 支持下拉/按钮刷新，独立于课表。

### 2.4 成绩

- 按学期筛选，展示课程、成绩、绩点、学分、课程性质、考试性质、学院。
- **重修去重**：按课程名分组，过滤被覆盖的不及格历史记录，默认只保留最终有效成绩；可切换「全部考试记录」。
- 统计「已获有效学分」「待重修/待补考门数」。
- 不及格课程单独预警，提示关注补考/重修安排。

### 2.5 学业

- 官方 GPA（总 GPA、必修 GPA、选修 GPA）。
- 毕业学分总进度：要求学分、已获学分、尚欠学分、完成率。
- 课程门数概览：计划总门数、已通过、在读、未通过、未修。
- 培养模块明细：通识/专业/实践等模块的要求、已获、尚欠。
- 数据来源标注「正方教务系统官方学业审核」。

### 2.6 我的 / 设置

- 当前校区切换（黄岛校区 / 市北校区），展示校区地址与作息。
- 系统日历同步：提前提醒时长 10/15/20/30 分钟；一键同步、一键清空。
- 桌面小组件：今日/明日/昨日课程，极简透明样式。
- 主题：跟随系统 / 浅色 / 深色（AMOLED）。
- 全量同步、退出登录、版本号。

---

## 3. 数据链路（核心）

以下 URL 均来自 APK 反编译，主机固定为 `https://jxgl.qut.edu.cn`。所有请求共用同一个带 CookieJar 的 OkHttpClient，所以登录后的 `JSESSIONID` 会自动带到后续请求。

### 3.1 正方教务账号密码直连（`y1.k`）

这是你当前缺失最多的一段。完整流程：

1. `GET /jwglxt/xtgl/login_slogin.html`
   - UA：`Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36`
   - 从 HTML 中取：`id="csrftoken" value="([^"]+)"`
2. `GET /jwglxt/xtgl/login_getPublicKey.html?time=<毫秒时间戳>`
   - 返回 JSON：`{"modulus": "...", "exponent": "..."}`，两者都是 Base64。
3. 用 RSA 公钥加密密码：
   - 算法：`RSA/ECB/PKCS1Padding`
   - 把 `modulus`/`exponent` 转成 `RSAPublicKeySpec`
   - 密码 UTF-8 → RSA → Base64
4. `POST /jwglxt/xtgl/login_slogin.html`
   - Header：`Referer: https://jxgl.qut.edu.cn/jwglxt/xtgl/login_slogin.html`
   - Form：
     ```
     csrftoken=<第 1 步的值>
     language=zh_CN
     yhm=<学号>
     mm=<第 3 步的 Base64 密文>
     hidMm=
     ydType=
     ```
5. 判定成功：
   - HTTP 状态码 301/302，或 `Location` 包含 `index_initMenu`；
   - 否则报「登录失败，请检查学号与密码是否正确」。
6. 跟随 `Location` 再 GET 一次（补全相对路径），建立会话，返回用户。

**你当前 `app/providers/zhengfang.py` 的问题：**

| 现状 | APK 实际 | 后果 |
|---|---|---|
| `GET {base}/login_slogin`，base=`.../jwglxt` | `GET /jwglxt/xtgl/login_slogin.html` | 少 `/xtgl/`，404 |
| `POST {base}/login_slogin.do` | `POST /jwglxt/xtgl/login_slogin.html` | 路径不存在 |
| 直接提交明文 `mm` | RSA/ECB/PKCS1 加密后 Base64 | 学校拒绝明文密码 |
| 没有取 `login_getPublicKey` | 必须先取公钥 | 无法登录 |
| 没有 `hidMm` / `ydType` | 表单包含这两个空字段 | 可能被服务端校验 |
| 课表参数 `gnmkdm=N2151` | `gnmkpath=N2151` | 不同正方版本参数名不同，可能 404/空数据 |
| 没有 `X-Requested-With` / `Referer` | 两个请求头都有 | 可能被当成普通页面请求 |

**可直接替换的登录实现（httpx + cryptography）：**

```python
import base64, re, time
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa, padding

BASE = "https://jxgl.qut.edu.cn/jwglxt"
UA = ("Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36")


def rsa_encrypt(password: str, modulus_b64: str, exponent_b64: str) -> str:
    n = int.from_bytes(base64.b64decode(modulus_b64), "big")
    e = int.from_bytes(base64.b64decode(exponent_b64), "big")
    key = rsa.RSAPublicNumbers(e, n).public_key()
    return base64.b64encode(key.encrypt(password.encode("utf-8"), padding.PKCS1v15())).decode()


async def zf_login(client: httpx.AsyncClient, username: str, password: str) -> None:
    r = await client.get(f"{BASE}/xtgl/login_slogin.html", headers={"User-Agent": UA})
    m = re.search(r'id="csrftoken"\s+value="([^"]+)"', r.text)
    csrf = m.group(1) if m else ""

    r = await client.get(
        f"{BASE}/xtgl/login_getPublicKey.html",
        params={"time": int(time.time() * 1000)},
        headers={"User-Agent": UA},
    )
    pub = r.json()
    mm = rsa_encrypt(password, pub["modulus"], pub["exponent"])

    r = await client.post(
        f"{BASE}/xtgl/login_slogin.html",
        headers={"User-Agent": UA, "Referer": f"{BASE}/xtgl/login_slogin.html"},
        data={
            "csrftoken": csrf,
            "language": "zh_CN",
            "yhm": username,
            "mm": mm,
            "hidMm": "",
            "ydType": "",
        },
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    if r.status_code not in (301, 302) and "index_initMenu" not in loc:
        raise ValueError("教务登录失败：学号/密码错误，或需要验证码")
    await client.get(loc if loc.startswith("http") else "https://jxgl.qut.edu.cn" + loc)
```

依赖加一条：`cryptography>=42`。如果不想加依赖，也可以继续只用 Cookie 模式，但那就无法实现「账号密码直连」。

### 3.2 统一身份认证直连（`y1.m`，备选）

APK 还实现了 SSO 直连，入口是 `GET https://jxgl.qut.edu.cn/sso/ktiotlogin`，手动跟随最多 8 次跳转，落到 CAS 登录页后：

1. 从 HTML 里取两个隐藏字段：`pid`、`publicKey`（正则兼容属性顺序）。
2. 如果 `publicKey` 非空：按 X.509 `SubjectPublicKeyInfo` 解析公钥，RSA/PKCS1 加密密码；如果为空：只允许页面是 HTTPS，否则中止（避免明文提交）。
3. `POST` 当前登录页，表单字段：`username`、`password`、`pid`。
4. 若返回非 200，继续跟随最多 10 次跳转，最后 `GET /jwglxt/xtgl/index_initMenu.html` 建立教务会话。
5. 若返回 200，解析 `id="errormes"` 的 value 作为错误提示。

**建议**：不要把它作为默认路径。你自己的 README 已经论证过 SSO 登录的脆弱性（SPA、RSA、`lt/execution`、微信扫码、验证码）。APK 的做法能跑，但同样依赖页面结构，且需要保存统一身份密码。保留「一键浏览器登录」作为首选，正方直连作为「教务密码」备选即可。

### 3.3 课表（`y1.i`）

```
POST https://jxgl.qut.edu.cn/jwglxt/kbcx/xskbcx_cxXsKb.html?gnmkpath=N2151
Header: X-Requested-With: XMLHttpRequest
Header: Referer: https://jxgl.qut.edu.cn/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html
Form: xnm=<学年>  xqm=<学期代码>  kzlx=ck
```

响应是一个 JSON，核心两个数组：

- `kbList`：正常课程
  - `kcmc` 课程名
  - `cdmc` 教室
  - `xm` 教师
  - `xqj` 星期（1–7）
  - `jcs` 节次，如 `1-2`
  - `zcd` 周次描述，如 `1-16周`、`1-16周(单)`
  - `xf` 学分
  - `cxbj` 重修标记，`1` 表示重修
- `sjkList`：实践/线上环节
  - `kcmc` 课程名
  - `jsxm` 教师
  - `qsjsz` 起止周/节次描述
  - `xf` 学分

APK 的解析规则：

- 课程 ID：`课程名_星期_节次_周次`，用于稳定 diff。
- 实践环节固定放在周日（7）、第 11–12 节，并标记 `isPractice=true`，界面提示「实践/线上环节，以学院通知为准」。
- 颜色：课程名 hash 对 10 色盘取模，色盘为
  `#4DB6AC #64B5F6 #9575CD #FF8A65 #81C784 #FFB74D #BA68C8 #4DD0E1 #A1887F #F06292`。
- 周次解析：支持 `1-16`、`1,3,5`、`1-16(单)`、`1-16(双)`、单周次；上限 30 周。

**你的 `parse_weeks` 已经覆盖这些格式**，但 `total_weeks` 默认 25，建议改成 30，避免 26 周以后被截断。

### 3.4 考试（`y1.e`）

```
POST https://jxgl.qut.edu.cn/jwglxt/kwgl/kscx_cxXsksxxIndex.html?doType=query&gnmkpath=N358105
Header: X-Requested-With: XMLHttpRequest
Header: Referer: https://jxgl.qut.edu.cn/jwglxt/kwgl/kscx_cxXsksxxIndex.html?gnmkpath=N358105&layout=default
Form:
  xnm=<学年>
  xqm=<学期代码>
  _search=false
  nd=<时间戳>
  queryModel.showCount=50
  queryModel.currentPage=1
  queryModel.sortName=
  queryModel.sortOrder=asc
```

响应：`totalResult` + `items[]`，字段：

- `kcmc` 课程名
- `kssj` 考试时间
- `cdmc` 考场
- `zwh` 座位号

界面按考试时间判断是否结束：未结束的展示，已结束的折叠。校区字段由设置/教室信息补充。

### 3.5 成绩（`y1.g`）

```
POST https://jxgl.qut.edu.cn/jwglxt/cjcx/cjcx_cxDgXscj.html?doType=query&gnmkpath=N305005
Header: X-Requested-With: XMLHttpRequest
Header: Referer: https://jxgl.qut.edu.cn/jwglxt/cjcx/cjcx_cxDgXscj.html?gnmkpath=N305005&layout=default
Form:
  xnm=<学年>
  xqm=<学期代码>
  _search=false
  nd=<时间戳>
  queryModel.showCount=500
  queryModel.currentPage=1
  queryModel.sortName=
  queryModel.sortOrder=asc
```

响应：`totalResult` + `items[]`，字段：

- `kcmc` 课程名
- `bfzcj` 百分制成绩
- `cj` 成绩
- `jd` 绩点
- `xf` 学分
- `kcxzmc` 课程性质
- `xnmmc` 学年名
- `xqmmc` 学期名
- `ksxz` 考试性质（正常考试/重修等）
- `jgmc` 学院

**重修去重逻辑**（APK `Grade.deduplicateGrades`）：

1. 按课程名分组。
2. 只保留 `isPassed()` 为真的记录；通过规则：成绩非空、不含「不及格/未通过/缺考」等关键字、分数 ≥ 60。
3. 有通过记录时，按「分数 → 是否重修 → 学年」排序取最终一条；没有通过记录时，保留分数最高的一次。
4. 界面注明「已过滤被重修/补考覆盖的历史不及格记录，学分与门数无重复计算」。

这个逻辑可以直接复用到你的后端，用 `(course_name, academic_year, semester)` 做分组，保留结果写库。

### 3.6 学业审核（`y1.c`）

```
GET  /jwglxt/xtgl/index_cxBczjsygnmk.html?gndm=N105515
GET  /jwglxt/xsxy/xsxyqk_cxXsxyqkIndex.html?gnmkdm=N105515&layout=default
Header: Referer: /jwglxt/xtgl/index_initMenu.html?jsdm=xs
POST /jwglxt/xsxy/xsxyqk_cxKczxAllIndex.html?doType=query&gnmkdm=N105515
Header: X-Requested-With: XMLHttpRequest
Form: xh_id=<学号>
```

APK 先取页面 HTML（检查 `alertBox` / `xsxyqk` 判断会话），再用 `xh_id` 拉课程组 JSON，最后把 HTML 里的概览与 JSON 里的模块合并成：

- `studentName`
- `officialGpa` / `compulsoryGpa` / `electiveGpa`
- `totalPlannedCourses` / `passedCourses` / `failedCourses` / `unstudiedCourses` / `studyingCourses`
- `totalRequiredCredits` / `totalEarnedCredits` / `totalRemainingCredits`
- `auditTime`
- `modules[]`：`name` / `requiredCredits` / `earnedCredits` / `unearnedCredits`

**注意**：这段逻辑依赖教务页面 HTML 结构，最容易随学校升级失效。建议放 P2，并且失败时只降级隐藏「学业」卡片，不影响课表/考试/成绩。

### 3.7 学期、周次与作息

APK 的默认值：

- 学期开始：`2026-08-31`
- 学年：`2026`
- 学期代码：`3`（秋季，8 月–次年 1 月）；`12`（春季，2–7 月）
- 当前周：`floor((今天 - 开学日) / 7) + 1`，夹在 1–30

**这里有一个值得核对的差异**：你项目默认 `semester_start = 2026-09-07`，APK 默认 `2026-08-31`，相差一周。今天 2026-11-07，按 APK 算是第 10 周，按你的默认算是第 9 周。不要盲改，先对照校历；如果 APK 是对的，改默认值的同时要改 `verify.py` 里「第 9 周」的断言。

作息时间（黄岛/市北当前相同，10 节，每节 50 分钟）：

| 节次 | 开始 | 结束 |
|---|---|---|
| 1 | 08:00 | 08:50 |
| 2 | 09:00 | 09:50 |
| 3 | 10:05 | 10:55 |
| 4 | 11:05 | 11:55 |
| 5 | 14:00 | 14:50 |
| 6 | 15:00 | 15:50 |
| 7 | 16:05 | 16:55 |
| 8 | 17:05 | 17:55 |
| 9 | 19:00 | 19:50 |
| 10 | 20:00 | 20:50 |

你当前的 `DEFAULT_SECTION_TIMES` / `DEFAULT_SECTION_ENDS` 本身就不自洽：开始时间按 50 分钟间隔排（08:00、08:50、10:00…），结束时间却按 45 分钟算（08:45、09:35…），所以「连堂」之间只剩 5 分钟；第 3、7 节又跳成整点。它既不是 50 分钟一节的青理作息，也不是统一 45 分钟。这会直接影响「今天第 3 节几点上课」「明日课表推送」「AI 回答」，建议整表替换。

### 3.8 系统日历（`A1.g` / `A1.c` / `A1.f`）

APK 不是发通知，而是把整学期课表写进系统日历：

- 新建独立日历本：
  - `account_name = qut_schedule_calendar`
  - `account_type = LOCAL`
  - `calendar_displayName = 青岛理工大学(<学号>)课表`
  - `calendar_color = #FF006494`
  - `calendar_access_level = 700`
  - `sync_events = 1`
- 每门课按 `weeksList` 逐周展开为**单次事件**（不是 RRULE）：
  - 标题：`[青理] <课程名>`
  - 地点：教室
  - 描述：校区 / 教室 / 教师 / 第 X–Y 节 / 学分
  - `dtstart` / `dtend`：按上面的节次时间表计算
  - 时区：`Asia/Shanghai`
- 每个事件写一条 `CalendarContract.Reminders`，提前 10/15/20/30 分钟（用户可选）。
- 清空时按 `calendar_id` 删除事件，不影响个人日历；因为用了独立日历本，天然隔离。

这个设计对你的 Web 项目有一个几乎零成本的等价物：**ICS 订阅**。见第 6 节。

### 3.9 桌面小组件（`ScheduleWidgetProvider`）

- 最多 3 行课程，按小组件高度自适应：
  - 1 行：≥80dp
  - 2 行：≥145dp
  - 3 行：≥180dp
- 支持「昨日 / 今日 / 明日」切换，偏移 -1/0/+1，点击左右按钮切换。
- 收到 `DATE_CHANGED`、`TIME_SET`、`TIMEZONE_CHANGED` 时重置回今日并刷新。
- 透明背景，行高 32dp，时间 52dp 固定宽，课程名加粗，教室/教师次要信息。
- 无课时显示插画 + 「今天没有课哦」；课程超过可见行数显示「还有 N 门课」。

Web/PWA 不必复刻小组件，但可以复刻「今日卡片」的信息层级：左侧时间、竖线、课程名/教室/教师、还有几门课。这个对你现有「今日」页是纯 UI 改进。

### 3.10 本地存储与凭证安全

- 偏好：`SharedPreferences("qut_campus_prefs")`，键包括 `key_login_type`、`key_student_id`、`key_campus`、`key_term_start`、`key_reminder_minutes`、`key_widget_day_offset` 等。
- 凭证：Android Keystore 生成 AES-256/GCM 密钥 `qut_campus_credential_key`，加密后写成 `enc:v1:<base64(iv + ciphertext)>`；退出登录清空 SharedPreferences 与 Room 缓存。
- 业务数据：Room 数据库 `qut_campus_database`，离线可看课表/考试/成绩/学业。
- 网络：OkHttp + CookieJar，登录后的会话自动复用；没有证书固定（certificate pinning）。

**安全评价**：在「单机、本地直连」模型下，这套做法是合格的：密码不明文落盘、退出可清除、无第三方统计 SDK、明文只对学校域名放行。但它仍然要求用户信任这个第三方 App；设备被 root、Keystore 被攻破、或学校页面被中间人替换时，风险依然存在。你在做多用户服务端，风险模型不同：**密码会离开用户设备到你的 VPS**，所以不能只靠「config.json 0600」。

---

## 4. 与你当前项目的对照

| 能力 | 你的 v1.0 | QutCampusAssistant v1.3.2 | 结论 |
|---|---|---|---|
| 数据源 | mock / portal / zhengfang / httpjson | SSO 直连 / 正方直连 | 你的抽象更好；但正方适配器未跑通 |
| 正方账号密码登录 | 明文 + 错误路径，实际不可用 | RSA + csrftoken + 跳转判定，完整 | **P0 抄它** |
| 门户 cURL / Cookie | 有，带诊断 | 无 | 你的优势，保留 |
| 课表解析 | 支持 | 支持 + 实践环节 + 重修标记 + 学分 | 补字段 |
| 周次解析 | 支持单双周/范围 | 支持，上限 30 周 | 基本对齐 |
| 作息时间 | 12 节 × 45 分钟，通用值 | 10 节 × 50 分钟，青理实测值 | **P0 校正** |
| 学期默认值 | 2026-09-07 | 2026-08-31 | 核对校历 |
| 考试 | 无 | 有，含座号/已结束折叠 | P1 |
| 成绩 | 无 | 有，重修去重/GPA/预警 | P1 |
| 学业审核 | 无 | 有，GPA/学分/模块 | P2 |
| 日历 | 无 | 写系统日历 + 提前提醒 | **P1 用 ICS 替代** |
| 桌面小组件 | 无 | 有，今日/明日/昨日 | 用 PWA 今日卡片替代 |
| 推送 | Bark + 邮件，每日中午 + 变更 | 无推送，靠系统日历提醒 | 你的优势；补课前提醒 |
| 课前提醒 | 无 | 系统日历 VALARM 10/15/20/30 分钟 | P1 |
| 冲突检测 | 无 | 有，按周检测 | P1 |
| 多用户 | 有，完全隔离 | 无，单学生单设备 | 你的优势 |
| AI | 有，注入课表上下文 | 无 | 你的优势 |
| 定时同步 | 服务端 APScheduler，关手机也能跑 | 未见后台定时组件 | 你的优势 |
| 离线 | 快照，但页面依赖服务 | Room 离线 | P2 PWA |
| 凭证存储 | SQLite 配置明文 | Android Keystore AES/GCM | **P0/P1 加密** |
| 跨平台 | Web，任何设备 | 仅 Android | 你的优势 |
| 主题/校区 | 无/仅字段 | 跟随系统/深浅色/黄岛市北 | P2 |

**一句话**：它把「单机原生体验」做深了，你把「服务端自动化 + 多用户 + AI」做深了。正确的策略不是换成它，而是把它验证过的**教务协议和教务数据**接进你的后端。

---

## 5. 提升路线

### P0-1：重写正方适配器（最高优先级）

**落点**：`app/providers/zhengfang.py`、`requirements.txt`、`verify.py`。

要做的事：

1. 加 `cryptography` 依赖。
2. `_login` 改为 `xtgl/login_slogin.html` + `login_getPublicKey.html` + RSA/PKCS1。
3. 保留 Cookie 模式作为回退：有 `zf_cookie` 就不登录。
4. 课表请求改 `gnmkpath=N2151`，加 `X-Requested-With` / `Referer`。
5. 解析 `sjkList`，给 `Course` 增加 `kind`（`lecture`/`practice`）或 `is_practice` 字段。
6. 解析 `cxbj`（重修）和 `xf`（学分），给 `Course` 增加 `is_retake`、`credit`。
7. 学期参数自动推导：由 `semester_start` 算出 `xnm`/`xqm`，允许配置覆盖。APK 固定用 `3` 表示秋季（8 月–次年 1 月）、`12` 表示春季（2–7 月），你当前默认 `3`，与 APK 一致。
8. 错误分类：账号密码错误、会话过期、接口变更、网络超时，分别给可操作提示。

**验收**：

- 用 `httpx.MockTransport` 模拟「登录页 → 公钥 → 302 → initMenu → 课表 JSON」，断言请求路径、表单字段、RSA 密文可解密、`kbList`/`sjkList` 解析条数。
- 真实账号仍需你本人跑一次；README 里把「未实测」改成「协议已按第三方客户端校对，账号密码直连待本人实测；Cookie 模式可用」。

### P0-2：校正作息与校历

**落点**：`app/models.py`、`app/config.py`、`README.md`、`verify.py`。

- 用第 3.7 节的 10 节时间表替换 `DEFAULT_SECTION_TIMES` / `DEFAULT_SECTION_ENDS`。
- `total_weeks` 默认 30。
- `zf_base` 默认 `https://jxgl.qut.edu.cn/jwglxt`。
- 核对 `semester_start` 是否为 `2026-08-31`；如果是，同步改 README 和 `verify.py` 的周次断言。
- 如果学校确实有 11–12 节晚课，保留 11/12 的配置项，但不要用它们冒充正常作息；实践环节用独立 `kind` 表达。

### P0-3：凭证加密（多用户必须）

**落点**：`app/config.py`、`app/db.py`、`app/main.py`。

APK 用 Android Keystore 做端侧加密；你在服务端可以用等价的「主密钥 + AES-GCM/Fernet」：

- 主密钥来自环境变量 `CAMPUS_SECRET_KEY`，或首次启动生成 `data/.secret_key`（0600）。
- 写入时加密 `SECRET_FIELDS`：`zf_password`、`zf_cookie`、`http_headers`、`bark_key`、`smtp_pass`、`ai.api_key`、`ai.headers`、`security.token`。
- 读取时解密；迁移旧明文配置时自动加密并回写。
- 日志/接口继续掩码；`masked_config` 不变。
- 如果不想存密码：默认推荐 Cookie/门户登录，账号密码直连作为显式选项，并在 UI 里写清楚「密码会保存在服务器，已加密」。

这不是「抄 APK」，而是把它「凭证不裸奔」的原则搬到服务端。你的 README 已经提醒「配置里含教务密码与 API Key，不要裸奔暴露公网」，加密落盘是这句话的补完。

### P1-1：考试模块

**后端**：

- 新增 `app/providers/zhengfang.py` 的 `fetch_exams()`，复用登录会话。
- 新增快照类型 `exam`（见 6.3 的通用表），或独立 `exams` 表。
- 新增 `GET /api/exams`，返回 `{items, updated_at, source}`。
- `run_sync` 增加 `scope`：`timetable`（默认）/`all`；`all` 时依次拉课表、考试、成绩，单模块失败不影响其他模块。
- 推送：新考试 / 时间地点座号变化时，Bark/邮件提醒；首次同步只汇总，不逐条轰炸。

**前端**：

- 底部导航从 5 个变 6 个会拥挤，建议把「考试 / 成绩」放进一个「教务」Tab，内部再分子页；或者桌面端放侧栏、移动端放「更多」。
- 考试卡片：课程、时间、考场、座号、校区；已结束折叠；空状态区分「无考试」和「同步失败」。
- AI 上下文注入考试：「我下周有什么考试」「高数在哪个教室考」。

### P1-2：成绩模块

**后端**：

- `fetch_grades()`：按 3.5 节接口，`showCount=500`。
- 去重逻辑按 3.5 节实现，保存「原始记录 + 有效记录」两份，UI 可切换。
- 新增 `GET /api/grades?semester=all|2026-2027-1`。
- 推送：出现新成绩、出现不及格、重修覆盖旧成绩时提醒。

**前端**：

- 学期筛选 chip；课程成绩列表；GPA/已获学分统计；不及格预警。
- 文案沿用「已自动过滤被重修/补考覆盖的历史不及格记录」这种可解释性表述。

### P1-3：ICS 日历订阅（替代原生日历）

这是把 APK 的「系统日历」翻译到 Web 的最优解：

- 新增 `GET /api/calendar.ics?token=<calendar_token>`。
- 每个用户生成一个只读、可撤销的 `calendar_token`，存哈希；不要把登录 Cookie 放进 URL。
- 每门课按周展开一个 `VEVENT`（和 APK 一样逐周，不用 RRULE，单双周最简单）。
- `SUMMARY`：`[青理] 课程名`；`LOCATION`：教室；`DESCRIPTION`：教师/节次/学分/校区。
- `DTSTART`/`DTEND` 用 `Asia/Shanghai`，节次时间用 P0-2 的表。
- 每个事件加 `VALARM`：`TRIGGER:-PT30M`（可配置 10/15/20/30）。
- 考试也写进同一个日历，`SUMMARY` 用 `[考试] 课程名`，`DESCRIPTION` 含座号。
- 响应头：`Content-Type: text/calendar; charset=utf-8`；`Content-Disposition: inline; filename="qut.ics"`。
- 设置页展示「复制订阅链接」，并给 iOS/Android 的添加日历说明。
- 独立订阅日历天然隔离，不会污染个人日程；清空只需删除订阅。

**安全**：URL 带 token，必须 HTTPS；token 只读；支持重置；日志里掩码。不要复用 `security.token`（那是 App 接入的写权限 token）。

### P1-4：课前提醒

APK 用系统日历的 VALARM；你有 Bark/邮件，可以两种都做：

- **轻量方案**：只做 ICS VALARM，服务端不跑定时任务，零维护。
- **增强方案**：APScheduler 每 5 分钟扫一次「今天 + 当前周」的课，命中「开始时间 - 提前分钟」就发 Bark；用 `sent_reminders` 表去重（`user_id, date, course_key, lead`）。
- 邮件不适合每节课发，建议只对 Bark 开启；每天中午的「明日课表」保留。
- 提前分钟做成配置：10/15/20/30，默认 15 或 20。
- 失败留痕，沿用现有 `push_log`。

### P1-5：课程冲突检测

APK 的规则很简单：同一周、同一天、节次区间重叠。

```python
def conflicts(courses, week):
    active = [c for c in courses if not c.weeks or week in c.weeks]
    out = []
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if a.day == b.day and a.start <= b.end and b.start <= a.end:
                out.append((a, b))
    return out
```

- 周视图顶部显示「第 N 周检测到 X 门课程时间重叠」。
- 冲突课程卡片加「冲突」徽标。
- 同步 diff 里如果新增/调整导致冲突，推送里单独提示。
- 加进 `verify.py`：构造两门重叠课，断言只报一次、不误报相邻节次。

### P2-1：学业审核

- 按 3.6 节实现，但把它当成「增强卡片」：失败时隐藏，不影响主流程。
- 页面 HTML 解析用正则/BeautifulSoup 均可，重点是把「概览」和「模块」分开，缺一块也能展示另一块。
- 数据只在用户主动点「刷新学业」时拉，降低对教务的压力。
- AI 可以引用「还差多少学分」「必修 GPA」这类结构化字段，比让模型读 HTML 稳。

### P2-2：PWA / 今日卡片 / 校区 / 主题

- `manifest.json` + service worker，缓存最近一次 `/api/timetable`，离线可看今日/本周。
- 「今日」页复刻 APK 小组件的信息层级：时间、竖线、课程名、教室/教师、「还有 N 门课」。
- 校区设置（黄岛/市北）用于展示和 AI 上下文；作息目前相同，但保留按校区覆盖的能力。
- 主题：跟随系统 + 深色；你的 CSS 变量已经具备条件。

### P2-3：AI 与 v2.0 RAG

- 现有 AI 只注入课表；把考试、成绩、学业也做成结构化上下文，回答「我哪天考试」「我还有多少学分」「哪门课拖了 GPA」。
- 隐私：成绩/学业属于敏感信息，AI 请求前明确告知会把哪些字段发给模型；支持关闭。
- v2.0 的 RAG 知识库可以复用同一套「数据快照 + 推送 + 上下文」管线：通知/政策文档走检索，课表/考试/成绩走结构化注入，回答时分别标注来源。

---

## 6. 关键实现片段

### 6.1 正方登录 + 课表（可直接落地）

见 3.1 的 `zf_login`。课表部分：

```python
async def fetch_timetable(client, year: str, term: str):
    r = await client.post(
        f"{BASE}/kbcx/xskbcx_cxXsKb.html",
        params={"gnmkpath": "N2151"},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/kbcx/xskbcx_cxXskbcxIndex.html",
        },
        data={"xnm": year, "xqm": term, "kzlx": "ck"},
    )
    r.raise_for_status()
    payload = r.json()
    if "kbList" not in payload and "sjkList" not in payload:
        raise ValueError("教务未返回课表数据，可能接口调整或会话过期")
    ...
```

注意：`r.json()` 前先判断 `content-type` 或响应首字符是否为 `{`，避免登录页 HTML 被当 JSON 解析（APK 也是先判断 `{`）。

### 6.2 ICS 生成骨架

```python
import hashlib
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SECTION_START = {1: "08:00", 2: "09:00", 3: "10:05", 4: "11:05",
                 5: "14:00", 6: "15:00", 7: "16:05", 8: "17:05",
                 9: "19:00", 10: "20:00"}
SECTION_END = {1: "08:50", 2: "09:50", 3: "10:55", 4: "11:55",
               5: "14:50", 6: "15:50", 7: "16:55", 8: "17:55",
               9: "19:50", 10: "20:50"}


def build_ics(courses, semester_start, lead_minutes=30):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//QUT Campus Assistant//CN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:青理课表",
    ]
    for c in courses:
        weeks = c.weeks or list(range(1, 31))
        for w in weeks:
            day = semester_start + timedelta(weeks=w - 1, days=c.day - 1)
            start = datetime.combine(day, time.fromisoformat(SECTION_START[c.start])).replace(tzinfo=TZ)
            end = datetime.combine(day, time.fromisoformat(SECTION_END[c.end])).replace(tzinfo=TZ)
            uid = hashlib.sha1(
                f"{c.name}-{c.day}-{c.start}-{c.end}-{w}".encode()
            ).hexdigest()[:16]
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}@qut-campus",
                f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
                f"DTSTART;TZID=Asia/Shanghai:{start:%Y%m%dT%H%M%S}",
                f"DTEND;TZID=Asia/Shanghai:{end:%Y%m%dT%H%M%S}",
                f"SUMMARY:[青理] {c.name}",
                f"LOCATION:{c.room}",
                f"DESCRIPTION:{c.teacher} / 第{c.start}-{c.end}节 / {c.credit} 学分",
                "BEGIN:VALARM",
                f"TRIGGER:-PT{lead_minutes}M",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{c.name} 即将开始",
                "END:VALARM",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
```

实际实现还要做：文本转义（`\n`、`,`、`;`）、考试事件、token 鉴权、按用户缓存。

### 6.3 通用快照表（考试/成绩/学业）

你现在的 `snapshot` 是「每个用户一份课表」。与其为每个模块加一张表，不如先加一张通用表：

```sql
CREATE TABLE IF NOT EXISTS user_snapshots (
  user_id    INTEGER NOT NULL,
  kind       TEXT NOT NULL,          -- timetable | exams | grades | academic
  payload    TEXT NOT NULL,          -- JSON
  source     TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, kind)
);
```

优点：迁移简单、和现有 `snapshot` 的 JSON 风格一致、AI 上下文直接读 JSON。等查询/筛选需求变复杂，再把 `exams`/`grades` 拆成独立表。

### 6.4 课前提醒去重

```sql
CREATE TABLE IF NOT EXISTS sent_reminders (
  user_id    INTEGER NOT NULL,
  day        TEXT NOT NULL,          -- 2026-11-09
  course_key TEXT NOT NULL,
  lead       INTEGER NOT NULL,
  sent_at    TEXT NOT NULL,
  PRIMARY KEY (user_id, day, course_key, lead)
);
```

调度器每 5 分钟跑一次：取今天、当前周、开始时间落在 `[now+lead, now+lead+5min)` 的课，插入去重表成功后发 Bark。这样服务重启/多进程也不会重复发。

---

## 7. 不建议照搬的部分

1. **原生桌面小组件**：Web/PWA 做不了同等体验，别为了它上原生壳；今日卡片 + ICS + Bark 已经覆盖 80% 场景。
2. **把学校密码长期存在设备上**：APK 有 Keystore 加密，但仍然是第三方 App 持有全校统一身份密码。你服务端多用户模型更要注意：默认 Cookie/门户登录，密码直连作为显式选项并加密落盘。
3. **硬编码接口和学期**：APK 的 URL、`2026-08-31`、`N2151` 都写死在代码里；你的配置化方向是对的，继续保留，只把 APK 的值作为默认值/参考值。
4. **学业 HTML 解析作为核心功能**：太脆，放 P2，失败降级。
5. **10 节作息直接覆盖 11/12 节**：先确认学校是否有晚课；实践环节用独立 `kind`，不要伪装成正常课程。
6. **无后台同步**：APK 更像手动/启动同步，靠系统日历提醒；你的 APScheduler 是优势，不要退化成纯前端。
7. **无 certificate pinning**：学校域名轮换证书时 pinning 反而容易炸；保持系统信任链即可，重点是 HTTPS。

---

## 8. 建议的落地顺序（两周视图）

**第 1 天**

- P0-2 作息/校历/总周数校正。
- 用 `verify.py` 回归，确认周次断言、今日课表、推送文案没被改坏。

**第 2–3 天**

- P0-1 正方适配器重写 + MockTransport 测试。
- README 更新「已按第三方客户端协议校对」。
- 真实账号自测（需要你本人）。

**第 4–5 天**

- P0-3 凭证加密 + 迁移脚本。
- 设置页加「密码将加密保存在服务器」的说明。

**第 6–8 天**

- P1-1 考试模块（后端 + 一个页面 + 推送 + AI 上下文）。

**第 9–10 天**

- P1-2 成绩模块（去重 + 统计 + 预警）。

**第 11–12 天**

- P1-3 ICS 订阅（含考试事件、VALARM、设置页复制链接）。

**第 13–14 天**

- P1-4 课前提醒 + P1-5 冲突检测 + 回归测试。
- 有余力再动 P2 学业/PWA。

---

## 9. 证据索引（临时反编译目录）

> 这些是本次分析的临时产物，重启后可能被清理；保留在此便于你复核。

- Manifest / 资源：`/tmp/qutapktool/AndroidManifest.xml`、`/tmp/qutapktool/res/values/strings.xml`、`/tmp/qutapktool/res/xml/network_security_config.xml`
- 登录：`/tmp/qutjadx/sources/y1/k.java`（正方）、`/tmp/qutjadx/sources/y1/m.java`（SSO）
- 课表：`/tmp/qutjadx/sources/y1/i.java`
- 考试：`/tmp/qutjadx/sources/y1/e.java` + `/tmp/qutapktool/smali/y1.1/e.smali`
- 成绩：`/tmp/qutjadx/sources/y1/g.java`
- 学业：`/tmp/qutjadx/sources/y1/c.java`
- 模型：`/tmp/qutjadx/sources/cn/edu/qut/campus/data/model/{Course,Exam,Grade,AcademicOverview,AcademicModule,CampusPeriod}.java`
- 日历：`/tmp/qutjadx/sources/A1/{g,c,f}.java`
- 小组件：`/tmp/qutjadx/sources/cn/edu/qut/campus/widget/ScheduleWidgetProvider.java`
- 凭证加密：`/tmp/qutjadx/sources/x1/j.java`
- 偏好/学期：`/tmp/qutjadx/sources/x1/C1190a.java`
- Repository：`/tmp/qutjadx/sources/z1/o.java`

---

## 10. 一句话总结

**把这个 APK 当成「青理教务接口的实测样本」来用，而不是当成要复刻的产品。** 先把它验证过的正方登录、课表/考试/成绩接口接进你的后端，再用 ICS、课前提醒、冲突检测、多用户和 AI 把你的服务端优势放大。这样你的 v1.1 会比「再做一个原生课表 App」更有差异化。
