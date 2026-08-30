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
  ideaMode: "current",
  achievementMode: "current",
  reportDay: null,
  report: null,
  calendarMonth: null,
};
let focusBeforeDrawer = null;
let reportRequest = 0;
let snapshotRequest = null;
let lastSnapshotAt = 0;
let pendingSnapshotRender = false;

const labels = {
  active: "进行中",
  waiting: "等待中",
  needs_confirmation: "待确认",
  paused: "已暂停",
  closed: "已关闭",
  completed: "已完成",
  cancelled: "已取消",
  inbox: "收件箱",
  incubating: "酝酿中",
  promoted: "已提升",
  archived: "已归档",
  current: "当前",
  superseded: "已替代",
  draft: "草稿",
  confirmed: "已确认",
};

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
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function stateClass(value) {
  if (["active", "current", "confirmed", "promoted", "completed"].includes(value)) return "is-blue";
  if (["waiting", "needs_confirmation", "paused", "draft"].includes(value)) return "is-amber";
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

function viewHeading(kicker) {
  return `<header class="view-heading">
    <p class="eyebrow">${esc(kicker)}</p>
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
  }[kind] || "";
  return `<svg class="due-icon" viewBox="0 0 16 16" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
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

function completionChip(task) {
  return `<span class="completion-chip" title="完成时间 ${esc(task.closed_at || "未记录")}">${dueIcon("done")}${esc(formatMoment(task.closed_at))}</span>`;
}

function workTaskLine(task) {
  const completed = task.status === "completed";
  return `<button class="task-line ${task.terminal ? "is-terminal" : ""}" data-open-kind="task" data-open-id="${esc(task.id)}">
    <span class="task-dot" aria-hidden="true"></span>
    <span class="task-outcome">${text(task.outcome)}</span>
    ${completed ? completionChip(task) : dueChip(task.due_at, false, true)}
  </button>`;
}

function momentValue(value) {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

function latestCompletion(item) {
  return Math.max(0, ...item.visibleTasks.map((task) => momentValue(task.closed_at)));
}

function renderWork() {
  const current = state.workMode === "current";
  const completed = state.workMode === "completed";
  const items = state.snapshot.work.items.map((item) => {
    let tasks;
    if (completed) tasks = item.tasks.filter((task) => task.status === "completed")
      .sort((left, right) => momentValue(right.closed_at) - momentValue(left.closed_at));
    else if (current) tasks = item.tasks.filter((task) => ["active", "waiting"].includes(task.status));
    else tasks = item.tasks.filter((task) => task.status !== "cancelled")
      .sort((left, right) => {
        const leftCompleted = left.status === "completed";
        const rightCompleted = right.status === "completed";
        if (leftCompleted !== rightCompleted) return leftCompleted ? 1 : -1;
        if (leftCompleted) return momentValue(right.closed_at) - momentValue(left.closed_at);
        return 0;
      });
    return { ...item, visibleTasks: tasks };
  }).filter((item) => {
    if (completed) return item.terminal || item.visibleTasks.length;
    if (current) return !item.terminal && item.state !== "paused" && item.visibleTasks.length > 0;
    return true;
  });
  items.sort((left, right) => {
    if (completed) return latestCompletion(right) - latestCompletion(left);
    if (state.workMode === "all" && left.terminal !== right.terminal) return left.terminal ? 1 : -1;
    if (state.workMode === "all" && left.terminal) return latestCompletion(right) - latestCompletion(left);
    return 0;
  });
  const standalone = state.workMode === "all" ? [] : state.snapshot.work.standalone_tasks.filter((task) => (
    completed ? task.status === "completed" : task.status === "active"
  ));
  const count = items.length + standalone.length;

  const cards = items.map((item) => {
    const project = item.project?.name ? `<span class="project-name">${esc(item.project.name)}</span>` : "";
    if (completed) {
      return `<article class="work-card is-completed-summary" id="locate-${esc(item.id)}">
        <button class="card-button" data-open-kind="work-item" data-open-id="${esc(item.id)}">
          <div class="work-topline">
            <h2 class="work-title">${text(item.title)}</h2>${project}
            <span class="id-label">${esc(item.id)}</span>
          </div>
        </button>
        ${item.visibleTasks.length ? `<div class="task-lines">${item.visibleTasks.map(workTaskLine).join("")}</div>` : ""}
      </article>`;
    }
    const action = item.current_milestone?.outcome || item.next_gate || "由关联待办承接下一步";
    return `<article class="work-card" id="locate-${esc(item.id)}">
      <button class="card-button" data-open-kind="work-item" data-open-id="${esc(item.id)}">
        <div class="work-topline">
          <h2 class="work-title">${text(item.title)}</h2>${project}
          <span class="id-label">${esc(item.id)}</span>
        </div>
        <div class="work-subline">
          <p class="next-gate"><strong>下一门槛</strong><span>${esc(action)}</span></p>
          ${status(item.state)}
        </div>
      </button>
      ${item.visibleTasks.length ? `<div class="task-lines">${item.visibleTasks.map(workTaskLine).join("")}</div>` : ""}
    </article>`;
  }).join("");

  const standaloneCards = standalone.length ? `<p class="standalone-label">Unbound / 独立</p>${standalone.map((task) => `<button class="standalone-card ${task.terminal ? "is-terminal" : ""}" id="locate-${esc(task.id)}" data-open-kind="task" data-open-id="${esc(task.id)}">
    <span class="task-dot" aria-hidden="true"></span>
    <span class="task-outcome">${text(task.outcome)}</span>
    ${completed ? `<span class="id-label">${esc(task.id)}</span>` : status(task.status)}
    ${task.status === "completed" ? completionChip(task) : dueChip(task.due_at, false, true)}
  </button>`).join("")}` : "";

  app.innerHTML = `${viewHeading("WORK / FOCUS")}
    ${segmented("work", state.workMode, [["current", "当前"], ["completed", "已完成"], ["all", "全部事项"]])}
    ${count ? `<section class="work-list">${cards}${standaloneCards}</section>` : emptyState(completed ? "还没有完成记录。" : "现在没有需要展示的工作。")}`;
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
    app.innerHTML = `${viewHeading("DAILY / LOG")}${emptyState("还没有可展示的日报。")}`;
    return;
  }
  const report = state.report;
  const paper = report ? `<article class="report-paper">
    <header class="report-header"><div><h2 class="report-date">${esc(report.day)}</h2><div class="report-meta">${status(report.status)}<span>${esc(report.counts.activities)} activities</span><span>·</span><span>${esc(report.counts.work_events)} work events</span></div></div><button class="open-source" data-action="open-report">打开原文 ↗</button></header>
    <div class="report-body">${markdown(report.body)}</div>
  </article>` : `<article class="report-paper report-skeleton" aria-label="正在读取日报"><span class="skeleton skeleton-report-date"></span><span class="skeleton skeleton-report-line"></span><span class="skeleton skeleton-report-line is-short"></span><span class="skeleton skeleton-report-line"></span></article>`;
  app.innerHTML = `${viewHeading("DAILY / LOG")}
    <section class="daily-layout">${calendarMarkup()}${paper}</section>`;
}

function renderIdeas() {
  const modes = {
    current: ["inbox", "incubating"],
    promoted: ["promoted"],
    archived: ["archived"],
  };
  const ideas = state.snapshot.ideas.filter((idea) => modes[state.ideaMode].includes(idea.status));
  const cards = ideas.map((idea) => `<button class="idea-card" data-open-kind="idea" data-open-id="${esc(idea.id)}">
    <span class="card-heading">${text(idea.text)}</span><span class="card-description">${text(idea.context, "尚未补充上下文")}</span>
    <span class="idea-meta">${status(idea.status)}<span class="date-label">${esc(formatMoment(idea.updated_at))}</span></span>
  </button>`).join("");
  app.innerHTML = `${viewHeading("IDEAS / SIGNAL")}
    ${segmented("ideas", state.ideaMode, [["current", "当前"], ["promoted", "已提升"], ["archived", "已归档"]])}
    ${ideas.length ? `<section class="card-grid">${cards}</section>` : emptyState("这个视图还没有闪念。")}`;
}

function renderAchievements() {
  const achievements = state.snapshot.achievements.filter((item) => state.achievementMode === "current" ? item.lifecycle === "current" : item.lifecycle !== "current");
  const cards = achievements.map((item) => `<button class="capsule-card" data-open-kind="achievement" data-open-id="${esc(item.id)}">
    <span class="capsule-body">
      <span class="capsule-copy"><span class="card-heading">${text(item.title)}</span><span class="card-description">${text(item.outcome)}</span></span>
    </span>
  </button>`).join("");
  app.innerHTML = `${viewHeading("CAPSULES / REUSE")}
    ${segmented("achievements", state.achievementMode, [["current", "当前"], ["history", "历史"]])}
    ${achievements.length ? `<section class="capsule-grid">${cards}</section>` : emptyState("这个视图还没有成果胶囊。")}`;
}

function render() {
  if (!state.snapshot) return;
  if (state.tab === "daily") renderDaily();
  else if (state.tab === "ideas") renderIdeas();
  else if (state.tab === "achievements") renderAchievements();
  else renderWork();
}

function detailSection(title, body) {
  if (!body) return "";
  return `<section class="detail-section"><h3>${esc(title)}</h3>${body}</section>`;
}

function findRecord(kind, id) {
  if (kind === "work-item") return state.snapshot.work.items.find((item) => item.id === id);
  if (kind === "task") {
    const nested = state.snapshot.work.items.flatMap((item) => item.tasks);
    return [...nested, ...state.snapshot.work.standalone_tasks].find((item) => item.id === id);
  }
  if (kind === "idea") return state.snapshot.ideas.find((item) => item.id === id);
  if (kind === "achievement") return state.snapshot.achievements.find((item) => item.id === id);
  return null;
}

function openDetail(kind, id) {
  const item = findRecord(kind, id);
  if (!item) return;
  let header = "";
  let body = "";
  if (kind === "work-item") {
    header = `<p class="eyebrow">WORK THREAD</p><h2>${text(item.title)}</h2><div class="detail-meta"><span class="id-label">${esc(item.id)}</span>${item.project?.name ? `<span class="project-name">${esc(item.project.name)}</span>` : ""}${status(item.state)}</div>`;
    body = `
      ${detailSection("下一门槛", `<p>${text(item.current_milestone?.outcome || item.next_gate)}</p>`)}
      ${detailSection("上下文", `<p>${text(item.context)}</p>`)}
      ${detailSection("关联结果", item.tasks.map((task) => `<button class="related-link" data-related-kind="task" data-related-id="${esc(task.id)}">${esc(task.outcome)}</button>`).join(""))}`;
  } else if (kind === "task") {
    header = `<p class="eyebrow">RESULT</p><h2>${text(item.outcome)}</h2><div class="detail-meta"><span class="id-label">${esc(item.id)}</span>${status(item.status)}${item.due_at ? dueChip(item.due_at, item.terminal) : ""}</div>`;
    body = `
      ${detailSection("下一行动", `<p>${text(item.next_action?.text)}</p>`)}
      ${detailSection("完成标准", `<p>${text(item.completion_criteria)}</p>`)}
      ${detailSection("为什么", `<p>${text(item.why)}</p>`)}
      ${detailSection("完成记录", item.completion ? `<p>${text(item.completion.summary)}</p>` : "")}
      ${item.work_item_id ? detailSection("所属主线", `<button class="related-link" data-related-kind="work-item" data-related-id="${esc(item.work_item_id)}">${esc(item.work_item_id)}</button>`) : ""}`;
  } else if (kind === "idea") {
    header = `<p class="eyebrow">IDEA</p><h2>${text(item.text)}</h2><div class="detail-meta"><span class="id-label">${esc(item.id)}</span>${status(item.status)}</div>`;
    body = `
      ${detailSection("上下文", `<p>${text(item.context)}</p>`)}
      ${detailSection("状态说明", `<p>${text(item.status_reason)}</p>`)}
      ${detailSection("提升到", (item.promoted_to || []).map((target) => `<button class="related-link" data-related-id="${esc(target)}">${esc(target)}</button>`).join(""))}`;
  } else {
    header = `<p class="eyebrow">CAPSULE</p><h2>${text(item.title)}</h2><div class="detail-meta"><span class="id-label">${esc(item.id)}</span>${status(item.lifecycle)}</div>`;
    body = `
      ${detailSection("结果", `<p>${text(item.outcome)}</p>`)}
      ${detailSection("背景", `<p>${text(item.context)}</p>`)}
      ${detailSection("关键经验", (item.key_learnings || []).map((value) => `<p>· ${esc(value)}</p>`).join(""))}
      ${detailSection("如何复用", `<p>${text(item.reuse)}</p>`)}
      ${detailSection("关联结果", (item.task_links || []).map((link) => `<button class="related-link" data-related-kind="task" data-related-id="${esc(link.task_id)}">${esc(link.task_id)} · ${esc(link.contribution)}</button>`).join(""))}`;
  }
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
  const focusKind = focusBeforeDrawer?.dataset.openKind;
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
    if (focusKind && focusId) {
      focusTarget = document.querySelector(`[data-open-kind="${CSS.escape(focusKind)}"][data-open-id="${CSS.escape(focusId)}"]`);
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

function navigateRelated(id, explicitKind) {
  const kind = explicitKind || (id.startsWith("WI-") ? "work-item" : "task");
  const record = findRecord(kind, id);
  if (!record) { showToast("没有找到关联记录"); return; }
  state.workMode = record.terminal ? "completed" : (record.status === "paused" || record.state === "paused" ? "all" : "current");
  state.tab = "work";
  window.location.hash = "work";
  closeDrawer();
  document.querySelectorAll(".tab").forEach((element) => element.classList.toggle("is-active", element.dataset.tab === "work"));
  renderWork();
  requestAnimationFrame(() => {
    document.querySelector(`#locate-${CSS.escape(id)}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    openDetail(kind, id);
  });
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) { event.preventDefault(); setTab(tab.dataset.tab); return; }
  const mode = event.target.closest("[data-mode]");
  if (mode) {
    if (mode.dataset.modeGroup === "work") state.workMode = mode.dataset.mode;
    if (mode.dataset.modeGroup === "ideas") state.ideaMode = mode.dataset.mode;
    if (mode.dataset.modeGroup === "achievements") state.achievementMode = mode.dataset.mode;
    render(); return;
  }
  const open = event.target.closest("[data-open-kind]");
  if (open) { openDetail(open.dataset.openKind, open.dataset.openId); return; }
  const related = event.target.closest("[data-related-id]");
  if (related) { navigateRelated(related.dataset.relatedId, related.dataset.relatedKind); return; }
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
    document.querySelector(".pulse").classList.remove("is-error");
    if (initializing) {
      const hash = window.location.hash.slice(1);
      if (["work", "daily", "ideas", "achievements"].includes(hash)) state.tab = hash;
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
    document.querySelector("#updated-at").textContent = `LOCAL · ${formatMoment(payload.updated_at)}`;
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
    document.querySelector(".pulse").classList.add("is-error");
    document.querySelector("#updated-at").textContent = "LOCAL · ERROR";
  }
}

boot();

window.setInterval(() => {
  if (!document.hidden) refreshSnapshot().catch(() => document.querySelector(".pulse").classList.add("is-error"));
}, 30000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - lastSnapshotAt > 5000) {
    refreshSnapshot().catch(() => document.querySelector(".pulse").classList.add("is-error"));
  }
});
