# Agenda 知序 · 体验与交互规范 v1.0

> Phase 2 交付物。基于对 `static/index.html`（9503 行）的全量核查：
> CSS（L23–4190，四个叠加皮肤 + 补丁块）、HTML（L4211–5169）、JS（L5170–9501）。
> 本文件是 Phase 3 的施工单：§2 每条问题带行号与修法；视觉细节一律引用
> `01-visual-direction.md` 的令牌名，不重复定义色值/字号。
>
> 核查基准行号以当前 index.html 为准，Phase 3 施工时若行号漂移，按选择器/id 定位。

---

## 0. 总体判断

**骨架是好的，皮肤是乱的。** 交互基建（骨架屏、空态、确认框、toast、弱网条、
焦点陷阱、Esc 关闭、IME 安全回车、滑动翻周、reduced-motion 降级、16px 防缩放）
已经齐全且质量不低——用户感到的"显示不好"主要来自：

1. **一整层死 CSS**（约 450 行）：`.sidebar`/`.main-content` 皮肤在 DOM 里
   没有匹配节点，里面声明的桌面布局规则全部不生效（详见 P0-1）；
2. **四层皮肤互相覆盖**留下的圆角/间距/字号漂移（01 文档已诊断色彩部分，
   本文件补布局与交互部分）；
3. 若干**局部体验硬伤**：确认框按钮永远红色、空教室页无导航入口、
   触屏下半透明按钮、空态文案误导等。

§2 按"P0 必修 / P1 应修 / P2 可修"分级，共 **28 条**。

---

## 1. 信息架构与导航建议

### 1.1 现状结构（实测，非注释宣称的结构）

```
登录页 authScreen（L4224）
└─ 主界面 #app（L4261）：平铺 header + nav + 7 个 section
   ├─ tab-today    今日（含明日卡）          L4288
   ├─ tab-week     日程（周课表+本周日程+考试） L4325
   ├─ tab-rooms    空教室                    L4382  ← 无导航入口！
   ├─ tab-ai       AI 助手                   L4423
   ├─ tab-source   课表来源                  L4478
   ├─ tab-admin    用户管理（仅管理员可见）   L4564
   └─ tab-settings 设置（8 张卡，5 个分区锚点）L4648
```

导航实际形态（实测）：
- **桌面（>720px）**：顶部悬浮胶囊横排 nav（L3169–3201，sticky top:12px），
  文字态，图标被 `#nav button svg{display:none}`（L3534）隐藏；
- **移动端（≤720px）**：底部固定标签栏（L3640–3681，z-index 45），
  图标+10px 文字，含 safe-area-inset-bottom。

> ⚠️ 注意：L4044–4046 注释和 01 文档都提到"≥901 桌面双栏侧边栏"，
> 该侧边栏 **在当前 DOM 中并不存在**（见 P0-1）。本规范以"顶部胶囊导航
> （桌面）/ 底部标签栏（移动）"为事实基线。若团队希望上桌面侧边栏，
> 那是 DOM 重构，超出本期范围，不建议做。

### 1.2 分组判断与建议

7 个 tab 的分组逻辑成立（查看类：今日/日程/空教室；工具类：AI；
配置类：来源/设置/管理），**不需要调整 JS 路由**。呈现层两条建议：

1. **给空教室补导航入口**（见 P1-2，HTML 微调，JS 零改动）。
2. **桌面 nav 项的顺序即当前顺序，不变**；移动端底部 7 项后需按 §4.4
   压缩 padding 防挤压。
3. "课表来源"是低频配置页，但在新用户 Onboarding 里是必经之路，
   保持在 nav 中合理；可在配置完成后降低其视觉权重（非必须，本期不做）。

---

## 2. 分区域问题清单（Phase 3 施工单）

严重度：**P0** 不修则规范无法自洽 / **P1** 明显影响使用 / **P2** 打磨项。

