// Run with: node --test tests/test_frontend_regressions.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/js/app.js"), "utf8");
function sourceBlock(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `Source block missing: ${start}`);
  return source.slice(from, to);
}
const selectors = sourceBlock("      const $ =", "      const WEEKDAY =");

function editorHarness(kind, request) {
  const course = kind === "Course";
  const lower = kind.toLowerCase();
  const saveId = course ? "#saveCourseBtn" : "#saveExamAdjustBtn";
  const resetId = course ? "#resetCoursesBtn" : "#resetExamsBtn";
  const buttons = [{ disabled: false }, { disabled: false }, { disabled: false }];
  const controls = new Map([
    [saveId, { disabled: false }],
    [resetId, { disabled: false }],
    ["#tab-week", { classList: { contains: () => true } }],
  ]);
  const calls = [];
  const context = vm.createContext({
    document: {
      querySelector: (selector) => selector.includes(",") ? buttons[0] : controls.get(selector),
      querySelectorAll: () => buttons,
    },
    api: (url, options) => {
      assert.equal(controls.get(saveId).disabled, true);
      assert.ok(buttons.every((button) => button.disabled));
      calls.push({ url, payload: JSON.parse(options.body) });
      return request();
    },
    courseData: { has_overrides: true },
    examAdjustData: { has_overrides: true },
    toast: () => {},
    loadCourseEditor: async () => {},
    loadExamEditor: async () => {},
    loadStatus: async () => {},
    loadToday: async () => {},
    loadExams: async () => {},
  });
  vm.runInContext(selectors + sourceBlock(
    `      let ${lower}ActionBusy = false;`,
    `      $("#${course ? "editCoursesBtn" : "editExamsBtn"}")`,
  ), context);
  return {
    calls,
    apply: (action) => context[`apply${kind}Action`](action, "item-id", { name: "测试" }),
    busy: () => vm.runInContext(`${lower}ActionBusy`, context),
    assertReleased: () => {
      assert.equal(vm.runInContext(`${lower}ActionBusy`, context), false);
      assert.equal(controls.get(saveId).disabled, false);
      assert.equal(controls.get(resetId).disabled, false);
      assert.ok(buttons.every((button) => !button.disabled));
    },
  };
}

for (const kind of ["Course", "Exam"]) {
  test(`${kind}: editing actions reach the API and release controls`, async () => {
    const harness = editorHarness(kind, async () => ({ ok: true }));
    for (const action of ["add", "update", "delete", "restore_hidden", "reset"]) {
      assert.equal(await harness.apply(action), true);
      assert.equal(harness.calls.at(-1).payload.action, action);
      harness.assertReleased();
    }
    assert.equal(harness.calls.length, 5);
  });

  test(`${kind}: pending and failed requests do not leave the editor locked`, async () => {
    let rejectRequest;
    const harness = editorHarness(kind, () => new Promise((_, reject) => { rejectRequest = reject; }));
    const pending = harness.apply("add");
    assert.equal(harness.busy(), true);
    assert.equal(await harness.apply("delete"), false);
    assert.equal(harness.calls.length, 1);
    rejectRequest(new Error("网络错误"));
    assert.equal(await pending, false);
    harness.assertReleased();
    const retry = harness.apply("add");
    assert.equal(harness.calls.length, 2);
    rejectRequest(new Error("网络错误"));
    assert.equal(await retry, false);
    harness.assertReleased();
  });
}

async function queryRooms(spans, start, end) {
  const controls = new Map();
  const calls = [], messages = [], notices = [];
  let click;
  const control = (selector) => {
    if (!controls.has(selector)) controls.set(selector, {
      disabled: false, value: "", textContent: "", innerHTML: "",
      addEventListener: (_, listener) => { click = listener; },
    });
    return controls.get(selector);
  };
  const context = vm.createContext({
    URLSearchParams,
    document: { querySelector: control, querySelectorAll: () => [] },
    api: async (url) => {
      calls.push(url);
      if (url === "/api/rooms/meta") return { periods: [{ jc: "12" }] };
      if (url.startsWith("/api/rooms/my-free?")) return { spans };
      if (url.startsWith("/api/rooms/free?")) return { rooms: [] };
      throw new Error(`Unexpected request: ${url}`);
    },
    openRoomPop: () => {},
    roomPopTitle: () => {},
    roomPopFillFilters: () => {
      control("#rpCampus").value = "campus-1";
      control("#rpBuilding").value = "";
    },
    roomPopMessage: (message) => messages.push(message),
    roomPopRender: () => {},
    skeletonRows: () => "",
    toast: (message) => notices.push(message),
  });
  vm.runInContext(selectors
    + sourceBlock("      function roomFreeBlocks(", "      function openRoomPop(")
    + sourceBlock("      async function roomPopQuery(", "      // 结果按座位数降序")
    + sourceBlock("      // 没课的空格子 → 查空教室", "      // 弹层交互："), context);
  const button = {
    dataset: { date: "2026-11-25", start: String(start), end: String(end) },
    closest: () => null,
  };
  await click({ target: { closest: () => button } });
  const query = calls.find((url) => url.startsWith("/api/rooms/free?"));
  return { params: query ? new URLSearchParams(query.split("?")[1]) : null, messages, notices };
}

test("Rooms: preserve a requested free block after an odd occupied period", async () => {
  for (const [start, end] of [[3, 4], [5, 6], [11, 12]]) {
    const result = await queryRooms([{ start: 2, end: 12 }], start, end);
    assert.equal(result.params.get("start"), String(start));
    assert.equal(result.params.get("end"), String(end));
    assert.deepEqual(result.notices, []);
  }
});

test("Rooms: still move a genuinely occupied block to the nearest free block", async () => {
  const result = await queryRooms([{ start: 1, end: 2 }, { start: 5, end: 12 }], 3, 4);
  assert.equal(result.params.get("start"), "1");
  assert.equal(result.params.get("end"), "2");
  assert.equal(result.notices.length, 1);
  assert.match(result.notices[0], /已改查第 1–2 节/);
});

test("Rooms: do not query when the timetable is missing or fully occupied", async () => {
  for (const spans of [null, []]) {
    const result = await queryRooms(spans, 3, 4);
    assert.equal(result.params, null);
    assert.equal(result.messages.length, 1);
  }
});
