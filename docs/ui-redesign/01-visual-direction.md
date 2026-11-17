# Agenda 知序 · 视觉方向规范 v1.0

> Phase 1 交付物。本文件是视觉层的唯一事实来源（Single Source of Truth），
> Phase 3 落地时只改 CSS 值与样式规则，不动 JS 逻辑与 DOM 结构。

---

## 0. 现状诊断（为什么现在"看着像 AI 默认脸"）

读完全部 CSS 后确认：`static/index.html` 里存在 **四层皮肤互相覆盖**：

1. **L24 `:root`**：AI 蓝 `#0071e3` + 冷灰 `#f5f5f7`（用户看到的"AI 化"源头，仍被部分组件继承）；
2. **L1164 "纸质课表"皮肤**：松绿 `#315c49` + 米纸 `#f3f3ee`（覆盖了上面，但漏改大量组件）；
3. **L3696 `darkThemeCss`**：独立深色块，里面 **散落 60+ 处硬编码 hex**（`#2c3a2e`、`#a3aca0`…），与令牌脱节；
4. **L3796+ 若干补丁块**：又引入一批不在任何色板里的色值（`.attach-btn` 的 `#dfe5dc`、`#f7f9f5`…）。

结果：同一个"强调色"在不同组件里实际渲染成 5 种以上的绿/蓝，深浅不一——这就是"显示效果不好"在视觉层的根因。**本规范的第一原则就是收敛到一套令牌。**

---

## 1. 设计概念

**一句话方向：把校园课表做成一本「有印章的课表册」——松烟墨绿为骨，宣纸米为底，朱砂红为印。**

气质关键词：

1. **书卷气**：衬线数字（Georgia）承担日期/周次等"时刻信息"，正文用现代无衬线保证中文可读性；
2. **克制**：全界面只有一个强调色相（松绿），红色只用于语义（重要/错误），不做装饰渐变；
3. **纸感**：暖调米白底 + 极浅投影 + 细分割线，模拟纸而非玻璃拟态；
4. **校园**：色彩取"松、纸、朱印"而非科技蓝，与"知序"品牌名和青岛理工的山海气质自洽；
5. **昼夜同源**：深色模式是同一套色相在暗处的反演（灯下看墨），不是另一套皮肤。

### 主强调色为什么是「松绿」而不是别的

- **避开 AI 蓝**：`#0071e3` 系蓝色是当前所有 AI 产品/系统默认主题的共同面孔，必须整体舍弃（含深色版里的蓝色调徽章 `pill-portal #388bfd` 等）。
- **绿色在课表场景有天然语义**：空闲/空闲教室/正常状态都是绿，强调色与产品高频语义同族，认知负担低。
- **色相选择**：取偏"墨"的深松绿 `#2E5C46`（HSL ≈ 150°/33%/27%），比翠绿沉稳、比墨绿有生气；配白字对比度 **7.3:1**（WCAG AA 大字号/组件远超，正文级也达标）。
- **辨识度**：朱砂红 `#B8442C` 作为第二记忆色只出现在"印章式"节点（品牌印、重要提醒、考试倒计时），用户一眼能认出这是"知序"而不是又一个蓝白工具。

---

## 2. 设计令牌（Design Tokens）

### 2.1 浅色色板（默认）