### 2.1 全局 / 导航 / 皮肤层

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| **P0-1** | CSS L2109–2560（`.sidebar`/`.main-content`/`.main-head`/`.brand-seal`/`.sidebar-bottom`/`.sidebar-actions` 全部规则） | **死代码皮肤**：这些类名在 HTML（L4211–5169）与 JS 中均不存在（grep 确认仅出现于 style 块）。其内两个 `@media (min-width:901px)` 块（L2183–2343、L2496–2560+）声明的桌面规则全部失效，包括：today-card 桌面大内边距（L2288–2308）、course-row 桌面字号（L2309–2319）、`week-grid{min-width:1000px}`（L2329–2331）、chat-box 桌面高度（L2338–2342）。桌面端实际渲染的是 L3118 块的单列样式 | 删除整段死皮肤；把其中仍需要的 3 条规则移植到活层（L3118 块之后）：① `week-grid{min-width:1000px}`（配合 `.week-grid-scroll{overflow-x:auto}`，防 901–1160px 宽度下列被压扁）；② today-card 桌面 padding 31px 32px；③ `.day-head .date` 桌面 34px（01 文档 Display 档）。同时把 L4044–4046 断点契约注释改为事实："≤480 微调 / ≤720 移动底栏 / >720 桌面顶部胶囊导航" |
| **P0-2** | JS L5291 `$("#confirmOk").classList.toggle("danger", opts.danger !== false)` | **确认框主按钮默认就是红色 danger**：所有未显式传 `danger:false` 的确认（恢复自动课表 L6519、恢复考试 L6665、批量启用 L9292 等非危险操作）主按钮都是红底，与 01 文档"朱砂/红只表重要语义"冲突，也让真正危险的确认失去辨识度 | JS 一行微改（已列入 §7 白名单）：默认改为 `opts.danger === true`；删除日程/删除记忆/删除用户/撤销邀请码等危险调用点已传 `ok:"删除"` 文案，逐个补 `danger:true`。CSS 同步加 `#confirmOk` 非 danger 态 = primary 样式 |
| **P1-1** | JS L5496 今日空态文案 | 空态副文写"或点击下方「添加日程」"，但今日卡内**没有**添加日程按钮（唯一按钮在明日卡 `addTomorrowEventBtn` L4317）——误导性空态 | JS 字符串微改（§7 白名单）：副文改为"可以安心安排自己的时间；要提前规划，可在下方「明日安排」里添加日程。" |
| **P1-2** | HTML L4278–4285 nav | **空教室页（tab-rooms L4382）没有导航入口**：只能从周课表空格"查空教室"（L6155）、AI 快捷 chip（L4444）进入；进入后底部标签栏无任何高亮项，用户无法直觉返回 | HTML 微调（§7 白名单）：在 nav 中"日程"后插入 `<button data-tab="rooms" role="tab" aria-selected="false" aria-controls="tab-rooms">…空教室</button>`（图标复用现有 svg 风格）。现有 JS 的 click/方向键委托（L8581–8599）按 `data-tab` 工作，零 JS 改动即生效。移动端底部栏变 7 项，按 §4.4 压缩 |
| **P1-3** | CSS L3300–3309 `.msg.ai` | 与 01 文档矛盾：01 §3.10 称保留"AI 消息无边框、靠署名+分隔线分层"，但现状 AI 消息是**带边框+投影的白色气泡卡**（border 1px + box-shadow + 4px 16px 16px 16px 圆角） | Phase 3 按 01 执行：去 border/box-shadow/气泡圆角，底 transparent（深色用 --card 页面底），保留 `.msg.ai::before`"知序"署名，消息间用 1px --line 底部分隔线。若评估后决定保留卡片形态，需回写修订 01 文档，二选一，不允许两样都不是 |
| **P1-4** | CSS L3555–3559 `.week-cell .wc-free` | 空格子的"查空教室/加日程"按钮默认 `opacity:.5`，仅靠 `.week-cell:hover` 恢复；移动端日视图复用同一 `cellHtml`（L6184–6189），触屏无 hover → **按钮永久半透明**，看起来像被禁用 | CSS：追加 `@media (hover:none){ .week-cell .wc-free{opacity:1} }`；桌面 hover 揭示交互保留（键盘用户见 P2-4） |
| **P1-5** | HTML L4901–4904 设置页底部保存 | 设置页 8 张长卡滚完才出现"保存设置"；用户改完"外观"（第一张卡）后要滚到页尾找保存，极易漏存 | 纯 CSS：给设置页最后一个 `.form-actions` 加 sticky 底条：`position:sticky; bottom:0; padding:12px 0; background:color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter:blur(8px); border-top:1px solid var(--line)`；移动端 bottom 抬升为 `calc(84px + env(safe-area-inset-bottom))` 避让底部标签栏 |
| **P2-1** | CSS L3399 auth-layout | 721–1020px 宽度区间登录页 intro 列被挤到约 186px（grid `1fr 410px` + `gap:100px`），产品文案几乎不可读 | CSS：加 `@media (max-width:1100px){ .auth-layout{gap:44px} }`，或 ≤1020px 直接 `display:block` 堆叠 |
| **P2-2** | CSS L3385–3393 `.chat-input-hint` | 10px、`#939a91` 硬编码色，白底对比约 2.9:1，不达 AA | 字号升 11px、色改 `--sub`；该文字属辅助说明，保留一行即可 |
| **P2-3** | CSS L1671–1682 `.msg-actions` | 消息"编辑/重发"按钮 `opacity:0`，仅 `:hover` 与 `@media(hover:none)` 兜底；**键盘 Tab 进入时不显现**（触屏反而常显，桌面键盘反而不可见） | CSS：补 `.msg:focus-within .msg-actions{opacity:1}` |
| **P2-4** | JS L6106–6112 + L8619 `#weekLabel` | "第 N 周 · 回到本周"是可点击的 `<span>`：有 cursor:pointer（L445 ✓）但**无 tabindex、无键盘事件**，键盘/读屏用户无法回到本周 | JS 微改（§7 白名单）：渲染时改输出 `<button type="button" class="label">`，样式不变；或加 `tabindex="0"` + Enter/Space 处理 |
| **P2-5** | CSS L3655 移动端底部导航 | `background:rgba(255,255,255,.94)` 硬编码白（深色有 L3791 覆盖，属硬编码对硬编码）；与令牌脱节 | 随 01 令牌化：浅色 `color-mix(in srgb, var(--card) 94%, transparent)`，深色 `color-mix(in srgb, var(--card) 94%, transparent)`（深色变量自动取暗色值），删 L3791 硬编码 |
| **P2-6** | CSS L3251 `.sheet-panel` | 底部抽屉圆角 20px、`.chat-box` 20px（L3260）、`.auth-card` 20px（L3407），超出 01 文档上限 `--radius-xl:16px` | 全部收敛到 `--radius-xl`（16px），移动端抽屉保持 `16px 16px 0 0` |

