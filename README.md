# Agenda 知序

> 版本号由 git tag 动态管理，见 `GET /api/health`。发布流程：`./release.sh -m "中文改动说明"`（自动 +1 版本号、打 tag 并推送）。

青岛理工大学校园日程助手（Agenda 是拉丁语里的「当为之事」，「日程」一词的本源）：**定时拉取个人课表与考试 → 变更检测 → Bark 推送 → 个人日程 → AI 日程问答**，支持多用户与管理员后台。

## 核心特性

### 课表数据源：双源 + 门户优先

| 来源 | 说明 |
|---|---|
| **门户**（推荐） | 青岛理工融合门户，数据最及时（含临时调课 / 停课）。支持**一键扫码登录**自动获取，或 cURL 导入兜底 |
| **正方教务** | 学号 + 密码直连（页面不再暴露 Cookie 选项），额外提供考试安排 |
| **演示数据** | 内置示例课表，开箱即用，页面明确标注「演示数据」 |

优先级由 `provider.primary` 控制：`auto`（默认）= 门户优先、无门户才用正方；也可强制 `portal` / `zhengfang`。后期再接入门户时，下次同步会用门户数据整表覆盖正方数据；考试始终优先从正方拉取。

### 一键扫码登录（可在服务器运行）

- 服务端启动 headless Chromium，截取微信登录二维码回传页面渲染，用手机扫码即可完成登录
- 登录后自动发现课表接口、取会话 Cookie、写回配置并立即同步一次
- 不接触账号密码；二维码过期可停止后重新发起
- 部署在无桌面的服务器 / Docker 也能用（容器内已装 `chromium-headless-shell`）

### 其他能力

- **考试安排**：门户用户也可只填正方账号用于考试，课表仍以门户为准；独立快照与「考试」页
- **个人日程**：手动添加课程之外的一次性安排（标题 / 日期 / 时间 / 地点 / 备注，支持全天），并入「今日」时间线，随每日推送预告明日日程
- **定时同步**：默认每天 12:00（Asia/Shanghai，可改），拉取全量课表并 diff（新增 / 取消 / 教室教师调整 / 周次调整）
- **Bark 推送**：同步后推送「课表变更 + 明日课表与日程预览」，发送结果全部留痕可查
- **AI 助手**：OpenAI 兼容接口（通义千问 / DeepSeek / Ollama 等），自动注入当前教学周、本周课表、待考考试与个人日程上下文；SSE 流式逐字输出，支持轻量 Markdown；对话里说一句「明天下午三点开会」即可生成日程确认卡片，一键存入
- **手动改课表**：添加、修改、隐藏课程，叠加在同步结果上，不会被下次同步覆盖，可一键恢复自动课表
- **接入诊断**：导入后给出候选数组路径、字段名、建议映射与告警；支持粘贴 Response JSON 离线分析（不联网、不写配置）
- **Web 界面**：今日安排 / 个人日程 / 周视图（单双周、周次范围过滤）/ 考试 / AI 对话 / 课表来源 / 用户管理 / 设置

## 多用户与管理员

每个账号的数据源、Cookie、推送配置、AI Key、课表快照与日志**完全隔离**。

### 首次使用

打开页面即注册表单。**第一个注册的账号自动成为管理员**。之后注册必须凭邀请码。

### 邀请码

管理员在「用户管理」页生成邀请码发给同学：一次性、7 天有效，支持**批量生成**与**撤销**；列表显示状态（可用 / 已使用 / 已过期）及使用者。

### 管理员能力（「用户管理」页）

- **用户管理**：启用 / 停用、角色切换、删除、重置密码、编辑昵称与学号
- **搜索与排序**：按用户名 / 昵称 / 学号模糊搜索；按注册顺序 / 注册时间 / 最后登录 / 用户名排序
- **批量操作**：全选 + 批量停用 / 批量启用（自动保护自己不被操作）
- **用量概览**：总用户 / 活跃 / 已停用 / 管理员数 / 本周新增
- **数据源总览**：查看每个用户当前课表来源（门户 / 正方 / 未配置）与最后同步时间
- **审计日志**：记录停用、删除、改角色、重置密码、编辑信息、邀请码生成与撤销、批量操作等关键操作
- **停用即踢下线**：停用用户时同时清除其登录会话