```css
:root {
  /* 表面 */
  --bg:            #F6F4ED;  /* 页面底·宣纸米 */
  --card:          #FDFCF7;  /* 卡片底·提亮的纸 */
  --fill:          #EFEEE6;  /* 凹陷区底：chips 条、auth-tabs 槽、stat-box */
  --fill-strong:   #E7E6DC;  /* hover 按压底 */

  /* 文字 */
  --text:          #22261F;  /* 墨色·主文本（对 --bg 对比 13.8:1） */
  --sub:           #6B7267;  /* 灰苔·次文本（对 --bg 对比 4.9:1，达 AA） */
  --faint:         #9BA294;  /* 占位符/禁用文本（仅用于非关键信息） */

  /* 分隔 */
  --line:          #E3E2D8;  /* 细线 */
  --line-strong:   #D3D4C8;  /* 输入框/按钮描边 */

  /* 强调·松绿 */
  --accent:        #2E5C46;
  --accent-hover:  #254A39;
  --accent-soft:   #E6EDE5;  /* 选中底/标签底（上叠 --accent 文字对比 5.6:1） */
  --accent-faint:  #8FA99A;  /* 深色模式下松绿的"提亮版"不在此用；浅色下仅用于 icon/装饰 */

  /* 朱砂·重要（印章红）：考试临近、今日高亮、品牌印 */
  --cinnabar:       #B8442C;
  --cinnabar-soft:  #F6E7E0;

  /* 语义色 */
  --ok:            #3A7D4C;
  --ok-soft:       #E8F0E7;
  --warn:          #98601C;
  --warn-soft:     #F5ECDB;
  --fail:          #9F2E24;
  --fail-soft:     #F6E4E0;

  /* 形状与深度 */
  --radius-s: 5px;
  --radius:   8px;    /* 按钮/输入/小卡 */
  --radius-l: 12px;   /* 卡片/周表 */
  --radius-xl: 16px;  /* 底部抽屉/对话框 */
  --shadow-1: 0 1px 2px rgba(34, 38, 31, 0.04);
  --shadow-2: 0 8px 24px rgba(34, 38, 31, 0.08);
  --shadow-3: 0 20px 56px rgba(24, 28, 23, 0.16);

  /* 间距刻度（4 的倍数） */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px;
  --sp-5: 20px; --sp-6: 24px; --sp-8: 32px; --sp-10: 40px;
}
```

### 2.2 深色色板（灯下看墨）

设计原则：**不是"反色"，而是同一组色相在暗处的低亮度版本**。底不用纯黑，用带绿相的墨底，与松绿强调色同源。

```css
:root {
  --bg:            #1B1E19;  /* 墨底（带绿相，非中性黑） */
  --card:          #23271F;
  --fill:          #2A2E25;
  --fill-strong:   #31362B;

  --text:          #E7E9E0;
  --sub:           #9AA192;  /* 对 --card 对比 6.0:1 */
  --faint:         #6E7568;

  --line:          #363B31;
  --line-strong:   #474D40;

  --accent:        #8FBC9E;  /* 提亮松绿，对 --card 对比 7.1:1；文字/描边用 */
  --accent-hover:  #A9CEB5;
  --accent-soft:   #2C372E;  /* 选中底 */
  /* 深色 primary 按钮：底 --accent，文字 #17251B（对比 9.2:1） */
  --accent-ink:    #17251B;

  --cinnabar:      #DF7A5E;  /* 朱砂的灯下版本 */
  --cinnabar-soft: #3B2A24;

  --ok:            #8FC49B;  --ok-soft:   #2B362B;
  --warn:          #D3A468;  --warn-soft: #38311F;
  --fail:          #E08575;  --fail-soft: #3D2A26;

  --shadow-1: none;
  --shadow-2: 0 8px 26px rgba(0, 0, 0, 0.28);
  --shadow-3: 0 20px 60px rgba(0, 0, 0, 0.42);
  /* radius / spacing 与浅色完全一致，不重复声明 */
}
```

深色下的硬性规则：

- **阴影整体降权**：卡片用 `--shadow-1: none`，层级靠"更亮的面"（card > bg > fill）而非投影表达；
- **禁止纯黑 `#000` 与纯白 `#FFF` 大面积相邻**；
- 代码块底色沿用现有 `#252a25`（已带绿相），保留；
- PWA `theme-color` meta：浅色 `#F6F4ED` / 深色 `#1B1E19`（JS 里 L4207/L8382 两处字符串常量同步替换，是唯一允许的 JS 改动）。

### 2.3 课程色调色板（week-grid 的 tone-0..4）

现有 5 色分类思路保留，但全部收敛为"松绿主调 + 四个大地色系邻近色"，深浅两版：

| tone | 浅色 border / 底 | 深色 border / 底 |
|------|------------------|------------------|
| 0 松绿 | `#5F7D68` / `#E6EEE4` | `#7BA98A` / `#2C3A2E` |
| 1 赭石 | `#9A7E50` / `#F1EBDF` | `#C09A68` / `#3A342A` |
| 2 黛灰 | `#69838B` / `#E6EDEF` | `#8AA3BD` / `#2C343D` |
| 3 陶土 | `#9B715D` / `#F1E7E1` | `#C08A72` / `#3A2E28` |
| 4 苔绿 | `#7F8869` / `#ECEEE6` | `#93A478` / `#2F3628` |