### 2.2 今日 tab-today

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L4296–4320 | 下一节课直达条（next-class）、正在上课态、明日卡联动、课程/日程/考试按时间混排——**设计良好，全部保留** | — |
| **P2-7** | CSS L2303–2308（死代码内）+ 活层缺失 | `#todayCount` 桌面的顶部分隔线样式随死皮肤失效，桌面下与上方元素挤在一起 | 随 P0-1 移植：`#todayCount{margin-top:13px;padding-top:9px;border-top:1px solid var(--line)}` 移到活层 ≥901 媒体查询 |

### 2.3 日程 tab-week

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L6102–6221 | 双视图（桌面 7 列网格 / 移动按天竖列）共用一份数据、骨架双容器、快速滑动竞态防护（weekLoadSeq）——**架构良好，保留** | — |
| **P1-6** | CSS（活层缺失，死在 L2329） | 桌面 901–1160px 宽度下周课表无 min-width，7 列被压到约 130px/列，mini-course 三行文字拥挤换行 | 随 P0-1 移植 `min-width:1000px` + 确认 `.week-grid-scroll{overflow-x:auto}` 生效 |
| **P2-8** | JS L6179–6199 移动端日视图 | 7 张 day-card 全量展开，本周课程多时首屏看不到"今天"；切回本周无自动定位 | 可选 JS 微改（§7 候选）：`loadWeek` 渲染后若 `data.week===data.current_week` 且 ≤720px，`day-card.today` `scrollIntoView({block:'start'})`。不做也可接受，列为二期 |
| **P2-9** | HTML L4334–4338 更多菜单 | `.week-menu` 三个低频操作（编辑课表/导出/订阅）收纳合理；但菜单打开态无 `aria-expanded` 同步 | JS 微改候选：切换菜单时同步 `aria-expanded`；或接受现状（菜单本身 role=menu 已标注） |

