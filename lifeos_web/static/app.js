"use strict";

const app = document.querySelector("#app");
const drawer = document.querySelector("#detail-drawer");
const drawerContent = document.querySelector("#drawer-content");
const drawerScrim = document.querySelector("#drawer-scrim");
const toast = document.querySelector("#toast");

const state = {
  snapshot: null,
  tab: "work",
  workMode: "doing",
  closedGroup: "month",
  expanded: null,
  noteMode: "open",
  insightMode: "active",
  reportDay: null,
  report: null,
  calendarMonth: null,
};
let focusBeforeDrawer = null;
let reportRequest = 0;
let snapshotRequest = null;
let lastSnapshotAt = 0;
let pendingSnapshotRender = false;

const TABS = ["work", "notes", "insights", "daily"];

const labels = {
  draft: "草稿",
  confirmed: "已确认",
};

const kindLabels = { task: "待办", note: "随记", question: "疑问", insight: "洞见" };

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function text(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : esc(value);
}

function stateClass(value) {
  if (["open", "confirmed"].includes(value)) return "is-blue";
  if (["scheduled", "draft"].includes(value)) return "is-amber";
  return "";
}

function status(value) {
  return `<span class="state-label ${stateClass(value)}">${esc(labels[value] || value || "未知")}</span>`;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 2200);
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll("[data-tab]").forEach((element) => {
    element.classList.toggle("is-active", element.dataset.tab === tab && element.classList.contains("tab"));
    if (element.classList.contains("tab")) {
      if (element.dataset.tab === tab) element.setAttribute("aria-current", "page");
      else element.removeAttribute("aria-current");
    }
  });
  window.location.hash = tab;
  closeDrawer();
  render();
}

function viewHeading(label, count, controls = "") {
  return `<header class="view-heading">
    ${controls}
    <p class="view-count">${esc(label)}<b>${count}</b></p>
  </header>`;
}

function segmented(name, active, options) {
  return `<div class="segmented" role="group" aria-label="${esc(name)}">
    ${options.map(([value, label]) => `<button class="${active === value ? "is-active" : ""}" data-mode-group="${esc(name)}" data-mode="${esc(value)}" aria-pressed="${active === value ? "true" : "false"}">${esc(label)}</button>`).join("")}
  </div>`;
}

function emptyState(message) {
  return `<section class="empty-state"><div class="empty-mark">∿</div><p>${esc(message)}</p></section>`;
}

// 子弹笔记的记号：手画的线性 SVG，线条故意不完全规整。完成用对号，排到以后用 <，转成别的用 >。
const MARKS = {
  task: `<path class="is-filled" d="M8.3 5.5c1.5.1 2.6 1.3 2.4 2.7-.1 1.4-1.4 2.4-2.8 2.2-1.4-.1-2.4-1.3-2.2-2.7.1-1.3 1.3-2.3 2.6-2.2z"/>`,
  done: `<path d="M3.6 8.6c1.1.9 2 1.9 2.9 3 1.7-2.8 3.7-5.4 6.1-7.8"/>`,
  scheduled: `<path d="M10.9 4.2C9 5.6 7.1 6.8 5.1 8.1c1.9 1.3 3.9 2.5 5.8 3.9"/>`,
  converted: `<path d="M5.1 4.2c1.9 1.4 3.8 2.6 5.8 3.9-1.9 1.3-3.9 2.6-5.8 3.9"/>`,
  note: `<path d="M4.1 8.4c2.6-.4 5.2-.1 7.8-.5"/>`,
  question: `<path d="M5.8 6c.1-1.5 1.2-2.4 2.5-2.3 1.3.1 2.3 1.1 2.1 2.4-.2 1-1 1.5-1.7 1.9-.6.4-.9.9-.9 1.7"/><path d="M7.8 12.3h.1"/>`,
  insight: `<path d="M8.2 3.2c-.2 2.1 0 4.2-.2 6.2"/><path d="M8 12.4h.1"/>`,
};
const ICONS = {
  star: `<path d="M8.1 1.8c.6 1.3 1.2 2.6 1.9 3.9 1.4.1 2.8.3 4.2.6-1 1-2.1 1.9-3.1 2.9.3 1.4.5 2.8.7 4.2L8 11.4c-1.3.7-2.5 1.3-3.8 2 .2-1.4.5-2.8.8-4.2-1-1-2-1.9-3-2.9 1.4-.2 2.8-.4 4.2-.5.6-1.4 1.2-2.7 1.9-4z"/>`,
  folder: `<path d="M2.4 4.6c0-.7.4-1.1 1.1-1.1h2.8c.5 0 .8.2 1.1.6l.6.9h4.6c.7 0 1.1.4 1.1 1.1v5.8c0 .7-.4 1.1-1.1 1.1H3.5c-.7 0-1.1-.4-1.1-1.1z"/>`,
  waiting: `<path d="M4.3 2.6c2.5.2 4.9-.1 7.4.1"/><path d="M4.4 13.4c2.4-.2 4.9.1 7.3-.1"/><path d="M5 2.8c.2 2.4 1.5 3.9 3 5.1 1.6-1.3 2.8-2.7 3-5"/><path d="M5.1 13.2c.2-2.3 1.4-3.9 2.9-5.3 1.6 1.4 2.8 2.9 3 5.2"/>`,
  chevron: `<path d="M5.2 6.3c1 1.1 1.9 2.2 2.9 3.3 1-1.1 1.9-2.2 2.9-3.2"/>`,
};
const CLOSED_LABELS = { done: "想通了", converted: "转成了", dropped: "划掉" };