### 2.4 字体与字号阶梯

```css
--font-text: -apple-system, BlinkMacSystemFont, "PingFang SC",
             "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
--font-num:  Georgia, "Times New Roman", "Songti SC", serif;  /* 仅日期/周次/倒计时等"时刻"数字 */
--font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
```

| 层级 | 字号/行高/字重 | 用途 |
|------|----------------|------|
| Display | 34/1.2 · 600（数字用 serif 400，字距 -0.04em） | 今日日期、周次大数字（`.day-head .date`、`.week-mark strong`） |
| H1 | 22/1.25 · 600 | 卡片标题（`.card h2`，带 3×14px accent 竖条，保留现有 `::before` 手法） |
| H2 | 17/1.35 · 600 | 课程名、列表主行（`.course-name`、`.exam-name`） |
| Body | 14/1.65 · 400 | 正文（中文行高 ≥1.6，不低于此） |
| Sub | 13/1.6 · 400 · `--sub` | 元信息（`.course-meta`） |
| Caption | 12/1.5 · 400 · `--sub` | 辅助说明、`.hint` |
| Kicker | 10/1.4 · 650 · `--accent` · 字距 0.14em | 区段小标（`.section-kicker`、"知序"消息署名） |

规则：
- **移动端输入框字号 ≥16px**（现有 `!important` 规则保留，防 iOS 自动缩放）；
- 时刻数字统一 `font-variant-numeric: tabular-nums`（现有，保留）；
- **禁止**新增字号档；字距负值只允许出现在 Display/H1。

### 2.5 对比度核对（WCAG AA）

| 组合 | 对比度 | 结论 |
|------|--------|------|
| `--text` on `--bg`（浅） | 13.8:1 | AAA |
| `--sub` on `--bg`（浅） | 4.9:1 | AA ✓ |
| `#FFF` on `--accent` `#2E5C46` | 7.3:1 | AAA（primary 按钮） |
| `--accent` on `--accent-soft`（浅） | 5.6:1 | AA ✓（选中态） |
| `--cinnabar` on `--bg`（浅） | 5.4:1 | AA ✓ |
| `--sub` on `--card`（深） | 6.0:1 | AA ✓ |
| `--accent` on `--card`（深） | 7.1:1 | AAA |
| `--accent-ink` `#17251B` on `--accent`（深 primary 按钮） | 9.2:1 | AAA |

`--faint` 仅允许用于占位符与装饰，不承担信息。

---

## 3. 组件视觉规范

### 3.1 按钮

| 变体 | 浅色规格 |
|------|----------|
| **primary** | 底 `--accent` / 字 `#FFF` / 无边框；hover 底 `--accent-hover`；active `translateY(1px)`（保留现有按压反馈）；圆角 `--radius`(8px)；高度 36px，padding `7px 15px` |
| **默认** | 底 `--card` / 边 `--line-strong` / 字 `--text`；hover 底 `--fill-strong`、边 `#B9BEB0` |
| **small** | 高 32px，padding `5px 12px`，字号 12px |
| **danger** | 描边 `--fail` 40% 透明度色（浅 `#D8B0AA` / 深 `#6B443C`），字 `--fail`；`.danger.solid` 底 `--fail` 字 `#FFF` |
| **禁用** | `opacity: .45` + `cursor: default`，禁止响应 active 缩放 |

统一规则：**放弃胶囊形（999px）圆角按钮**——胶囊是"AI 聊天产品"的符号，课表册用 8px 方圆角。现有 `border-radius: 999px` 的按钮定义全部收敛到 `--radius`。
focus-visible：`outline: 2px solid color-mix(in srgb, var(--accent) 32%, transparent); outline-offset: 2px`（现有实现手法保留）。

### 3.2 导航 Tab

- **桌面（≥901px 侧边栏）**：选中项 = 底 `--accent` + 字 `#FFF` + 边 `--accent`（现有实底药丸，保留）；hover 底 `--fill-strong`；序号用 `--font-num` 10px（保留，这是现有设计里最有辨识度的细节之一）。
- **移动端/窄屏顶部 tab**：选中 = 字 `--accent` + 2px `--accent` 下划线；未选中 `--sub`；**去掉背景模糊阴影**（现有 `rgba(39,43,36,0.92)+shadow` 换成 `--bg` 92% 透明 + `--line` 底边线）。
- 底部安全区与 sticky 行为不动。