### 2.4 空教室 tab-rooms

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L4388–4415 + JS L5986–5996 | 6 条件筛选栅格（≤720 收 2 列 L373–383 ✓）、查询中骨架+按钮文案切换 ✓、无结果空态 ✓、座位/类别二次筛选 ✓、教学楼 `<details>` 折叠（原生键盘可用 ✓）、"查我没课的时候"chips ✓——**覆盖最全的页面** | — |
| **P2-10** | HTML L4394 `<div class="field room-query"><span>&nbsp;</span>` | 用 `&nbsp;` 占位撑对齐 | HTML 微调：删 `&nbsp;`，CSS `.room-query{align-self:end}`（栅格已 align-items:end，删掉 span 后按钮自对齐） |
| **P1-2** | （同 2.1） | 无导航入口是本页最大问题 | 见 P1-2 |

### 2.5 AI 助手 tab-ai

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | JS L7777–7902 | 流式渲染按帧合并、用户上翻不强行回拉（nearBottom 80px 阈值）、部分中断保留已生成内容并给"继续"提示、错误内联卡带重试（L7880–7892 + CSS L3968–3977）、"正在思考…"思考态 + streaming 光标（L606）——**交互完成度最高的模块** | — |
| **P1-7** | HTML L4439 `aria-live="polite"` | 挂在整个 `.chat-msgs` 滚动容器上：流式期间每帧 innerHTML 更新，读屏器会被 Delta 洪水轰炸 | HTML 微改：容器去 aria-live，改加 `role="log"`；如需播报，在发送/完成时由 JS 往独立 visually-hidden status 节点写一句话（JS 微改候选，二期可做） |
| **P2-11** | CSS L3394 `.msg.ai.error` vs L3968 `.chat-error` | 两套错误样式并存：`.msg.ai.error`（整卡变红底）与 `.chat-error`（内联错误卡）。现状 JS 同时给 bubble 加 error class 又渲染 chat-error 卡（L7887–7891），视觉叠加 | CSS：删 `.msg.ai.error` 整卡样式，统一用 `.chat-error` 内联卡（01 已定：--fail-soft + --fail 35% 描边） |
| **P2-12** | CSS L4073–4189 附件组件 | `.attach-btn`/`.chat-file` 一批硬编码色（#dfe5dc/#f7f9f5/#4a554b 等）且深色皮肤漏覆盖（01 文档 §4 已点名） | 随 01 令牌化统一替换，无新增交互 |

### 2.6 课表来源 tab-source

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L4484–4543 | 双数据源卡片+状态 badge、扫码登录进度日志、二维码白底内边距（L4503，扫码识别需要，深色下保留白底是**正确**的）、cURL 导入预览 | — |
| **P2-13** | HTML L4505/L4513/L4879 | `browserLoginLog`/`importPreview`/`aiDiag` 三处 `<pre>` 用重复的内联 style | HTML+CSS 微调：抽 `.log-box` class（样式并入令牌：底 --fill、字 --sub、--font-mono、max-height 180–320px、overflow auto），三处替换 |

### 2.7 用户管理 tab-admin

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L4564–4617 + JS | 危险操作确认**全覆盖**：删除用户（L9396）、重置密码 inputDialog（L9381）、批量启停（L9292）、撤销邀请码（L9269）；审计日志卡✓ | — |
| **P2-14** | HTML L4574 `#adminSearch` | 搜索框只有 placeholder，无 label/aria-label | HTML 微调：加 `aria-label="搜索用户"` |
| **P2-15** | HTML L4598–4604 邀请码表单 | 两处内联 `style="margin:0;max-width:…px"` | CSS class 化（`.invite-field`），与表单体系对齐 |

### 2.8 设置 tab-settings

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L4649–4655 + JS L8850–8862 | sticky 分区导航 + IntersectionObserver 滚动高亮 + scroll-margin-top 补偿——**良好** | — |
| **P1-8** | HTML L4649–4655 vs L4656–4899 | 分区导航 5 个锚点（外观/账号/同步/推送/AI）对应 8 张卡：天气与复习提醒（L4730）、Bark（L4768）、群机器人（L4801）、最近推送（L4823）**四张卡无锚点**，都堆在"推送"锚点之后，滚动 spy 定位不准 | HTML 微调：① settingsNav "推送"改锚到 `settingsPush`，保持；② 给天气/Bark/群机器人/最近推送四卡加 id 并归入"推送"分组语义（无需新增 nav 按钮，文案上把"推送"理解为"推送与提醒"即可）；③ 或加第 6 个锚点"提醒"。推荐①②，成本最低 |
| **P2-16** | HTML L4251 vs L5140 | 密码长度口径不一致：注册"至少 8 位"，修改密码"至少 6 位" | 统一文案（建议都 8 位；若后端限制是 6，先改文案对齐后端，属 JS 字符串微改） |