function svg(body) {
  return `<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">${body}</svg>`;
}

function markName(entry) {
  if (["done", "scheduled", "converted"].includes(entry.status)) return entry.status;
  return entry.kind;
}

function bullet(entry) {
  return `<span class="bullet" title="${esc(entry.status_label || kindLabels[entry.kind])}">${svg(MARKS[markName(entry)])}</span>`;
}

function starMark(entry) {
  return entry.starred && entry.live ? `<span class="star-mark" title="重要">${svg(ICONS.star)}</span>` : "";
}

function monthDay(value) {
  const [, month, day] = String(value).slice(0, 10).split("-").map(Number);
  return month && day ? `${month}/${day}` : esc(value);
}

function monthLabel(value) {
  const [year, month] = String(value || "").split("-").map(Number);
  if (!year || !month) return esc(value);
  return year === new Date().getFullYear() ? `${month} 月` : `${year} 年 ${month} 月`;
}

function daysUntil(value) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const due = new Date(`${value}T00:00:00+08:00`);
  return Number.isNaN(due.getTime()) ? null : Math.round((due - today) / 86400000);
}

// 截止只在有的时候写：普通的灰色日期，快到期的橙色写剩几天，过期的红色写逾期几天；前面的小圆点跟着变色。
function dueMark(value) {
  if (!value) return "";
  const days = daysUntil(value);
  const title = `截止 ${esc(value)}`;
  if (days === null) return `<span class="due-mark" title="${title}">${esc(value)}</span>`;
  if (days < 0) return `<span class="due-mark is-overdue" title="${title}">逾期 ${-days} 天</span>`;
  if (days === 0) return `<span class="due-mark is-urgent" title="${title}">今天到期</span>`;
  if (days <= state.snapshot.urgent_window_days) return `<span class="due-mark is-urgent" title="${title}">${days} 天后</span>`;
  return `<span class="due-mark" title="${title}">${monthDay(value)}</span>`;
}

function waitingMark(owner) {
  return `<span class="waiting-mark">${svg(ICONS.waiting)}等${esc(owner)}</span>`;
}

function projectDay(project, day) {
  const name = project?.name ? `<span class="aside-project">${svg(ICONS.folder)}<span>${esc(project.name)}</span></span><span class="aside-divider"></span>` : "";
  return `${name}<span>${monthDay(day)}</span>`;
}

