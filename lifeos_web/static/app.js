"use strict";

const app = document.querySelector("#app");
const drawer = document.querySelector("#detail-drawer");
const drawerContent = document.querySelector("#drawer-content");
const drawerScrim = document.querySelector("#drawer-scrim");
const toast = document.querySelector("#toast");

const state = {
  snapshot: null,
  tab: "work",
  workMode: "current",
  logMode: "today",
  questionMode: "open",
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

const TABS = ["log", "work", "questions", "insights", "daily"];

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

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value.length === 10 ? `${value}T00:00:00+08:00` : value);
  if (Number.isNaN(date.getTime())) return esc(value);
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
}

function formatMoment(value) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(value);
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function stateClass(value) {
  if (["open", "confirmed"].includes(value)) return "is-blue";
  if (["scheduled", "draft"].includes(value)) return "is-amber";
  return "";
}

function entryStatus(entry) {
  return `<span class="state-label ${stateClass(entry.status)}">${esc(entry.status_label || entry.status)}</span>`;
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

function viewHeading(kicker, count, controls = "") {
  return `<header class="view-heading">
    ${controls}
    <p class="running-head"><span class="running-title">${esc(kicker)}</span><span class="running-count">${String(count).padStart(2, "0")}</span></p>
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

function dueIcon(kind) {
  const body = {
    normal: `<circle cx="8" cy="8" r="5.5"></circle><path d="M8 5v3.25l2.25 1.25"></path>`,
    urgent: `<path d="M8 2.5a5.5 5.5 0 1 0 4.7 2.65"></path><path d="M8 5v3.25l2.25 1.25"></path><path d="M10.75 2.25h2.5v2.5"></path>`,
    overdue: `<circle cx="8" cy="8" r="5.5"></circle><path d="M8 4.75v4"></path><path d="M8 11.25h.01"></path>`,
    done: `<circle cx="8" cy="8" r="5.5"></circle><path d="m5.25 8 1.8 1.8 3.7-3.7"></path>`,
    gate: `<path d="M4 13.5V2.75"></path><path d="M4 3h7.5l-1.75 2.75L11.5 8.5H4"></path>`,
    later: `<path d="M9.5 4 5.5 8l4 4"></path>`,
    star: `<path d="m8 2.4 1.7 3.5 3.8.5-2.8 2.7.7 3.8L8 11.1l-3.4 1.8.7-3.8-2.8-2.7 3.8-.5Z"></path>`,
    note: `<path d="M4.5 8.2h7"></path>`,
    question: `<path d="M6 6.1a2.1 2.1 0 1 1 3 1.9c-.7.4-1 .9-1 1.6v.3"></path><path d="M8 12.2h.01"></path>`,
    insight: `<path d="M8 3.2v6"></path><path d="M8 12.2h.01"></path>`,
  }[kind] || "";
  return `<svg class="due-icon" viewBox="0 0 16 16" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
}

function bulletMark(entry) {
  if (entry.kind === "task") return `<span class="task-dot" aria-hidden="true"></span>`;
  return `<span class="bullet-mark" title="${esc(kindLabels[entry.kind])}">${dueIcon(entry.kind)}</span>`;
}

function starMark(entry) {
  return entry.starred ? `<span class="star-mark" title="重要">${dueIcon("star")}</span>` : "";
}

function dueInfo(value, terminal) {
  if (!value) return { tone: "none", icon: "normal", label: "无截止", title: "没有截止日期" };
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const due = new Date(`${value}T00:00:00+08:00`);
  if (Number.isNaN(due.getTime())) return { tone: "later", icon: "normal", label: value, title: value };
  const days = Math.round((due - today) / 86400000);
  if (terminal) return { tone: "done", icon: "done", label: formatDate(value), title: `截止 ${value}` };
  if (days < 0) return { tone: "overdue", icon: "overdue", label: `逾期${Math.abs(days)}天`, title: `已逾期 ${Math.abs(days)} 天 · 截止 ${value}` };
  if (days === 0) return { tone: "urgent", icon: "urgent", label: "今天到期", title: `今天截止 · ${value}` };
  if (days <= 3) return { tone: "urgent", icon: "urgent", label: `${days}天后`, title: `${days} 天后截止 · ${value}` };
  if (days <= 7) return { tone: "later", icon: "normal", label: `${days}天后`, title: `截止 ${value}` };
  return { tone: "later", icon: "normal", label: formatDate(value), title: `截止 ${value}` };
}

function dueChip(value, terminal, showEmpty = false) {
  if (!value && !showEmpty) return "";
  const due = dueInfo(value, terminal);
  return `<span class="due-chip is-${due.tone}" title="${esc(due.title)}">${dueIcon(due.icon)}<span>${esc(due.label)}</span></span>`;
}

function completionChip(entry) {
  return `<span class="completion-chip" title="完成时间 ${esc(entry.updated_at || "未记录")}">${dueIcon("done")}${esc(formatMoment(entry.updated_at))}</span>`;
}

function stateChip(entry) {
  if (entry.kind === "task" && entry.status === "done") return completionChip(entry);
  if (entry.status === "scheduled") return `<span class="due-chip is-later" title="排到 ${esc(entry.month)}">${dueIcon("later")}<span>${esc(entry.month)}</span></span>`;
  if (entry.kind === "task" && entry.live) return dueChip(entry.due, false, true);
  return entryStatus(entry);
}

function entryLine(entry, { showProject = false } = {}) {
  const project = showProject && entry.project?.name ? `<span class="project-name">${esc(entry.project.name)}</span>` : "";
  const owner = entry.owner ? `<span class="project-name">${esc(entry.owner)}</span>` : "";
  return `<button class="task-line ${entry.kept ? "" : "is-terminal"}" data-open-id="${esc(entry.id)}">
    ${bulletMark(entry)}${starMark(entry)}
    <span class="task-outcome">${text(entry.text)}</span>${owner}${project}
    ${stateChip(entry)}
  </button>`;
}

function momentValue(value) {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

function tasksFor(mode) {
  const tasks = state.snapshot.entries.filter((entry) => entry.kind === "task");
  if (mode === "current") return tasks.filter((task) => task.current).sort((left, right) => left.rank - right.rank);
  if (mode === "others") return tasks.filter((task) => task.status === "open" && task.owner).sort((left, right) => left.rank - right.rank);
  if (mode === "later") return tasks.filter((task) => task.status === "scheduled" && !task.current)
    .sort((left, right) => String(left.month).localeCompare(String(right.month)));
  return tasks.filter((task) => task.status === "done").sort((left, right) => momentValue(right.updated_at) - momentValue(left.updated_at));
}

function groupByProject(tasks) {
  const groups = new Map();
  tasks.forEach((task) => {
    const key = task.project?.key || "";
    if (!groups.has(key)) groups.set(key, { project: task.project, tasks: [] });
    groups.get(key).tasks.push(task);
  });
  return [...groups.values()];
}

function renderWork() {
  const tasks = tasksFor(state.workMode);
  const groups = groupByProject(tasks);
  const cards = groups.map((group) => {
    const title = group.project?.name || "不归项目";
    const id = group.project?.key ? `<span class="id-label">${esc(group.project.key)}</span>` : "";
    return `<article class="work-card">
      <div class="card-sheet">
        <div class="work-topline"><h2 class="work-title">${esc(title)}</h2>${id}</div>
      </div>
      <div class="task-lines">${group.tasks.map((task) => entryLine(task)).join("")}</div>
    </article>`;
  }).join("");
  const empty = {
    current: "现在没有进行中的待办。",
    others: "没有在等别人的事。",
    later: "没有排到以后的待办。",
    done: "还没有完成记录。",
  }[state.workMode];
  app.innerHTML = `${viewHeading("TODO / FOCUS", tasks.length, segmented("work", state.workMode, [["current", "当前"], ["others", "等别人"], ["later", "排到以后"], ["done", "已完成"]]))}
    ${tasks.length ? `<section class="work-list">${cards}</section>` : emptyState(empty)}`;
}

function dayLabel(day) {
  const date = new Date(`${day}T00:00:00+08:00`);
  if (Number.isNaN(date.getTime())) return esc(day);
  const weekday = "日一二三四五六"[date.getDay()];
  return `${date.getMonth() + 1}月${date.getDate()}日 周${weekday}`;
}

function renderLog() {
  const reference = state.snapshot.reference_date;
  const start = new Date(`${reference}T00:00:00+08:00`);
  start.setDate(start.getDate() - 6);
  const earliest = `${start.getFullYear()}-${String(start.getMonth() + 1).padStart(2, "0")}-${String(start.getDate()).padStart(2, "0")}`;
  const entries = state.snapshot.entries.filter((entry) => (
    state.logMode === "today" ? entry.logged_on === reference : entry.logged_on >= earliest && entry.logged_on <= reference
  ));
  const days = new Map();
  entries.forEach((entry) => {
    if (!days.has(entry.logged_on)) days.set(entry.logged_on, []);
    days.get(entry.logged_on).push(entry);
  });
  const cards = [...days.entries()].map(([day, members]) => `<article class="work-card">
    <div class="card-sheet"><div class="work-topline"><h2 class="work-title">${dayLabel(day)}</h2></div></div>
    <div class="task-lines">${members.map((entry) => entryLine(entry, { showProject: true })).join("")}</div>
  </article>`).join("");
  app.innerHTML = `${viewHeading("LOG / TODAY", entries.length, segmented("log", state.logMode, [["today", "今天"], ["week", "最近 7 天"]]))}
    ${entries.length ? `<section class="work-list">${cards}</section>` : emptyState(state.logMode === "today" ? "今天还没有记下什么。" : "最近 7 天没有记录。")}`;
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
    app.innerHTML = `${viewHeading("DAILY / LOG", 0)}${emptyState("还没有可展示的日报。")}`;
    return;
  }
  const report = state.report;
  const paper = report ? `<article class="report-paper">
    <header class="report-header"><div><h2 class="report-date">${esc(report.day)}</h2><div class="report-meta">${status(report.status)}<span>${esc(report.counts.activities)} activities</span><span>·</span><span>${esc(report.counts.work_events)} work events</span></div></div>${state.snapshot?.published ? "" : '<button class="open-source" data-action="open-report">打开原文 ↗</button>'}</header>
    <div class="report-body">${markdown(report.body)}</div>
  </article>` : `<article class="report-paper report-skeleton" aria-label="正在读取日报"><span class="skeleton skeleton-report-date"></span><span class="skeleton skeleton-report-line"></span><span class="skeleton skeleton-report-line is-short"></span><span class="skeleton skeleton-report-line"></span></article>`;
  app.innerHTML = `${viewHeading("DAILY / LOG", reports.length)}
    <section class="daily-layout">${calendarMarkup()}${paper}</section>`;
}

function entryCard(entry, description, className) {
  return `<button class="${className}" data-open-id="${esc(entry.id)}">
    <span class="card-sheet"><span class="card-heading">${text(entry.text)}</span><span class="card-description">${text(description)}</span></span>
    <span class="idea-meta">${starMark(entry)}<span class="date-label">${esc(entry.logged_on || "")}</span>${entry.project?.name ? `<span class="project-name">${esc(entry.project.name)}</span>` : ""}</span>
  </button>`;
}

function renderQuestions() {
  const open = state.questionMode === "open";
  const questions = state.snapshot.entries.filter((entry) => entry.kind === "question" && (open ? entry.status === "open" : entry.status !== "open"))
    .sort((left, right) => (open ? Number(right.starred) - Number(left.starred) : 0));
  const cards = questions.map((entry) => entryCard(entry, open ? entry.context || "还没有补充背景" : entry.note || entry.status_label, "idea-card")).join("");
  app.innerHTML = `${viewHeading("QUESTIONS / OPEN", questions.length, segmented("questions", state.questionMode, [["open", "没想通"], ["closed", "已了结"]]))}
    ${questions.length ? `<section class="card-grid">${cards}</section>` : emptyState(open ? "眼下没有没想通的问题。" : "还没有了结的问题。")}`;
}

function renderInsights() {
  const active = state.insightMode === "active";
  const insights = state.snapshot.entries.filter((entry) => entry.kind === "insight" && ((entry.status === "open") === active));
  const cards = insights.map((entry) => entryCard(entry, entry.context, "achievement-card")).join("");
  app.innerHTML = `${viewHeading("INSIGHTS / KEPT", insights.length, segmented("insights", state.insightMode, [["active", "有效"], ["history", "历史"]]))}
    ${insights.length ? `<section class="card-grid">${cards}</section>` : emptyState(active ? "还没有留下的洞见。" : "没有被推翻或退役的洞见。")}`;
}

function render() {
  if (!state.snapshot) return;
  if (state.tab === "daily") renderDaily();
  else if (state.tab === "log") renderLog();
  else if (state.tab === "questions") renderQuestions();
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
  const header = `<p class="eyebrow">${esc(kindLabels[item.kind] || "")}</p><h2>${text(item.text)}</h2>
    <div class="detail-meta"><span class="id-label">${esc(item.id)}</span>${item.project?.name ? `<span class="project-name">${esc(item.project.name)}</span>` : ""}${starMark(item)}${entryStatus(item)}${item.kind === "task" && item.due ? dueChip(item.due, !item.live) : ""}</div>`;
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
    if (group === "log") state.logMode = mode.dataset.mode;
    if (group === "questions") state.questionMode = mode.dataset.mode;
    if (group === "insights") state.insightMode = mode.dataset.mode;
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