### 3.3 卡片

- 底 `--card`、边 `1px solid --line`、圆角 `--radius-l`(12px)、投影 `--shadow-1`（浅色）/ `none`（深色）；
- 内边距：桌面 24px（今日主卡 28–32px）、≤720px 19px；
- 卡片间距 16px；标题用 H1 档 + accent 竖条 `::before`（保留）；
- **避免**：圆角 >16px、多层投影、玻璃拟态。

### 3.4 徽章 / 标签 / Pill

- 胶囊（999px）只允许出现在 **pill/徽章** 这类"印章式"小元素，按钮不用；
- 语义配色严格对应：`ok / warn / fail / cinnabar` 四组 `*-soft` 底 + 文字色；
- `.event-tag`（个人日程标）：边 `--accent`、字 `--accent`、圆角 4px（保留现有形式）；
- 来源标识 `pill-portal / pill-zhengfang / pill-none`：把现有的蓝 `#388bfd`/金 `#d29922` **改为** 松绿 `--accent-soft`+`--accent` 与赭石 `--warn-soft`+`--warn`，消除界面里最后的蓝色。

### 3.5 表单输入

- 高 39px、padding `8px 11px`、圆角 7px、底 `--card`（深色用 `--fill` 以区隔卡片底）、边 `--line-strong`；
- focus：边 `--accent` + `box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent)`；
- 错误态 `.invalid`：边 `--fail` + 2px fail 18% 光晕（现有实现保留，色值随令牌走）；
- placeholder：`--faint`；label 用 Caption 档。

### 3.6 表格（周课表 / md-table / 邀请码列表）

- 网格线 `--line`；表头底 `--fill`；斑马纹 `color-mix(in srgb, var(--sub) 5%, transparent)`（替换现有硬编码 `rgba(127,127,127,.04)`）；
- "今日"列底 `color-mix(in srgb, var(--accent) 8%, var(--card))`（替换硬编码 `#f7fbff/#2c3529`）；
- 迷你课块 `--radius-s` 圆角、左 2px 色条，5 个 tone 按 §2.3。

### 3.7 模态框 / 抽屉 / 确认框

- 遮罩：`rgba(24, 28, 23, .38)`（浅色）/ `rgba(0,0,0,.55)`（深色），可带 3px blur（现有）；
- 面板：移动端底部抽屉 `16px 16px 0 0` 圆角，桌面居中 `--radius-xl`，投影 `--shadow-3`；
- 确认框 `.confirm-box` max-width 340px、圆角 16px（保留），主按钮 min-width 78px。

### 3.8 空状态

- `--empty-pad` 默认 58px（紧凑场景 `.compact` 20px，保留现有修饰类机制）；
- 标题 H2 档 `--text` + 副文 Caption 档 `--sub`；允许一个 `--sub` 色的线稿 icon/字符画，**禁止**彩色插画；
- 引导按钮用 primary。

### 3.9 Toast / 顶部条

- 中性 toast：底 `--text` 反色（浅 `#22261F` / 深 `#3C4437`）、字 `#FFF`、圆角 8px、`--shadow-2`；
- 语义 toast（`data-type`）继续用固定实色（现有注释已说明原因，正确），但色值对齐新板：success `#2F7D4F` → **`#356F45`**、error `#C0392B` → **`#9F2E24`**、warn `#A35F00` → **`#8F5A12`**；
- `.net-banner` 弱网条：底 `--fail` 固定实色 `#9F2E24`（替换 `#c0392b`）。

### 3.10 AI 对话

保留现有"去气泡化"设计（AI 消息无边框、靠"知序"署名 + 底部分隔线分层），这是现有设计里最好的决策，**只换色**：
- 用户气泡：底 `--accent`、字 `#FFF`（深：字 `--accent-ink`）；
- AI 署名/引用条/列表点：`--accent`；代码块 `#252a25` 保留；错误卡用 `--fail-soft` + `--fail` 35% 描边（现有 color-mix 手法保留）。

---

## 4. 深色模式组织策略（架构决策）

**现状问题**：`darkThemeCss` 是一个 100 行的独立 style 块，用 60+ 处硬编码 hex 逐组件覆盖，新增组件时极易漏改（现状里已经漏了 `.attach-btn` 等一批）。