### 2.9 登录 / 启动 / 反馈层

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| ✅ | L4213–4223 | boot 骨架屏（role=status）、弱网条（role=alert + 重试按钮）、顶部进度条——**三件套齐全** | — |
| ✅ | L5154–5167 + JS L5268–5325 | toast（aria-live + 语义色）、应用内 confirm/input 对话框（Esc、遮罩关闭、焦点返还 L5276–5277、防并发悬挂 L5282）——**规范基线** | — |
| **P2-17** | CSS L3415–3462 ≤720 auth | 登录页移动端已堆叠（L3011 display:block）✓；但 L3399 的 `grid-template-columns` 在非媒体查询层仍声明，属冗余覆盖链 | 随 P0-1 死代码清理时一并理顺，无视觉变化 |

---

## 3. 交互反馈规范（现状盘点 → 统一标准）

### 3.1 加载态

| 场景 | 现状 | 规范 |
|------|------|------|
| 首屏启动 | bootSplash 骨架屏（L4213） | 保留为基准 |
| 列表首载 | `skeletonRows(n)`（L5197）今日/周/考试/AI 卡/管理列表均用 | **唯一骨架形式**；禁止"加载中…"纯文字（例外：`<select>` 的 option 占位 L4553 允许） |
| 周课表 | 网格 sk-grid（L6209）+ 移动日视图骨架（L6213）双容器 | 保留；新增列表场景一律复用 skeletonRows |
| 按钮级等待 | 查询中切换按钮文案+禁用（L5990）、同步禁用 syncBtn（L8137） | 规范：**等待 >300ms 的操作必须禁用触发按钮**；等待文案用"…中"，完成后恢复原文案 |
| 全局请求 | topProgress（L5182–5192） | 保留，不额外加 spinner |

### 3.2 空态

- 统一 `.empty / .empty-title / .empty-sub` 组件（L167–184），紧凑场景 `.empty.compact`（L3910）；
- 文案公式：**一句现状陈述（empty-title）+ 一句下一步引导（empty-sub）**；
  ✅ 范例：L5945"没有符合条件的空教室/换个节次…试试"；
  ❌ 反例：L5496 引导了不存在的按钮（P1-1 已修）；
- 空态引导按钮只允许**真实存在**的下一步。

### 3.3 错误态

- 页面级断网：`netBanner`（role=alert）持续可见直到恢复，**不用 toast 报断网**；
- 区块级失败：空态容器内联错误（范例 L5592/L7933"加载失败 + 原因"）；
- 操作级失败：toast `data-type="error"`，4 秒，文案带原因与建议动作；
- AI 错误：`chat-error` 内联卡 + 重试按钮（P2-11 后唯一形式）；
- 表单错误：字段下 `.field-error` + 输入框 `.invalid` 光晕（L3961–3965），
  **禁止只用红字 hint 不带字段定位**（现状各 sheet 页尾红字 hint 保留，但需同时
  给对应输入框加 `.invalid`）。

### 3.4 成功反馈

- toast `data-type="success"`，≤2.5 秒，动词开头（"已保存""已同步"）；
- 就地有视觉变化的操作（勾选、添加日程）**不再叠加 toast**，防打扰。

### 3.5 危险操作确认模式（分级）

| 级别 | 适用 | 形式 |
|------|------|------|
| L1 可逆 | 删除单条日程/记忆 | confirmDialog，主按钮 danger，文案说清对象名（范例 L5666 带日程标题 ✓） |
| L2 批量/覆盖 | 恢复自动课表、批量停启用、清空对话、撤销邀请码 | confirmDialog + 说明后果（"会清除所有手动…" ✓） |
| L3 不可恢复 | 注销账号、删除用户 | L3a 注销：专用 sheet + 输入用户名+密码双因子（L5097 ✓ 业界标杆做法，保留）；L3b 删除用户：confirmDialog 文案明示"无法恢复" ✓ |

- **红色只给真正的 danger**（P0-2 修复后生效）；
- 确认框主按钮文案一律用动词（"删除""撤销""恢复"），不用"确定"。

---

## 4. 响应式与移动端规范

### 4.1 断点契约（修正版）