### 注销账号

「设置 → 账号安全 → 注销账号」需再次输入**当前用户名 + 当前密码**双重确认。注销会一并删除该账号的会话、课表与考试快照、个人日程、手动改课记录、同步与推送日志、全部个人配置、已用邀请码记录。管理员注销时，权限自动交给最早注册的其他活跃账号。

### 隔离实现

`app/user_context.py` 用 `ContextVar` 绑定当前请求用户；`config.load_config()` 与 `db` 的快照 / 日志函数自动按用户读写，业务接口无需逐个改造。定时任务逐个用户切换上下文后同步。

## 快速开始（本地）

```bash
./run.sh            # 默认端口 8300，首次自动建 venv 装依赖
# 或指定端口：./run.sh 9000
```

打开 http://127.0.0.1:8300 。首次启动会自动同步一次演示课表，可直接体验全部界面。

macOS 下也可**双击 `start.command`**：自动起服务并打开浏览器；保持终端窗口开着即可。

> 本地 Web 服务不是常驻进程：`./run.sh` 和 `start.command` 都需要终端窗口一直开着。

## 部署（Docker + nginx）

```bash
docker compose up -d --build
```

数据（配置 + SQLite）持久化在 `./data`。nginx 反代示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8300;
    proxy_set_header Host $host;
    proxy_read_timeout 120s;   # AI 回答可能较慢
}
```

不用 Docker 也可以：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`，再用 systemd 跑 `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8300`。

**安全提醒**：`data/config.json` 含门户 Cookie，`data/campus.db` 含用户密码哈希，均已加入 `.gitignore`，**切勿提交到仓库**。公网多用户务必配置 HTTPS，否则会话 Cookie 明文传输。

### 代码热更新（不重建镜像）

```bash
tar czf /tmp/update.tgz app static
scp /tmp/update.tgz root@<server>:/tmp/
ssh root@<server> "rm -rf /tmp/staging && mkdir -p /tmp/staging && \
  tar xzf /tmp/update.tgz -C /tmp/staging && \
  docker cp /tmp/staging/app/. <container>:/srv/app/app/ && \
  docker cp /tmp/staging/static/. <container>:/srv/app/static/ && \
  docker restart <container>"
```

## 接入真实课表

### 门户：一键扫码登录（推荐）

「课表来源」页 →「一键扫码登录」→ 用微信扫描页面上的二维码 → 登录成功后自动发现接口、写回配置并同步。

**为什么不让程序替你输密码**：学校 SSO 是纯 SPA（1.2 MB 打包 JS），密码用页面下发的 RSA 公钥加密，带 `lt` / `execution` / `mfaCode` 一次性令牌，默认微信扫码，异常还会触发图形验证码，登录后还要走 OAuth 换 code。复刻这条链路既脆弱又要保存全校统一身份密码——所以改由真实浏览器完成登录，程序只接管登录之后的会话。

**实测接口**：门户课表来自 `portal/api/v2/datacenter/get/xskb?XN=…&XQ=…`，数组路径 `results.data.Rows`，字段为全大写 `KCMC / XQJ / KSJC / JSJC / SKDD / SKZC`。校历接口 `get/xlxx` 返回学期起止，已据此校正默认开学日期。

**限制**：接口 URL 写死学年学期（`XN` / `XQ`），**换学期请重新扫码登录一次**。若门户返回「按日期的日程事件流」而非每周循环课表，会明确提示需要事件适配器，不会写入错误配置。

### 门户：cURL 导入（兜底）

扫码不可用时（如微信风控）：

1. 浏览器登录门户，进入课表 / 日程页
2. F12 → Network → 筛选 Fetch/XHR → 刷新 → 找到 Response 是课程 JSON 的请求
3. 右键 → Copy → **Copy as cURL**，粘到「课表来源 → 导入 cURL」
4. 导入自动配置接口地址、请求头、数组路径与字段映射，并立即试拉一次

> cURL 含登录 Cookie，**只粘到本地设置页，不要发到聊天或群聊**。Cookie 过期后重新导入即可。

### 正方教务（账号密码）

「课表来源」→ 填学号、密码，点「保存并测试登录」验证连通性。学期只需选第一学期（`3`，秋季）或第二学期（`12`，春季）。