function entryLine(entry, { aside = "", sub = "" } = {}) {
  const classes = ["entry-line", `is-${entry.kind}`];
  if (["done", "converted"].includes(entry.status)) classes.push("is-done");
  if (entry.status === "dropped") classes.push("is-dropped");
  if (entry.kind === "task" && entry.owner && entry.live) classes.push("is-waiting");
  return `<button class="${classes.join(" ")}" data-open-id="${esc(entry.id)}">
    ${bullet(entry)}
    <span class="entry-body"><span class="entry-text">${esc(entry.text)}</span>${starMark(entry)}${sub ? `<span class="entry-sub">${esc(sub)}</span>` : ""}</span>
    <span class="entry-aside">${aside}</span>
  </button>`;
}

function workCard(title, lines, fold = null) {
  const head = fold
    ? `<button class="card-sheet fold-toggle" data-fold="${esc(fold.key)}" aria-expanded="${fold.open}"><h2 class="work-title">${esc(title)}</h2><span class="card-summary">${esc(fold.summary)}</span><span class="fold-chevron">${svg(ICONS.chevron)}</span></button>`
    : `<div class="card-sheet"><h2 class="work-title">${esc(title)}</h2></div>`;
  const body = !fold || fold.open ? `<div class="entry-lines">${lines.join("")}</div>` : "";
  return `<article class="work-card${fold && !fold.open ? " is-folded" : ""}">${head}${body}</article>`;
}

