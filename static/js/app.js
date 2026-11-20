      const $ = (s) => document.querySelector(s);
      const $$ = (s) => document.querySelectorAll(s);
      const WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
      let status = null,
        weekData = null,
        viewWeek = null,
        weatherData = null,
        weatherLoading = false,
        weekRender = null,
        chatHistory = [];

      let pendingRequests = 0, progressTimer = 0;
      function requestBegin() {
        pendingRequests += 1;
        clearTimeout(progressTimer);
        progressTimer = setTimeout(() => {
          if (pendingRequests > 0) $("#topProgress").classList.add("show");
        }, 250);
      }
      function requestEnd() {
        pendingRequests = Math.max(0, pendingRequests - 1);
        if (!pendingRequests) {
          clearTimeout(progressTimer);
          $("#topProgress").classList.remove("show");
        }
      }
      function skeletonRows(n = 3) {
        return Array.from({ length: n }, () =>
          `<div class="sk-row"><div class="sk sk-a"></div><div class="sk-lines"><div class="sk sk-b"></div><div class="sk sk-c"></div></div></div>`
        ).join("");
      }
      // 仅在容器还空着时铺骨架，避免已有缓存数据时来回闪烁
      function showSkeleton(box, n = 2) {
        if (box && !box.children.length) box.innerHTML = skeletonRows(n);
      }
      async function api(path, opts = {}) {
        requestBegin();
        try {
          const r = await fetch(path, {
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            ...opts,
          });
          const data = await r.json().catch(() => ({}));
          if (r.status === 401 && !path.startsWith("/api/auth/")) {
            showAuthScreen();
            throw new Error("登录已失效，请重新登录");
          }
          if (!r.ok) throw new Error(data.detail || `请求失败 (${r.status})`);
          setNetBanner(false);
          return data;
        } catch (e) {
          // TypeError 表示网络层失败（断网 / DNS / 服务不可达）；业务错误不弹横幅
          if (e instanceof TypeError) setNetBanner(true);
          throw e;
        } finally {
          requestEnd();
        }
      }
      function setNetBanner(show) {
        const el = $("#netBanner");
        if (el) el.classList.toggle("hidden", !show);
      }
      window.addEventListener("offline", () => setNetBanner(true));
      window.addEventListener("online", () => setNetBanner(false));
      $("#netRetryBtn").addEventListener("click", async () => {
        try {
          await api("/api/status");   // 成功时 api() 会自动收起横幅
          toast("网络已恢复", 2500, "success");
        } catch (e) {
          toast("仍然连不上，请检查网络或稍后再试", 3500, "error");
        }
      });
      function hideBootSplash() {
        const el = $("#bootSplash");
        if (el) el.classList.add("hidden");
      }
      // 极端情况下（脚本异常导致 boot 没跑完）也不把用户卡在启动页
      setTimeout(hideBootSplash, 10000);
      function toast(msg, ms = 2600, type = "") {
        const t = $("#toast");
        t.textContent = msg;
        // 显式 type 优先；未指定时按文案兜底推断，让历史调用点也能获得语义色
        let kind = type;
        if (!kind) {
          const s = String(msg ?? "");
          if (/失败|错误|无法|没能|不可用|失效|异常|超时|请先|请填写|请选择|还没|稍后再试|没有空闲|已改查|不合法|格式/.test(s))
            kind = "warn";
          else if (/^(已|日程已|设置已|密码已|对话已|用户已|邀请码已|模型|正方已)/.test(s))
            kind = "success";
        }
        t.dataset.type = kind || "info";
        t.classList.add("show");
        clearTimeout(t._timer);
        t._timer = setTimeout(() => t.classList.remove("show"), ms);
      }

      /* 应用内对话框：替代原生 confirm / prompt，跟随主题、支持 Esc 与点遮罩关闭 */
      let dialogSettle = null;
      function settleDialog(value) {
        if (!dialogSettle) return;
        const fn = dialogSettle;
        dialogSettle = null;
        const layer = $("#confirmLayer");
        layer.classList.add("hidden");
        const prev = layer._prevFocus;
        if (prev && typeof prev.focus === "function") prev.focus();
        fn(value);
      }
      function showDialog(opts) {
        return new Promise((resolve) => {
          if (dialogSettle) settleDialog(null); // 防止并发时旧 Promise 悬挂
          const layer = $("#confirmLayer");
          const inp = $("#confirmInput");
          layer._prevFocus = document.activeElement;
          layer._opts = opts;
          $("#confirmTitle").textContent = opts.title || "请确认";
          $("#confirmText").textContent = opts.message || "";
          $("#confirmOk").textContent = opts.ok || "确定";
          $("#confirmCancel").textContent = opts.cancel || "取消";
          $("#confirmOk").classList.toggle("danger", opts.danger === true);
          inp.classList.toggle("hidden", !opts.input);
          inp.type = opts.password ? "password" : "text";
          inp.value = "";
          inp.placeholder = opts.placeholder || "";
          $("#confirmError").classList.add("hidden");
          layer.classList.remove("hidden");
          dialogSettle = resolve;
          (opts.input ? inp : $("#confirmCancel")).focus();
        });
      }
      function confirmDialog(message, opts = {}) {
        return showDialog({ ...opts, message }).then((v) => v === true);
      }
      function inputDialog(message, opts = {}) {
        return showDialog({ ...opts, message, input: true });
      }
      $("#confirmCancel").addEventListener("click", () => settleDialog(null));
      $("#confirmOk").addEventListener("click", () => {
        const opts = $("#confirmLayer")._opts || {};
        if (!opts.input) { settleDialog(true); return; }
        const val = $("#confirmInput").value.trim();
        const err = opts.validate ? opts.validate(val) : val ? "" : "不能为空";
        if (err) {
          const box = $("#confirmError");
          box.textContent = err;
          box.classList.remove("hidden");
          return;
        }
        settleDialog(val);
      });
      $("#confirmLayer").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) settleDialog(null);
      });
      bindImeSafeEnter($("#confirmInput"), () => $("#confirmOk").click());
      function esc(s) {
        const d = document.createElement("div");
        d.textContent = s ?? "";
        return d.innerHTML;
      }
      const escAttr = (s) => esc(s).replace(/"/g, "&quot;");

      /* ---------- 状态与今日 ---------- */
      async function loadStatus() {
        status = await api("/api/status");
        $("#headerSub").textContent =
          `${status.school} · ${status.week > 0 ? "第 " + status.week + " 周" : "假期"} · ${status.weekday}`;
        const badge = $("#demoBadge");
        badge.classList.toggle("hidden", !status.is_demo);
        badge.textContent = "演示数据";
        const providerName =
          {
            mock: "演示数据",
            zhengfang: "正方教务",
            httpjson: "自定义接口",
            portal: "校门户",
          }[status.provider] || status.provider;
        $("#statusbar").innerHTML = [
          `数据源：${esc(providerName)}`,
          `课程数：${status.course_count}`,
          status.upcoming_exam_count
            ? `考试：${status.upcoming_exam_count} 场待考`
            : "",
          status.upcoming_event_count
            ? `日程：${status.upcoming_event_count} 项待办`
            : "",
          `上次同步：${esc(status.last_sync || "尚未同步")}`,
          `下次同步：${esc(status.next_sync || "-")}（每日 ${esc(status.sync_time)}）`,
          `推送：${status.push_channels.length ? status.push_channels.join(" / ") : "未启用"}`,
          `AI：${status.ai_enabled ? "已启用" : "未配置"}`,
        ]
          .filter(Boolean)
          .map((s) => `<span>${s}</span>`)
          .join("");
        if (typeof applyAiNavVisibility === "function") applyAiNavVisibility();
      }

      function courseRow(c) {
        const weeks = Array.isArray(c.weeks) && c.weeks.length
          ? (c.weeks.length > 1 ? `第 ${c.weeks[0]}–${c.weeks[c.weeks.length - 1]} 周（共 ${c.weeks.length} 周）` : `第 ${c.weeks[0]} 周`)
          : "整学期";
        const detail = [
          `<b>节次</b> 第 ${c.start}–${c.end} 节`,
          `<b>周次</b> ${esc(weeks)}`,
          c.teacher ? `<b>教师</b> ${esc(c.teacher)}` : "",
          c.room ? `<b>教室</b> ${esc(c.room)}` : "",
          c.campus ? `<b>校区</b> ${esc(c.campus)}` : "",
        ].filter(Boolean).join("　·　");
        return `<div class="course-row course-clickable" data-name="${esc(c.name)}" data-start="${esc(c.start_time || "")}" data-end="${esc(c.end_time || "")}">
    <div class="course-time"><div class="range">${esc(c.start_time)}–${esc(c.end_time)}</div><div class="sections">第 ${c.start}–${c.end} 节</div></div>
    <div class="course-main">
      <div class="course-name">${esc(c.name)}<span class="row-chevron" aria-hidden="true">›</span></div>
      <div class="course-meta">${esc(c.room || "教室待定")}${c.teacher ? " · " + esc(c.teacher) : ""}${c.campus ? " · " + esc(c.campus) : ""}</div>
      ${c.note ? `<div class="course-note">${esc(c.note)}</div>` : ""}
      <div class="course-extra hidden"><div class="ce-line">${detail}</div><button type="button" class="small ce-week" data-act="go-week">在周课表中查看 →</button></div>
    </div>
  </div>`;
      }

      // 把排序后的今日安排拼成时间轴 HTML：相邻安排间隔 ≥15 分钟时插入「课间休息」节点
      function composeTimeline(items, gaps = true) {
        const toMin = (t) => {
          if (!t || !/^\d{1,2}:\d{2}$/.test(t)) return null;
          const [h, m] = t.split(":").map(Number);
          return h * 60 + m;
        };
        const parts = [];
        let prevEnd = null;
        items.forEach((it) => {
          const startMin = toMin(it.start);
          const endMin = toMin(it.end);
          const gap = prevEnd !== null && startMin !== null ? startMin - prevEnd : 0;
          if (gaps && gap >= 15) {
            const label = gap >= 60
              ? (prevEnd < 12 * 60 && startMin < 15 * 60 ? "午间空闲" : "空闲")
              : "课间休息";
            const timeText = gap >= 60
              ? (gap % 60 === 0 ? `${gap / 60} 小时` : `${Math.floor(gap / 60)} 小时 ${gap % 60} 分`)
              : `${gap} 分钟`;
            parts.push(`<div class="course-gap"><span>${label} ${timeText}</span></div>`);
          }
          parts.push(it.html);
          prevEnd = endMin;
        });
        return parts.join("");
      }

      // 给时间轴首末行打标，修剪竖线避免上下悬空
      function markTimelineEdges(root) {
        const rows = root.querySelectorAll(".course-row");
        rows.forEach((r) => r.classList.remove("tl-first", "tl-last"));
        if (rows.length) {
          rows[0].classList.add("tl-first");
          rows[rows.length - 1].classList.add("tl-last");
        }
      }

      // 今日列表按当前时间标记「进行中 / 已结束」
      function applyTodayStates() {
        const root = $("#todayList");
        const now = new Date();
        const hm = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
        root.querySelectorAll(".course-row[data-start]").forEach((row) => {
          row.classList.remove("is-ongoing", "is-done");
          const s = row.dataset.start;
          const e = row.dataset.end;
          if (!s || !e) return;
          if (hm >= e) row.classList.add("is-done");
          else if (hm >= s) row.classList.add("is-ongoing");
        });
        markTimelineEdges(root);
      }

      // 今日概览 Bento：教学周 / 天气 / 课程数 / 日程数 + 明日引导
      function renderTodayBento() {
        const wd = weekData;
        if (!wd) return;
        const setVal = (id, num, unit) => {
          const el = $(id);
          if (el) el.innerHTML = `${num}<small>${unit}</small>`;
        };
        const setSub = (id, text) => {
          const el = $(id);
          if (el) el.textContent = text || "";
        };
        // 教学周：显示学期进度（当前周 / 总周）
        setVal("#tbWeekVal", wd.current_week > 0 ? wd.current_week : "—", "周");
        const totalWeeks = status?.total_weeks || wd.total_weeks || 0;
        setSub("#tbWeekSub", wd.current_week > 0 && totalWeeks
          ? `共 ${totalWeeks} 周 · 已过 ${Math.round((wd.current_week / totalWeeks) * 100)}%`
          : wd.current_week > 0 ? "学期进行中" : "假期");
        // 今日课程 / 日程数（与 loadToday 的一级统计口径一致）
        const courses = wd.today_courses || [];
        const events = wd.today_events || [];
        const exams = wd.today_exams || [];
        setVal("#tbCourseVal", courses.length, "门");
        setSub("#tbCourseSub", courses.length
          ? courses.map((c) => c.start_time).filter(Boolean).sort().slice(0, 3).join(" · ") + " 开课"
          : "今天没有课");
        setVal("#tbEventVal", events.length + exams.length, "项");
        const evBits = [];
        if (events.length) evBits.push(`${events.length} 项日程`);
        if (exams.length) evBits.push(`${exams.length} 场考试`);
        setSub("#tbEventSub", evBits.length ? evBits.join(" · ") : "暂无安排");
        // 天气：取当前时段的逐小时天气，未配置/未取到则隐藏该卡
        const now = new Date();
        const hhmm = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
        const w = weatherAt(wd.today, hhmm);
        const wCell = $("#tbWeatherCell");
        if (wCell) {
          if (w && w.temp != null) {
            setVal("#tbWeatherVal", Math.round(w.temp), "℃");
            const wl = $("#tbWeatherLabel");
            if (wl) wl.textContent = "今日天气";
            setSub("#tbWeatherSub", w.text || "实时天气");
            wCell.classList.remove("hidden");
          } else {
            wCell.classList.add("hidden");
            setSub("#tbWeatherSub", "");
          }
        }
        // 明日引导：统计明日课程/日程/考试，突出最近一场考试
        const tmCourses = wd.tomorrow_courses || [];
        const tmEvents = wd.tomorrow_events || [];
        const tmExams = wd.tomorrow_exams || [];
        const tmTotal = tmCourses.length + tmEvents.length + tmExams.length;
        const tmBtn = $("#tbTomorrow");
        if (!tmBtn) return;
        if (!tmTotal) {
          tmBtn.classList.add("hidden");
          return;
        }
        const parts = [];
        if (tmCourses.length) parts.push(`${tmCourses.length} 门课`);
        if (tmEvents.length) parts.push(`${tmEvents.length} 项日程`);
        if (tmExams.length) parts.push(`${tmExams.length} 场考试`);
        const title = $("#tbTomorrowTitle");
        const sub = $("#tbTomorrowSub");
        if (title) title.textContent = `明日有 ${parts.join(" · ")}`;
        if (sub) sub.textContent = tmExams.length && tmExams[0].name ? tmExams[0].name : "点击查看完整安排";
        tmBtn.classList.remove("hidden");
      }

      // 顶部「下一节课」与下方对应课程行联动高亮
      function setNextHighlight(pick) {
        const root = $("#todayList");
        root.querySelectorAll(".course-row").forEach((r) => r.classList.remove("is-next"));
        if (!pick) return;
        const row = Array.from(root.querySelectorAll(".course-row[data-name]")).find(
          (r) => r.dataset.name === pick.name && (!pick.start_time || r.dataset.start === pick.start_time),
        );
        if (row) row.classList.add("is-next");
      }

      function eventTimeText(e) {
        if (e.start && e.end) return `${e.start}–${e.end}`;
        if (e.start) return e.start;
        return "全天";
      }

      function eventRow(e, withDate = false, grouped = false) {
        const time = withDate
          ? grouped
            ? `<div class="range">${esc(eventTimeText(e))}</div><div class="sections">日程</div>`
            : `<div class="range">${esc(e.date.slice(5).replace("-", "/"))}</div><div class="sections">${esc(eventTimeText(e))}</div>`
          : `<div class="range">${esc(eventTimeText(e))}</div><div class="sections">日程</div>`;
        const meta = [e.location, e.note].filter(Boolean).map(esc).join(" · ");
        return `<div class="course-row event-row${e.finished ? " done" : ""}" data-id="${esc(e.id)}" data-start="${esc(e.start || "")}" data-end="${esc(e.end || "")}">
    <div class="course-time">${time}</div>
    <div class="course-main">
      <div class="course-name">${esc(e.title)}${withDate ? "" : '<span class="event-tag">日程</span>'}</div>
      ${meta ? `<div class="course-meta">${meta}</div>` : ""}
    </div>
    ${withDate ? `<div class="row-actions">
      <button class="small" data-act="edit">编辑</button>
      <button class="small" data-act="del">删除</button>
    </div>` : ""}
  </div>`;
      }

      function agendaExamRow(e) {
        const d = new Date(e.date + "T00:00:00");
        const dow = WEEKDAY[d.getDay() === 0 ? 6 : d.getDay() - 1];
        const time = e.start && e.end ? `${e.start}–${e.end}` : e.display_time || e.time;
        const meta = [time, e.room || "考场待定", e.seat ? `座位 ${e.seat}` : ""]
          .filter(Boolean).map(esc).join(" · ");
        let when = "";
        if (!e.finished) {
          const now = new Date();
          const pad = (n) => String(n).padStart(2, "0");
          const today = weekData?.today ||
            `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
          const days = Math.round(
            (d - new Date(today + "T00:00:00")) / 86400000,
          );
          if (days === 0) when = "今天";
          else if (days === 1) when = "明天";
          else if (days > 1) when = `${days} 天后`;
        }
        return `<div class="course-row event-row exam-agenda${e.finished ? " done" : ""}">
    <div class="course-time"><div class="range">${esc(e.date.slice(5).replace("-", "/"))}</div><div class="sections">${esc(dow)}</div></div>
    <div class="course-main">
      <div class="course-name">${esc(e.name)}<span class="event-tag">考试</span>${when ? `<span class="exam-when">${when}</span>` : ""}</div>
      ${meta ? `<div class="course-meta">${meta}</div>` : ""}
    </div>
  </div>`;
      }

      // 今日页顶部：正在上 / 下一节课 + 倒计时，点一下直达周课表
      function renderNextClass() {
        const bar = $("#nextClassBar");
        const courses = (weekData?.today_courses || [])
          .filter((c) => c.start_time && c.end_time)
          .sort((a, b) => a.start_time.localeCompare(b.start_time));
        const now = new Date();
        const hm = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
        const nowMin = now.getHours() * 60 + now.getMinutes();
        const toMin = (t) => {
          const [h, m] = t.split(":").map(Number);
          return h * 60 + m;
        };
        const current = courses.find((c) => c.start_time <= hm && hm < c.end_time);
        const pick = current || courses.find((c) => c.start_time > hm);
        if (!pick) {
          const finished = courses.length > 0;
          const first = finished ? courses[0] : null;
          const last = finished ? courses[courses.length - 1] : null;
          const span = finished && first.start_time && last.end_time
            ? `${first.start_time}–${last.end_time}` : "";
          const nodeCount = finished ? courses.reduce((n, c) => n + Math.max(1, (c.end || c.start || 0) - (c.start || 1) + 1), 0) : 0;
          const sub = finished
            ? [ `${courses.length} 门课`, span && `首末课 ${span}`, nodeCount && `共 ${nodeCount} 节` ].filter(Boolean).join(" · ")
            : "好好休息，或添加一条日程";
          bar.innerHTML = `<div class="nc-main">
        <div class="nc-kicker">${finished ? "今日课程已结束" : "今日无课"}</div>
        <div class="nc-name">${finished ? "今天的课都上完了" : "今天没有课程安排"}</div>
        <div class="nc-meta">${esc(sub)}</div>
      </div>
      <div class="nc-done" aria-hidden="true"><span class="nc-done-check">✓</span><span class="nc-done-label">已完成</span></div>
      <div class="nc-arrow">›</div>`;
        } else {
          const left = Math.max(0, (current ? toMin(pick.end_time) : toMin(pick.start_time)) - nowMin);
          const meta = [pick.room || "教室待定", pick.campus || ""].filter(Boolean).join(" · ");
          // 进度：正在上课 = 已过时长 / 总时长；尚未开始 = 0
          let progress = 0;
          if (current) {
            const total = toMin(pick.end_time) - toMin(pick.start_time);
            const elapsed = nowMin - toMin(pick.start_time);
            progress = total > 0 ? Math.min(100, Math.max(0, Math.round((elapsed / total) * 100))) : 0;
          }
          const CIRC = 188.5; // 2πr，r=30
          const dash = (CIRC * (100 - progress) / 100).toFixed(1);
          bar.innerHTML = `<div class="nc-main">
        <div class="nc-kicker">${current ? `正在上课 · 第 ${pick.start}–${pick.end} 节` : "下一节课"}</div>
        <div class="nc-name">${esc(pick.name)}</div>
        <div class="nc-meta">${esc(pick.start_time)}–${esc(pick.end_time)}${meta ? " · " + esc(meta) : ""}</div>
        <div class="nc-track">
          <div class="nc-track-fill" style="width:${progress}%"></div>
          <div class="nc-track-dot" style="left:${progress}%"></div>
        </div>
        <div class="nc-track-times"><span>${esc(pick.start_time)}</span><span>${esc(pick.end_time)}</span></div>
      </div>
      <div class="nc-ring" title="课程进度 ${progress}%">
        <svg viewBox="0 0 72 72" aria-hidden="true">
          <circle class="nc-ring-bg" cx="36" cy="36" r="30"></circle>
          <circle class="nc-ring-fg" cx="36" cy="36" r="30" stroke-dasharray="${CIRC}" stroke-dashoffset="${dash}"></circle>
        </svg>
        <div class="nc-ring-center"><b>${left}</b><span>分钟${current ? "后下课" : "后上课"}</span></div>
      </div>`;
        }
        bar.classList.remove("hidden");
        // 定时刷新时同步下方时间轴状态与联动高亮
        applyTodayStates();
        setNextHighlight(pick);
      }

      async function loadToday() {
        if (!$("#todayList").children.length)
          $("#todayList").innerHTML = skeletonRows(3);
        weekData = await api("/api/timetable");
        if (viewWeek === null) viewWeek = Math.max(weekData.current_week, 1);
        const d = new Date(weekData.today + "T00:00:00");
        $("#todayDate").innerHTML =
          `${d.getMonth() + 1} 月 ${d.getDate()} 日 <span class="dow">${WEEKDAY[d.getDay() === 0 ? 6 : d.getDay() - 1]}</span>`;
        $("#todayWeek").textContent =
          weekData.current_week > 0
            ? `第 ${weekData.current_week} 教学周`
            : "假期";
        const courses = weekData.today_courses;
        const events = weekData.today_events || [];
        const exams = weekData.today_exams || [];
        // 课程、日程与当天考试按开始时间混排；全天安排排最前
        const items = [
          ...events.map((e) => ({ sort: e.start || "00:00", start: e.start, end: e.end, html: eventRow(e) })),
          ...exams.map((e) => ({ sort: e.start || "00:00", start: e.start, end: e.end, html: agendaExamRow(e) })),
          ...courses.map((c) => ({ sort: c.start_time || "99:99", start: c.start_time, end: c.end_time, html: courseRow(c) })),
        ].sort((a, b) => a.sort.localeCompare(b.sort));
        const countText = [
          courses.length ? `${courses.length} 门课程` : "",
          events.length ? `${events.length} 项日程` : "",
          exams.length ? `${exams.length} 场考试` : "",
        ].filter(Boolean).join(" · ");
        $("#todayCount").textContent = countText || "今日无安排";
        const tmHasItems =
          (weekData.tomorrow_courses || []).length +
          (weekData.tomorrow_events || []).length +
          (weekData.tomorrow_exams || []).length > 0;
        if (items.length) {
          $("#todayList").innerHTML = composeTimeline(items);
          applyTodayStates();
        } else {
          $("#todayList").innerHTML =
            `<div class="empty"><div class="empty-title">今天没有课程和日程</div><div class="empty-sub">可以安心安排自己的时间；要提前规划，可在下方「明日安排」里添加日程。</div></div>`;
        }
        // 明天有安排时保持「明日安排」可见并刷新内容（含刚添加的日程），
        // 今日无安排时也照常展示方便规划；避免添加明日日程后模块被隐藏
        if (tmHasItems || !items.length) renderTomorrow();
        else $("#tomorrowBlock").classList.add("hidden");
        // 今日页同时承载个人日程与未考完的考试
        await loadEvents().catch(() => {});
        renderNextClass();
        renderTodayBento();
      }

      // 今日无安排时，在下方展示「明日安排」模块，方便提前规划
      function renderTomorrow() {
        const block = $("#tomorrowBlock");
        const tmCourses = weekData.tomorrow_courses || [];
        const tmEvents = weekData.tomorrow_events || [];
        const tmExams = weekData.tomorrow_exams || [];
        const d = new Date(weekData.tomorrow + "T00:00:00");
        const dow = WEEKDAY[d.getDay() === 0 ? 6 : d.getDay() - 1];
        const items = [
          ...tmEvents.map((e) => ({ sort: e.start || "00:00", start: e.start, end: e.end, html: eventRow(e) })),
          ...tmExams.map((e) => ({ sort: e.start || "00:00", start: e.start, end: e.end, html: agendaExamRow(e) })),
          ...tmCourses.map((c) => ({ sort: c.start_time || "99:99", start: c.start_time, end: c.end_time, html: courseRow(c) })),
        ].sort((a, b) => a.sort.localeCompare(b.sort));
        $("#tomorrowDate").innerHTML =
          `${d.getMonth() + 1} 月 ${d.getDate()} 日 <span class="dow">${dow}</span>`;
        if (items.length) {
          const list = $("#tomorrowList");
          list.innerHTML = composeTimeline(items);
          markTimelineEdges(list);
        } else {
          $("#tomorrowList").innerHTML = `<div class="empty-sub center">明天暂时没有课程和日程，点击下方按钮提前安排。</div>`;
        }
        block.classList.remove("hidden");
      }

      function openTomorrowEvent() {
        // 打开日程编辑，日期默认填明天
        const now = new Date();
        const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
        const iso = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, "0")}-${String(tomorrow.getDate()).padStart(2, "0")}`;
        openEventEditor();
        $("#eventDate").value = iso;
        $("#eventEditorTitle").textContent = "添加明日日程";
      }

      /* ---------- 个人日程（并入今日页） ---------- */
      // 周课表页：显示当前查看周的个人日程，和空格子里的「加日程」形成闭环
      function weekSectionTimes(s, e) {
        const t = weekRender?.section_times || {};
        const n = weekRender?.section_ends || {};
        return { start: t[String(s)] || "", end: n[String(e)] || "" };
      }
      function weekEventDefaultDate() {
        const r = weekRender;
        const now = new Date();
        const localToday =
          `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
        if (!r || !Array.isArray(r.dates) || r.dates.length < 7) return localToday;
        if (r.week === r.current_week) return r.today || localToday;
        // 翻到别的周时，默认落到与今天相同的星期几，方便安排「这一周」的任务
        const dow = now.getDay();
        return r.dates[dow === 0 ? 6 : dow - 1] || r.dates[0];
      }
      function openWeekEventEditor(date, start, end) {
        openEventEditor();
        $("#eventDate").value = date || weekEventDefaultDate();
        if (start && end) {
          const t = weekSectionTimes(start, end);
          if (t.start) $("#eventStart").value = t.start;
          if (t.end) $("#eventEnd").value = t.end;
        }
        $("#eventEditorTitle").textContent = "添加本周日程";
      }
      function renderWeekEvents() {
        const box = $("#weekEventList");
        if (!box) return;
        const dates = Array.isArray(weekRender?.dates) ? weekRender.dates : [];
        const inWeek = new Set(dates);
        const list = (eventsCache || [])
          .filter((x) => x.date && inWeek.has(x.date))
          .sort((a, b) =>
            `${a.date} ${a.start || "00:00"}`.localeCompare(
              `${b.date} ${b.start || "00:00"}`));
        $("#weekEventSub").textContent = list.length ? `${list.length} 项日程` : "";
        if (list.length) {
          // 按日期分组，每天一个小标题 + 一条时间轴（与今日页风格统一）
          const byDay = new Map();
          list.forEach((x) => {
            if (!byDay.has(x.date)) byDay.set(x.date, []);
            byDay.get(x.date).push(x);
          });
          const parts = [];
          byDay.forEach((dayList, iso) => {
            const d = new Date(iso + "T00:00:00");
            const md = `${d.getMonth() + 1}/${d.getDate()}`;
            const dow = WEEKDAY[d.getDay() === 0 ? 6 : d.getDay() - 1];
            const isToday = iso === weekData?.today;
            const items = dayList.map((x) => ({
              sort: x.start || "00:00",
              start: x.start,
              end: x.end,
              html: eventRow(x, true, true),
            }));
            const dayBox = document.createElement("div");
            dayBox.innerHTML = composeTimeline(items, false);
            markTimelineEdges(dayBox);
            parts.push(
              `<div class="day-group${isToday ? " today" : ""}"><div class="day-group-head"><span class="dg-date">${md}</span><span class="dg-dow">${dow}${isToday ? " · 今天" : ""}</span><span class="dg-count">${dayList.length} 项</span></div><div class="timeline-list">${dayBox.innerHTML}</div></div>`,
            );
          });
          box.innerHTML = parts.join("");
        } else {
          box.innerHTML = `<div class="empty"><div class="empty-title">这一周还没有个人日程</div><div class="empty-sub">点没课时段里的「加日程」，或用右上角「添加日程」快速安排本周任务。</div></div>`;
        }
        renderWeekOverview();
      }
      // 本周概览：课程 / 日程 / 考试数量 + 最近一场考试倒计时
      // 三处数据（weekRender / eventsCache / examData）就绪时各自刷新，取已缓存的并集
      function renderWeekOverview() {
        const box = $("#weekOverview");
        if (!box) return;
        const wr = weekRender;
        if (!wr || !Array.isArray(wr.dates) || !wr.dates.length) return;
        const inWeek = new Set(wr.dates);
        const courseCount = Array.isArray(wr.courses) ? wr.courses.length : 0;
        const eventCount = (eventsCache || []).filter((e) => e.date && inWeek.has(e.date)).length;
        const allExams = examData?.exams || [];
        const weekExams = allExams.filter((e) => e.date && inWeek.has(e.date));
        const upcoming = (examData?.upcoming || []).filter((e) => !e.finished);
        const next = upcoming.length ? upcoming[0] : null;
        let examHtml = "";
        if (next && examData?.today) {
          const days = Math.round(
            (new Date(next.date + "T00:00:00") - new Date(examData.today + "T00:00:00")) / 86400000,
          );
          if (days >= 0) {
            const md = next.date.slice(5).replace("-", "/");
            const time = next.start ? `${next.start}–${next.end || ""}` : (next.display_time || next.time || "");
            const meta = [md + (time ? " " + time : ""), next.room || "考场待定"].filter(Boolean).join(" · ");
            const cdLabel = days === 0 ? "今天" : days === 1 ? "明天" : "天后";
            const cdNum = days === 0 ? "今" : days === 1 ? "明" : days;
            examHtml = `<div class="ov-exam">
              <div class="ov-cd"><b>${cdNum}</b><span>${cdLabel}${days > 1 ? "考试" : ""}</span></div>
              <div><div class="ov-en">${esc(next.name || "考试")}</div><div class="ov-em">${esc(meta)}</div></div>
            </div>`;
          }
        }
        box.innerHTML =
          `<div class="ov-cell"><div class="ov-val">${courseCount}<small>节</small></div><div class="ov-label">本周课程</div></div>` +
          `<div class="ov-cell"><div class="ov-val">${eventCount}<small>项</small></div><div class="ov-label">本周日程</div></div>` +
          `<div class="ov-cell"><div class="ov-val">${weekExams.length}<small>场</small></div><div class="ov-label">本周考试</div></div>` +
          `<div class="ov-cell"><div class="ov-val">${upcoming.length}<small>场</small></div><div class="ov-label">待考</div></div>` +
          examHtml;
      }

      let eventsCache = [];
      async function loadEvents() {
        // 今日页已不再单列日程卡片；这里只维护缓存，供「日程」页的本周日程列表渲染
        const box = $("#weekEventList");
        showSkeleton(box, 2);
        try {
          const data = await api("/api/events");
          eventsCache = data.events;
          renderWeekEvents();
        } catch (e) {
          if (box) box.innerHTML =
            `<div class="empty"><div class="empty-title">日程加载失败</div><div class="empty-sub">${esc(e.message)}</div></div>`;
        }
      }

      function openEventEditor(e = null) {
        $("#eventEditorTitle").textContent = e ? "编辑日程" : "添加日程";
        $("#saveEventBtn").textContent = e ? "保存修改" : "添加日程";
        $("#eventId").value = e ? e.id : "";
        $("#eventTitle").value = e ? e.title : "";
        const now = new Date();
        const localToday = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
        $("#eventDate").value = e ? e.date : (weekData?.today || localToday);
        $("#eventStart").value = e ? e.start || "" : "";
        $("#eventEnd").value = e ? e.end || "" : "";
        $("#eventLocation").value = e ? e.location || "" : "";
        $("#eventNote").value = e ? e.note || "" : "";
        $("#eventFormError").textContent = "";
        $("#eventEditor").classList.remove("hidden");
        $("#eventTitle").focus();
      }
      function closeEventEditor() {
        $("#eventEditor").classList.add("hidden");
      }

      $("#addWeekEventBtn").addEventListener("click", () =>
        openWeekEventEditor(weekEventDefaultDate()));
      $("#addTomorrowEventBtn").addEventListener("click", openTomorrowEvent);
      $("#closeEventEditorBtn").addEventListener("click", closeEventEditor);
      $("#eventEditor").addEventListener("click", (e) => {
        if (e.target.id === "eventEditor") closeEventEditor();
      });
      $("#eventForm").addEventListener("submit", async (e) => {
        e.preventDefault();
        const id = $("#eventId").value;
        const payload = {
          action: id ? "update" : "add",
          id: id || undefined,
          event: {
            title: $("#eventTitle").value.trim(),
            date: $("#eventDate").value,
            start: $("#eventStart").value,
            end: $("#eventEnd").value,
            location: $("#eventLocation").value.trim(),
            note: $("#eventNote").value.trim(),
          },
        };
        $("#eventFormError").textContent = "";
        $("#saveEventBtn").disabled = true;
        try {
          await api("/api/events", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          closeEventEditor();
          toast(id ? "日程已保存" : "日程已添加");
          await Promise.all([loadStatus(), loadToday()]);
        } catch (err) {
          $("#eventFormError").textContent = err.message;
        } finally {
          $("#saveEventBtn").disabled = false;
        }
      });
      // 今日页「日程」与周课表页「本周日程」共用同一套编辑/删除逻辑
      async function handleEventAction(e) {
        const btn = e.target.closest("button[data-act]");
        if (!btn) return;
        const row = btn.closest(".event-row");
        const item = eventsCache.find((x) => x.id === row.dataset.id);
        if (!item) return;
        if (btn.dataset.act === "edit") {
          openEventEditor(item);
          return;
        }
        if (!(await confirmDialog(`删除日程「${item.title}」？`, { ok: "删除", danger: true }))) return;
        try {
          await api("/api/events", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "delete", id: item.id }),
          });
          toast("日程已删除");
          await Promise.all([loadStatus(), loadToday()]);
        } catch (err) {
          toast(err.message, 4000, "error");
        }
      }
      $("#weekEventList").addEventListener("click", handleEventAction);

      /* ---------- 周视图 ---------- */
      // 天气是「今明两天」的短时数据：既要能在失败后自愈，也要定期换新，
      // 不能像以前那样一次拉空就整场会话都没有天气。
      let weatherFetchedAt = 0;
      const WEATHER_TTL = 30 * 60 * 1000;
      const WEATHER_RETRY_DELAYS = [0, 2500, 8000];

      function weatherHasHours(d) {
        return !!(d && d.enabled !== false && d.hours && Object.keys(d.hours).length);
      }

      async function loadWeather(opts = {}) {
        if (weatherLoading) return;
        const force = !!opts.force;
        if (!force && weatherFetchedAt &&
            Date.now() - weatherFetchedAt < WEATHER_TTL) return;
        weatherLoading = true;
        try {
          for (let i = 0; i < WEATHER_RETRY_DELAYS.length; i++) {
            if (WEATHER_RETRY_DELAYS[i])
              await new Promise((r) => setTimeout(r, WEATHER_RETRY_DELAYS[i]));
            let ok = false;
            try {
              weatherData = (await api("/api/weather")) || { hours: {} };
              ok = true;
            } catch (e) {
              // 失败不写时间戳，下一轮（含页面切回）还能自愈重试
              weatherData = weatherData || { hours: {} };
            }
            if (!ok) continue;
            if (weatherHasHours(weatherData)) { weatherFetchedAt = Date.now(); break; }
            // 管理员主动在设置里关掉了天气，不必重试
            if (weatherData && weatherData.enabled === false) { weatherFetchedAt = Date.now(); break; }
          }
        } finally {
          weatherLoading = false;
        }
        // 天气比课表晚到时补画一次当前周与今日概览
        if (weekRender && viewWeek === weekRender.current_week) renderWeek(weekRender);
        if (weekData) renderTodayBento();
      }

      // 切回页面时，如果天气已过 30 分钟就换一次（手机上常见长时间驻留后台）
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) return;
        if (weatherFetchedAt && Date.now() - weatherFetchedAt > WEATHER_TTL)
          loadWeather({ force: true });
      });

      function weatherAt(iso, hhmm) {
        if (!weatherData || !weatherData.enabled || !weatherData.hours) return null;
        const hour = String(hhmm || "").slice(0, 2);
        if (!hour) return null;
        return weatherData.hours[`${iso}T${hour}:00`] || null;
      }

      function renderWeek(data) {
        weekRender = data;
        const backWeek = Math.max(data.current_week, 1);
        const isCurrent = data.week === backWeek;
        const label = $("#weekLabel");
        // 周次标题兼任「回到本周」：不额外增减按钮，导航条布局恒定
        label.innerHTML = isCurrent
          ? `第 ${data.week} 周（本周）`
          : `第 ${data.week} 周 <span class="back-hint">· 回到本周</span>`;
        label.classList.toggle("clickable", !isCurrent);
        label.title = isCurrent ? "" : "点击回到本周";
        $("#weekHint").textContent = data.is_demo
          ? "当前为演示课表，接入真实数据源后自动替换。"
          : `数据来源：${data.fetched_at || ""} 同步`;
        // 固定五大节（1-2、3-4、5-6、7-8、9-10），课程按节次落格，跨午别也不会错位
        const blocks = [
          { s: 1, e: 2 }, { s: 3, e: 4 }, { s: 5, e: 6 },
          { s: 7, e: 8 }, { s: 9, e: 10 },
        ];
        const maxEnd = data.courses.reduce((m, c) => Math.max(m, c.end), 10);
        for (let s = 11; s <= maxEnd; s += 2)
          blocks.push({ s, e: Math.min(s + 1, maxEnd) });
        const blockOf = (section) => Math.floor((section - 1) / 2);
        // 课程色调色板（01 §2.3）：同名课程恒定同色，跨天跨周不跳变
        const toneOf = (name) => {
          let h = 0;
          for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
          return h % 5;
        };
        const cellHtml = (day, bi, isToday, iso) => {
          const list = data.courses
            .filter((c) => c.day === day && blockOf(c.start) === bi)
            .sort(
              (a, b) =>
                a.start - b.start ||
                a.end - b.end ||
                a.name.localeCompare(b.name, "zh"),
            );
          const b = blocks[bi];
          // 有课的格子只展示课程；没课的格子并列给「查空教室」和「加日程」，
          // 都按这个大节的节次/日期，方便直接给这一周的空档安排任务
          const content = list.length
            ? list
                .map((c) => {
                  const w = weatherAt(iso, c.start_time);
                  const wline = w
                    ? `<div class="w">${esc(w.text)}${
                        w.temp != null ? " " + w.temp + "℃" : ""
                      }${w.pop >= 30 ? " · " + w.pop + "%" : ""}</div>`
                    : "";
                  return `<div class="mini-course tone-${toneOf(c.name)}">
        <div class="t">${esc(c.start_time)}–${esc(c.end_time)} · ${c.start}-${c.end}节</div>
        <div class="n">${esc(c.name)}</div>
        <div class="r">${esc(c.room || "教室待定")}${c.note ? " · " + esc(c.note) : ""}</div>
        ${wline}
      </div>`;
                })
                .join("")
            : `<div class="wc-actions">
        <button type="button" class="wc-free" data-wc-rooms data-date="${iso}" data-start="${b.s}" data-end="${b.e}" title="这个时段没课，查空教室自习">查空教室<span>⌕</span></button>
        <button type="button" class="wc-free wc-event" data-wc-event data-date="${iso}" data-start="${b.s}" data-end="${b.e}" title="给这个空闲时段添加一条日程">加日程<span>＋</span></button>
      </div>`;
          return `<div class="week-cell${list.length ? "" : " free"}${isToday ? " today" : ""}${day >= 6 ? " weekend" : ""}">${content}</div>`;
        };
        const heads = data.dates
          .map((iso, i) => {
            const d = new Date(iso + "T00:00:00");
            return `<div class="week-head-cell${iso === data.today ? " today" : ""}${i >= 5 ? " weekend" : ""}">
        <div class="dow">${WEEKDAY[i]}</div><div class="dnum">${d.getMonth() + 1}/${d.getDate()}</div>
      </div>`;
          })
          .join("");
        const rows = blocks
          .map((b, bi) => {
            const startTime = data.section_times[String(b.s)] || "";
            const endTime = data.section_ends[String(b.e)] || "";
            return `<div class="week-period">
        <div class="p">第 ${b.s}-${b.e} 节</div><div class="t">${esc(startTime)}–${esc(endTime)}</div>
      </div>` + data.dates.map((iso, i) => cellHtml(i + 1, bi, iso === data.today, iso)).join("");
          })
          .join("");
        $("#weekGrid").innerHTML = `<div></div>${heads}${rows}`;
        // 手机端按天重排：同一份数据、同一套格子内容，只是从「行优先」改成「天优先」
        const daysHtml = data.dates.map((iso, i) => {
          const d = new Date(iso + "T00:00:00");
          const slots = blocks.map((b, bi) => {
            const st = data.section_times[String(b.s)] || "";
            const et = data.section_ends[String(b.e)] || "";
            const cell = cellHtml(i + 1, bi, iso === data.today, iso);
            const free = cell.includes('class="week-cell free');
            return `<div class="day-slot${free ? " free" : ""}">
        <div class="slot-label">第 ${b.s}-${b.e} 节 <span>${esc(st)}–${esc(et)}</span></div>
        ${cell}
      </div>`;
          }).join("");
          return `<div class="day-card${iso === data.today ? " today" : ""}${i >= 5 ? " weekend" : ""}" data-day="${iso}">
        <div class="day-card-head"><span class="day-name">${WEEKDAY[i]}</span>
          <span class="day-date">${d.getMonth() + 1}/${d.getDate()}</span>
          ${iso === data.today ? '<span class="day-today">今天</span>' : ""}</div>
        <div class="day-slots">${slots}</div>
      </div>`;
        }).join("");
        const daysBox = $("#weekDays");
        if (daysBox) daysBox.innerHTML = daysHtml;
        renderWeekEvents();
        renderWeekOverview();
      }

      let weekLoadSeq = 0;
      async function loadWeek(week) {
        // 快速左右连滑时只应用最后一次的结果，避免慢响应覆盖新周次
        const seq = ++weekLoadSeq;
        if (!$("#weekGrid").children.length)
          $("#weekGrid").innerHTML =
            `<div style="grid-column:1/-1"><div class="sk-grid">${
              Array.from({ length: 10 }, () => '<div class="sk"></div>').join("")
            }</div></div>`;
        // 手机上网格是隐藏的，骨架要铺在日视图容器里才看得见
        const daysBox = $("#weekDays");
        if (daysBox && !daysBox.children.length)
          daysBox.innerHTML = `<div class="day-skeleton">${skeletonRows(4)}</div>`;
        const data = await api("/api/timetable?week=" + week);
        if (seq !== weekLoadSeq) return;
        viewWeek = data.week;
        renderWeek(data);
        if (data.week === data.current_week) loadWeather();
      }

      // 没课的空格子 → 加日程：预填该天日期与这个大节的起止时间
      $("#weekGridScroll").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-wc-event]");
        if (!btn) return;
        openWeekEventEditor(
          btn.dataset.date,
          parseInt(btn.dataset.start, 10),
          parseInt(btn.dataset.end, 10),
        );
      });

      /* ---------- 查空教室（课表内联弹层） ---------- */
      // 空闲区间 → 可查询的两节一档（不跨占用节次，超出教务节次表的截断）。
      // 纯函数，与 v1.5.1 实现一致。
      function roomFreeBlocks(spans, maxJc) {
        const blocks = [];
        for (const s of spans || []) {
          for (let a = s.start; a <= s.end && a <= maxJc; a += 2)
            blocks.push({ start: a, end: Math.min(a + 1, s.end, maxJc) });
        }
        return blocks;
      }
      // 教务节次表的最大节次（v1.5.1 读 #roomStart 下拉；独立页下线后改从 meta.periods 取）
      function roomMaxJc() {
        const nums = ((roomMeta && roomMeta.periods) || [])
          .map((p) => parseInt(p.jc, 10))
          .filter(Boolean);
        return nums.length ? Math.max(...nums) : 12;
      }

      let roomMeta = null;     // /api/rooms/meta 缓存（校区/教学楼/节次表）
      let roomPopState = null; // 弹层当前查询状态 {date,start,end,campus,building,rooms}
      let roomPopBusy = false;
      let roomPopOpen = false;

      function openRoomPop(anchor) {
        const layer = $("#roomPop");
        layer.classList.remove("hidden");
        layer.setAttribute("aria-hidden", "false");
        roomPopOpen = true;
        const panel = $("#roomPopPanel");
        if (window.matchMedia("(max-width: 720px)").matches) {
          // 移动端底部面板：由 CSS 定位，清掉桌面浮层的内联坐标
          panel.style.left = "";
          panel.style.top = "";
          panel.style.right = "";
        } else if (anchor && typeof anchor.getBoundingClientRect === "function") {
          positionRoomPop(anchor);
        }
      }
      // 桌面浮层：优先落在格子右侧，贴边时翻到左侧，再不行居中
      function positionRoomPop(anchor) {
        const panel = $("#roomPopPanel");
        const r = anchor.getBoundingClientRect();
        const pw = 340;
        const ph = Math.min(430, window.innerHeight - 24);
        let left = r.right + 10;
        if (left + pw > window.innerWidth - 8) left = r.left - pw - 10;
        if (left < 8) left = Math.max(8, (window.innerWidth - pw) / 2);
        let top = r.top;
        if (top + ph > window.innerHeight - 8) top = window.innerHeight - ph - 8;
        if (top < 8) top = 8;
        panel.style.left = left + "px";
        panel.style.top = top + "px";
        panel.style.right = "auto";
      }
      function closeRoomPop() {
        const layer = $("#roomPop");
        if (layer.classList.contains("hidden")) return;
        layer.classList.add("hidden");
        layer.setAttribute("aria-hidden", "true");
        roomPopOpen = false;
      }
      function roomPopTitle() {
        const st = roomPopState;
        if (!st) return;
        const d = new Date(st.date + "T00:00:00");
        const label =
          st.start === st.end ? `第 ${st.start} 节` : `第 ${st.start}–${st.end} 节`;
        $("#roomPopTitle").textContent =
          `${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAY[(d.getDay() + 6) % 7]} · ${label}`;
      }
      function roomPopMessage(title, sub) {
        $("#roomPopCount").textContent = "";
        $("#roomPopList").innerHTML =
          `<div class="rp-msg"><div class="rp-msg-t">${esc(title)}</div>` +
          (sub ? `<div class="rp-msg-s">${esc(sub)}</div>` : "") + `</div>`;
      }
      // 弹层筛选控件：用 meta 数据填充校区/教学楼下拉
      function roomPopFillFilters() {
        const m = roomMeta;
        if (!m) return;
        const campusSel = $("#rpCampus");
        campusSel.innerHTML = m.campuses
          .map((c) => `<option value="${escAttr(c.id)}">${esc(c.name)}</option>`)
          .join("");
        campusSel.value =
          m.campus || (m.campuses.length ? m.campuses[0].id : "");
        const bSel = $("#rpBuilding");
        bSel.innerHTML =
          [`<option value="">全部教学楼</option>`]
            .concat(
              (m.buildings || []).map(
                (b) => `<option value="${escAttr(b.id)}">${esc(b.name)}</option>`,
              ),
            )
            .join("");
        bSel.value = "";
      }
      async function roomPopQuery() {
        const st = roomPopState;
        if (!st || !roomMeta || roomPopBusy) return;
        roomPopBusy = true;
        const btn = $("#rpQueryBtn");
        btn.disabled = true;
        btn.textContent = "查询中…";
        $("#roomPopCount").textContent = "";
        $("#roomPopList").innerHTML = skeletonRows(3);
        try {
          const params = new URLSearchParams({
            date_s: st.date,
            start: String(st.start),
            end: String(st.end),
            campus: st.campus || "",
            building: st.building || "",
          });
          const data = await api("/api/rooms/free?" + params.toString());
          st.rooms = data.rooms || [];
          roomPopRender();
        } catch (e) {
          roomPopMessage("查询失败", e.message || "请稍后重试");
        } finally {
          roomPopBusy = false;
          btn.disabled = false;
          btn.textContent = "查询";
        }
      }
      // 结果按座位数降序（同学找自习室基本按容量挑），最少座位为客户端过滤
      function roomPopRender() {
        const st = roomPopState;
        if (!st) return;
        const minSeats = parseInt($("#rpSeats").value, 10) || 0;
        const total = (st.rooms || []).length;
        const rooms = (st.rooms || [])
          .filter((r) => !minSeats || (r.seats || 0) >= minSeats)
          .slice()
          .sort(
            (a, b) =>
              (b.seats || 0) - (a.seats || 0) ||
              String(a.name).localeCompare(String(b.name), "zh"),
          );
        $("#roomPopCount").textContent = minSeats
          ? `${rooms.length} 间（共 ${total} 间）`
          : `共 ${total} 间空教室`;
        if (!rooms.length) {
          // 空态保留计数（如「0 间（共 2 间）」），比纯提示更能说明是筛选所致
          $("#roomPopList").innerHTML =
            `<div class="rp-msg"><div class="rp-msg-t">没有符合条件的空教室</div>` +
            `<div class="rp-msg-s">换个节次、教学楼、座位数或校区试试。</div></div>`;
          return;
        }
        $("#roomPopList").innerHTML = rooms
          .map((r) => {
            const meta = [r.building, r.category || r.campus]
              .filter(Boolean)
              .map(esc)
              .join(" · ");
            return `<div class="rp-room">
      <div class="rp-room-main">
        <div class="rp-room-name">${esc(r.name)}</div>
        ${meta ? `<div class="rp-room-meta">${meta}</div>` : ""}
      </div>
      ${r.seats ? `<div class="rp-room-seats"><b>${r.seats}</b><span>座</span></div>` : ""}
    </div>`;
          })
          .join("");
      }

      // 没课的空格子 → 查空教室：先按原逻辑校验空闲（meta + my-free），
      // 跨节课程可能让网格看起来没课、实际被占用，所以用 my-free 校验一次，
      // 真被占用时自动改查当天最接近的空闲时段并 toast 提示；
      // 结果直接在内联弹层里展示，不再跳转独立页（该页已下线）。
      $("#weekGridScroll").addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-wc-rooms]");
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        try {
          const wantStart = parseInt(btn.dataset.start, 10) || 1;
          const wantEnd = parseInt(btn.dataset.end, 10) || wantStart;
          roomPopState = {
            date: btn.dataset.date,
            start: wantStart,
            end: wantEnd,
            campus: "",
            building: "",
            rooms: null,
          };
          openRoomPop(btn.closest(".week-cell") || btn);
          roomPopTitle();
          $("#roomPopCount").textContent = "";
          $("#roomPopList").innerHTML = skeletonRows(3);
          if (!roomMeta) roomMeta = await api("/api/rooms/meta");
          roomPopFillFilters();
          roomPopState.campus = $("#rpCampus").value;
          const free = await api(
            "/api/rooms/my-free?date_s=" + encodeURIComponent(roomPopState.date),
          );
          if (free.spans === null) {
            roomPopMessage("课表还没同步", "先去「课表来源」同步课表，再来查空教室。");
            return;
          }
          const freeBlocks = roomFreeBlocks(free.spans, roomMaxJc());
          if (!freeBlocks.length) {
            roomPopMessage("这一天都有课", "课表显示当天没有空闲时段，换个日期试试。");
            return;
          }
          const exact = freeBlocks.find(
            (x) => x.start === wantStart && x.end === wantEnd,
          );
          const block =
            exact ||
            freeBlocks.reduce(
              (best, x) =>
                Math.abs(x.start - wantStart) < Math.abs(best.start - wantStart)
                  ? x
                  : best,
              freeBlocks[0],
            );
          if (!exact)
            toast(
              `第 ${wantStart}–${wantEnd} 节有课，已改查第 ${block.start}–${block.end} 节`,
              3500,
            );
          roomPopState.start = block.start;
          roomPopState.end = block.end;
          roomPopTitle();
          await roomPopQuery();
        } catch (err) {
          roomPopMessage("查询失败", err.message || "请稍后重试");
          toast(err.message, 4000, "error");
        } finally {
          btn.disabled = false;
        }
      });
      // 弹层交互：关闭按钮 / 点遮罩 / Esc；改筛选后点「查询」重新查
      $("#roomPopClose").addEventListener("click", closeRoomPop);
      $("#roomPop").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeRoomPop();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && roomPopOpen) closeRoomPop();
      });
      $("#rpQueryBtn").addEventListener("click", () => {
        if (!roomPopState || !roomMeta) return;
        roomPopState.campus = $("#rpCampus").value;
        roomPopState.building = $("#rpBuilding").value;
        roomPopQuery();
      });
      // 换校区要重新取 meta（教学楼列表是跟着校区走的）
      $("#rpCampus").addEventListener("change", async (e) => {
        if (!roomPopOpen) return;
        const campus = e.target.value;
        if (roomPopState) roomPopState.campus = campus;
        const bSel = $("#rpBuilding");
        bSel.disabled = true;
        try {
          roomMeta = await api(
            "/api/rooms/meta?campus=" + encodeURIComponent(campus),
          );
          roomPopFillFilters();
        } catch (err) {
          toast(err.message, 4000, "error");
        } finally {
          bSel.disabled = false;
        }
      });
      $("#rpBuilding").addEventListener("change", (e) => {
        if (roomPopState) roomPopState.building = e.target.value;
      });

      /* ---------- 编辑课表 ---------- */
      let courseData = null;
      let editingUid = "";

      function weeksLabel(weeks) {
        if (!weeks || !weeks.length) return "每周";
        const s = [...weeks].sort((a, b) => a - b);
        if (s.length > 3) {
          const step = s[1] - s[0];
          const same =
            (step === 1 || step === 2) &&
            s.every((w, i) => i === 0 || w - s[i - 1] === step);
          if (same && step === 2)
            return `${s[0]}-${s[s.length - 1]} ${s[0] % 2 ? "单周" : "双周"}`;
        }
        const parts = [];
        let a = s[0],
          b = s[0];
        for (let i = 1; i <= s.length; i++) {
          const w = s[i];
          if (w === b + 1) {
            b = w;
            continue;
          }
          parts.push(a === b ? `${a}` : `${a}-${b}`);
          a = b = w;
        }
        return `第 ${parts.join(",")} 周`;
      }

      function resetCourseForm() {
        editingUid = "";
        $("#courseForm").reset();
        $("#courseStart").value = 1;
        $("#courseEnd").value = 2;
        $("#saveCourseBtn").textContent = "添加课程";
      }

      function openCourseEditor() {
        $("#courseEditor").classList.remove("hidden");
        document.body.style.overflow = "hidden";
        loadCourseEditor();
      }

      function closeCourseEditor() {
        $("#courseEditor").classList.add("hidden");
        document.body.style.overflow = "";
        resetCourseForm();
      }

      async function exportCalendar() {
        const btn = $("#exportIcsBtn");
        btn.disabled = true;
        const old = btn.textContent;
        btn.textContent = "导出中…";
        try {
          const resp = await fetch("/api/export/calendar.ics");
          if (!resp.ok) {
            let msg = "导出失败";
            try {
              const j = await resp.json();
              if (j && j.detail) msg = j.detail;
            } catch (e) {}
            throw new Error(msg);
          }
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          const cd = resp.headers.get("Content-Disposition") || "";
          let fname = "课程表.ics";
          const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i);
          const m2 = cd.match(/filename="?([^";]+)"?/i);
          if (m1) fname = decodeURIComponent(m1[1]);
          else if (m2) fname = m2[1];
          a.download = fname;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          toast("已导出日历文件，请到系统日历中导入", 2500);
        } catch (e) {
          toast(e.message || "导出失败", 3000, "error");
        } finally {
          btn.disabled = false;
          btn.textContent = old;
        }
      }

      // 检测设备类型，决定订阅跳转方式
      function _detectPlatform() {
        const ua = navigator.userAgent || "";
        if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
        if (/Android/i.test(ua)) return "android";
        return "desktop";
      }

      async function subscribeCalendar() {
        const btn = $("#subscribeIcsBtn");
        btn.disabled = true;
        const old = btn.textContent;
        btn.textContent = "准备中…";
        try {
          const resp = await fetch("/api/export/subscribe");
          if (!resp.ok) {
            let msg = "获取订阅信息失败";
            try {
              const j = await resp.json();
              if (j && j.detail) msg = j.detail;
            } catch (e) {}
            throw new Error(msg);
          }
          const info = await resp.json();
          const platform = _detectPlatform();

          if (platform === "ios") {
            // iOS：webcal:// 直接唤起系统日历订阅
            toast("正在唤起系统日历订阅…", 2500);
            window.location.href = info.webcal;
          } else if (platform === "android") {
            // 安卓：优先跳 Google 日历订阅；无 Google 日历则提示复制链接
            toast("正在打开日历订阅，若未自动弹出请用「下载」方式导入", 3500);
            window.location.href = info.google;
          } else {
            // 桌面：默认下载 .ics，同时给出订阅链接提示
            exportCalendar();
            toast("桌面端建议下载后导入；也可复制订阅链接到在线日历", 4000);
            try { await navigator.clipboard.writeText(info.webcal); } catch (e) {}
          }
        } catch (e) {
          toast(e.message || "订阅失败", 3000, "error");
        } finally {
          btn.disabled = false;
          btn.textContent = old;
        }
      }

      function fillCourseForm(uid) {
        const c = (courseData?.courses || []).find((x) => x.uid === uid);
        if (!c) return;
        editingUid = uid;
        $("#courseName").value = c.name || "";
        $("#courseDay").value = String(c.day || 1);
        $("#courseStart").value = c.start;
        $("#courseEnd").value = c.end;
        $("#courseWeeks").value = (c.weeks || []).join(",");
        $("#courseRoom").value = c.room || "";
        $("#courseTeacher").value = c.teacher || "";
        $("#courseNote").value = c.note || "";
        $("#saveCourseBtn").textContent = "保存修改";
        const _cp = $("#courseName").closest(".sheet-panel");
        if (_cp) _cp.scrollTop = 0;
        $("#courseName").focus();
      }

      async function loadCourseEditor() {
        try {
          courseData = await api("/api/courses");
        } catch (e) {
          toast(e.message, 4000, "error");
          return;
        }
        const cs = courseData.courses || [];
        const counts = [];
        if (courseData.added_count) counts.push(`手动添加 ${courseData.added_count}`);
        if (courseData.edited_count) counts.push(`修改 ${courseData.edited_count}`);
        if (courseData.hidden_count) counts.push(`隐藏 ${courseData.hidden_count}`);
        $("#courseCount").textContent =
          `${cs.length} 门课` + (counts.length ? " · " + counts.join(" / ") : "");
        $("#resetCoursesBtn").disabled = !courseData.has_overrides;
        $("#restoreHiddenBtn").classList.toggle(
          "hidden",
          !courseData.hidden_count,
        );
        $("#restoreHiddenBtn").textContent = `恢复隐藏（${courseData.hidden_count}）`;
        const box = $("#courseList");
        if (!cs.length) {
          box.innerHTML = `<div class="empty">还没有课程，先同步或在上方手动添加</div>`;
          return;
        }
        box.innerHTML = WEEKDAY.map((dow, i) => {
          const list = cs.filter((c) => c.day === i + 1);
          if (!list.length) return "";
          return (
            `<div class="editor-day">${dow}</div>` +
            list
              .map(
                (c) => `<div class="editor-row">
        <div class="info">
          <div class="nm">${esc(c.name)}${c.manual ? ' <span class="muted" style="font-size:11px">手动</span>' : ""}</div>
          <div class="meta">${c.start}-${c.end} 节 · ${esc(c.start_time)}–${esc(c.end_time)} · ${weeksLabel(c.weeks)}${c.room ? " · " + esc(c.room) : ""}</div>
        </div>
        <div class="acts">
          <button type="button" data-edit="${esc(c.uid)}">修改</button>
          <button type="button" data-del="${esc(c.uid)}">${c.manual ? "删除" : "隐藏"}</button>
        </div>
      </div>`,
              )
              .join("")
          );
        }).join("");
      }

      async function applyCourseAction(action, uid, course) {
        try {
          await api("/api/courses", {
            method: "POST",
            body: JSON.stringify({
              action,
              uid: uid || null,
              course: course || null,
            }),
          });
          const msg = {
            add: "已添加课程",
            update: "已保存修改",
            delete: "已从课表移除",
            restore_hidden: "已恢复隐藏的课程",
            reset: "已恢复自动课表",
          };
          toast(msg[action] || "已更新", 3000);
          await loadCourseEditor();
          await Promise.all([loadStatus(), loadToday()]);
          if (!$("#tab-week").classList.contains("hidden"))
            await loadWeek(viewWeek ?? Math.max(weekData?.current_week || 1, 1));
          return true;
        } catch (e) {
          toast(e.message, 4000, "error");
          return false;
        }
      }

      $("#editCoursesBtn").addEventListener("click", openCourseEditor);
      $("#exportIcsBtn").addEventListener("click", exportCalendar);
      $("#subscribeIcsBtn").addEventListener("click", subscribeCalendar);
      $("#closeEditorBtn").addEventListener("click", closeCourseEditor);
      $("#cancelCourseBtn").addEventListener("click", resetCourseForm);
      $("#courseEditor").addEventListener("click", (e) => {
        if (e.target.id === "courseEditor") closeCourseEditor();
      });
      $("#courseList").addEventListener("click", (ev) => {
        const edit = ev.target.closest("[data-edit]");
        const del = ev.target.closest("[data-del]");
        if (edit) fillCourseForm(edit.dataset.edit);
        else if (del) applyCourseAction("delete", del.dataset.del);
      });
      $("#resetCoursesBtn").addEventListener("click", async () => {
        if (await confirmDialog("恢复自动课表？会清除所有手动添加、修改和隐藏的课程。", { ok: "恢复" }))
          applyCourseAction("reset");
      });
      $("#restoreHiddenBtn").addEventListener("click", () =>
        applyCourseAction("restore_hidden"),
      );
      $("#courseForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const body = {
          name: $("#courseName").value.trim(),
          day: Number($("#courseDay").value),
          start: Number($("#courseStart").value),
          end: Number($("#courseEnd").value),
          weeks: $("#courseWeeks").value.trim(),
          room: $("#courseRoom").value.trim(),
          teacher: $("#courseTeacher").value.trim(),
          note: $("#courseNote").value.trim(),
        };
        if (!body.name) {
          toast("请填写课程名称");
          $("#courseName").focus();
          return;
        }
        if (await applyCourseAction(editingUid ? "update" : "add", editingUid, body))
          resetCourseForm();
      });

      /* ---------- 编辑考试 ---------- */
      let examAdjustData = null;
      let editingExamUid = "";

      function resetExamForm() {
        editingExamUid = "";
        $("#examForm").reset();
        $("#saveExamAdjustBtn").textContent = "添加考试";
      }

      function openExamEditor() {
        $("#examEditor").classList.remove("hidden");
        document.body.style.overflow = "hidden";
        loadExamEditor();
      }

      function closeExamEditor() {
        $("#examEditor").classList.add("hidden");
        document.body.style.overflow = "";
        resetExamForm();
      }

      function fillExamForm(uid) {
        const x = (examAdjustData?.exams || []).find((e) => e.uid === uid);
        if (!x) return;
        editingExamUid = uid;
        $("#examName").value = x.name || "";
        $("#examDate").value = x.date || "";
        $("#examStart").value = x.start || "";
        $("#examEnd").value = x.end || "";
        $("#examRoom").value = x.room || "";
        $("#examSeat").value = x.seat || "";
        $("#examNote").value = x.note || "";
        $("#saveExamAdjustBtn").textContent = "保存修改";
        const _ep = $("#examName").closest(".sheet-panel");
        if (_ep) _ep.scrollTop = 0;
        $("#examName").focus();
      }

      async function loadExamEditor() {
        try {
          examAdjustData = await api("/api/exams");
        } catch (e) {
          toast(e.message, 4000, "error");
          return;
        }
        const list = examAdjustData.exams || [];
        const counts = [];
        if (examAdjustData.added_count) counts.push(`手动添加 ${examAdjustData.added_count}`);
        if (examAdjustData.edited_count) counts.push(`修改 ${examAdjustData.edited_count}`);
        if (examAdjustData.hidden_count) counts.push(`隐藏 ${examAdjustData.hidden_count}`);
        $("#examAdjustCount").textContent =
          `${list.length} 场考试` + (counts.length ? " · " + counts.join(" / ") : "");
        $("#resetExamsBtn").disabled = !examAdjustData.has_overrides;
        $("#restoreHiddenExamsBtn").classList.toggle(
          "hidden",
          !examAdjustData.hidden_count,
        );
        $("#restoreHiddenExamsBtn").textContent = `恢复隐藏（${examAdjustData.hidden_count}）`;
        const box = $("#examAdjustList");
        if (!list.length) {
          box.innerHTML = `<div class="empty">暂无考试，先同步或在上方手动添加</div>`;
          return;
        }
        box.innerHTML = list
          .map(
            (e) => `<div class="editor-row${e.finished ? " done" : ""}">
        <div class="info">
          <div class="nm">${esc(e.name)}${e.manual ? ' <span class="muted" style="font-size:11px">手动</span>' : ""}</div>
          <div class="meta">${esc(e.display_time || "")}${e.room ? " · " + esc(e.room) : ""}${e.seat ? " · 座位 " + esc(e.seat) : ""}${e.note ? " · " + esc(e.note) : ""}</div>
        </div>
        <div class="acts">
          <button type="button" data-edit-exam="${esc(e.uid)}">修改</button>
          <button type="button" data-del-exam="${esc(e.uid)}">${e.manual ? "删除" : "隐藏"}</button>
        </div>
      </div>`,
          )
          .join("");
      }

      async function applyExamAction(action, uid, exam) {
        try {
          await api("/api/exams", {
            method: "POST",
            body: JSON.stringify({
              action,
              uid: uid || null,
              exam: exam || null,
            }),
          });
          const msg = {
            add: "已添加考试",
            update: "已保存修改",
            delete: "已从考试列表移除",
            restore_hidden: "已恢复隐藏的考试",
            reset: "已恢复自动考试",
          };
          toast(msg[action] || "已更新", 3000);
          await loadExamEditor();
          await Promise.all([loadStatus(), loadExams(), loadToday()]);
          return true;
        } catch (e) {
          toast(e.message, 4000, "error");
          return false;
        }
      }

      $("#editExamsBtn").addEventListener("click", openExamEditor);
      $("#closeExamEditorBtn").addEventListener("click", closeExamEditor);
      $("#cancelExamAdjustBtn").addEventListener("click", resetExamForm);
      $("#examEditor").addEventListener("click", (e) => {
        if (e.target.id === "examEditor") closeExamEditor();
      });
      $("#examAdjustList").addEventListener("click", (ev) => {
        const edit = ev.target.closest("[data-edit-exam]");
        const del = ev.target.closest("[data-del-exam]");
        if (edit) fillExamForm(edit.dataset.editExam);
        else if (del) applyExamAction("delete", del.dataset.delExam);
      });
      $("#resetExamsBtn").addEventListener("click", async () => {
        if (await confirmDialog("恢复自动考试？会清除所有手动添加、修改和隐藏的考试。", { ok: "恢复" }))
          applyExamAction("reset");
      });
      $("#restoreHiddenExamsBtn").addEventListener("click", () =>
        applyExamAction("restore_hidden"),
      );
      $("#examForm").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const body = {
          name: $("#examName").value.trim(),
          date: $("#examDate").value,
          start: $("#examStart").value,
          end: $("#examEnd").value,
          room: $("#examRoom").value.trim(),
          seat: $("#examSeat").value.trim(),
          note: $("#examNote").value.trim(),
        };
        if (!body.name) {
          toast("请填写考试名称");
          $("#examName").focus();
          return;
        }
        if (!body.date) {
          toast("请选择考试日期");
          $("#examDate").focus();
          return;
        }
        if (await applyExamAction(editingExamUid ? "update" : "add", editingExamUid, body))
          resetExamForm();
      });

      /* ---------- 考试 ---------- */
      let examData = null;
      function examRow(e) {
        const d = new Date(e.date + "T00:00:00");
        const md = `${d.getMonth() + 1}/${d.getDate()}`;
        const dow = WEEKDAY[d.getDay() === 0 ? 6 : d.getDay() - 1];
        let when = "";
        let countdown = "";
        let stateClass = "";
        if (!e.finished && examData?.today) {
          const days = Math.round(
            (new Date(e.date + "T00:00:00") -
              new Date(examData.today + "T00:00:00")) /
              86400000,
          );
          const cd = (num, label, urgent) =>
            `<div class="exam-countdown${urgent ? " urgent" : ""}${/[\u4e00-\u9fa5]/.test(String(num)) ? " cd-cn" : ""}"><b>${num}</b><span>${label}</span></div>`;
          if (days === 0) {
            when = "今天";
            stateClass = " exam-today";
            countdown = cd("今", "今天考试", true);
          } else if (days === 1) {
            when = "明天";
            stateClass = " exam-soon";
            countdown = cd("明", "明天考试", true);
          } else if (days > 1) {
            when = `${days} 天后`;
            stateClass = days <= 3 ? " exam-soon" : "";
            countdown = cd(days, "天后考试", days <= 3);
          }
        }
        const time =
          e.start && e.end ? `${e.start}–${e.end}` : e.display_time || e.time;
        const meta = [time, e.room || "考场待定", e.seat ? `座位 ${e.seat}` : ""]
          .filter(Boolean)
          .map(esc)
          .join(" · ");
        const extra = [e.campus, e.note].filter(Boolean).map(esc).join(" · ");
        return `<div class="exam-row${e.finished ? " done" : ""}${stateClass}">
    <div class="exam-date"><div class="md">${md}</div><div class="dow">${dow}</div></div>
    <div class="exam-main">
      <div class="exam-name">${esc(e.name)}${when ? `<span class="exam-when">${when}</span>` : ""}</div>
      <div class="exam-meta">${meta}</div>
      ${extra ? `<div class="exam-meta">${extra}</div>` : ""}
    </div>
    ${countdown}
  </div>`;
      }
      async function loadExams() {
        if (!$("#examList").children.length)
          $("#examList").innerHTML = skeletonRows(2);
        examData = await api("/api/exams");
        const box = $("#examList");
        const sub = $("#examSub");
        if (!examData.exams.length) {
          sub.textContent = "";
          box.innerHTML = examData.supported
            ? `<div class="empty">暂无考试安排，点「立即同步」从教务拉取</div>`
            : examData.provider === "portal"
              ? `<div class="empty">门户只提供课表；填写正方账号后，同步会额外拉取考试<br><button class="small" id="examToSettings">去课表来源填写正方账号</button></div>`
              : `<div class="empty">当前数据源不支持考试查询，请在课表来源接入正方教务<br><button class="small" id="examToSettings">去课表来源</button></div>`;
          $("#examHint").textContent = examData.supported
            ? "考试数据来自正方教务，随每次同步更新；点「编辑考试」可手动添加或调整，临时调考以教务系统为准。"
            : "";
          renderWeekOverview();
          return;
        }
        sub.textContent = [
          examData.is_demo ? "演示数据" : "",
          examData.fetched_at ? `${examData.fetched_at} 同步` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        const group = (title, list) =>
          list.length
            ? `<div class="exam-group">${title} · ${list.length} 场</div><div class="exam-list">${list.map(examRow).join("")}</div>`
            : "";
        box.innerHTML =
          group("即将进行", examData.upcoming) +
          group("已结束", examData.finished);
        $("#examHint").textContent = examData.is_demo
          ? "当前为演示考试数据，接入正方教务后自动替换；点「编辑考试」可手动添加或调整。"
          : "考试数据来自正方教务，随每次同步更新；点「编辑考试」可手动添加或调整，临时调考以教务系统为准。";
        renderWeekOverview();
      }

      /* ---------- AI ---------- */
      // 行内 Markdown（输入已整体转义）：代码 / 粗体 / 斜体 / 删除线 / 链接
      function renderInline(s) {
        const stash = [];
        let t = s.replace(/`([^`\n]+)`/g, (_, code) => {
          stash.push(`<code>${code}</code>`);
          return `\u0001${stash.length - 1}\u0001`;
        });
        t = t
          .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
          .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
          .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
          .replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
            // 只允许 http/https/mailto，避免 javascript: 之类的链接注入
            if (!/^(https?:\/\/|mailto:)/i.test(url.replace(/&amp;/g, "&"))) return m;
            // esc 只转义 & < >，引号需单独处理，防止提前闭合 href 属性
            const safe = url.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
            return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
          });
        return t.replace(/\u0001(\d+)\u0001/g, (_, i) => stash[Number(i)] || "");
      }
      // 流式输出时先隐藏未写完的动作标记，结束后再渲染成卡片
      function stripPendingMarkers(s) {
        return (s || "").replace(/\[(添加日程|日程已添加|记住|推送|追问)\][\s\S]*$/, "");
      }
      // 轻量 Markdown：标题 / 列表 / 表格 / 引用 / 分隔线 / 代码块，先整体转义再渲染
      function renderReply(raw) {
        const lines = esc(raw || "").split("\n");
        const out = [];
        let para = [];
        let inCode = false, codeLang = "", codeBuf = [];
        const flushPara = () => {
          if (!para.length) return;
          out.push(`<div class="md-p">${para.map(renderInline).join("<br>")}</div>`);
          para = [];
        };
        const isTableSep = (l) =>
          /^[\s|:-]+$/.test(l) && l.includes("-") && l.includes("|");
        const isRow = (l) => l.includes("|") && l.trim().length > 1;
        const cells = (l) => {
          let v = l.trim();
          if (v.startsWith("|")) v = v.slice(1);
          if (v.endsWith("|")) v = v.slice(0, -1);
          return v.split("|").map((x) => renderInline(x.trim()));
        };
        const codeBlock = () => {
          const head = `<div class="md-code-head"><span>${codeLang || "代码"}</span><button type="button" data-mact="copy-code">复制</button></div>`;
          return `<div class="md-code">${head}<pre><code>${codeBuf.join("\n")}</code></pre></div>`;
        };
        for (let i = 0; i < lines.length; i++) {
          const line = lines[i];
          const trimmed = line.trim();
          if (trimmed.startsWith("```")) {
            flushPara();
            if (inCode) {
              out.push(codeBlock());
              codeBuf = [];
              inCode = false;
            } else {
              inCode = true;
              codeLang = trimmed.slice(3).trim();
            }
            continue;
          }
          if (inCode) { codeBuf.push(line); continue; }
          if (!trimmed) { flushPara(); continue; }
          // 表格：当前行是表头，且下一行是 | --- | --- | 分隔行
          if (isRow(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
            flushPara();
            const head = `<thead><tr>${cells(line).map((c) => `<th>${c}</th>`).join("")}</tr></thead>`;
            const body = [];
            i += 2;
            while (i < lines.length && isRow(lines[i]) && !isTableSep(lines[i])) {
              body.push(`<tr>${cells(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`);
              i++;
            }
            i--;
            out.push(`<table class="md-table">${head}<tbody>${body.join("")}</tbody></table>`);
            continue;
          }
          const h = line.match(/^\s*(#{1,4})\s+(.+)$/);
          if (h) {
            flushPara();
            out.push(`<div class="md-h lv${h[1].length}">${renderInline(h[2])}</div>`);
            continue;
          }
          if (/^\s*([-*_])\s*\1\s*\1[\s-]*$/.test(line) || /^\s*-{3,}\s*$/.test(line)) {
            flushPara();
            out.push('<hr class="md-hr">');
            continue;
          }
          // esc 后 > 会变成 &gt;，两种写法都要识别
          const quote = line.match(/^\s*(?:>|&gt;)\s?(.*)$/);
          if (quote) {
            flushPara();
            out.push(`<div class="md-quote">${renderInline(quote[1])}</div>`);
            continue;
          }
          const ul = line.match(/^\s*[-*+]\s+(.+)$/);
          if (ul) {
            flushPara();
            out.push(`<div class="md-li">${renderInline(ul[1])}</div>`);
            continue;
          }
          const ol = line.match(/^\s*(\d+)[.)]\s+(.+)$/);
          if (ol) {
            flushPara();
            out.push(`<div class="md-li num">${ol[1]}. ${renderInline(ol[2])}</div>`);
            continue;
          }
          para.push(line);
        }
        flushPara();
        if (inCode && codeBuf.length) out.push(codeBlock());
        return out.join("");
      }
      // 滚动聊天区到底部；instant=true 时临时关闭 smooth，立即定位（用于恢复历史/切 tab）
      function scrollChatToBottom(instant = false) {
        const box = $("#chatMsgs");
        if (!box) return;
        if (instant) {
          const prev = box.style.scrollBehavior;
          box.style.scrollBehavior = "auto";
          box.scrollTop = box.scrollHeight;
          // 下一帧恢复 smooth，避免影响后续发送消息的平滑滚动
          requestAnimationFrame(() => { box.style.scrollBehavior = prev; });
        } else {
          box.scrollTop = box.scrollHeight;
        }
      }
      function addMsg(role, text, isError = false, hidx = null) {
        const div = document.createElement("div");
        div.className =
          "msg " +
          (role === "user" ? "user" : "ai" + (isError ? " error" : ""));
        div.textContent = text;
        // 用户消息里若带附件，把【附件：xxx】前缀拆成小标签，正文只留提问
        if (role === "user" && typeof text === "string" && text.startsWith("【附件：")) {
          const parsed = splitAttachPrefix(text);
          if (parsed.files.length) {
            div.textContent = "";
            const bar = document.createElement("div");
            bar.className = "msg-files";
            parsed.files.forEach((f) => {
              const tag = document.createElement("span");
              tag.className = "msg-file";
              tag.title = f.name;
              const label = document.createElement("span");
              label.textContent = f.name;
              tag.appendChild(label);
              bar.appendChild(tag);
            });
            div.appendChild(bar);
            div.appendChild(document.createTextNode(parsed.text));
          }
        }
        if (hidx !== null) {
          div.dataset.hidx = hidx;
          attachMsgActions(div, role);
        }
        $("#chatMsgs").appendChild(div);
        $("#chatMsgs").scrollTop = $("#chatMsgs").scrollHeight;
        return div;
      }
      function attachMsgActions(div, role) {
        const bar = document.createElement("div");
        bar.className = "msg-actions";
        bar.innerHTML =
          role === "user"
            ? '<button data-mact="edit">编辑</button>'
            : '<button data-mact="copy">复制</button><button data-mact="retry">重试</button>';
        div.appendChild(bar);
      }
      function copyText(s) {
        if (!s) return;
        const done = () => toast("已复制", 1500);
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(s).then(done).catch(() => fallbackCopy(s, done));
        } else {
          fallbackCopy(s, done);
        }
      }
      function fallbackCopy(s, done) {
        const ta = document.createElement("textarea");
        ta.value = s;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); }
        catch (e) { toast("复制失败，请手动选择文本", 2500, "error"); }
        ta.remove();
      }
      let chatBusy = false;
      const CHAT_WELCOME =
        "你好，我是知序。课程、考试和个人日程都可以问我；也可以直接说「明天下午三点开会」或「记住我喜欢坐前排」。要读课件、作业要求、表格，点输入框左边的回形针传文件给我就行。";

      // 编辑重发：从某条消息起截断历史，再按窗口重渲染
      function truncateChat(k) {
        if (k < chatHistory.length) chatHistory.length = k;
        renderChatWindow();
      }
      function startEditMsg(div, hidx) {
        const original = chatHistory[hidx]?.content;
        if (original == null) return;
        // 编辑只改提问正文，附件前缀留着，重发时仍能带上文件
        const parsed = splitAttachPrefix(original);
        div.classList.add("editing");
        div.innerHTML = `<textarea>${esc(parsed.text)}</textarea>
          <div class="msg-edit-actions">
            <button class="small" data-edit="no">取消</button>
            <button class="small primary" data-edit="ok">重发</button>
          </div>`;
        const ta = div.querySelector("textarea");
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
      }
      function cancelEditMsg(div, hidx) {
        div.classList.remove("editing");
        renderChatWindow();
      }
      function retryFrom(hidx) {
        const userMsg = chatHistory[hidx - 1];
        if (!userMsg || userMsg.role !== "user") return;
        truncateChat(hidx);
        sendChat(userMsg.content, { resend: true });
      }
      // 失败时在对话流里留一张明确的错误卡片，而不是让提示一闪而过
      function renderChatError(bubble, message, partial) {
        bubble.classList.remove("thinking", "streaming");
        // renderAiContent 会挂一组小按钮，这里移除以免出现两个「重试」
        const oldBar = bubble.querySelector(".msg-actions");
        if (oldBar) oldBar.remove();
        const card = document.createElement("div");
        card.className = "chat-error";
        const head = document.createElement("div");
        head.className = "chat-error-head";
        head.textContent = partial ? "回答中断" : "发送失败";
        const msg = document.createElement("div");
        msg.className = "chat-error-msg";
        msg.textContent = message || "网络或模型服务暂时不可用";
        const actions = document.createElement("div");
        actions.className = "chat-error-actions";
        const retry = document.createElement("button");
        retry.className = "small";
        retry.type = "button";
        retry.textContent = "重试";
        retry.addEventListener("click", () => {
          const hidx = Number(bubble.dataset.hidx);
          if (Number.isInteger(hidx)) retryFrom(hidx);
        });
        actions.appendChild(retry);
        card.append(head, msg, actions);
        bubble.appendChild(card);
      }
      $("#chatMsgs").addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        // 代码块复制按钮可能出现在还没有历史序号的流式气泡里，先单独处理
        if (btn.dataset.mact === "copy-code") {
          const code = btn.closest(".md-code")?.querySelector("pre code");
          if (code) copyText(code.textContent);
          return;
        }
        if (chatBusy) return;
        if (btn.id === "chatMoreBtn") {
          const box = $("#chatMsgs");
          const prevH = box.scrollHeight;
          const prevT = box.scrollTop;
          chatRenderLimit += CHAT_WINDOW;
          renderChatWindow();
          // 保持视觉锚点：新加载的消息撑高多少，就把滚动位置下移多少
          box.scrollTop = box.scrollHeight - prevH + prevT;
          return;
        }
        if (btn.dataset.followup) {
          sendChat(btn.dataset.followup);
          return;
        }
        const div = btn.closest(".msg");
        if (!div || div.dataset.hidx === undefined) return;
        const hidx = parseInt(div.dataset.hidx, 10);
        if (btn.dataset.mact === "copy") {
          const m = chatHistory[hidx];
          if (m) copyText(stripPendingMarkers(m.content).trim());
        } else if (btn.dataset.mact === "edit") startEditMsg(div, hidx);
        else if (btn.dataset.mact === "retry") retryFrom(hidx);
        else if (btn.dataset.edit === "no") cancelEditMsg(div, hidx);
        else if (btn.dataset.edit === "ok") {
          const text = div.querySelector("textarea").value.trim();
          if (!text) return;
          const keep = splitAttachPrefix(chatHistory[hidx]?.content || "").files;
          truncateChat(hidx);
          sendChat(text, { attachFiles: keep });
        }
      });

      // 对话落库：每轮结束整体回存（客户端为准），刷新后从服务器恢复
      function persistChat() {
        api("/api/chat/history", {
          method: "POST",
          body: JSON.stringify({ messages: chatHistory }),
        }).catch(() => {});
      }
      // 长对话只渲染最近 N 条，更早的按需向上加载，避免整页越拉越长
      const CHAT_WINDOW = 30;
      let chatRenderLimit = CHAT_WINDOW;
      function renderChatWindow() {
        const box = $("#chatMsgs");
        box.innerHTML = "";
        if (!chatHistory.length) {
          box.innerHTML = `<div class="msg ai">${CHAT_WELCOME}</div>`;
          return;
        }
        const hidden = Math.max(0, chatHistory.length - chatRenderLimit);
        if (hidden > 0) {
          const more = document.createElement("button");
          more.type = "button";
          more.id = "chatMoreBtn";
          more.className = "chat-more";
          more.textContent = `加载更早的消息（还有 ${hidden} 条）`;
          box.appendChild(more);
        }
        chatHistory.slice(hidden).forEach((m, i) => {
          const hidx = hidden + i;
          if (m.role === "user") {
            addMsg("user", m.content, false, hidx);
          } else {
            const bubble = addMsg("ai", "", false, hidx);
            renderAiContent(bubble, m.content, true);
          }
        });
      }
      async function loadChatHistory() {
        const box = $("#chatMsgs");
        // 首次进入时欢迎语还在，加载期间用骨架承接，弱网下不会像卡住
        const isFirst = !chatHistory.length;
        if (isFirst) box.innerHTML = skeletonRows(3);
        try {
          const r = await api("/api/chat/history");
          const msgs = r.messages || [];
          if (!msgs.length) { if (isFirst) renderChatWindow(); return; }
          chatHistory = msgs;
          chatRenderLimit = CHAT_WINDOW;
          renderChatWindow();
          // 所有消息渲染完后滚到底，避免最后一条 AI 长回复把位置顶回中间
          requestAnimationFrame(() => scrollChatToBottom(true));
        } catch (e) {
          if (isFirst) renderChatWindow();   // 失败也恢复欢迎语，别把骨架留在页面上
        }
      }
      // AI 回复末尾的 [追问] ["...", "..."] → 可点击选项；正文不暴露控制标记
      function extractFollowups(text) {
        const m = (text || "").match(/\[追问\]\s*(\[[^\n]*\])/);
        if (!m) return { text, followups: [] };
        let items = [];
        try {
          const parsed = JSON.parse(m[1]);
          if (Array.isArray(parsed)) {
            items = parsed
              .filter((x) => typeof x === "string")
              .map((x) => x.trim().replace(/\s+/g, " ").slice(0, 40))
              .filter(Boolean)
              .slice(0, 3);
          }
        } catch (e) {}
        return { text: text.replace(m[0], "").trim(), followups: items };
      }
      function fallbackFollowups(text, flags = {}) {
        if (flags.proposal)
          return ["调整这条日程", "查看我最近的安排", "明天还有什么课？"];
        if (flags.memory)
          return ["我明天有什么安排？", "这周哪天没课？", "我最近有什么考试？"];
        if (/考试|考场|复习/.test(text))
          return ["最近的考试是哪一场？", "帮我安排考前复习", "考试当天还有什么课？"];
        if (/下周|大下周/.test(text))
          return ["再往后一周呢？", "下周哪天没课？", "下周第一节课是什么？"];
        if (/明天|明日/.test(text))
          return ["后天呢？", "帮我安排明天的自习", "我下一节课在哪？"];
        if (/本周|这周|课表|课程/.test(text))
          return ["这周哪天没课？", "我明天有什么课？", "下一节课在哪里？"];
        return ["我明天有什么安排？", "这周哪天没课？", "我最近有什么考试？"];
      }
      function appendFollowups(bubble, items) {
        if (!items || !items.length) return;
        const box = document.createElement("div");
        box.className = "reply-followups";
        const label = document.createElement("span");
        label.className = "reply-followups-label";
        label.textContent = "继续问";
        box.appendChild(label);
        items.slice(0, 3).forEach((question) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "reply-followup";
          btn.dataset.followup = question;
          btn.textContent = question;
          box.appendChild(btn);
        });
        bubble.appendChild(box);
      }
      // 渲染一条 AI 回复：动作卡片、正文 Markdown 与推荐追问统一组装
      function renderAiContent(bubble, acc, fromHistory = false) {
        const { text: ta, added: eventAdded } = extractEventAdded(acc);
        const { text: t1, proposal } = extractEventProposal(ta);
        const { text: t2, memory } = extractMemory(t1);
        const { text: t3, push } = extractPush(t2);
        const { text: cleanText, followups } = extractFollowups(t3);
        const bodyText = cleanText || (proposal ? "为你整理了一条日程，确认后存入："
          : eventAdded ? "已为你添加到日程："
          : push ? "正在把你的消息发到手机：" : "已为你整理好，可以继续这样问：");
        bubble.innerHTML = renderReply(bodyText);
        attachMsgActions(bubble, "ai");
        if (eventAdded) appendEventAddedCard(bubble, eventAdded);
        if (proposal) appendEventProposal(bubble, proposal, acc);
        if (memory) appendMemoryChip(bubble, memory, fromHistory);
        if (push) appendPushChip(bubble, push, fromHistory);
        appendFollowups(bubble, followups.length ? followups : fallbackFollowups(bodyText, {
          proposal, memory,
        }));
      }

      function pushChannelName(channel) {
        return channel === "bark" ? "Bark" : channel === "wecom" ? "企业微信" : channel;
      }
      // [推送] {json} 标记 → 通过 Bark / 企业微信把消息发到手机；历史回放不重发
      function extractPush(text) {
        const m = text.match(/\[推送\]\s*(\{[^\n]*\})/);
        if (!m) return { text, push: null };
        try {
          const obj = JSON.parse(m[1]);
          const title = typeof obj.title === "string" ? obj.title.trim().slice(0, 60) : "";
          const body = typeof obj.body === "string" ? obj.body.trim().slice(0, 1000) : "";
          if (body) {
            const channels = Array.isArray(obj.channels)
              ? obj.channels.filter((c) => c === "bark" || c === "wecom")
              : [];
            return {
              text: text.replace(m[0], "").trim(),
              push: { title, body, channels },
            };
          }
        } catch (e) {}
        return { text, push: null };
      }
      async function appendPushChip(bubble, push, fromHistory = false) {
        const chip = document.createElement("div");
        chip.className = "memory-chip push-chip";
        const label = push.title || push.body.slice(0, 20);
        if (fromHistory) {
          chip.innerHTML = `<span>已推送到手机：${esc(label)}</span>`;
          bubble.appendChild(chip);
          return;
        }
        chip.innerHTML = "<span>正在推送到手机…</span>";
        bubble.appendChild(chip);
        try {
          const r = await api("/api/push/send", {
            method: "POST",
            body: JSON.stringify(push),
          });
          const ok = r.results.filter((x) => x.ok).map((x) => pushChannelName(x.channel));
          const bad = r.results.filter((x) => !x.ok);
          if (ok.length) {
            chip.innerHTML = `<span>已推送到 ${esc(ok.join("、"))}${
              bad.length
                ? `；<span class="push-err">${esc(bad.map((x) => x.error).join("；"))}</span>`
                : ""
            }</span>`;
          } else {
            chip.innerHTML = `<span class="push-err">推送失败：${esc(
              bad.map((x) => x.error).join("；") || "未知错误",
            )}</span>`;
          }
        } catch (e) {
          chip.innerHTML = `<span class="push-err">推送失败：${esc(e.message)}</span>`;
        }
      }

      // [日程已添加] {json}：卡片确认保存后回写进历史，刷新时只显示结果，
      // 不再出现可重复点击的「添加到日程」按钮（避免刷新后重复添加）
      function extractEventAdded(text) {
        const m = text.match(/\[日程已添加\]\s*(\{[^\n]*\})/);
        if (!m) return { text, added: null };
        try {
          return { text: text.replace(m[0], "").trim(), added: JSON.parse(m[1]) };
        } catch (e) {
          return { text: text.replace(m[0], "").trim(), added: {} };
        }
      }
      function appendEventAddedCard(bubble, added) {
        const card = document.createElement("div");
        card.className = "event-proposal";
        const when = [added.date, added.start].filter(Boolean).join(" ");
        card.innerHTML = `<div class="ep-head">已添加到日程</div>
          <div class="ep-title">${esc(added.title || "日程")}</div>
          ${when ? `<div class="ep-meta">${esc(when)}</div>` : ""}
          <div class="ep-done">已添加</div>`;
        bubble.appendChild(card);
      }
      // AI 回复末尾的 [添加日程] {json} 标记 → 确认卡片，用户点击后才真正保存
      function extractEventProposal(text) {
        const m = text.match(/\[添加日程\]\s*(\{[^\n]*\})/);
        if (!m) return { text, proposal: null };
        try {
          const obj = JSON.parse(m[1]);
          if (obj && typeof obj.title === "string" && obj.title.trim()) {
            const str = (v) => (typeof v === "string" ? v.trim() : "");
            return {
              text: text.replace(m[0], "").trim(),
              proposal: {
                title: str(obj.title).slice(0, 100),
                date: str(obj.date),
                start: str(obj.start),
                end: str(obj.end),
                location: str(obj.location).slice(0, 100),
                note: str(obj.note).slice(0, 500),
              },
            };
          }
        } catch (e) {}
        return { text, proposal: null };
      }
      // [记住] {json} 标记 → 自动存入长期记忆，可撤销
      function extractMemory(text) {
        const m = text.match(/\[记住\]\s*(\{[^\n]*\})/);
        if (!m) return { text, memory: null };
        try {
          const obj = JSON.parse(m[1]);
          if (obj && typeof obj.content === "string" && obj.content.trim()) {
            return {
              text: text.replace(m[0], "").trim(),
              memory: obj.content.trim().slice(0, 200),
            };
          }
        } catch (e) {}
        return { text, memory: null };
      }
      async function appendMemoryChip(bubble, content, alreadySaved = false) {
        const chip = document.createElement("div");
        chip.className = "memory-chip";
        chip.innerHTML = `<span>已记住：${esc(content)}</span><button class="small" data-mem="undo">撤销</button>`;
        bubble.appendChild(chip);
        let memId = null;
        if (!alreadySaved) {
          try {
            const r = await api("/api/memories", {
              method: "POST",
              body: JSON.stringify({ action: "add", content }),
            });
            memId = r.memory.id;
          } catch (e) {
            chip.innerHTML = `<span>记忆未保存：${esc(e.message)}</span>`;
            return;
          }
        }
        chip.addEventListener("click", async (e) => {
          if (!e.target.closest('button[data-mem="undo"]')) return;
          try {
            if (!memId) {
              const r = await api("/api/memories");
              const found = r.memories.find((m) => m.content === content);
              if (!found) {
                chip.innerHTML = "<span>这条记忆已不存在</span>";
                return;
              }
              memId = found.id;
            }
            await api("/api/memories", {
              method: "POST",
              body: JSON.stringify({ action: "delete", id: memId }),
            });
            chip.innerHTML = "<span>已撤销这条记忆</span>";
          } catch (err) {
            toast(err.message, 4000, "error");
          }
        });
      }
      function appendEventProposal(bubble, p, acc = "") {
        const card = document.createElement("div");
        card.className = "event-proposal";
        const when = p.date
          ? p.date + (p.start ? " " + p.start + (p.end ? "–" + p.end : "") : " 全天")
          : "日期待定";
        card.innerHTML = `
          <div class="ep-head">添加到个人日程</div>
          <div class="ep-title">${esc(p.title)}</div>
          <div class="ep-meta">${esc(when)}${p.location ? " · " + esc(p.location) : ""}</div>
          ${p.note ? `<div class="ep-note">${esc(p.note)}</div>` : ""}
          <div class="ep-actions">
            <button class="small primary" data-ep="ok">添加到日程</button>
            <button class="small" data-ep="no">忽略</button>
          </div>`;
        bubble.appendChild(card);
        card.addEventListener("click", async (e) => {
          const btn = e.target.closest("button[data-ep]");
          if (!btn) return;
          if (btn.dataset.ep === "no") {
            card.remove();
            return;
          }
          btn.disabled = true;
          try {
            await api("/api/events", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action: "add", event: p }),
            });
            card.querySelector(".ep-actions").outerHTML =
              '<div class="ep-done">已添加到日程</div>';
            toast("日程已添加");
            // 回写对话历史：把 [添加日程] 换成 [日程已添加]，刷新后只显示结果卡片，
            // 不会再出现可重复点击的按钮（否则刷新一次就能重复添加同一条日程）
            const hidx = parseInt(bubble.dataset.hidx, 10);
            if (Number.isInteger(hidx) && chatHistory[hidx]?.role === "assistant"
                && chatHistory[hidx].content === acc) {
              const mark = acc.match(/\[添加日程\]\s*\{[^\n]*\}/);
              if (mark) {
                chatHistory[hidx].content = acc.replace(
                  mark[0],
                  "[日程已添加] " + JSON.stringify({
                    title: p.title, date: p.date || "", start: p.start || "",
                    end: p.end || "",
                  }),
                );
                persistChat();
              }
            }
            await Promise.all([loadStatus(), loadToday()]);
          } catch (err) {
            btn.disabled = false;
            toast(err.message, 4000, "error");
          }
        });
      }
      /* ---------- AI 附件：上传、待发送列表、随消息发送 ---------- */
      const ATTACH_MAX_FILES = 5;
      const ATTACH_MAX_MB = 20;
      let chatFiles = [];      // 待发送附件 [{id,name,size,kind,chars,note,uploading,failed}]
      let attachUploading = 0;

      function fmtFileSize(n) {
        if (!n && n !== 0) return "";
        if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
        if (n >= 1024) return Math.round(n / 1024) + " KB";
        return n + " B";
      }
      function renderAttachChips() {
        const box = $("#chatAttachList");
        if (!box) return;
        box.innerHTML = "";
        chatFiles.forEach((f, i) => {
          const chip = document.createElement("div");
          chip.className = "chat-file" +
            (f.uploading ? " uploading" : f.failed ? " failed" : "");
          chip.title = f.failed ? `${f.name}：${f.error || "读取失败"}`
            : [f.name, f.note, f.chars ? `${f.chars} 字` : ""]
              .filter(Boolean).join(" · ");
          const status = f.uploading ? "读取中…" : f.failed ? "失败" : fmtFileSize(f.size);
          chip.innerHTML = `<span class="cf-name">${esc(f.name)}</span>
            <span class="cf-size">${esc(status)}</span>
            <button type="button" aria-label="移除附件" title="移除">×</button>`;
          chip.querySelector("button").addEventListener("click", () => {
            const [gone] = chatFiles.splice(i, 1);
            if (gone && gone.id) {
              api(`/api/chat/attachments/${gone.id}`, { method: "DELETE" }).catch(() => {});
            }
            renderAttachChips();
          });
          box.appendChild(chip);
        });
      }
      async function uploadAttach(file) {
        if (!file) return;
        if (attachUploading + chatFiles.filter((f) => !f.failed).length >= ATTACH_MAX_FILES) {
          toast(`一次最多带 ${ATTACH_MAX_FILES} 个文件`, 3500);
          return;
        }
        const name = file.name || `粘贴的图片-${Date.now()}.png`;
        if (file.size > ATTACH_MAX_MB * 1024 * 1024) {
          toast(`${name} 有 ${fmtFileSize(file.size)}，超过 ${ATTACH_MAX_MB} MB 上限`, 4500);
          return;
        }
        const item = { name, size: file.size, uploading: true };
        chatFiles.push(item);
        attachUploading += 1;
        renderAttachChips();
        try {
          const r = await api(`/api/chat/upload?name=${encodeURIComponent(name)}`, {
            method: "POST",
            headers: { "Content-Type": file.type || "application/octet-stream" },
            body: file,
          });
          Object.assign(item, r, { uploading: false, failed: false });
        } catch (e) {
          item.uploading = false;
          item.failed = true;
          item.error = e.message || "读取失败";
          toast(`${name} 读取失败：${item.error}`, 6000, "error");
        } finally {
          attachUploading -= 1;
          renderAttachChips();
        }
      }
      // 历史里的用户消息带上附件名（含内部 id），方便刷新/重试时还原
      function attachPrefix(list) {
        if (!list.length) return "";
        return "【附件：" + list.map((f) => `${f.name}#${f.id}`).join("、") + "】\n";
      }
      function splitAttachPrefix(text) {
        const m = /^【附件：([^】]*)】\n?/.exec(text || "");
        if (!m) return { files: [], text: text || "" };
        const files = m[1].split("、").map((s) => s.trim()).filter(Boolean)
          .map((s) => {
            const hit = /^(.*)#([0-9a-f]{12})$/.exec(s);
            return hit ? { name: hit[1], id: hit[2] } : { name: s, id: "" };
          });
        return { files, text: (text || "").slice(m[0].length) };
      }
      function bindAttachInputs() {
        const btn = $("#chatAttachBtn");
        const input = $("#chatFileInput");
        if (btn && input) {
          btn.addEventListener("click", () => input.click());
          input.addEventListener("change", async () => {
            const files = Array.from(input.files || []);
            input.value = "";   // 允许重复选同一个文件
            for (const f of files) await uploadAttach(f);
          });
        }
        const chatInput = $("#chatInput");
        if (chatInput) {
          chatInput.addEventListener("paste", (e) => {
            const files = Array.from(e.clipboardData?.files || []);
            if (!files.length) return;
            e.preventDefault();
            files.forEach((f) => uploadAttach(f));
          });
        }
        const box = document.querySelector("#tab-ai .chat-box");
        if (box) {
          let depth = 0;
          const hasFiles = (e) =>
            Array.from(e.dataTransfer?.types || []).includes("Files");
          box.addEventListener("dragenter", (e) => {
            if (!hasFiles(e)) return;
            e.preventDefault();
            depth += 1;
            box.classList.add("dragging");
          });
          box.addEventListener("dragover", (e) => {
            if (!hasFiles(e)) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
          });
          box.addEventListener("dragleave", (e) => {
            if (!hasFiles(e)) return;
            depth = Math.max(0, depth - 1);
            if (!depth) box.classList.remove("dragging");
          });
          box.addEventListener("drop", (e) => {
            if (!hasFiles(e)) return;
            e.preventDefault();
            depth = 0;
            box.classList.remove("dragging");
            Array.from(e.dataTransfer.files || [])
              .slice(0, ATTACH_MAX_FILES).forEach((f) => uploadAttach(f));
          });
        }
      }
      bindAttachInputs();

      async function sendChat(text, opts = {}) {
        if (chatBusy) return;
        text = (text || $("#chatInput").value).trim();
        let attachIds = [];
        let histContent = text;
        if (opts.resend) {
          // 重发：从历史内容里把附件 id 还原出来，能带就把文件一起再发一遍
          const parsed = splitAttachPrefix(text);
          attachIds = parsed.files.map((f) => f.id).filter(Boolean);
          text = parsed.text.trim();
        } else if (opts.attachFiles && opts.attachFiles.length) {
          // 编辑已有消息后重发：沿用原来的附件
          const keep = opts.attachFiles.filter((f) => f.id);
          attachIds = keep.map((f) => f.id);
          histContent = attachPrefix(keep) + text;
        } else {
          if (attachUploading) { toast("文件还在读取中，稍等一下再发送", 3000); return; }
          const ready = chatFiles.filter((f) => f.id && !f.failed);
          if (!text && !ready.length) return;
          if (!text) text = "帮我看看这个文件。";
          attachIds = ready.map((f) => f.id);
          histContent = attachPrefix(ready) + text;
          chatFiles = chatFiles.filter((f) => f.failed);
          renderAttachChips();
        }
        if (!text) text = "帮我看看这个文件。";
        $("#chatInput").value = "";
        // 重发模式：用户消息已在历史里，只补一条新的 AI 回复
        if (!opts.resend) {
          chatHistory.push({ role: "user", content: histContent });
          addMsg("user", histContent, false, chatHistory.length - 1);
        }
        chatBusy = true;
        $("#chatSend").disabled = true;
        const bubble = addMsg("ai", "正在思考…");
        bubble.classList.add("thinking");
        let acc = "";
        // 流式渲染按帧合并，用户向上翻阅时不强行拉回底部
        const nearBottom = () => {
          const box = $("#chatMsgs");
          return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        };
        let rafId = 0;
        // 节流：整段 Markdown 每帧重建是 O(n²)，120ms 一次足够顺滑且大幅省 CPU
        const scheduleRender = () => {
          if (rafId) return;
          rafId = setTimeout(() => {
            rafId = 0;
            const pinned = nearBottom();
            bubble.innerHTML = renderReply(stripPendingMarkers(acc));
            if (pinned) $("#chatMsgs").scrollTop = $("#chatMsgs").scrollHeight;
          }, 120);
        };
        try {
          const resp = await fetch("/api/chat/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message: text,
              history: chatHistory.slice(0, -1),
              attachments: attachIds,
            }),
          });
          if (!resp.ok) {
            let detail = `请求失败（${resp.status}）`;
            try {
              detail = (await resp.json()).detail || detail;
            } catch (e) {}
            throw new Error(detail);
          }
          bubble.classList.remove("thinking");
          bubble.classList.add("streaming");
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const evt = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              const line = evt
                .split("\n")
                .find((l) => l.startsWith("data:"));
              if (!line) continue;
              let payload;
              try {
                payload = JSON.parse(line.slice(5).trim());
              } catch (e) {
                continue;
              }
              if (payload.error) throw new Error(payload.error);
              if (payload.delta) {
                acc += payload.delta;
                scheduleRender();
              }
            }
          }
          if (!acc) throw new Error("AI 没有返回内容");
          chatHistory.push({ role: "assistant", content: acc });
          bubble.dataset.hidx = chatHistory.length - 1;
          renderAiContent(bubble, acc);
        } catch (e) {
          if (acc) {
            chatHistory.push({ role: "assistant", content: acc });
            bubble.dataset.hidx = chatHistory.length - 1;
            renderAiContent(bubble, acc);
            renderChatError(bubble, `已生成部分内容后中断：${e.message}`, true);
          } else {
            bubble.classList.add("error");
            bubble.textContent = "";
            // 占位序号：编辑更早的消息时这条错误气泡也会被清掉；重试仍可用
            bubble.dataset.hidx = chatHistory.length;
            renderChatError(bubble, e.message, false);
          }
        } finally {
          // 取消待执行的流式帧，避免它在最终卡片渲染之后覆盖掉卡片
          if (rafId) { clearTimeout(rafId); rafId = 0; }
          bubble.classList.remove("streaming", "thinking");
          chatBusy = false;
          $("#chatSend").disabled = false;
          $("#chatMsgs").scrollTop = $("#chatMsgs").scrollHeight;
          persistChat();
        }
      }

      /* ---------- 清空对话与长期记忆管理 ---------- */
      $("#chatClearBtn").addEventListener("click", async () => {
        if (chatBusy) return;
        if (!chatHistory.length) { toast("对话已经是空的"); return; }
        if (!(await confirmDialog("清空全部对话？长期记忆会保留。", { ok: "清空", danger: true }))) return;
        try {
          await api("/api/chat/clear", { method: "POST" });
          chatHistory = [];
          chatRenderLimit = CHAT_WINDOW;
          $("#chatMsgs").innerHTML = `<div class="msg ai">${CHAT_WELCOME}</div>`;
          toast("对话已清空");
        } catch (e) { toast(e.message, 4000, "error"); }
      });
      async function loadMemories() {
        const box = $("#memoryList");
        showSkeleton(box, 2);
        try {
          const r = await api("/api/memories");
          $("#memoryCount").textContent = `${r.memories.length} / 50 条`;
          box.innerHTML = r.memories.length
            ? r.memories.map((m) => `<div class="memory-row" data-id="${esc(m.id)}">
                <div>
                  <div class="m-content">${esc(m.content)}</div>
                  <div class="m-date">${esc((m.created_at || "").slice(0, 10))}</div>
                </div>
                <button class="small" data-mem-del>删除</button>
              </div>`).join("")
            : `<div class="empty"><div class="empty-title">还没有长期记忆</div><div class="empty-sub">对知序说「记住我……」，或在上方手动添加。</div></div>`;
        } catch (e) {
          box.innerHTML = `<div class="empty"><div class="empty-title">记忆加载失败</div><div class="empty-sub">${esc(e.message)}</div></div>`;
        }
      }
      function closeMemorySheet() {
        $("#memorySheet").classList.add("hidden");
      }
      $("#memoryBtn").addEventListener("click", async () => {
        $("#memorySheet").classList.remove("hidden");
        $("#memoryError").textContent = "";
        try { await loadMemories(); } catch (e) { $("#memoryError").textContent = e.message; }
      });
      $("#memoryCloseBtn").addEventListener("click", closeMemorySheet);
      $("#memorySheet").addEventListener("click", (e) => {
        if (e.target.id === "memorySheet") closeMemorySheet();
      });
      $("#memoryAddBtn").addEventListener("click", async () => {
        const content = $("#memoryInput").value.trim();
        if (!content) return;
        $("#memoryError").textContent = "";
        try {
          await api("/api/memories", {
            method: "POST",
            body: JSON.stringify({ action: "add", content }),
          });
          $("#memoryInput").value = "";
          await loadMemories();
          toast("已记住");
        } catch (e) { $("#memoryError").textContent = e.message; }
      });
      bindImeSafeEnter($("#memoryInput"), () => $("#memoryAddBtn").click());
      $("#memoryList").addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-mem-del]");
        if (!btn) return;
        const row = btn.closest(".memory-row");
        if (!(await confirmDialog("删除这条记忆？", { ok: "删除", danger: true }))) return;
        try {
          await api("/api/memories", {
            method: "POST",
            body: JSON.stringify({ action: "delete", id: row.dataset.id }),
          });
          await loadMemories();
        } catch (err) { toast(err.message, 4000, "error"); }
      });
      /* ---------- 日志（已移除独立页，保留空实现避免引用报错） ---------- */
      async function loadLogs() {}

      /* ---------- 设置 ---------- */
      function setNested(obj, path, value) {
        const parts = path.split(".");
        let cur = obj;
        parts.slice(0, -1).forEach((p) => {
          cur[p] = cur[p] || {};
          cur = cur[p];
        });
        cur[parts[parts.length - 1]] = value;
      }
      function getNested(obj, path) {
        return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
      }

      async function loadSettings() {
        const cfg = await api("/api/settings");
        $$("#tab-settings [name]").forEach((el) => {
          const v = getNested(cfg, el.name);
          if (v !== undefined && v !== null)
            el.value = typeof v === "object" ? JSON.stringify(v) : v;
        });
        $("#pushNextDay").checked = !!cfg.push_next_day;
        $("#alertOnFail").checked = cfg.alert_on_fail !== false;
        $("#dailyCourses").checked = cfg.push?.daily_courses !== false;
        $("#dailyEvents").checked = cfg.push?.daily_events !== false;
        $("#dailyWeather").checked = !!cfg.push?.daily_weather;
        $("#barkEnabled").checked = !!cfg.push?.bark_enabled;
        $("#wecomEnabled").checked = !!cfg.push?.wecom_enabled;
        $("#reminderEnabled").checked = cfg.push?.reminder_enabled !== false;
        $("#weatherEnabled").checked = cfg.weather?.enabled !== false;
        $("#examReviewEnabled").checked = cfg.push?.exam_review_enabled !== false;
        $("#aiEnabled").checked = !!cfg.ai?.enabled;
        $("#aiShowInNav").checked = cfg.ai?.show_in_nav !== false;
        // 载入的是服务器已保存值，不算用户改动：隐藏保存条并清除 dirty
        setSettingsDirty(false);
        if (typeof layoutSettingsColumns === "function") requestAnimationFrame(layoutSettingsColumns);
      }
      /* ---------- 保存条：仅在有未保存改动时显示（避免滚动时常驻突兀） ---------- */
      function setSettingsDirty(on) {
        const bar = document.querySelector("#tab-settings > .form-actions");
        if (!bar) return;
        bar.classList.toggle("hidden", !on);
        bar.classList.toggle("dirty", !!on);
      }
      /* ---------- 设置表单内联校验（Webhook 规则与后端 push.wecom_input_error 一致） ---------- */
      const CONFIG_MASK = "********";
      const WECOM_HINT = "在企业微信群里点右上角「…」→ 群机器人 → 添加机器人，复制形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=… 的地址";
      const BARE_KEY_RE = /^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{64})$/;
      function wecomInputError(v) {
        v = (v || "").trim();
        if (!v || v === CONFIG_MASK) return "";
        if (v.includes("://")) {
          let u;
          try { u = new URL(v); } catch (e) { return ""; }
          const host = (u.hostname || "").toLowerCase();
          if (host === "work.weixin.qq.com" || host === "open.work.weixin.qq.com")
            return `这是企业微信管理后台的网页地址，不是推送地址。${WECOM_HINT}`;
          if (host === "qyapi.weixin.qq.com" && !(u.searchParams.get("key") || "").trim())
            return `地址里缺少 key 参数。${WECOM_HINT}`;
          return "";
        }
        return BARE_KEY_RE.test(v) ? "" :
          `只填 key 时必须是 Webhook 里的 key（36 位带横线），当前值既不是完整地址也不是 key。${WECOM_HINT}`;
      }
      function fieldError(el) {
        const v = (el.value || "").trim();
        // 掩码表示沿用服务器已保存的值，不参与校验
        if (!v || v === CONFIG_MASK) return "";
        if (el.name === "push.wecom_webhook") return wecomInputError(v);
        if (el.name === "push.bark_server" && !/^https?:\/\//i.test(v))
          return "Bark 服务器地址需以 http:// 或 https:// 开头";
        if (el.name === "push.reminder_advance") {
          const n = Number(v);
          if (!Number.isFinite(n) || n < 1 || n > 180)
            return "提前提醒需在 1–180 分钟之间";
        }
        return "";
      }
      function setFieldError(el, msg) {
        const wrap = el.closest("label.field") || el.parentElement;
        let tip = wrap.querySelector(".field-error");
        el.classList.toggle("invalid", !!msg);
        if (msg) el.setAttribute("aria-invalid", "true");
        else el.removeAttribute("aria-invalid");
        if (!msg) { if (tip) tip.remove(); return; }
        if (!tip) {
          tip = document.createElement("p");
          tip.className = "field-error";
          wrap.appendChild(tip);
        }
        tip.textContent = msg;
      }
      function validateSettings() {
        let first = null;
        $$("#tab-settings [name]").forEach((el) => {
          const msg = fieldError(el);
          setFieldError(el, msg);
          if (msg && !first) first = el;
        });
        // 开关已启用但必填项为空（掩码算已配置）
        [["#barkEnabled", '[name="push.bark_key"]', "已启用 Bark，请先填写 Bark Key"],
         ["#wecomEnabled", '[name="push.wecom_webhook"]', "已启用群机器人，请先填写 Webhook 地址"]]
          .forEach(([toggle, sel, msg]) => {
            const el = $(sel);
            if (el && $(toggle)?.checked && !(el.value || "").trim()) {
              setFieldError(el, msg);
              if (!first) first = el;
            }
          });
        if (first) {
          toast("请先修正标红的设置项", 3000, "warn");
          first.focus();
        }
        return !first;
      }
      $$("#tab-settings [name]").forEach((el) => {
        el.addEventListener("input", () => setFieldError(el, fieldError(el)));
        el.addEventListener("blur", () => setFieldError(el, fieldError(el)));
      });
      // 任意表单控件变动都标记为未保存（事件委托，覆盖文本框/下拉/复选框）
      (function bindSettingsDirty() {
        const root = document.getElementById("tab-settings");
        if (!root) return;
        const onChange = (e) => {
          const t = e.target;
          // 主题开关即时生效、存在本机浏览器，既不算改动也不清掉其他字段的未保存状态
          if (t.closest && t.closest("#themeSwitches")) return;
          if (t.closest("button")) return; // 普通按钮点击不算改动
          setSettingsDirty(true);
        };
        root.addEventListener("input", onChange);
        root.addEventListener("change", onChange);
      })();
      function collectSettings() {
        const cfg = {};
        $$("#tab-settings [name]").forEach((el) => {
          let v = el.value;
          if (el.type === "number") v = v === "" ? null : Number(v);
          setNested(cfg, el.name, v);
        });
        cfg.push_next_day = $("#pushNextDay").checked;
        cfg.alert_on_fail = $("#alertOnFail").checked;
        cfg.push = cfg.push || {};
        cfg.push.daily_courses = $("#dailyCourses").checked;
        cfg.push.daily_events = $("#dailyEvents").checked;
        cfg.push.daily_weather = $("#dailyWeather").checked;
        cfg.push.bark_enabled = $("#barkEnabled").checked;
        cfg.push.wecom_enabled = $("#wecomEnabled").checked;
        cfg.push.reminder_enabled = $("#reminderEnabled").checked;
        cfg.weather = cfg.weather || {};
        cfg.weather.enabled = $("#weatherEnabled").checked;
        cfg.push.exam_review_enabled = $("#examReviewEnabled").checked;
        cfg.ai = cfg.ai || {};
        cfg.ai.enabled = $("#aiEnabled").checked;
        cfg.ai.show_in_nav = $("#aiShowInNav").checked;
        return cfg;
      }
      function toggleProviderFields() {
        // 已迁移到「课表来源」页，保留空实现避免旧引用报错
      }
      async function saveSettings(syncAfter = false) {
        if (!validateSettings()) return;
        try {
          await api("/api/settings", {
            method: "POST",
            body: JSON.stringify(collectSettings()),
          });
          toast("设置已保存");
          setSettingsDirty(false);
          await loadStatus();
          weatherData = null;   // 位置可能改过，重新取一次天气
          loadWeather({ force: true });
          if (syncAfter) await doSync();
          else {
            await loadToday();
            if (!$("#tab-week").classList.contains("hidden")) {
              await loadWeek(viewWeek);
              await loadExams();
            }
          }
        } catch (e) {
          toast(e.message, 4000, "error");
        }
      }
      async function doSync() {
        $("#syncBtn").disabled = true;
        toast("正在同步课表与考试…", 1800);
        try {
          const r = await api("/api/sync", { method: "POST" });
          toast(r.message, 4000);
          await Promise.all([loadStatus(), loadToday()]);
          if (!$("#tab-week").classList.contains("hidden")) {
            await loadWeek(viewWeek);
            await loadExams();
          }
        } catch (e) {
          toast(e.message, 4000, "error");
        } finally {
          $("#syncBtn").disabled = false;
        }
      }

      /* ---------- 课表来源 ---------- */
      function fillZfYears(saved) {
        // 教务学年按起始年份存（2026 = 2026-2027 学年），8 月开学后进入新学年
        const now = new Date();
        const m = now.getMonth() + 1;
        const current = m >= 8 ? now.getFullYear() : now.getFullYear() - 1;
        const years = [];
        for (let y = current + 1; y >= current - 4; y--) years.push(String(y));
        if (saved && !years.includes(saved)) years.unshift(saved);
        $("#zfYear").innerHTML = years
          .map(
            (y) =>
              `<option value="${y}">${y}-${Number(y) + 1}${y === String(current) ? "（当前学年）" : ""}</option>`,
          )
          .join("");
        $("#zfYear").value = saved || String(current);
      }
      async function loadSources() {
        try {
          const s = await api("/api/provider/sources");
          const badge = (el, ok, onText, offText) => {
            el.textContent = ok ? onText : offText;
            el.classList.toggle("real", !!ok);
          };
          badge($("#portalBadge"), s.portal.configured, "已配置", "未配置");
          badge($("#zfBadge"), s.zhengfang.configured, "已配置", "未配置");
          // 回填正方表单（密码不回显，留空表示未改）
          $("#zfBase").value = s.zhengfang.base || "https://jxgl.qut.edu.cn/jwglxt";
          $("#zfUsername").value = s.zhengfang.username || "";
          fillZfYears(s.zhengfang.year || "");
          $("#zfTerm").value = (s.zhengfang.term || "3");
          // 门户表单回填 cURL 占位信息
          if (s.portal.url) {
            $("#curlInput").placeholder = (s.portal.url === "********")
              ? "已配置课表接口（地址已隐藏，粘贴新 cURL 可覆盖）"
              : "已配置：" + s.portal.url;
          }
          renderPrimaryState(s);
        } catch (e) {
          toast(e.message, 4000, "error");
        }
      }

      // 课表主数据源：正方（教务原始排课）/ 门户（含临时调课）/ 自动（正方优先）
      function renderPrimaryState(s) {
        const seg = $("#primarySeg");
        if (!seg) return;
        const active = ["zhengfang", "portal"].includes(s.primary) ? s.primary : "auto";
        $$("#primarySeg button").forEach((b) => {
          const on = b.dataset.primary === active;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        const cn = { zhengfang: "正方教务", portal: "融合门户", mock: "演示数据" };
        const chosen = { zhengfang: "正方教务", portal: "融合门户", auto: "自动（正方优先）" }[active];
        const resolved = cn[s.resolved] || s.resolved || "未配置";
        const snap = cn[s.snapshot_source] || s.snapshot_source || "未同步";
        $("#primaryState").textContent =
          `当前选择：${chosen} · 实际生效：${resolved} · 课表快照来自：${snap}`;
      }
      let primaryBusy = false;
      $("#primarySeg").addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-primary]");
        if (!btn || primaryBusy || btn.classList.contains("active")) return;
        primaryBusy = true;
        $$("#primarySeg button").forEach((b) => (b.disabled = true));
        const label = btn.textContent;
        try {
          const r = await api("/api/provider/primary", {
            method: "POST",
            body: JSON.stringify({ primary: btn.dataset.primary }),
          });
          if (r.ok) toast(`已切换到${label}：${r.message || "课表已更新"}`, 4500);
          else toast(r.message || "切换失败，请先配置该数据源", 5000, "warn");
          await loadSources();
          await loadStatus();
          await loadToday();
          await loadWeek(viewWeek ?? Math.max(status?.week || 1, 1));
          await loadExams();
        } catch (err) {
          toast(err.message, 4500, "error");
        } finally {
          primaryBusy = false;
          $$("#primarySeg button").forEach((b) => (b.disabled = false));
        }
      });


      /* ---------- 用户管理 ---------- */
      const ACTION_CN = {
        set_role: "调整角色", set_active: "启用/停用", delete_user: "删除用户",
        reset_password: "重置密码", edit_user: "编辑信息", create_invite: "生成邀请码",
        create_invite_batch: "批量生成邀请码", delete_invite: "撤销邀请码",
        batch_active: "批量启用/停用",
      };
      function fmtTime(s) {
        return s ? s.slice(0, 16) : "—";
      }
      async function loadAdminStats() {
        if (!currentUser || currentUser.role !== "admin") return;
        try {
          const s = await api("/api/auth/admin-stats");
          const items = [
            ["总用户", s.total], ["活跃", s.active], ["已停用", s.inactive],
            ["管理员", s.admin], ["本周新增", s.new_week],
          ];
          $("#adminStats").innerHTML = items
            .map(([l, n]) => `<div class="stat-box"><div class="num">${n}</div><div class="lbl">${l}</div></div>`)
            .join("");
        } catch (e) { toast(e.message, 4000, "error"); }
      }
      async function loadAdminUsers() {
        if (!currentUser || currentUser.role !== "admin") return;
        try {
          const q = ($("#adminSearch") || {}).value || "";
          const sort = ($("#adminSort") || {}).value || "id";
          const us = await api(`/api/auth/users?q=${encodeURIComponent(q)}&sort=${sort}`);
          const rows = us.users.map((u) => {
            const me = u.id === currentUser.id;
            const inactive = u.is_active === 0 || u.is_active === false;
            return `
              <div class="admin-user-row" data-uid="${u.id}">
                <div class="admin-user-info">
                  <div><input type="checkbox" class="admin-user-check" data-uid="${u.id}" ${me ? "disabled" : ""} />
                    <b>${esc(u.username)}</b>${u.role === "admin" ? ' <span class="badge real">管理员</span>' : ""}${inactive ? ' <span class="badge">已停用</span>' : ""}${me ? ' <span class="badge">我</span>' : ""}</div>
                  <div class="muted" style="font-size:12px">${esc(u.display_name || "—")} · 学号 ${esc(u.student_id || "—")}</div>
                  <div class="muted" style="font-size:11px">注册 ${fmtTime(u.created_at)} · 最后登录 ${fmtTime(u.last_login_at) || "从未登录"}</div>
                </div>
                <div class="admin-user-actions">
                  <button data-act="edit" data-id="${u.id}">编辑</button>
                  <button data-act="toggle-active" data-id="${u.id}" ${me ? "disabled" : ""}>${inactive ? "启用" : "停用"}</button>
                  <button data-act="role" data-id="${u.id}" data-role="${u.role}" ${me ? "disabled" : ""}>${u.role === "admin" ? "降为普通" : "设为管理"}</button>
                  <button data-act="reset-pwd" data-id="${u.id}">重置密码</button>
                  <button data-act="delete" data-id="${u.id}" class="danger" ${me ? "disabled" : ""}>删除</button>
                </div>
              </div>`;
          });
          $("#adminUserList").innerHTML = rows.length
            ? rows.join("")
            : '<div class="empty">暂无用户</div>';
        } catch (e) {
          toast(e.message, 4000, "error");
        }
      }
      async function loadAdminSources() {
        if (!currentUser || currentUser.role !== "admin") return;
        try {
          const r = await api("/api/auth/users/source-status");
          const rows = r.users.map((u) => {
            let pill;
            if (!u.portal_configured && !u.zhengfang_configured)
              pill = '<span class="source-pill pill-none">未配置</span>';
            else if (u.resolved === "portal")
              pill = '<span class="source-pill pill-portal">门户</span>';
            else if (u.resolved === "zhengfang")
              pill = '<span class="source-pill pill-zhengfang">正方</span>';
            else
              pill = `<span class="source-pill pill-none">${esc(u.resolved)}</span>`;
            const snap = u.snapshot_source
              ? `<span class="muted" style="font-size:11px">快照来源 ${esc(u.snapshot_source)} · 同步 ${fmtTime(u.fetched_at)}</span>`
              : '<span class="muted" style="font-size:11px">尚未同步课表</span>';
            return `<div class="source-row">
              <div><b>${esc(u.display_name || u.username)}</b> <span class="muted" style="font-size:12px">(${esc(u.username)})</span> ${pill}</div>
              <div>${snap}</div>
            </div>`;
          });
          $("#adminSourceList").innerHTML = rows.length
            ? rows.join("")
            : '<div class="empty">暂无用户</div>';
        } catch (e) { toast(e.message, 4000, "error"); }
      }
      async function loadAdminLog() {
        if (!currentUser || currentUser.role !== "admin") return;
        try {
          const r = await api("/api/auth/admin-log");
          const rows = r.logs.map((l) => {
            const act = ACTION_CN[l.action] || l.action;
            const target = l.target ? ` → ${esc(l.target)}` : "";
            const detail = l.detail ? ` <span class="muted">(${esc(l.detail)})</span>` : "";
            return `<div class="log-row">
              <span class="log-action">${esc(act)}</span>${target}${detail}
              <div class="muted" style="font-size:11px">操作人 ${esc(l.admin_name)} · ${fmtTime(l.ts)}</div>
            </div>`;
          });
          $("#adminLogList").innerHTML = rows.length
            ? rows.join("")
            : '<div class="empty">暂无操作记录</div>';
          // 折叠区：更新条数，并按实际高度决定是否显示「展开全部」
          const cnt = $("#adminLogCount");
          if (cnt) cnt.textContent = rows.length ? `${rows.length} 条` : "";
          setAdminLogExpanded(false);
          const box = $("#adminLogCollapse");
          const toggle = $("#adminLogToggle");
          if (box && toggle) {
            requestAnimationFrame(() => {
              const list = $("#adminLogList");
              const over = !!list && list.scrollHeight > box.clientHeight + 2;
              box.classList.toggle("more", over);
              toggle.classList.toggle("hidden", !over);
            });
          }
        } catch (e) { toast(e.message, 4000, "error"); }
      }
      /* 审计日志折叠/展开 */
      function setAdminLogExpanded(on) {
        const box = $("#adminLogCollapse");
        if (!box) return;
        box.classList.toggle("expanded", !!on);
        box.classList.toggle("collapsed", !on);
        const toggle = $("#adminLogToggle");
        if (toggle) toggle.setAttribute("aria-expanded", on ? "true" : "false");
        const label = $("#adminLogToggleText");
        if (label) label.textContent = on ? "收起" : "展开全部";
        if (!on) box.scrollTop = 0;
      }
      const adminLogToggleEl = $("#adminLogToggle");
      if (adminLogToggleEl) {
        adminLogToggleEl.addEventListener("click", () => {
          const box = $("#adminLogCollapse");
          const willExpand = !(box && box.classList.contains("expanded"));
          setAdminLogExpanded(willExpand);
          if (willExpand && box) {
            // 展开后若列表比视口高，温和滚到可见位置（仅当顶部已划出视口）
            const top = box.getBoundingClientRect().top;
            if (top < 0) box.scrollIntoView({ block: "start", behavior: "smooth" });
          }
        });
      }

      /* ---------- 外观主题（深色模式开关 + 自动切换开关） ---------- */
      const THEME_KEY = "qut.theme";
      const themeDarkToggle = document.getElementById("themeDarkToggle");
      const themeAutoToggle = document.getElementById("themeAutoToggle");
      function readThemeChoice() {
        try {
          const v = localStorage.getItem(THEME_KEY);
          return ["system", "light", "dark"].includes(v) ? v : "system";
        } catch (e) { return "system"; }
      }
      function systemPrefersDark() {
        return window.matchMedia("(prefers-color-scheme: dark)").matches;
      }
      function effectiveDark(choice) {
        return choice === "dark" || (choice === "system" && systemPrefersDark());
      }
      /** 开关状态始终反映"当前实际效果"，自动模式下深色开关随系统实时变化 */
      function syncThemeSwitches(choice) {
        if (themeDarkToggle) themeDarkToggle.checked = effectiveDark(choice);
        if (themeAutoToggle) themeAutoToggle.checked = choice === "system";
      }
      function applyTheme(choice) {
        const dark = effectiveDark(choice);
        const el = document.getElementById("darkThemeCss");
        if (el) el.disabled = !dark;
        document.documentElement.dataset.theme = dark ? "dark" : "light";
        document.documentElement.style.colorScheme = dark ? "dark" : "light";
        const meta = document.getElementById("themeColorMeta");
        if (meta) meta.content = dark ? "#1c1c1e" : "#f5f5f7";
        syncThemeSwitches(choice);
      }
      function setThemeChoice(choice) {
        try { localStorage.setItem(THEME_KEY, choice); } catch (err) {}
        applyTheme(choice);
      }
      // 深色开关：手动指定浅色 / 深色（自动切换会自动让位）
      if (themeDarkToggle) {
        themeDarkToggle.addEventListener("change", () => {
          setThemeChoice(themeDarkToggle.checked ? "dark" : "light");
        });
      }
      // 自动切换：打开则跟随系统；关闭则固化为当前看到的样子，避免闪一下
      if (themeAutoToggle) {
        themeAutoToggle.addEventListener("change", () => {
          if (themeAutoToggle.checked) setThemeChoice("system");
          else setThemeChoice(systemPrefersDark() ? "dark" : "light");
        });
      }
      // 自动模式下，系统外观变化要即时跟随（含老浏览器 addListener 兜底）
      const themeMq = window.matchMedia("(prefers-color-scheme: dark)");
      const onSystemThemeChange = () => {
        if (readThemeChoice() === "system") applyTheme("system");
      };
      if (themeMq.addEventListener) themeMq.addEventListener("change", onSystemThemeChange);
      else if (themeMq.addListener) themeMq.addListener(onSystemThemeChange);
      applyTheme(readThemeChoice());

      /* ---------- AI 助手主页分块显隐 ---------- */
      // 关闭「在主页显示 AI 助手」后，只隐藏主页导航里的 AI 助手分块；
      // 设置页的 AI 入口（PC 侧栏 + 手机 iOS 首页）必须始终可见，
      // 否则用户关了之后再也进不去 AI 设置页、无法重新打开这个开关。
      // 若当前正停在 AI 页，则自动切回「今日」，避免停在已隐藏的页面上。
      function applyAiNavVisibility() {
        const aiNavBtn = document.querySelector('#nav button[data-tab="ai"]');
        const show = status && status.ai_show_in_nav !== false;
        if (aiNavBtn) aiNavBtn.classList.toggle("hidden", !show);
        if (!show) {
          const saved = readSavedTab();
          if (saved === "ai") {
            saveTab("today");
            switchTab("today");
          }
        }
      }

      /* ---------- Tab 切换 ---------- */
      // 记住当前页面，刷新后停留在原 tab（按浏览器本地保存，不含任何隐私数据）
      const TAB_KEY = "qut.currentTab";
      const TAB_IDS = ["today", "week", "rooms", "ai", "source", "admin", "settings"];
      function readSavedTab() {
        try {
          const t = localStorage.getItem(TAB_KEY);
          return TAB_IDS.includes(t) && $("#tab-" + t) ? t : null;
        } catch (e) { return null; }
      }
      function saveTab(tab) {
        try { localStorage.setItem(TAB_KEY, tab); } catch (e) {}
      }
      // 页面加载失败时给出可见提示，避免切页后永远停在骨架屏（原来是无 catch 的裸调用）
      const safeLoad = (p) => Promise.resolve(p).catch((e) => {
        console.error(e);
        if (typeof toast === "function") toast("加载失败：" + ((e && e.message) || "网络异常"));
      });
      function switchTab(tab) {
        saveTab(tab);
        // 移动端底部导航切页后回到顶部，避免停在上一页的滚动位置
        if (window.matchMedia("(max-width: 720px)").matches) window.scrollTo(0, 0);
        $$("#nav button").forEach((b) => {
          const on = b.dataset.tab === tab;
          b.classList.toggle("active", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
          b.tabIndex = on ? 0 : -1;
        });
        ["today", "week", "ai", "source", "admin", "settings"].forEach((t) =>
          $("#tab-" + t).classList.toggle("hidden", t !== tab),
        );
        if (tab === "today") safeLoad(loadToday());
        if (tab === "week") {
          safeLoad(loadWeek(viewWeek ?? Math.max(status?.week || 1, 1)));
          safeLoad(loadExams());
        }
        if (tab === "source") safeLoad(loadSources());
        if (tab === "admin") {
          safeLoad(loadAdminStats());
          safeLoad(loadAdminUsers());
          safeLoad(loadAdminSources());
          safeLoad(loadAdminLog());
          safeLoad(refreshInvites());
        }
        if (tab === "settings") {
          safeLoad(loadSettings());
          safeLoad(loadPushLog());
          // macOS(PC)：默认落在"外观"分区；iOS(手机)：回到设置首页列表
          if (typeof showSettingsHome === "function") {
            if (isPhoneLayout()) showSettingsHome();
            else showSettingsSection("settingsAppearance");
          }
          // 设置页从 hidden 变可见后才能量到吸顶栏的真实高度，补一次同步
          queueSyncStickyMetrics();
        }
        // 切到 AI 页时滚到底部：历史在页面加载时（容器还隐藏）就渲染了，
        // 那时的滚底无效，需等 tab 可见、布局完成后重新定位到最后一条消息。
        if (tab === "ai") {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => scrollChatToBottom(true));
          });
        }
      }

      /* ---------- 接口诊断展示 ---------- */
      const KIND_CN = {
        courses: "周课表",
        events: "日程事件流",
        unknown: "未识别",
        empty: "无数组",
      };
      function formatAnalysis(a) {
        if (!a) return "";
        const lines = [
          "",
          `【响应诊断】类型：${KIND_CN[a.kind] || a.kind}`,
          a.hint || "",
          `顶层字段：${(a.top_keys || []).join(", ")}`,
        ];
        (a.arrays || []).forEach((x, i) => {
          lines.push(
            `候选数组 ${i + 1}：${x.path}（${x.count} 条）字段：${(x.keys || []).join(", ")}`,
          );
        });
        lines.push(
          `建议路径：${a.suggested_path || "(根数组)"}`,
          `建议映射：${JSON.stringify(a.mapping || {})}`,
        );
        return lines.join("\n");
      }
      function formatWarnings(w) {
        return w && w.length
          ? "\n【注意】\n" + w.map((x) => "- " + x).join("\n")
          : "";
      }

      /* ---------- 一键登录门户 ---------- */
      let browserLoginTimer = null;
      const LOGIN_STATE_CN = {
        idle: "",
        starting: "正在启动浏览器",
        waiting_login: "请用微信扫描二维码登录",
        analyzing: "正在分析接口",
        done: "完成",
        error: "出错",
        cancelled: "已取消",
      };
      // 窄屏（手机）只讲人话：二维码 + 一句状态，不展示接口地址等技术细节
      const NARROW_STATE_CN = {
        starting: "正在准备登录二维码…",
        waiting_login: "请用微信扫描下方二维码登录",
        analyzing: "已扫码，正在读取课表…",
        done: "已接入课表",
        cancelled: "已取消",
      };
      function isNarrowScreen() {
        return window.matchMedia("(max-width: 720px)").matches;
      }
      // 去掉文本里的 http(s) 链接，并清理因此残留的空括号 / 重复标点
      function stripUrls(text) {
        return String(text || "")
          .replace(/https?:\/\/[^\s，。；、）)】]+/g, "")
          .replace(/[（(]\s*[)）]/g, "")
          .replace(/\s{2,}/g, " ")
          .replace(/^[\s，、；:：·-]+|[\s，、；:：·-]+$/g, "")
          .trim();
      }
      // 手机上的失败文案：去掉技术前缀与英文堆栈，只留一句能看懂的
      function narrowError(msg) {
        const t = stripUrls(msg).replace(/^[^：:]{0,12}登录失败[：:]\s*/, "").trim();
        if (/timeout|timed out|exceeded|time out/i.test(t))
          return "登录超时，请重新扫码";
        return t ? `登录失败：${t}` : "登录失败，请重试";
      }
      function stopBrowserLoginPolling() {
        if (browserLoginTimer) {
          clearInterval(browserLoginTimer);
          browserLoginTimer = null;
        }
      }
      function renderBrowserLogin(s) {
        const running = ["starting", "waiting_login", "analyzing"].includes(
          s.status,
        );
        $("#browserLoginBtn").disabled = running;
        $("#browserLoginStopBtn").classList.toggle("hidden", !running);
        const label = LOGIN_STATE_CN[s.status] || s.status;
        const narrow = isNarrowScreen();
        let stateText;
        if (narrow) {
          stateText =
            s.status === "error"
              ? narrowError(s.message)
              : NARROW_STATE_CN[s.status] ||
                stripUrls(s.message) ||
                label;
        } else {
          stateText =
            s.message && s.message !== label
              ? `${label} — ${s.message}`
              : label;
        }
        $("#browserLoginState").textContent = stateText;
        const logEl = $("#browserLoginLog");
        if (s.log && s.log.length) {
          logEl.classList.remove("hidden");
          logEl.textContent = s.log.join("\n");
        }
        const qrEl = $("#qrImage");
        // 只有在等待扫码/识别阶段才显示二维码，登录结束（成功/失败/取消）就收起，避免遗留一张失效的码
        if (s.qr_image && running) {
          qrEl.src = "data:image/png;base64," + s.qr_image;
          qrEl.classList.remove("hidden");
        } else {
          qrEl.classList.add("hidden");
          qrEl.removeAttribute("src");
        }
        let extra = "";
        if (s.status === "done" && s.result) {
          if (s.result.ok) {
            extra = narrow
              ? `已接入课表接口（${s.result.method || "GET"}）` +
                (s.result.message ? `\n${stripUrls(s.result.message)}` : "")
              : `已写入数据源：${s.result.url}（${s.result.method}）\n数组路径：${s.result.list_path || "(根数组)"}\n${s.result.message}`;
            if (s.result.warnings && s.result.warnings.length)
              extra +=
                "\n" +
                s.result.warnings
                  .map((w) => (narrow ? stripUrls(w) : w))
                  .join("\n");
          } else {
            extra = narrow
              ? `登录未完成：${stripUrls(s.result.message || "")}`
              : s.result.message || "";
          }
        }
        $("#browserLoginResult").textContent = extra;
        return running;
      }
      async function pollBrowserLogin() {
        let s;
        try {
          s = await api("/api/provider/browser-login/status");
        } catch (e) {
          stopBrowserLoginPolling();
          return;
        }
        const running = renderBrowserLogin(s);
        if (running) {
          if (!browserLoginTimer)
            browserLoginTimer = setInterval(pollBrowserLogin, 1500);
        } else {
          stopBrowserLoginPolling();
          if (s.status === "done") {
            await loadSettings();
            await loadStatus();
            await loadToday();
          }
        }
      }

      /* ---------- 事件绑定 ---------- */
      // 中文/日文等输入法组词时，回车只用于把候选词上屏，不能触发发送/添加。
      // 三重判断覆盖不同浏览器：isComposing、IME 期间的 keyCode 229、
      // 以及 compositionstart/end 维护的状态（部分浏览器事件顺序不一致）。
      function bindImeSafeEnter(el, action) {
        let composing = false;
        let justComposed = false;
        el.addEventListener("compositionstart", () => { composing = true; });
        el.addEventListener("compositionend", () => {
          composing = false;
          // compositionend 可能早于确认用的那次 keydown，延后一帧再放行，
          // 避免刚上屏的英文被误当成发送；之后正常再按回车不受影响。
          justComposed = true;
          setTimeout(() => { justComposed = false; }, 0);
        });
        el.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          if (e.isComposing || e.keyCode === 229 || composing || justComposed) return;
          e.preventDefault();
          action();
        });
      }
      $("#nav").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-tab]");
        if (btn) switchTab(btn.dataset.tab);
      });
      // tablist 的方向键导航：配合 roving tabindex，键盘也能切换页面
      $("#nav").addEventListener("keydown", (e) => {
        const tabs = [...$$("#nav button[role=tab]")]
          .filter((b) => !b.classList.contains("hidden"));
        const i = tabs.indexOf(document.activeElement);
        if (i < 0) return;
        let next = -1;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % tabs.length;
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") next = 0;
        else if (e.key === "End") next = tabs.length - 1;
        else return;
        e.preventDefault();
        tabs[next].focus();
        switchTab(tabs[next].dataset.tab);
      });
      $("#syncBtn").addEventListener("click", doSync);
      $("#examList").addEventListener("click", (e) => {
        if (!e.target.closest("#examToSettings")) return;
        switchTab("source");
        setTimeout(() => {
          const f = $("#zfUsername");
          if (f) {
            f.scrollIntoView({ block: "center" });
            f.focus();
          }
        }, 60);
      });
      $("#prevWeek").addEventListener("click", () =>
        loadWeek(Math.max(1, viewWeek - 1)),
      );
      $("#nextWeek").addEventListener("click", () =>
        loadWeek(Math.min(status?.total_weeks || 30, viewWeek + 1)),
      );
      $("#weekLabel").addEventListener("click", () => {
        if (!$("#weekLabel").classList.contains("clickable")) return;
        loadWeek(Math.max(weekRender?.current_week || status?.week || 1, 1));
      });
      $("#nextClassBar").addEventListener("click", () => switchTab("week"));
      $("#tbTomorrow").addEventListener("click", () => {
        const block = $("#tomorrowBlock");
        if (!block || block.classList.contains("hidden")) return;
        block.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      // 时间轴课程行：点击展开/收起详情；点「在周课表中查看」直达周视图
      function bindTimelineClicks(listSel) {
        $(listSel).addEventListener("click", (e) => {
          const goWeek = e.target.closest('button[data-act="go-week"]');
          if (goWeek) {
            e.stopPropagation();
            switchTab("week");
            return;
          }
          const row = e.target.closest(".course-row.course-clickable");
          if (!row) return;
          const extra = row.querySelector(".course-extra");
          if (!extra) return;
          const willOpen = extra.classList.contains("hidden");
          extra.classList.toggle("hidden", !willOpen);
          row.classList.toggle("expanded", willOpen);
        });
      }
      bindTimelineClicks("#todayList");
      bindTimelineClicks("#tomorrowList");
      setInterval(() => {
        // 页面不可见时不做无意义的重建（移动端长期驻留后台的省电优化）
        if (document.hidden) return;
        if (weekData && !$("#tab-today").classList.contains("hidden")) renderNextClass();
      }, 30000);
      $("#chatSend").addEventListener("click", () => sendChat());
      bindImeSafeEnter($("#chatInput"), () => sendChat());
      $("#chatChips").addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        if (btn.dataset.prefill) {
          $("#chatInput").value = btn.dataset.prefill;
          $("#chatInput").focus();
          return;
        }
        sendChat(btn.textContent.trim());
      });
      $("#importCurlBtn").addEventListener("click", async () => {
        const text = $("#curlInput").value.trim();
        if (!text) {
          toast("请先粘贴 cURL");
          return;
        }
        $("#importCurlBtn").disabled = true;
        toast("正在导入并试拉取…", 2000);
        try {
          const r = await api("/api/provider/import-curl", {
            method: "POST",
            body: JSON.stringify({ curl: text }),
          });
          const pre = $("#importPreview");
          pre.classList.remove("hidden");
          const head = r.ok
            ? `导入成功：解析出 ${r.course_count} 门课程\n数组路径：${r.list_path || "(根数组)"}\n字段映射：${JSON.stringify(r.mapping)}\n首门课程：${JSON.stringify(r.sample, null, 2)}`
            : `导入未完全成功：${r.error}\n已保存接口地址与请求头，可调整下方字段后点「预览当前接口响应」。`;
          pre.textContent =
            head +
            formatWarnings(r.warnings) +
            formatAnalysis(r.analysis) +
            `\n\n响应预览（不含 Cookie，可复制发给助手调字段）：\n${r.raw_preview}`;
          toast(
            r.ok ? `导入成功，${r.course_count} 门课程` : r.error || "导入失败",
            5000,
          );
          await loadSettings();
          await loadStatus();
          await loadToday();
        } catch (e) {
          toast(e.message, 5000, "error");
        } finally {
          $("#importCurlBtn").disabled = false;
        }
      });
      $("#browserLoginBtn").addEventListener("click", async () => {
        try {
          const r = await api("/api/provider/browser-login/start", {
            method: "POST",
          });
          if (!r.ok) {
            toast(r.message, 6000, "error");
            return;
          }
          toast(r.message, 4000);
          $("#browserLoginState").textContent = "正在启动浏览器…";
          pollBrowserLogin();
        } catch (e) {
          toast(e.message, 5000, "error");
        }
      });
      $("#browserLoginStopBtn").addEventListener("click", async () => {
        try {
          await api("/api/provider/browser-login/stop", { method: "POST" });
          toast("已请求取消");
        } catch (e) {
          toast(e.message, 4000, "error");
        }
      });

      /* ---------- 可插拔模型服务 ---------- */
      const AI_PRESETS = {
        qwen: {
          base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
          model: "qwen-plus",
        },
        deepseek: {
          base_url: "https://api.deepseek.com/v1",
          model: "deepseek-chat",
        },
        ollama: { base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
      };
      function showAiDiag(t) {
        const el = $("#aiDiag");
        el.classList.remove("hidden");
        el.textContent = t;
      }
      async function saveThenProbe(path, method) {
        await api("/api/settings", {
          method: "POST",
          body: JSON.stringify(collectSettings()),
        });
        return api(path, { method });
      }
      $$("[data-ai-preset]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const p = AI_PRESETS[btn.dataset.aiPreset];
          if (!p) return;
          $('[name="ai.base_url"]').value = p.base_url;
          $('[name="ai.model"]').value = p.model;
          toast("已填入预设，记得点保存", 2500);
        }),
      );
      $("#aiModelsBtn").addEventListener("click", async () => {
        try {
          const r = await saveThenProbe("/api/ai/models", "GET");
          showAiDiag(
            r.models?.length
              ? `可用模型（${r.models.length}）：\n${r.models.join("\n")}\n\n复制想要的模型名填到「模型」框即可。`
              : "该服务未返回模型列表（部分网关不提供 /models 接口）。",
          );
          toast("模型探测完成", 2500, "success");
        } catch (e) {
          showAiDiag("探测失败：" + e.message);
          toast(e.message, 5000, "error");
        }
      });
      $("#aiTestBtn").addEventListener("click", async () => {
        try {
          const r = await saveThenProbe("/api/ai/test", "POST");
          showAiDiag(`连接正常\n模型：${r.model}\n回复：${r.reply}`);
          toast("模型连接正常", 2500, "success");
        } catch (e) {
          showAiDiag("连接失败：" + e.message);
          toast(e.message, 5000, "error");
        }
      });
      $("#saveSettingsBtn").addEventListener("click", () =>
        saveSettings(false),
      );
      $("#syncNowBtn2").addEventListener("click", () => saveSettings(true));
      $("#zfSaveBtn").addEventListener("click", async () => {
        const btn = $("#zfSaveBtn"),
          out = $("#zfSaveResult");
        btn.disabled = true;
        out.style.color = "var(--sub)";
        out.textContent = "正在保存并登录教务…";
        try {
          const r = await api("/api/provider/zhengfang", {
            method: "POST",
            body: JSON.stringify({
              base: $("#zfBase").value.trim(),
              username: $("#zfUsername").value.trim(),
              password: $("#zfPassword").value,
              year: $("#zfYear").value,
              term: $("#zfTerm").value,
              test: true,
            }),
          });
          out.textContent = r.message;
          out.style.color = r.ok ? "var(--ok)" : "var(--warn)";
          if (r.ok) {
            toast("正方已保存，课表已切换为教务数据", 4500);
            await loadSources();
            await loadStatus();
            await loadToday();
            await loadWeek(viewWeek ?? Math.max(status?.week || 1, 1));
            await loadExams();
          }
        } catch (e) {
          out.textContent = e.message;
          out.style.color = "var(--warn)";
        } finally {
          btn.disabled = false;
        }
      });
      async function loadPushLog() {
        showSkeleton($("#pushLogList"), 3);
        try {
          const r = await api("/api/push/log?limit=8");
          const logs = r.logs || [];
          $("#pushLogList").innerHTML = logs.length
            ? logs
                .map((x) => {
                  const ok = x.status === "ok";
                  const state =
                    ok ? "成功" : x.status === "skip" ? "跳过" : "失败";
                  const detail = ok ? "" : `：${x.error || ""}`;
                  const time = esc((x.ts || "").slice(5, 16));
                  return `<div class="memory-row"><div>
                      <div class="m-content">${esc(x.title || "推送")} · ${esc(pushChannelName(x.channel))}</div>
                      <div class="m-date">${time} · ${
                        ok
                          ? state
                          : `<span class="push-err">${esc(state + detail)}</span>`
                      }</div>
                    </div></div>`;
                })
                .join("")
            : `<div class="empty"><div class="empty-title">还没有推送记录</div><div class="empty-sub">点上面的「测试 Bark 推送」或「测试企业微信推送」试一次。</div></div>`;
        } catch (e) {
          $("#pushLogList").innerHTML = `<div class="hint">读取失败：${esc(e.message)}</div>`;
        }
      }
      async function runPushTest(channel, btn) {
        if (!validateSettings()) return;
        try {
          if (btn) btn.disabled = true;
          await api("/api/settings", {
            method: "POST",
            body: JSON.stringify(collectSettings()),
          });
          const r = await api("/api/push/test", {
            method: "POST",
            body: JSON.stringify(channel ? { channel } : {}),
          });
          const msgs = r.results.map(
            (x) => `${pushChannelName(x.channel)}：${x.ok ? "已发送" : x.error}`,
          );
          toast(msgs.join("；"), 5000);
        } catch (e) {
          toast(e.message, 4000, "error");
        } finally {
          if (btn) btn.disabled = false;
          loadPushLog();
        }
      }
      $("#testPushBtn").addEventListener("click", (e) => runPushTest("bark", e.currentTarget));
      $("#testWecomBtn").addEventListener("click", (e) => runPushTest("wecom", e.currentTarget));
      $("#refreshPushLogBtn").addEventListener("click", () => loadPushLog());

      /* ---------- 设置分区导航：粘性锚点 + 滚动联动 ---------- */
      const settingsNav = $("#settingsNav");
      const mainNav = $("#nav");
      // 设置分区卡片 -> 导航按钮的映射（一个分区可能对应多张卡片）
      const SETTINGS_GROUP_CARDS = {
        settingsAppearance: ["settingsAppearance"],
        settingsAccount: ["settingsAccount"],
        settingsSync: ["settingsSync"],
        settingsPush: ["settingsPush", "settingsBark", "settingsPushLog"],
        settingsAi: ["settingsAi"],
      };
      function isPhoneLayout() { return window.matchMedia("(max-width: 720px)").matches; }

      // macOS(PC) / iOS(手机) 双平台设置导航。卡片 id/name/事件全程保留，仅切换 .section-hidden。
      const settingsRoot = $("#tab-settings");
      const settingsHome = $("#settingsHome");
      const settingsBack = $("#settingsBack");
      const SETTINGS_TITLES = {
        settingsAppearance: "外观", settingsAccount: "账号安全", settingsSync: "同步",
        settingsPush: "推送", settingsAi: "AI 助手",
      };
      let currentSettingsSection = "settingsAppearance";

      // 切到某个分区：显示对应卡片 + 同步两侧导航的选中态
      function showSettingsSection(target) {
        const show = SETTINGS_GROUP_CARDS[target] || [target];
        if (!SETTINGS_GROUP_CARDS[target]) return;
        currentSettingsSection = target;
        document.querySelectorAll("#tab-settings .card").forEach((c) =>
          c.classList.toggle("section-hidden", !show.includes(c.id)));
        settingsNav.querySelectorAll("button[data-target]").forEach((b) =>
          b.classList.toggle("active", b.dataset.target === target));
        // 手机：进入详情页
        if (isPhoneLayout()) {
          settingsRoot.classList.add("detail-mode");
          settingsBack.classList.add("show");
        }
      }
      // 手机：回设置首页
      function showSettingsHome() {
        settingsRoot.classList.remove("detail-mode");
        settingsBack.classList.remove("show");
        settingsNav.querySelectorAll("button[data-target]").forEach((b) => b.classList.remove("active"));
        window.scrollTo({ top: 0, behavior: "auto" });
      }
      // 保留函数名以兼容既有调用点（loadSettings / switchTab / resize）
      function layoutSettingsColumns() {
        if (!isPhoneLayout()) showSettingsSection(currentSettingsSection);
      }
      // PC：点侧栏切分区；手机：侧栏已隐藏，这里不响应
      settingsNav.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-target]");
        if (!btn || isPhoneLayout()) return;
        showSettingsSection(btn.dataset.target);
      });
      // 手机：点 iOS 列表行进详情
      if (settingsHome) {
        settingsHome.addEventListener("click", (e) => {
          const row = e.target.closest(".ios-row");
          if (row) showSettingsSection(row.dataset.target);
        });
      }
      if (settingsBack) settingsBack.addEventListener("click", showSettingsHome);

      // 视口真正跨越手机/桌面断点时：重建到对应平台的默认视图
      let wasPhoneLayout = isPhoneLayout();
      function onSettingsLayoutChange() {
        const nowPhone = isPhoneLayout();
        if (nowPhone === wasPhoneLayout) return; // 地址栏收放等高度变化，忽略
        wasPhoneLayout = nowPhone;
        if (settingsRoot && !settingsRoot.classList.contains("hidden")) {
          if (nowPhone) showSettingsHome();
          else { settingsRoot.classList.remove("detail-mode"); showSettingsSection(currentSettingsSection); }
        }
        queueSyncStickyMetrics();
      }
      window.addEventListener("resize", onSettingsLayoutChange, { passive: true });

      /**
       * 实测两条吸顶栏的高度并写进 :root 的 CSS 变量。
       *
       * 主导航高度不是常量（字号、语言、视口变窄都可能让 tab 换行），
       * 若在 CSS 里写死 45px，一旦换行快捷跳转条就会再次压到主导航上。
       * 因此每次 resize / 旋转屏 / 字体就绪都重新量一次，交给 CSS 算吸顶位置。
       */
      let stickySyncPending = false;
      function syncStickyMetrics() {
        const root = document.documentElement;
        const isMobile = window.matchMedia("(max-width: 720px)").matches;
        // 移动端主导航是底部固定标签栏，与顶部吸顶无关，置 0 让 CSS 的 ≤720px 分支接管
        const navH = mainNav ? Math.round(mainNav.getBoundingClientRect().height) : 0;
        const nextNavH = isMobile ? "0px" : (navH > 0 ? navH + "px" : "");
        if (nextNavH && root.style.getPropertyValue("--mainnav-h") !== nextNavH) {
          root.style.setProperty("--mainnav-h", nextNavH);
        }
        // 快捷条自身高度：设置页处于 hidden（display:none）时量不到，保留上次的合法值
        if (settingsNav && settingsNav.offsetParent !== null) {
          const snH = Math.round(settingsNav.getBoundingClientRect().height);
          const nextSnH = snH > 0 ? snH + "px" : "";
          if (nextSnH && root.style.getPropertyValue("--settingsnav-h") !== nextSnH) {
            root.style.setProperty("--settingsnav-h", nextSnH);
          }
        }
        refreshSettingsObserver();
      }
      function queueSyncStickyMetrics() {
        if (stickySyncPending) return;
        stickySyncPending = true;
        requestAnimationFrame(() => {
          stickySyncPending = false;
          syncStickyMetrics();
        });
      }

      /** 顶部被两条吸顶栏吃掉的像素数，决定分区高亮（IntersectionObserver）的判定上边界 */
      function settingsStickyInset() {
        if (!settingsNav) return 90;
        const top = parseFloat(window.getComputedStyle(settingsNav).top) || 0;
        const h = settingsNav.getBoundingClientRect().height || 0;
        return Math.round(top + h) + 12;
      }

      const SETTINGS_SECTION_IDS = [
        "settingsAppearance", "settingsAccount", "settingsSync", "settingsPush", "settingsAi",
      ];
      let settingsObserver = null;
      let settingsObserverInset = -1;
      /** 分区切换模式下高亮完全由点击驱动，取消滚动观察器，避免隐藏/显示卡片时误改 active */
      function refreshSettingsObserver() {
        if (settingsObserver) { settingsObserver.disconnect(); settingsObserver = null; }
      }
      refreshSettingsObserver();
      syncStickyMetrics();
      window.addEventListener("resize", queueSyncStickyMetrics, { passive: true });
      window.addEventListener("orientationchange", queueSyncStickyMetrics, { passive: true });
      // 主导航自身尺寸变化（换行、字体替换）也要跟着重算
      if (window.ResizeObserver && mainNav) {
        new ResizeObserver(queueSyncStickyMetrics).observe(mainNav);
      }
      // 系统字体可能晚于首屏就绪，字宽变了导航高度就变了
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(queueSyncStickyMetrics).catch(() => {});
      }

      /* ---------- 周视图：更多菜单、日期跳转、滑动翻周、全局搜索 ---------- */
      const weekMenu = $("#weekMenu");
      $("#weekMoreBtn").addEventListener("click", (e) => {
        e.stopPropagation();
        weekMenu.classList.toggle("hidden");
      });
      document.addEventListener("click", (e) => {
        if (!e.target.closest(".week-menu-wrap")) weekMenu.classList.add("hidden");
      });
      weekMenu.addEventListener("click", () => weekMenu.classList.add("hidden"));

      let lastJumpIso = "";
      $("#weekJumpBtn").addEventListener("click", () => {
        const row = $("#weekJump");
        row.classList.toggle("hidden");
        if (!row.classList.contains("hidden")) {
          lastJumpIso = ""; // 重新打开面板时允许再跳同一天
          const base = weekRender?.dates?.[0] || weekData?.today;
          if (base) $("#weekJumpDate").value = base;
          $("#weekJumpDate").focus();
        }
      });
      function mondayOf(iso) {
        const d = new Date(iso + "T00:00:00");
        if (isNaN(d)) return "";
        d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      }
      function jumpToDate(iso) {
        if (!iso) return;
        // 周课表还没渲染好时用今天所在周的周一兜底，不再静默失效
        const now = new Date();
        const localToday =
          `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
        const anchor = weekRender?.dates?.[0] || mondayOf(weekData?.today || localToday);
        const targetMonday = mondayOf(iso);
        if (!anchor || !targetMonday) { toast("课表还没加载好，稍后再试", 3000); return; }
        // 选完日期自动跳一次，再点「转到这一周」只收起面板，不重复请求
        if (iso === lastJumpIso) { $("#weekJump").classList.add("hidden"); return; }
        lastJumpIso = iso;
        const a = new Date(anchor + "T00:00:00");
        const t = new Date(targetMonday + "T00:00:00");
        const diff = Math.round((t - a) / 604800000);
        const total = status?.total_weeks || 30;
        loadWeek(Math.min(Math.max((viewWeek || 1) + diff, 1), total));
        $("#weekJump").classList.add("hidden");
      }
      $("#weekJumpGo").addEventListener("click", () => jumpToDate($("#weekJumpDate").value));
      $("#weekJumpDate").addEventListener("change", (e) => jumpToDate(e.target.value));

      // 横向滑动翻周；网格本身还能横向滚动时，中间的手势留给滚动
      let touchX = 0, touchY = 0, touchActive = false;
      const gridScroll = $("#weekGridScroll");
      gridScroll.addEventListener("touchstart", (e) => {
        if (e.touches.length !== 1) { touchActive = false; return; }
        touchActive = true;
        touchX = e.touches[0].clientX;
        touchY = e.touches[0].clientY;
      }, { passive: true });
      gridScroll.addEventListener("touchcancel", () => { touchActive = false; },
        { passive: true });
      gridScroll.addEventListener("touchend", (e) => {
        if (!touchActive) return;
        touchActive = false;
        const t = e.changedTouches[0];
        const dx = t.clientX - touchX, dy = t.clientY - touchY;
        if (Math.abs(dx) < 64 || Math.abs(dx) < Math.abs(dy) * 1.6) return;
        if (gridScroll.scrollWidth > gridScroll.clientWidth + 4) {
          if (dx < 0 && gridScroll.scrollLeft + gridScroll.clientWidth < gridScroll.scrollWidth - 8) return;
          if (dx > 0 && gridScroll.scrollLeft > 8) return;
        }
        const total = status?.total_weeks || 30;
        loadWeek(Math.min(Math.max((viewWeek || 1) + (dx < 0 ? 1 : -1), 1), total));
      }, { passive: true });

      const SEARCH_KINDS = { course: "课程", event: "日程", exam: "考试" };
      let searchTimer = 0, searchSeq = 0;
      async function runScheduleSearch(q) {
        const box = $("#searchResults");
        const query = q.trim();
        if (!query) { box.classList.add("hidden"); box.innerHTML = ""; return; }
        const seq = ++searchSeq;
        box.classList.remove("hidden");
        box.innerHTML = `<div class="sk-inline">${skeletonRows(2)}</div>`;
        try {
          const r = await api("/api/search?q=" + encodeURIComponent(query));
          if (seq !== searchSeq) return;
          const list = r.results || [];
          box.innerHTML = list.length
            ? list.map((x, i) => `<button type="button" class="sr-item" data-sr="${i}">
                <span class="sr-kind">${SEARCH_KINDS[x.kind] || "结果"}</span>
                <span><span class="sr-title">${esc(x.title)}</span>
                <span class="sr-meta" style="display:block">${esc(
                  [x.meta, x.sub].filter(Boolean).join(" · "))}${
                  x.week ? ` · 第 ${x.week} 周` : ""}</span></span>
              </button>`).join("")
            : `<div class="sr-empty">没有找到「${esc(query)}」相关的课程、日程或考试。</div>`;
          box.dataset.items = JSON.stringify(
            list.map((x) => ({ week: x.week, kind: x.kind })));
        } catch (e) {
          if (seq === searchSeq)
            box.innerHTML = `<div class="sr-empty">搜索失败：${esc(e.message)}</div>`;
        }
      }
      $("#scheduleSearch").addEventListener("input", (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => runScheduleSearch(e.target.value), 320);
      });
      $("#searchResults").addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-sr]");
        if (!btn) return;
        const items = JSON.parse($("#searchResults").dataset.items || "[]");
        const item = items[Number(btn.dataset.sr)];
        if (!item) return;
        const total = status?.total_weeks || 30;
        if (item.week) await loadWeek(Math.min(Math.max(item.week, 1), total));
        const target = item.kind === "exam" ? $("#examList")
          : item.kind === "event" ? $("#weekEventList") : $("#weekGridScroll");
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "start" });
          target.classList.remove("flash");
          void target.offsetWidth;
          target.classList.add("flash");
        }
      });

      /* ---------- 安装到主屏幕：安卓用系统提示，iOS 给手动指引 ---------- */
      let deferredInstall = null;
      const INSTALL_KEY = "agenda.install.dismissed";
      function maybeShowInstallTip() {
        const standalone = window.matchMedia("(display-mode: standalone)").matches
          || window.navigator.standalone === true;
        if (standalone) return;
        try { if (localStorage.getItem(INSTALL_KEY)) return; } catch (e) {}
        const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
        if (!deferredInstall && !ios) return;
        if (ios) {
          $("#installTipText").textContent =
            "用 Safari 打开，点分享 →「添加到主屏幕」，就能像 App 一样使用。";
          $("#installBtn").textContent = "知道了";
        }
        $("#installTip").classList.remove("hidden");
      }
      window.addEventListener("beforeinstallprompt", (e) => {
        e.preventDefault();
        deferredInstall = e;
        maybeShowInstallTip();
      });
      $("#installBtn").addEventListener("click", async () => {
        if (!deferredInstall) {
          // iOS 只有手动指引，点「知道了」也要记下，否则每次刷新都会再弹
          try { localStorage.setItem(INSTALL_KEY, "1"); } catch (e) {}
          $("#installTip").classList.add("hidden");
          return;
        }
        deferredInstall.prompt();
        try { await deferredInstall.userChoice; } catch (e) {}
        deferredInstall = null;
        try { localStorage.setItem(INSTALL_KEY, "1"); } catch (e) {}
        $("#installTip").classList.add("hidden");
      });
      $("#installDismiss").addEventListener("click", () => {
        try { localStorage.setItem(INSTALL_KEY, "1"); } catch (e) {}
        $("#installTip").classList.add("hidden");
      });
      window.addEventListener("appinstalled", () => {
        deferredInstall = null;
        try { localStorage.setItem(INSTALL_KEY, "1"); } catch (e) {}
        $("#installTip").classList.add("hidden");
      });

      /* ---------- 启动 ---------- */
      /* ---------- 多用户：登录 / 注册 / 邀请码 ---------- */
      let currentUser = null;

      function showAuthScreen() {
        $("#authScreen").classList.remove("hidden");
        $("#app").classList.add("hidden");
      }
      function showApp() {
        $("#authScreen").classList.add("hidden");
        $("#app").classList.remove("hidden");
        maybeShowInstallTip();
      }
      function authError(msg) { $("#authError").textContent = msg || ""; }
      function switchAuthTab(which) {
        const reg = which === "register";
        $("#loginForm").classList.toggle("hidden", reg);
        $("#registerForm").classList.toggle("hidden", !reg);
        $("#authTabLogin").classList.toggle("primary", !reg);
        $("#authTabRegister").classList.toggle("primary", reg);
        authError("");
      }
      async function refreshInvites() {
        if (!currentUser || currentUser.role !== "admin") return;
        try {
          const iv = await api("/api/auth/invites");
          const now = new Date().toISOString().slice(0, 19).replace("T", " ");
          const rows = iv.invites.map((x) => {
            const max = x.max_uses || 1;
            const used = x.used_count || 0;
            const left = Math.max(0, max - used);
            const expired = x.expires_at < now;
            let tag, txt;
            if (expired) { tag = "tag-expired"; txt = used ? `已过期 · 用过 ${used}/${max}` : "已过期"; }
            else if (left <= 0) { tag = "tag-used"; txt = `已用完 ${used}/${max}`; }
            else if (max === 1) { tag = "tag-ok"; txt = "可用 · 一次性"; }
            else { tag = "tag-ok"; txt = `可用 · 已用 ${used}/${max}，剩 ${left} 次`; }
            const who = (x.used_by_names && x.used_by_names.length)
              ? ` · ${esc(x.used_by_names.join("、"))}` : "";
            return `<div class="invite-row">
              <code>${esc(x.code)}</code>
              <span class="invite-tag ${tag}">${txt}</span>
              <span class="muted">到期 ${fmtTime(x.expires_at)}${who}</span>
              <button class="small danger" data-invite="${esc(x.code)}">撤销</button>
            </div>`;
          });
          $("#inviteList").innerHTML = rows.length ? rows.join("") : '<div class="empty">暂无邀请码</div>';
        } catch (e) { toast(e.message, 4000, "error"); }
      }
      function renderAccountInfo() {
        if (!currentUser) return;
        const rows = [
          ["用户名", currentUser.username],
          ["显示名称", currentUser.display_name || "—"],
          ["学号", currentUser.student_id || "未填写"],
          ["角色", currentUser.role === "admin" ? "管理员" : "普通用户"],
          ["注册时间", currentUser.created_at || "—"],
        ];
        $("#accountInfo").innerHTML = rows
          .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`)
          .join("");
      }
      function openDeleteSheet() {
        $("#deleteAccountSheet").classList.remove("hidden");
        $("#delUsername").value = "";
        $("#delPassword").value = "";
        $("#delUsername").placeholder = currentUser ? currentUser.username : "";
        $("#delError").textContent = "";
        setTimeout(() => $("#delUsername").focus(), 0);
      }
      function closeDeleteSheet() {
        $("#deleteAccountSheet").classList.add("hidden");
        $("#delPassword").value = "";
      }
      async function deleteAccount() {
        const username = $("#delUsername").value.trim();
        const password = $("#delPassword").value;
        $("#delError").textContent = "";
        if (!username || !password) {
          $("#delError").textContent = "请填写用户名和当前密码";
          return;
        }
        $("#confirmDeleteBtn").disabled = true;
        try {
          const r = await api("/api/auth/delete-account", {
            method: "POST",
            body: JSON.stringify({ username, password }),
          });
          toast(r.promoted
            ? `账号已注销，管理员已转交给 ${r.promoted}`
            : "账号已注销", 5000);
          setTimeout(() => location.reload(), 900);
        } catch (e) {
          $("#delError").textContent = e.message;
          $("#confirmDeleteBtn").disabled = false;
        }
      }
      async function boot() {
        try {
          const st = await api("/api/auth/status");
          if (!st.authenticated) {
            showAuthScreen();
            $("#authHint").textContent = st.needs_setup
              ? "首次使用：注册的第一个账号将成为管理员，并继承现有课表配置"
              : "请登录后使用（注册需要邀请码）";
            $("#inviteField").classList.toggle("hidden", !st.invite_required);
            switchAuthTab(st.needs_setup ? "register" : "login");
            return;
          }
          currentUser = st.user;
          showApp();
          $("#userBox").textContent = (currentUser.display_name || currentUser.username) +
            (currentUser.role === "admin" ? "（管理员）" : "");
          renderAccountInfo();
          $("#inviteBtn").classList.toggle("hidden", currentUser.role !== "admin");
          $("#adminNavBtn").classList.toggle("hidden", currentUser.role !== "admin");
          await loadStatus();
          await loadToday();
          loadWeather();
          await loadChatHistory();
          pollBrowserLogin();
          if (currentUser.role === "admin") refreshInvites();
          // 刷新后停留在上一次的页面；首次未配置数据源时仍优先引导去「课表来源」
          if (status && status.is_demo && !status.has_source) {
            switchTab("source");
          } else {
            let saved = readSavedTab();
            // 旧版本可能把已下线的页签（如已移除的「空教室」）存进 localStorage，
            // 落到不存在的 tab 会让所有页面都隐藏，这里统一回落到今日页
            if (!["today", "week", "ai", "source", "admin", "settings"].includes(saved)) {
              saved = "today";
              saveTab("today");
            }
            // 非管理员不恢复「用户管理」，并清掉无效记录；today 是默认页且已加载，无需重复切换
            if (saved === "admin" && currentUser.role !== "admin") {
              saveTab("today");
            } else if (saved === "ai" && status && status.ai_show_in_nav === false) {
              // AI 分块已隐藏，不落回 AI 页
              saveTab("today");
            } else if (saved && saved !== "today") {
              switchTab(saved);
            }
          }
        } catch (e) {
          toast("加载失败：" + e.message, 5000, "error");
        } finally {
          hideBootSplash();
        }
      }
      $("#authTabLogin").addEventListener("click", () => switchAuthTab("login"));
      $("#authTabRegister").addEventListener("click", () => switchAuthTab("register"));
      $("#loginForm").addEventListener("submit", async (e) => {
        e.preventDefault();
        authError("");
        try {
          await api("/api/auth/login", { method: "POST", body: JSON.stringify({
            username: $("#loginUsername").value.trim(),
            password: $("#loginPassword").value }) });
          location.reload();
        } catch (err) { authError(err.message); }
      });
      $("#registerForm").addEventListener("submit", async (e) => {
        e.preventDefault();
        authError("");
        try {
          await api("/api/auth/register", { method: "POST", body: JSON.stringify({
            username: $("#regUsername").value.trim(),
            password: $("#regPassword").value,
            display_name: $("#regName").value.trim(),
            student_id: $("#regStudentId").value.trim(),
            invite: $("#regInvite").value.trim() }) });
          location.reload();
        } catch (err) { authError(err.message); }
      });
      async function doLogout() {
        try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
        try { localStorage.removeItem(TAB_KEY); } catch (e) {}
        location.reload();
      }
      $("#logoutBtn").addEventListener("click", doLogout);
      $("#securityLogoutBtn").addEventListener("click", doLogout);
      $("#deleteAccountBtn").addEventListener("click", openDeleteSheet);
      $("#closeDeleteBtn").addEventListener("click", closeDeleteSheet);
      $("#confirmDeleteBtn").addEventListener("click", deleteAccount);
      $("#deleteAccountSheet").addEventListener("click", (e) => {
        if (e.target.id === "deleteAccountSheet") closeDeleteSheet();
      });
      $("#inviteBtn").addEventListener("click", () => switchTab("admin"));
      function inviteMaxUses() {
        const v = parseInt(($("#inviteMaxUses").value || "1").trim(), 10);
        return (!v || v < 1) ? 1 : Math.min(v, 999);
      }
      $("#inviteMaxUses").addEventListener("change", () => {
        $("#inviteMaxUses").value = inviteMaxUses();
      });
      function inviteCustomCode() {
        const raw = ($("#inviteCustomCode").value || "").normalize("NFKC").trim();
        if (!raw) return "";
        const len = Array.from(raw).length;
        if (len < 4 || len > 32)
          throw new Error("自定义邀请码长度需在 4-32 个字符之间");
        if (!/^[\p{L}\p{N}_-]+$/u.test(raw))
          throw new Error("只能用中文/字母/数字/下划线（_）/短横线（-），不能有空格、标点或表情");
        return raw;
      }
      $("#genInviteBtn").addEventListener("click", async () => {
        try {
          const maxUses = inviteMaxUses();
          const code = inviteCustomCode();
          const r = await api("/api/auth/invites", {
            method: "POST", body: JSON.stringify({ max_uses: maxUses, code }) });
          const label = r.custom ? "自定义邀请码已创建" : "新邀请码";
          toast(`${label}（可用 ${maxUses} 次）：${r.code}`, 8000);
          if (r.custom) $("#inviteCustomCode").value = "";
          await refreshInvites();
        } catch (e) { toast(e.message, 5000, "error"); }
      });
      $("#genInviteBatchBtn").addEventListener("click", async () => {
        try {
          const maxUses = inviteMaxUses();
          if (($("#inviteCustomCode").value || "").trim()) {
            toast("批量生成只能随机码；自定义码请用「生成邀请码」单个生成", 5000, "error");
            return;
          }
          const r = await api("/api/auth/invites/batch", {
            method: "POST", body: JSON.stringify({ count: 5, max_uses: maxUses }) });
          toast(`已生成 ${r.codes.length} 个邀请码，每个可用 ${maxUses} 次`, 5000);
          await refreshInvites();
        } catch (e) { toast(e.message, 5000, "error"); }
      });
      $("#refreshInvitesBtn").addEventListener("click", refreshInvites);
      $("#inviteList").addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-invite]");
        if (!btn) return;
        if (!(await confirmDialog("确定撤销该邀请码？撤销后此码立即失效。", { ok: "撤销", danger: true }))) return;
        try {
          await api(`/api/auth/invites/${encodeURIComponent(btn.dataset.invite)}`, { method: "DELETE" });
          toast("邀请码已撤销", 2500);
          await refreshInvites();
        } catch (err) { toast(err.message, 4000, "error"); }
      });
      // 搜索 / 排序
      let adminSearchTimer = null;
      $("#adminSearch").addEventListener("input", () => {
        clearTimeout(adminSearchTimer);
        adminSearchTimer = setTimeout(loadAdminUsers, 300);
      });
      $("#adminSort").addEventListener("change", loadAdminUsers);
      $("#adminSelectAll").addEventListener("click", () => {
        const boxes = $$("#adminUserList .admin-user-check:not(:disabled)");
        const allChecked = boxes.length && [...boxes].every((b) => b.checked);
        boxes.forEach((b) => (b.checked = !allChecked));
      });
      async function batchActive(active) {
        const ids = [...$$("#adminUserList .admin-user-check:checked")]
          .map((b) => parseInt(b.dataset.uid, 10));
        if (!ids.length) { toast("请先勾选用户", 3000); return; }
        if (!(await confirmDialog(`确定${active ? "启用" : "停用"}选中的 ${ids.length} 个用户？`, {
          ok: active ? "启用" : "停用",
          danger: !active,
        }))) return;
        try {
          const r = await api("/api/auth/users/batch-active", {
            method: "POST",
            body: JSON.stringify({ ids, active }),
          });
          toast(`已${active ? "启用" : "停用"} ${r.count} 个用户`, 3000);
          await loadAdminUsers();
          await loadAdminStats();
          await loadAdminLog();
        } catch (e) { toast(e.message, 4000, "error"); }
      }
      $("#adminBatchDisable").addEventListener("click", () => batchActive(false));
      $("#adminBatchEnable").addEventListener("click", () => batchActive(true));

      /* ---------- 编辑用户信息（弹窗） ---------- */
      let editUserId = null;
      function openEditUser(id, username, displayName, studentId) {
        editUserId = id;
        $("#editUserName").textContent = username;
        $("#editUserDisplay").value = displayName || "";
        $("#editUserStudent").value = studentId || "";
        $("#editUserError").textContent = "";
        $("#editUserSheet").classList.remove("hidden");
        setTimeout(() => $("#editUserDisplay").focus(), 0);
      }
      function closeEditUser() {
        $("#editUserSheet").classList.add("hidden");
        editUserId = null;
      }
      $("#editUserCloseBtn").addEventListener("click", closeEditUser);
      $("#editUserSheet").addEventListener("click", (e) => {
        if (e.target.id === "editUserSheet") closeEditUser();
      });
      $("#editUserSaveBtn").addEventListener("click", async () => {
        if (editUserId == null) return;
        $("#editUserError").textContent = "";
        const display_name = $("#editUserDisplay").value.trim();
        const student_id = $("#editUserStudent").value.trim();
        $("#editUserSaveBtn").disabled = true;
        try {
          await api(`/api/auth/users/${editUserId}/info`, {
            method: "POST",
            body: JSON.stringify({ display_name, student_id }),
          });
          toast("用户信息已更新", 2500, "success");
          closeEditUser();
          await loadAdminUsers();
          await loadAdminLog();
        } catch (err) {
          $("#editUserError").textContent = err.message;
          $("#editUserSaveBtn").disabled = false;
        }
      });

      /* ---------- 用户管理操作（事件委托） ---------- */
      $("#adminUserList").addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-act]");
        if (!btn) return;
        const id = btn.dataset.id;
        const act = btn.dataset.act;
        try {
          if (act === "edit") {
            const row = btn.closest(".admin-user-row");
            const nameEl = row.querySelector("b");
            const infoEl = row.querySelector(".muted");
            const [displayName, studentId] = infoEl
              ? infoEl.textContent.split("·").map((s) => s.trim())
              : ["", ""];
            openEditUser(id, nameEl ? nameEl.textContent : "", displayName, studentId.replace(/^学号\s*/, ""));
            return;
          }
          if (act === "toggle-active") {
            const r = await api(`/api/auth/users/${id}/active`, {
              method: "POST",
              body: JSON.stringify({ active: btn.textContent.trim() === "启用" }),
            });
            toast("已更新用户状态", 2500);
          } else if (act === "role") {
            const newRole = btn.dataset.role === "admin" ? "user" : "admin";
            await api(`/api/auth/users/${id}/role`, {
              method: "POST",
              body: JSON.stringify({ role: newRole }),
            });
            toast("已更新角色", 2500);
          } else if (act === "reset-pwd") {
            const pwd = await inputDialog("为该用户设置新密码（至少 8 位）：", {
              title: "重置密码",
              ok: "保存",
              placeholder: "至少 8 位",
              password: true,
              danger: false,
              validate: (v) => (v.length >= 8 ? "" : "密码至少 8 位"),
            });
            if (!pwd) return;
            await api(`/api/auth/users/${id}/reset-password`, {
              method: "POST",
              body: JSON.stringify({ new_password: pwd }),
            });
            toast("密码已重置，该用户需用新密码登录", 4000);
          } else if (act === "delete") {
            if (!(await confirmDialog("确定删除该用户？其课表与配置将一并删除，无法恢复。", { ok: "删除", danger: true }))) return;
            await api(`/api/auth/users/${id}`, { method: "DELETE" });
            toast("用户已删除", 2500);
          }
          await loadAdminUsers();
          await loadAdminStats();
          await loadAdminLog();
        } catch (err) {
          toast(err.message, 4000, "error");
        }
      });

      /* ---------- 修改密码（自助） ---------- */
      $("#changePwdBtn").addEventListener("click", () => {
        $("#changePwdSheet").classList.remove("hidden");
        $("#cpOld").value = "";
        $("#cpNew").value = "";
        $("#cpConfirm").value = "";
        $("#cpError").textContent = "";
        setTimeout(() => $("#cpOld").focus(), 0);
      });
      $("#cpCloseBtn").addEventListener("click", () =>
        $("#changePwdSheet").classList.add("hidden"));
      $("#changePwdSheet").addEventListener("click", (e) => {
        if (e.target.id === "changePwdSheet")
          $("#changePwdSheet").classList.add("hidden");
      });
      $("#confirmChangePwdBtn").addEventListener("click", async () => {
        const oldPwd = $("#cpOld").value;
        const newPwd = $("#cpNew").value;
        const confirmPwd = $("#cpConfirm").value;
        $("#cpError").textContent = "";
        if (!oldPwd || !newPwd) {
          $("#cpError").textContent = "请填写原密码和新密码";
          return;
        }
        if (newPwd !== confirmPwd) {
          $("#cpError").textContent = "两次输入的新密码不一致";
          return;
        }
        $("#confirmChangePwdBtn").disabled = true;
        try {
          const r = await api("/api/auth/me/password", {
            method: "POST",
            body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
          });
          toast(r.message || "密码已修改", 4000);
          $("#changePwdSheet").classList.add("hidden");
        } catch (err) {
          $("#cpError").textContent = err.message;
          $("#confirmChangePwdBtn").disabled = false;
        }
      });
      /* ---------- 统一 Esc 与焦点陷阱：所有弹层集中登记，新增弹层只需加一行 ---------- */
      const ESC_LAYERS = [
        ["#confirmLayer", () => settleDialog(null)],
        ["#eventEditor", () => closeEventEditor()],
        ["#courseEditor", () => closeCourseEditor()],
        ["#examEditor", () => closeExamEditor()],
        ["#memorySheet", () => closeMemorySheet()],
        ["#deleteAccountSheet", () => closeDeleteSheet()],
        ["#editUserSheet", () => closeEditUser()],
        ["#changePwdSheet", () => $("#changePwdSheet").classList.add("hidden")],
        // 下面三个是轻量浮层：只响应 Esc，不做焦点陷阱（否则会挡住正常 Tab 导航）
        ["#weekMenu", () => $("#weekMenu").classList.add("hidden"), false],
        ["#weekJump", () => $("#weekJump").classList.add("hidden"), false],
        ["#searchResults", () => {
          const b = $("#searchResults");
          b.classList.add("hidden");
          b.innerHTML = "";
        }, false],
      ];
      function openLayers() {
        return ESC_LAYERS.filter(([sel]) => {
          const el = $(sel);
          return el && !el.classList.contains("hidden");
        });
      }
      document.addEventListener("keydown", (e) => {
        const layers = openLayers();
        if (!layers.length) return;
        const el = $(layers[0][0]);
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopImmediatePropagation();
          layers[0][1]();     // 只关最上层，再按一次才轮到下一层
          return;
        }
        if (e.key !== "Tab" || layers[0][2] === false) return;
        // 焦点陷阱：Tab 只在当前最上层弹层内循环，不会跑到背后的页面
        const focusables = [...el.querySelectorAll(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])'
        )].filter((x) => x.offsetParent !== null || x === document.activeElement);
        if (!focusables.length) return;
        const first = focusables[0], last = focusables[focusables.length - 1];
        if (!el.contains(document.activeElement)) {
          e.preventDefault(); first.focus();
        } else if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }, true);

      boot();