`jxgl.qut.edu.cn` 登录页使用 RSA 加密密码 + csrftoken，适配器已按真实页面实现（纯 Python RSA，无需额外依赖）。周次支持 `第15周`、`1-15周(单)`、`1-3周(单),4-8周(双),9周` 等教务原始写法。

> 连续输错 3 次会要求验证码（学校策略），等几分钟再试。

### 学期参数

- **开学日期**：第 1 周周一，按校历修改（默认 2026-08-31）
- **节次时间**：默认 10 节 × 50 分钟，可在配置的 `section_times` / `section_ends` 覆盖

## 版本与发布

- 版本号**唯一来源是 git tag**，运行时由 `app/version.py` 动态读取；容器内若无 `.git` 则回落到 `FALLBACK_VERSION`（发布脚本会自动同步该值）
- `GET /api/health` 返回 `{ok, version, tag, commit, committed_at}`
- 发布：`./release.sh -m "本次改了什么"`
  1. 从 GitHub 远程读取最新 tag
  2. 补丁号 +1（可传 `minor` / `major`）
  3. 自动写入 `app/version.py` 的 `FALLBACK_VERSION`
  4. commit（标题 = 改动说明，正文 = 版本号）→ 打 tag → push

## 验证情况

`.venv/bin/python verify.py` 即可，全程使用独立临时数据目录（`CAMPUS_DATA_DIR`），**不会碰你的 `data/config.json` 和 `campus.db`**。

覆盖：diff 三类变更、单双周解析、教学周计算、考试时间解析、AI 上下文与 SSE 流式解析、手动增删改课与叠加顺序、个人日程增删改查与多用户隔离、推送失败留痕、密钥掩码、cURL 导入、响应诊断、一键登录、数据注入、注册登录、会话鉴权、邀请码、注销与管理员权限转让、配置与课表隔离、跨源 token 与 CORS —— 当前 **157 项全部通过**。

**未实测**：Bark 真实送达、AI 真实回答（均需自备 Key）；门户扫码登录与正方账号登录需真实账号验证。

## 目录结构

```
app/               后端（FastAPI + APScheduler + SQLite）
  providers/       数据源：mock / portal（含通用 HTTP JSON）/ zhengfang
  browser_login.py 一键扫码登录（headless Chromium）
  version.py       版本号（从 git tag 读取）
  db.py            用户 / 会话 / 邀请码 / 快照 / 日志 / 审计
static/            单文件前端
data/              运行时生成（已 gitignore）：config.json、campus.db、browser_profile
verify.py          核心链路验证脚本
release.sh         发布脚本（版本号 +1 / 打 tag / 推送）
```

## 后期封装成 App 的预埋

登录方式与数据处理已解耦，换 App 外壳时核心逻辑不用改。

### 数据入口：POST /api/ingest

原生登录容器（App 内 WebView / 浏览器扩展）在登录态里直接 fetch 课表接口，只把 **JSON 数据**传进来，Cookie 不导出：

```bash
curl -X POST http://127.0.0.1:8300/api/ingest \
  -H 'Content-Type: application/json' \
  -H 'X-Local-Token: <token>' \
  -d '{"payload": {"code":200,"data":{"courses":[]}}, "push": false, "source": "app"}'
```

### 为什么不能调系统默认浏览器

系统浏览器的 Cookie 在它自己的沙盒里，App 读不到（`HttpOnly` 连 JS 也读不到）；学校 OAuth 的 `redirect_uri` 写死门户回调，无法注册自有回调 scheme。正解是 **App 内 WebView 登录后在该上下文直接 fetch，只传数据不传会话**。

桌面 App 可直接复用现有 Python 后端 + WebView 壳；移动 App 跑不了 uvicorn，需要后端上云（用 `/api/ingest` 上传数据）或用 Swift / Kotlin 重写核心。

## 边界与下一步

当前覆盖「课表 + 考试 + 个人日程 + 推送 + AI 问答」。数据模型只表达周期性课程，**教务侧的一次性调课 / 停课需单独的事件适配器**（个人手动日程已支持）。开题报告中的 RAG 校园知识库（通知 / 政策问答、引用溯源）为下一阶段方向。