| 断点 | 形态 |
|------|------|
| >720px（桌面/平板） | 单列内容容器 max-width 1160px + 顶部悬浮胶囊导航（sticky） |
| ≤720px（手机） | 底部固定标签栏（含 safe-area）、周课表切换为按天竖列、chat 全屏高 |
| ≤480px（小屏） | 字号/内边距微调、隐藏品牌徽章与演示 badge（L3464–3492 ✓） |

> 修正说明：原契约注释（L4044）中的"≥901 桌面双栏"不存在（P0-1）；
> ≥901 的媒体查询在死代码清理后仅保留少量桌面增强（today-card 内边距、
> week-grid min-width、course 字号）。

### 4.2 触控与指针差异

- hover 揭示的控件必须有 `@media (hover:none)` 常显兜底：msg-actions 已有
  （L1678 ✓）、wc-free 缺失（P1-4 修）；键盘 focus 揭示用 `:focus-within`（P2-3）；
- 触控目标：底部标签栏项 ≥46px ✓（L3666）、chips/按钮 ≥31px ✓；
  `.chat-file` 的删除小圆钮 20px（L4129–4143）**偏小**，建议触控档放大到 28px
  （可只放大可点区域 padding，视觉尺寸不变）。

### 4.3 已知移动端专项

- 输入框 16px 防缩放（L4051–4055 ✓ 保留 !important 机制）；
- 状态栏横滚隐藏滚动条（L3437 ✓）；
- toast 抬升避让底部栏（L3676 ✓）；设置保存条同样避让（P1-5）；
- 抽屉 z-60 盖住底栏 z-45 ✓（L1073/L3647）。

### 4.4 新增"空教室"导航项后的压缩方案（配合 P1-2）

- 桌面：胶囊 nav 横排自然容纳 7 项，无需处理；
- ≤720px 底部栏：`#nav button{padding:4px 0; font-size:10px}` 保持，
  4 字标签（课表来源/用户管理）允许缩至 9.5px 或 `letter-spacing:-0.02em`，
  禁止换行（white-space:nowrap 已有）；
- 验证点：360px 宽（iPhone SE/小屏安卓）7 项不溢出。

---

## 5. 表单与输入规范

1. **label 包裹式**（`.field>span+input`，L4245 等）是全站范式，a11y 天然成立，
   新增表单项必须沿用；无法包裹时（adminSearch）补 `aria-label`（P2-14）；
2. **placeholder 不承担必填说明**：格式要求写进 label（"密码（至少 8 位）"✓）
   或字段下 hint；placeholder 只放示例（"例如 高等数学"✓）；
3. **提交路径**：每个表单一个 primary submit 按钮，Enter 提交经
   `bindImeSafeEnter` 防中文输入法误触发（L5325/8569 ✓ 保留）；
4. **校验时机**：提交时统一校验；失败 = 字段 `.invalid` + 字段下
   `.field-error` + 焦点移到首个错误字段；
5. **危险/不可逆表单**（注销、改密码）：独立 sheet，不与普通设置混排 ✓；
6. **数字输入**（可用次数/提前分钟）：保留 min/max 原生约束，提交时再兜底；
7. 移动端输入框 ≥16px（已有 !important 兜底），新增输入组件不再单独声明字号。

---

## 6. 无障碍清单

**已达标（保留）**：
- nav 完整 tab 语义：role=tablist/tab、aria-selected 同步（L8421）、
  方向键切换（L8586–8599）——符合 WAI-ARIA tabs 模式；
- 所有 sheet/对话框：role=dialog + aria-modal + aria-label ✓；
- 焦点管理：确认框焦点返还（L5276）、sheet 焦点陷阱 + Esc（L9474–9496）✓；
- 教学楼折叠用原生 `<details>/<summary>` ✓；
- bootSplash role=status、netBanner role=alert、toast aria-live=polite ✓；
- prefers-reduced-motion 全局降级（L4062–4071）✓；
- 01 文档令牌对比度全表达 AA（§2.5）。

**待修（按优先级）**：
| # | 项 | 修法 |
|---|----|------|
| A1 | AI 消息容器 aria-live 洪水（P1-7） | 去容器 aria-live 改 role="log" |
| A2 | "回到本周" span 不可键盘（P2-4） | 改 button 或加 tabindex+键控 |
| A3 | 消息操作按钮键盘不可见（P2-3） | `.msg:focus-within` 显现 |
| A4 | adminSearch 无标签（P2-14） | aria-label |
| A5 | chat-input-hint 对比度 2.9:1（P2-2） | 升 11px + --sub |
| A6 | week-menu 按钮 aria-expanded 未同步（P2-9） | JS 切换时同步（候选） |
| A7 | svg 图标 aria-hidden 已标 ✓（L4279 等），新增"空教室"图标同样处理 | 沿用 |