**新方案（落地时执行）**：

1. **单一令牌源**：所有组件颜色只引用 `var(--*)`，CSS 里不允许出现新的硬编码 hex（遗留的 `#f7fbff`、`#2c3529`、`#dfe5dc`、`#388bfd` 等在 Phase 3 逐一替换为令牌或 color-mix 派生）。
2. **深色 = 只覆写 `:root` 变量**：`darkThemeCss` 块瘦身到约 30 行，只包含 §2.2 的变量表 + `::selection` 等个别无法变量化的规则。
3. **JS 契约不动**：继续由现有脚本 `el.disabled = !dark` 启停该块、`data-theme` 属性照旧写入——选择器可同步增强为 `html[data-theme="dark"] :root { … }` 作为双保险，但不依赖它。
4. `color-scheme: light dark`（L3694）保留，保证滚动条/表单控件原生件跟主题。

---

## 5. 明确"不改什么"

- **不改**任何 JS 行为逻辑、API、事件绑定；唯一允许的 JS 改动是两处 `theme-color` meta 的字符串常量（`#f5f5f7`→`#F6F4ED`、`#1e211c`→`#1B1E19`）。
- **不改** DOM 结构、id/class 命名约定；新增样式如需钩子，优先用现有 class + 属性选择器（如 `#toast[data-type]` 的现有手法）。
- **不改**断点契约（≤480 / ≤720 / ≥901，L4044 注释明确要求保留）。
- **不改**现有动效时长体系（0.12–0.18s）与 `prefers-reduced-motion` 全局降级。
- **不改**布局栅格（960/1080/1360 容器、208px 侧栏）。

---

## 6. 要避免的清单（防返工）

1. ❌ 任何形式的蓝（含 badge、pill、focus ring、链接色）；
2. ❌ 胶囊形按钮 / 大圆角卡片（>16px）/ 玻璃拟态 / 渐变；
3. ❌ 新硬编码 hex 出现在组件规则里（必须走令牌）；
4. ❌ 中性灰底（`#f5f5f7` 系）——底色必须带暖纸相；
5. ❌ 中文正文用衬线 / 字距负值用在正文 / 行高 <1.5；
6. ❌ 深色模式里复用浅色 accent `#2E5C46` 做文字（对比不足），必须用 `--accent: #8FBC9E`；
7. ❌ 红色做装饰——朱砂红只属于"重要/印章"语义。

---

## 7. 快速预览（令牌 → 观感）

```
浅色：米纸 #F6F4ED 底 / 墨字 #22261F / 松绿 #2E5C46 主按钮 / 朱砂 #B8442C 印章
深色：墨底 #1B1E19 / 苔字 #E7E9E0 / 亮松绿 #8FBC9E 强调 / 灯下朱砂 #DF7A5E
声音：Georgia 衬线数字 + PingFang SC 正文，10–34px 七档
```

---

## 8. v1.1 修订（2026-11-17）：素笺墨色

用户反馈松绿+米纸仍偏"设计感"，要求更素雅。全站已收敛到单一令牌源，本次仅改令牌值，
组件规则零改动（这正是令牌化的收益）。

**方向：素笺墨色**——暖纸白底、浓墨主按钮，彩色只留最低限度的语义。

### 浅色
- bg `#F7F6F2`（暖纸白，去黄绿相）/ card `#FFFFFF` / text `#262622` / sub `#77756D`
- **accent `#3A3833`（浓墨）**：主按钮、选中态、focus 全部墨色化，不再有彩色装饰
- cinnabar `#A9563E`（仅考试临近等"印章"节点）；语义色降饱和：ok `#5B7D64` / warn `#96703F` / fail `#A34F42`

### 深色（夜墨）
- bg `#21211E`（暖炭黑，去绿相）/ card `#292926` / text `#E6E5DF`
- accent `#D6D3C8`（纸白反相，primary 按钮配字 `#23231F`）

### 课程色板（低饱和灰调）
浅：`#80887E/#948A74/#7F8790/#928077/#86887C`，底均近白；深：对应提亮灰调。

### 同步更新
theme-color（`#F7F6F2`/`#21211E`）、manifest、favicon（墨色）、toast 语义实色、
弱网条、代码块底色（`#2A2A27` 中性夜墨）。