function groupBy(items, keyOf) {
  const groups = new Map();
  items.forEach((item) => {
    const key = keyOf(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return groups;
}

function projectTitle(task) {
  return task.project?.name || "不归项目";
}

function momentValue(value) {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

// 在办：自己的按简报的重要紧急顺序排在前，等别人的跟在同一个项目的最后；项目按组里最靠前的那条排。
function doingCards(tasks) {
  const doing = tasks.filter((task) => task.current)
    .sort((left, right) => Number(!left.mine) - Number(!right.mine) || left.rank - right.rank);
  const groups = [...groupBy(doing, (task) => task.project?.key || "").entries()].map(([key, members]) => ({
    key,
    members,
    order: Math.min(...members.map((task) => (task.mine ? task.rank : task.rank + doing.length))),
  })).sort((left, right) => Number(left.key === "") - Number(right.key === "") || left.order - right.order);
  return groups.map(({ members }) => workCard(projectTitle(members[0]), members.map((task) => entryLine(task, {
    aside: task.mine ? dueMark(task.due) : waitingMark(task.owner),
  }))));
}

function laterCards(tasks) {
  const later = tasks.filter((task) => task.status === "scheduled" && !task.current)
    .sort((left, right) => String(left.month).localeCompare(String(right.month)));
  return [...groupBy(later, (task) => task.project?.key || "").values()]
    .map((members) => workCard(projectTitle(members[0]), members.map((task) => entryLine(task, { aside: monthLabel(task.month) }))));
}

// 已结束：完成和划掉放在一起，按时间（了结的月份）或按项目分组；默认只展开最近的一组。
function closedCards(tasks) {
  const closed = tasks.filter((task) => ["done", "dropped"].includes(task.status))
    .sort((left, right) => momentValue(right.updated_at) - momentValue(left.updated_at));
  const byMonth = state.closedGroup === "month";
  const groups = groupBy(closed, (task) => (byMonth ? String(task.updated_at).slice(0, 7) : projectTitle(task)));
  if (!state.expanded) state.expanded = new Set([...groups.keys()].slice(0, 1));
  return [...groups.entries()].map(([key, members]) => {
    const done = members.filter((task) => task.status === "done").length;
    const dropped = members.length - done;
    const [year, month] = key.split("-");
    return workCard(byMonth ? `${year} 年 ${Number(month)} 月` : key, members.map((task) => entryLine(task, {
      aside: byMonth ? projectDay(task.project, task.updated_at) : `<span>${monthDay(task.updated_at)}</span>`,
      sub: task.status === "dropped" && task.note ? `划掉：${task.note}` : "",
    })), { key, open: state.expanded.has(key), summary: `完成 ${done}${dropped ? ` · 划掉 ${dropped}` : ""}` });
  });
}

function renderWork() {
  const tasks = state.snapshot.entries.filter((entry) => entry.kind === "task");
  const mode = state.workMode;
  const cards = { doing: doingCards, later: laterCards, closed: closedCards }[mode](tasks);
  const count = {
    doing: tasks.filter((task) => task.current).length,
    later: tasks.filter((task) => task.status === "scheduled" && !task.current).length,
    closed: tasks.filter((task) => ["done", "dropped"].includes(task.status)).length,
  }[mode];
  const empty = { doing: "现在没有在办的待办。", later: "没有排到以后的待办。", closed: "还没有结束的待办。" }[mode];
  const controls = segmented("work", mode, [["doing", "在办"], ["later", "以后"], ["closed", "已结束"]])
    + (mode === "closed" ? `<span class="heading-divider"></span>${segmented("closed", state.closedGroup, [["month", "按时间"], ["project", "按项目"]])}` : "");
  app.innerHTML = `${viewHeading({ doing: "在办", later: "以后", closed: "已结束" }[mode], count, controls)}
    ${cards.length ? `<section class="work-list">${cards.join("")}</section>` : emptyState(empty)}`;
}

function reportMap() {
  return new Map(state.snapshot.reports.map((report) => [report.day, report]));
}

function monthKey(day) { return day.slice(0, 7); }

function calendarMarkup() {
  const reports = reportMap();
  const [year, month] = state.calendarMonth.split("-").map(Number);
  const first = new Date(Date.UTC(year, month - 1, 1));
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const leading = (first.getUTCDay() + 6) % 7;
  const cells = Array.from({ length: leading }, () => "<span></span>");
  for (let day = 1; day <= days; day += 1) {
    const key = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    const report = reports.get(key);
    cells.push(`<button class="calendar-day ${report ? "has-report" : ""} ${report?.status === "draft" ? "is-draft" : ""} ${state.reportDay === key ? "is-selected" : ""}" ${report ? `data-report-day="${key}"` : "disabled"}>${day}</button>`);
  }
  return `<aside class="calendar-panel">
    <div class="calendar-head"><h2 class="calendar-title">${year} / ${String(month).padStart(2, "0")}</h2><div class="calendar-nav"><button data-calendar-step="-1" aria-label="上个月">←</button><button data-calendar-step="1" aria-label="下个月">→</button></div></div>
    <div class="calendar-grid"><span class="weekday">一</span><span class="weekday">二</span><span class="weekday">三</span><span class="weekday">四</span><span class="weekday">五</span><span class="weekday">六</span><span class="weekday">日</span>${cells.join("")}</div>
  </aside>`;
}

function inlineMarkdown(value) {
  return esc(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function markdown(value) {
  const lines = String(value || "").split("\n");
  const output = [];
  let list = null;
  const closeList = () => { if (list) { output.push(`</${list}>`); list = null; } };
  lines.forEach((line) => {
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    const bullet = /^[-*]\s+(.+)$/.exec(line);
    const ordered = /^\d+[.)]\s+(.+)$/.exec(line);
    if (heading) { closeList(); const level = heading[1].length; output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`); return; }
    if (bullet || ordered) {
      const desired = bullet ? "ul" : "ol";
      if (list !== desired) { closeList(); list = desired; output.push(`<${desired}>`); }
      output.push(`<li>${inlineMarkdown((bullet || ordered)[1])}</li>`);
      return;
    }
    closeList();
    if (line.trim()) output.push(`<p>${inlineMarkdown(line)}</p>`);
  });
  closeList();
  return output.join("");
}

function renderDaily() {
  const reports = state.snapshot.reports;
  if (!reports.length) {
    app.innerHTML = `${viewHeading("日报", 0)}${emptyState("还没有可展示的日报。")}`;
    return;
  }
  const report = state.report;
  const paper = report ? `<article class="report-paper">
    <header class="report-header"><div><h2 class="report-date">${esc(report.day)}</h2><div class="report-meta">${status(report.status)}<span>${esc(report.counts.activities)} activities</span><span>·</span><span>${esc(report.counts.work_events)} work events</span></div></div>${state.snapshot?.published ? "" : '<button class="open-source" data-action="open-report">打开原文 ↗</button>'}</header>
    <div class="report-body">${markdown(report.body)}</div>
  </article>` : `<article class="report-paper report-skeleton" aria-label="正在读取日报"><span class="skeleton skeleton-report-date"></span><span class="skeleton skeleton-report-line"></span><span class="skeleton skeleton-report-line is-short"></span><span class="skeleton skeleton-report-line"></span></article>`;
  app.innerHTML = `${viewHeading("日报", reports.length)}
    <section class="daily-layout">${calendarMarkup()}${paper}</section>`;
}

function closedNote(entry) {
  if (entry.status === "converted" && entry.ref) {
    const target = findRecord(entry.ref);
    return `${CLOSED_LABELS.converted}：${target ? target.text : entry.ref}`;
  }
  const label = entry.kind === "insight" ? "退役" : CLOSED_LABELS[entry.status] || entry.status_label;
  return entry.note ? `${label}：${entry.note}` : "";
}

// 随记和疑问是一套东西：不套卡片，整张列表写在一块内凹的托盘里。
function renderNotes() {
  const open = state.noteMode === "open";
  const items = state.snapshot.entries
    .filter((entry) => ["note", "question"].includes(entry.kind) && (open ? entry.status === "open" : entry.status !== "open"))
    .sort((left, right) => (open
      ? Number(right.starred) - Number(left.starred) || momentValue(right.created_at) - momentValue(left.created_at)
      : momentValue(right.updated_at) - momentValue(left.updated_at)));
  const lines = items.map((entry) => entryLine(entry, {
    aside: projectDay(entry.project, open ? entry.logged_on : entry.updated_at),
    sub: open ? entry.context || "" : closedNote(entry),
  }));
  app.innerHTML = `${viewHeading(open ? "开着" : "已了结", items.length, segmented("notes", state.noteMode, [["open", "开着"], ["closed", "已了结"]]))}
    ${items.length ? `<section class="entry-sheet">${lines.join("")}</section>` : emptyState(open ? "没有还开着的随记和疑问。" : "还没有了结的随记和疑问。")}`;
}

function renderInsights() {
  const active = state.insightMode === "active";
  const items = state.snapshot.entries.filter((entry) => entry.kind === "insight" && ((entry.status === "open") === active));
  const lines = items.map((entry) => entryLine(entry, { sub: active ? entry.context || "" : closedNote(entry) }));
  app.innerHTML = `${viewHeading(active ? "留着" : "退役", items.length, segmented("insights", state.insightMode, [["active", "留着的"], ["history", "退役的"]]))}
    ${items.length ? `<section class="entry-sheet">${lines.join("")}</section>` : emptyState(active ? "还没有留下的洞见。" : "没有退役的洞见。")}`;
}

function render() {
  if (!state.snapshot) return;
  if (state.tab === "daily") renderDaily();
  else if (state.tab === "notes") renderNotes();
  else if (state.tab === "insights") renderInsights();
  else renderWork();
}

function detailSection(title, body) {
  if (!body) return "";
  return `<section class="detail-section"><h3>${esc(title)}</h3>${body}</section>`;
}

function findRecord(id) {
  return state.snapshot.entries.find((entry) => entry.id === id) || null;
}

function relatedLink(id) {
  const target = findRecord(id);
  return `<button class="related-link" data-related-id="${esc(id)}">${esc(target ? target.text : id)}</button>`;
}

function openDetail(id) {
  const item = findRecord(id);
  if (!item) return;
  // 头部三行：记号引出记法、状态（默认状态不写）和 ID → 正文 → 与列表右侧同一写法的项目与截止，没有就不出这一行。
  const kind = `${bullet(item)}<span>${esc(kindLabels[item.kind] || "")}</span>${item.status === "open" ? "" : `<span aria-hidden="true">·</span><span>${esc(item.status_label || item.status)}</span>`}<span class="aside-divider"></span><span class="id-label">${esc(item.id)}</span>`;
  const project = item.project?.name ? `<span class="aside-project">${svg(ICONS.folder)}<span>${esc(item.project.name)}</span></span>` : "";
  const due = item.kind === "task" && item.due ? (item.live ? dueMark(item.due) : `<span class="due-mark" title="截止 ${esc(item.due)}">截止 ${monthDay(item.due)}</span>`) : "";
  const header = `<p class="detail-kind${item.status === "dropped" ? " is-dropped" : ""}">${kind}</p><h2>${text(item.text)}${starMark(item)}</h2>
    ${project || due ? `<div class="detail-meta">${project}${project && due ? '<span class="aside-divider"></span>' : ""}${due}</div>` : ""}`;
  const noteTitle = {
    done: item.kind === "question" ? "答案" : "完成了什么",
    dropped: item.kind === "insight" ? "为什么退役" : "为什么划掉",
    scheduled: "为什么以后再做",
  }[item.status] || "批注";
  const refTitle = { converted: "转成了", done: "答案来自", dropped: "被它取代" }[item.status] || "指向";
  const multiline = (value) => text(value).replaceAll("\n", "<br>");
  const body = `
    ${detailSection(noteTitle, item.note ? `<p>${multiline(item.note)}</p>` : "")}
    ${detailSection(refTitle, item.ref ? relatedLink(item.ref) : "")}
    ${detailSection("负责人", item.owner ? `<p>${text(item.owner)}</p>` : "")}
    ${detailSection("排到", item.month ? `<p>${text(item.month)}</p>` : "")}
    ${detailSection(item.kind === "insight" ? "来由" : "背景", item.context ? `<p>${multiline(item.context)}</p>` : "")}
    ${detailSection("记在", `<p>${esc(item.logged_on || "")}</p>`)}`;
  drawerContent.innerHTML = `<header class="drawer-head">${header}</header><div class="drawer-body">${body}</div>`;
  focusBeforeDrawer = document.activeElement;
  document.querySelector(".topbar").setAttribute("inert", "");
  app.setAttribute("inert", "");
  drawer.removeAttribute("inert");
  drawer.classList.add("is-open");
  drawerScrim.classList.add("is-open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  drawer.querySelector(".drawer-close").focus();
}

function closeDrawer() {
  const focusId = focusBeforeDrawer?.dataset.openId;
  let focusTarget = focusBeforeDrawer;
  drawer.classList.remove("is-open");
  drawerScrim.classList.remove("is-open");
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("inert", "");
  document.querySelector(".topbar").removeAttribute("inert");
  app.removeAttribute("inert");
  document.body.classList.remove("drawer-open");
  if (pendingSnapshotRender && state.snapshot) {
    pendingSnapshotRender = false;
    render();
    if (state.tab === "daily" && state.reportDay) loadReport(state.reportDay, { skeleton: false });
    if (focusId) {
      focusTarget = document.querySelector(`[data-open-id="${CSS.escape(focusId)}"]`);
    }
  }
  if (focusTarget?.isConnected) focusTarget.focus();
  focusBeforeDrawer = null;
}

async function loadReport(day, { skeleton = true } = {}) {
  const requestId = ++reportRequest;
  state.reportDay = day;
  state.calendarMonth = monthKey(day);
  if (skeleton) state.report = null;
  if (state.tab === "daily") renderDaily();
  try {
    const response = await fetch(`/api/reports/${encodeURIComponent(day)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "日报读取失败");
    if (requestId !== reportRequest) return;
    state.report = payload;
    if (state.tab === "daily") renderDaily();
  } catch (error) {
    if (requestId !== reportRequest) return;
    state.report = { day, status: "invalid", counts: { activities: 0, work_events: 0 }, body: `## 无法读取\n\n${error.message}` };
    if (state.tab === "daily") renderDaily();
  }
}

async function openReport() {
  if (!state.reportDay) return;
  try {
    const response = await fetch(`/api/reports/${encodeURIComponent(state.reportDay)}/open`, {
      method: "POST",
      headers: { "X-LifeOS-Intent": "open-report" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "打开失败");
    showToast("已交给系统默认应用打开");
  } catch (error) {
    showToast(error.message);
  }
}

function navigateRelated(id) {
  const record = findRecord(id);
  if (!record) { showToast("没有找到关联记录"); return; }
  closeDrawer();
  requestAnimationFrame(() => openDetail(id));
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) { event.preventDefault(); setTab(tab.dataset.tab); return; }
  const mode = event.target.closest("[data-mode]");
  if (mode) {
    const group = mode.dataset.modeGroup;
    if (group === "work") state.workMode = mode.dataset.mode;
    if (group === "closed") { state.closedGroup = mode.dataset.mode; state.expanded = null; }
    if (group === "notes") state.noteMode = mode.dataset.mode;
    if (group === "insights") state.insightMode = mode.dataset.mode;
    render(); return;
  }
  const fold = event.target.closest("[data-fold]");
  if (fold) {
    const key = fold.dataset.fold;
    if (state.expanded.has(key)) state.expanded.delete(key);
    else state.expanded.add(key);
    render(); return;
  }
  const open = event.target.closest("[data-open-id]");
  if (open) { openDetail(open.dataset.openId); return; }
  const related = event.target.closest("[data-related-id]");
  if (related) { navigateRelated(related.dataset.relatedId); return; }
  const reportDay = event.target.closest("[data-report-day]");
  if (reportDay) { loadReport(reportDay.dataset.reportDay); return; }
  const calendarStep = event.target.closest("[data-calendar-step]");
  if (calendarStep) {
    const [year, month] = state.calendarMonth.split("-").map(Number);
    const next = new Date(Date.UTC(year, month - 1 + Number(calendarStep.dataset.calendarStep), 1));
    state.calendarMonth = `${next.getUTCFullYear()}-${String(next.getUTCMonth() + 1).padStart(2, "0")}`;
    renderDaily(); return;
  }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "close-drawer") closeDrawer();
  if (action === "open-report") openReport();
});

document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });

async function refreshSnapshot({ initial = false } = {}) {
  if (snapshotRequest) return snapshotRequest;
  snapshotRequest = (async () => {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "本地账本读取失败");
    const initializing = initial || !state.snapshot;
    state.snapshot = payload;
    lastSnapshotAt = Date.now();
    if (initializing) {
      const hash = window.location.hash.slice(1);
      if (TABS.includes(hash)) state.tab = hash;
      const firstReport = payload.reports.find((report) => report.readable);
      if (firstReport) {
        state.reportDay = firstReport.day;
        state.calendarMonth = monthKey(firstReport.day);
        loadReport(firstReport.day);
      } else {
        state.calendarMonth = new Date().toISOString().slice(0, 7);
      }
    }
    document.querySelectorAll(".tab").forEach((element) => {
      const active = element.dataset.tab === state.tab;
      element.classList.toggle("is-active", active);
      if (active) element.setAttribute("aria-current", "page");
      else element.removeAttribute("aria-current");
    });
    if (drawer.classList.contains("is-open")) {
      pendingSnapshotRender = true;
    } else {
      render();
      if (!initializing && state.tab === "daily" && state.reportDay) loadReport(state.reportDay, { skeleton: false });
    }
  })().finally(() => { snapshotRequest = null; });
  return snapshotRequest;
}

async function boot() {
  try {
    await refreshSnapshot({ initial: true });
  } catch (error) {
    app.innerHTML = `<section class="error-state"><div class="empty-mark">!</div><h2>无法读取 LifeOS</h2><p>${esc(error.message)}</p></section>`;
  }
}

boot();

window.setInterval(() => {
  if (!document.hidden) refreshSnapshot().catch(() => {});
}, 30000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - lastSnapshotAt > 5000) {
    refreshSnapshot().catch(() => {});
  }
});