---

## 7. 明确"不改什么"与 JS 微改白名单

### 7.1 不改

- JS 行为逻辑、API 路径、事件绑定结构、状态机；
- DOM id/class 命名、tab 路由结构（switchTab 机制）；
- 双视图渲染架构（weekGrid/weekDays）、流式聊天实现、骨架机制；
- 断点数值（≤480/≤720/>720）、动效时长体系、reduced-motion 机制；
- 二维码白底（L4503）、`<details>` 折叠、原生 date/time/select 控件。

### 7.2 JS 微改白名单（仅此 5 项，均需逐条 review）

| # | 位置 | 改动 | 理由 |
|---|------|------|------|
| J1 | L5291 | `opts.danger !== false` → `opts.danger === true`，危险调用点补 `danger:true` | P0-2，确认框红色语义修复，无第二途径 |
| J2 | L5496 | 空态文案字符串改一句 | P1-1，文案在 JS innerHTML 里 |
| J3 | L6106–6112/L8619 | weekLabel 渲染 span→button（或加 tabindex+键盘事件） | P2-4/A2，键盘可达性 |
| J4 | HTML L4439 | chat-msgs `aria-live="polite"` → `role="log"`（这其实是 HTML 属性改动，零 JS 逻辑） | P1-7/A1，读屏洪水 |
| J5（候选，可二期） | loadWeek 渲染后 | ≤720px 且为本周时滚动到 day-card.today | P2-8，移动端定位；评估有跳动风险则放弃 |

### 7.3 HTML 微调白名单（纯展示层，不动逻辑）

- nav 增"空教室"按钮（P1-2）——现有 click/方向键委托自动生效；
- room-query 删 `&nbsp;`（P2-10）、adminSearch 加 aria-label（P2-14）、
  三处 `<pre>` 抽 `.log-box`（P2-13）、邀请码表单内联样式 class 化（P2-15）、
  设置四张推送卡加 id 锚点（P1-8）。

其余一切改动以 **CSS-only** 为限。

---

## 8. 高风险交互与降级方案

| 风险 | 场景 | 降级方案 |
|------|------|----------|
| 删除死皮肤误伤活规则 | P0-1 清理 L2109–2560 时，其中 `@keyframes section-enter` 等被后层重复定义 | 删前先 grep 每个选择器确认只出现在死块；`section-enter` 在 L3202 有活版本 |
| 底部栏 7 项挤压 | P1-2 后 320–360px 小屏 | 按 §4.4 缩字号；极限情况允许"用户管理"保持 hidden（现状即如此，管理员桌面操作） |
| sticky 保存条遮挡 | P1-5 在短视口（横屏手机） | `bottom` 条加 `max-height` 与半透明底；遮挡可接受（只在设置页） |
| AI 消息去卡片化 | P1-3 改动面大（表格/代码块/追问 chips 都在卡片语境设计） | 若评审认为风险高，回退为"保留卡片但令牌化"，并修订 01 §3.10 措辞；二选一并存档决策 |
| wc-free 常显后视觉噪 | P1-4 触屏常显后空格子信息变多 | 保持 10px 虚线框小按钮形态即可，不再加色 |

---

## 9. 验收清单（Phase 3 完成后逐项核对）

- [ ] 死皮肤删除，grep `.sidebar`/`.main-content` 零残留；桌面 1000px 宽周课表不挤压
- [ ] 确认框只有危险操作是红按钮；逐个过 9 个调用点
- [ ] 底部标签栏出现"空教室"，360px 宽不溢出，aria-selected 正常
- [ ] 触屏（或 DevTools 模拟 hover:none）周课表空格按钮不透明常显
- [ ] 设置页任意位置滚屏可见保存条
- [ ] 键盘：Tab 顺序 → 消息操作可聚焦可见；weekLabel 可 Enter 触发
- [ ] 读屏（VoiceOver 快速抽查）：AI 流式回复不刷屏
- [ ] 今日空态文案与页面按钮一致
- [ ] 全部新样式零硬编码 hex（与 01 令牌对齐）
