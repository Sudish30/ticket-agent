/* Hallmark · macrostructure: Index-First · tone: technical-utilitarian · anchor hue: cobalt
 * Hallmark · genre: modern-minimal · theme: Cobalt · enrichment: none · nav: N9 · footer: Ft1
 * Hallmark · pre-emit critique: P5 H5 E5 S5 R5 V5
 * interaction: ticket disclosure · solution selection · delivery decision
 */

const STORAGE_KEY = "quorum-ui-v2";
const ROLE_KEY = "quorum-role";                       // "reporter" (default) | "engineer"
const ROLES = { reporter: "Reporter", engineer: "Engineer" };

const screens = {
  tickets: {
    title: "Tickets",
    description: "Incoming work appears here. Open a ticket to read its description, reporter, status, and attempted solutions."
  },
  solutions: {
    title: "Solutions",
    description: "Select a ticket, review its context and attempts, then choose how to ship a solution."
  },
  setup: {
    title: "Setup",
    description: "Choose the repository and limits Quorum uses when it works a ticket."
  },
  submission: {
    title: "New ticket",
    description: "Describe the problem once. The intake agent starts clarifying it right away."
  }
};

const routeAliases = {
  ticket: "tickets",
  history: "tickets",
  live: "solutions",
  results: "solutions",
  candidate: "solutions",
  preview: "solutions",
  resolution: "solutions"
};

const deliveryOptions = {
  preview: {
    title: "Host preview",
    description: "Publish the selected solution to a temporary review environment.",
    action: "Host selected solution"
  },
  pullRequest: {
    title: "Open pull request",
    description: "Send the selected branch to code review with the ticket context attached.",
    action: "Open pull request"
  },
  branch: {
    title: "Keep branch",
    description: "Preserve the work without opening a pull request or preview.",
    action: "Keep selected branch"
  }
};

const state = {
  currentScreen: "tickets",
  role: "reporter",
  tickets: [],
  selectedTicketId: null,
  expandedTicketId: null,
  selectedSolutionId: null,
  deliveryChoice: "preview",
  indexState: "ready"
};

restoreState();
restoreRole();
applyRole();

// ---- View as: Reporter (answers the agent) / Engineer (sees the brief and solves) ----
function isReporter() {
  return state.role === "reporter";
}

function restoreRole() {
  try {
    const saved = window.localStorage.getItem(ROLE_KEY);
    state.role = ROLES[saved] ? saved : "reporter";
  } catch {
    state.role = "reporter";
  }
}

function setRole(role) {
  if (!ROLES[role] || role === state.role) return;
  state.role = role;
  try { window.localStorage.setItem(ROLE_KEY, role); } catch { /* storage unavailable: keep it for this page load */ }
  applyRole();
  if (isReporter() && state.currentScreen === "solutions") renderScreen("tickets", { focus: false });
  else renderScreen(state.currentScreen, { focus: false, updateHash: false });
}

// The topbar toggle and the Solutions tab follow the role; called on every render.
function applyRole() {
  document.querySelectorAll("[data-role]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.role === state.role));
  });
  const solutionsTab = document.querySelector('.tab-link[data-screen-link="solutions"]');
  if (solutionsTab) solutionsTab.hidden = isReporter();
}

const API = window.location.port === "8000" ? "" : "http://localhost:8000";
async function api(path, body, method) {
  const r = await fetch(API + path, {
    method: method || (body ? "POST" : "GET"),
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// Backend ticket -> UI ticket. Solutions/resolution are client-side (teammates' stage) and preserved across syncs.
function fromBackend(t) {
  const local = state.tickets.find((x) => x.id === t.key);
  return {
    id: t.key, title: t.summary, description: t.description, reporter: t.reporter,
    createdAt: t.created, repository: t.repository || "", status: t.status,
    comments: t.comments || [], brief: t.brief || null, briefMd: t.brief_md || "", error: t.error || "",
    prPackage: t.pr_package || null, prPackageMd: t.pr_package_md || "", prUrl: t.pr_url || "",
    solutions: local?.solutions || [], resolution: local?.resolution || null
  };
}

async function syncTickets() {
  try {
    const list = await api("/api/tickets");
    state.tickets = list.map(fromBackend);
    persistState();
  } catch (err) {
    console.warn("backend unreachable", err);
  }
}

let pollTimer = null;
function startPolling() {
  stopPolling();
  pollTimer = window.setInterval(async () => {
    if (state.currentScreen !== "tickets" && state.currentScreen !== "solutions") return;
    const sig = (ts) => JSON.stringify(ts.map((t) => [t.id, t.status, t.comments?.length, Boolean(t.prPackage), t.prUrl]));
    const before = sig(state.tickets);
    await syncTickets();
    const after = sig(state.tickets);
    if (before !== after) renderScreen(state.currentScreen, { focus: false, updateHash: false });
  }, 3000);
}
function stopPolling() { if (pollTimer) window.clearInterval(pollTimer); pollTimer = null; }

const screenRoot = document.querySelector("#screenRoot");
const screenTitle = document.querySelector("#screenTitle");
const screenDescription = document.querySelector("#screenDescription");
const main = document.querySelector("#main");

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function restoreState() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY));
    if (!saved || !Array.isArray(saved.tickets)) return;
    state.tickets = saved.tickets;
    state.selectedTicketId = saved.selectedTicketId || null;
    state.expandedTicketId = saved.expandedTicketId || null;
    state.selectedSolutionId = saved.selectedSolutionId || null;
    state.deliveryChoice = deliveryOptions[saved.deliveryChoice] ? saved.deliveryChoice : "preview";
  } catch {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}

function persistState() {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
    tickets: state.tickets,
    selectedTicketId: state.selectedTicketId,
    expandedTicketId: state.expandedTicketId,
    selectedSolutionId: state.selectedSolutionId,
    deliveryChoice: state.deliveryChoice
  }));
}

function statusBadge(label, tone = "") {
  const toneClass = tone ? ` status-badge--${tone}` : "";
  return `<span class="status-badge${toneClass}">${escapeHtml(label)}</span>`;
}

function selectedTicket() {
  return state.tickets.find((ticket) => ticket.id === state.selectedTicketId) || null;
}

function ticketStatus(ticket) {
  if (ticket.resolution) return { label: "Decided", tone: "success" };
  if (ticket.status === "Clarifying") return { label: "Clarifying", tone: "active" };
  if (ticket.status === "Agent error") return { label: "Agent error", tone: "error" };
  if (ticket.status === "Brief ready" && !ticket.solutions.length) return { label: "Brief ready", tone: "warning" };
  if (ticket.status === "Solving") return { label: "Solving", tone: "active" };
  if (ticket.status === "PR ready") return { label: "PR ready", tone: "success" };
  if (ticket.status === "Opening PR") return { label: "Opening PR", tone: "active" };
  if (ticket.status === "PR opened") return { label: "PR opened", tone: "success" };
  if (ticket.status === "Needs human review") return { label: "Needs human review", tone: "warning" };
  if (ticket.solutions.length) return { label: "Needs decision", tone: "warning" };
  return { label: "Queued", tone: "" };   // legacy "Ready" tickets from before the agent auto-started
}

function ticketStatusControl(ticket, status) {
  return statusBadge(status.label, status.tone);
}

function ticketsTemplate() {
  if (!state.tickets.length) {
    return `
      <section class="empty-state" aria-labelledby="emptyTicketsHeading">
        <div class="empty-state__copy">
          <h2 id="emptyTicketsHeading">No tickets yet.</h2>
          <p>New tickets will appear here as they are submitted.</p>
          <button class="btn btn--primary" type="button" data-screen-link="submission">Create first ticket</button>
        </div>
      </section>
    `;
  }

  return `
    <section class="ticket-index" aria-labelledby="ticketIndexHeading">
      <div class="index-heading">
        <h2 id="ticketIndexHeading">${state.tickets.length} ${state.tickets.length === 1 ? "ticket" : "tickets"}</h2>
        <p>Newest first</p>
      </div>
      <div class="ticket-list">
        ${state.tickets.map((ticket) => `${ticketRow(ticket)}${ticketDetail(ticket, state.expandedTicketId === ticket.id)}`).join("")}
      </div>
    </section>
  `;
}

function ticketRow(ticket) {
  const status = ticketStatus(ticket);
  const expanded = state.expandedTicketId === ticket.id;
  const attemptCopy = ticket.solutions.length
    ? `${ticket.solutions.length} ${ticket.solutions.length === 1 ? "attempt" : "attempts"}`
    : "No attempts";

  return `
    <article class="ticket-row" data-expanded="${expanded}">
      <span class="ticket-row__id mono">${escapeHtml(ticket.id)}</span>
      <span class="ticket-row__main">
        <strong>${escapeHtml(ticket.title)}</strong>
        <span>${escapeHtml(ticket.repository)}</span>
      </span>
      <span class="ticket-row__attempts">${attemptCopy}</span>
      <span class="ticket-row__status">${ticketStatusControl(ticket, status)}</span>
      <button class="btn btn--quiet ticket-row__open" type="button" data-open-ticket="${escapeHtml(ticket.id)}" aria-expanded="${expanded}" aria-controls="ticketDetail-${escapeHtml(ticket.id)}">${expanded ? "Hide details" : "View ticket"}</button>
    </article>
  `;
}

// Clarification thread, shared by the ticket detail and the Solutions context. Only the reporter lens gets a reply box;
// "Retry agent" appears after an Agent error (the agent starts by itself when a ticket is created).
function clarificationBlock(ticket, { canReply }) {
  const isAgent = (name) => (name || "").toLowerCase().includes("agent");
  const awaitingReply = ticket.status === "Clarifying" && ticket.comments.length > 0 && isAgent(ticket.comments.at(-1).author);
  const placeholder = ticket.status === "Clarifying"
    ? "The agent is reading the ticket and the code…"
    : ticket.status === "Agent error" ? "The agent stopped before asking anything." : "No clarification was needed.";
  const thread = ticket.comments.length
    ? ticket.comments.map((c) => `
        <article class="thread-comment ${isAgent(c.author) ? "thread-comment--agent" : ""}">
          <header><strong>${escapeHtml(c.author)}</strong><span class="mono">${escapeHtml(c.created)}</span></header>
          <pre class="thread-body">${escapeHtml(c.body)}</pre>
        </article>`).join("")
    : `<p class="muted">${placeholder}</p>`;
  const replyBox = canReply && ticket.status === "Clarifying" ? `
        <form class="thread-reply" data-reply-form="${escapeHtml(ticket.id)}">
          <label class="field-label" for="reply-${escapeHtml(ticket.id)}">Reply as ${escapeHtml(ticket.reporter)}${awaitingReply ? "" : " (agent is thinking…)"}</label>
          <textarea class="field-textarea" id="reply-${escapeHtml(ticket.id)}" rows="3" placeholder="Answer the agent, or type confirm"></textarea>
          <div class="form-actions"><button class="btn btn--primary" type="submit" ${awaitingReply ? "" : "disabled"}>Post reply</button></div>
        </form>` : "";
  const retry = ticket.status === "Agent error" || ticket.status === "Ready"
    ? `<button class="btn btn--quiet" type="button" data-retry-agent="${escapeHtml(ticket.id)}">${ticket.status === "Ready" ? "Start agent" : "Retry agent"}</button>`
    : "";
  const errorBlock = ticket.error ? `<p class="field-help" role="alert">Agent error: ${escapeHtml(ticket.error)}</p>` : "";

  return `
      <section class="ticket-thread" aria-label="Clarification with the agent">
        <strong>Clarification</strong>
        ${errorBlock}
        ${thread}
        ${replyBox}
        ${retry}
      </section>`;
}

function briefBlock(ticket) {
  if (!ticket.briefMd) return "";
  return `
      <details class="ticket-brief" open>
        <summary><strong>Task brief</strong> · confidence ${Math.round((ticket.brief?.confidence || 0) * 100)}%</summary>
        <pre class="thread-body">${escapeHtml(ticket.briefMd)}</pre>
      </details>`;
}

// The orchestrator's PR package: subtask table, review table, and the combined diff in a collapsible block.
function prPackageBlock(ticket) {
  const p = ticket.prPackage;
  const tone = { complete: "success", partial: "warning", failed: "error", needs_human_review: "warning" }[p.status] || "";
  const subtaskRows = (p.subtasks || []).map((s) => `
        <tr><td class="mono">${escapeHtml(s.id)}</td><td>${escapeHtml(s.worker)}</td>
        <td>${statusBadge(s.status, s.status === "accepted" ? "success" : s.status === "skipped" ? "warning" : "error")}</td>
        <td>${Number(s.attempts) || 0}</td><td>${escapeHtml(s.summary || "")}</td></tr>`).join("");
  const newTests = (p.new_tests_added || []).map((t) => `<li><code>${escapeHtml(t)}</code></li>`).join("");
  // "Open PR on GitHub" (POST /open-pr) until a PR exists; then a link to it. Errors from a failed
  // attempt ride on ticket.error and the status reverts, so the button comes back for a retry.
  const openingPr = ticket.status === "Opening PR";
  const prAction = ticket.prUrl
    ? `<p><a class="btn btn--primary" href="${escapeHtml(ticket.prUrl)}" target="_blank" rel="noopener">View PR on GitHub ↗</a></p>`
    : `<p><button class="btn btn--primary" type="button" data-open-pr="${escapeHtml(ticket.id)}" ${openingPr ? 'disabled data-state="loading"' : ""}>${openingPr ? "Opening PR…" : "Open PR on GitHub"}</button>${ticket.error ? ` <span class="muted">${escapeHtml(ticket.error)}</span>` : ""}</p>`;
  return `
      <section class="ticket-thread" aria-label="PR package">
        <header class="solution-ticket-context__head">
          <div><strong>${escapeHtml(p.pr_title || p.ticket_id)}</strong></div>
          ${statusBadge(p.status.replaceAll("_", " "), tone)}
        </header>
        <p class="muted">${Number(p.tests_passed) || 0} passed · ${Number(p.tests_failed) || 0} failed · ${(p.new_tests_added || []).length} new test(s) · ${Math.round(p.duration_seconds || 0)}s</p>
        ${prAction}
        <details class="ticket-brief" open><summary><strong>PR description</strong></summary>
          <pre class="thread-body">${escapeHtml(p.pr_description || "")}</pre></details>
        <strong>Subtasks</strong>
        <table class="pr-table"><thead><tr><th>id</th><th>worker</th><th>status</th><th>attempts</th><th>summary</th></tr></thead>
          <tbody>${subtaskRows}</tbody></table>
        <strong>Review</strong>
        ${p.review ? reviewTable(p.review) : '<p class="muted">No review recorded.</p>'}
        ${newTests ? `<strong>New tests</strong><ul class="pr-list">${newTests}</ul>` : ""}
        <details class="ticket-brief"><summary><strong>Combined diff</strong> · ${(p.files_changed || []).length} file(s): ${escapeHtml((p.files_changed || []).join(", "))}</summary>
          <pre class="thread-body">${escapeHtml(p.combined_diff || "(empty)")}</pre></details>
      </section>`;
}

function reviewTable(review) {
  const icon = { pass: "✅", fail: "❌", warn: "⚠️" };
  const rows = (review.checks || []).map((c) => `
        <tr><td>${escapeHtml(c.name)}</td><td>${icon[c.result] || ""} ${escapeHtml(c.result)}</td><td>${escapeHtml(c.note || "")}</td></tr>`).join("");
  const requests = (review.change_requests || []).map((c) => `
        <li><strong>${escapeHtml(c.severity)}</strong> · <code>${escapeHtml(c.file || "—")}</code>: ${escapeHtml(c.issue)}${c.suggestion ? ` — <em>${escapeHtml(c.suggestion)}</em>` : ""}</li>`).join("");
  return `
        <p class="muted">Verdict: <strong>${escapeHtml(review.verdict)}</strong> · ${Number(review.rounds) || 1} round(s)</p>
        <table class="pr-table"><thead><tr><th>check</th><th>result</th><th>note</th></tr></thead><tbody>${rows}</tbody></table>
        ${requests ? `<ul class="pr-list">${requests}</ul>` : ""}`;
}

// Engineer-only header action: "Start solving" once the brief is ready, "Review solutions" once attempts exist.
function ticketAction(ticket) {
  if (isReporter()) return "";
  const label = ticket.prPackage ? "View PR package"
    : ticket.status === "Solving" ? "View progress"
    : ticket.status === "Brief ready" ? "Start solving"
    : ticket.solutions.length ? "Review solutions" : "";
  if (!label) return "";
  return `<button class="btn btn--primary" type="button" data-view-solutions="${escapeHtml(ticket.id)}">${label}</button>`;
}

function ticketDetail(ticket, expanded) {
  const status = ticketStatus(ticket);
  const attempts = ticket.solutions.length
    ? `${ticket.solutions.length} ${ticket.solutions.length === 1 ? "attempt" : "attempts"}`
    : "No attempts yet";
  const delivery = ticket.resolution ? deliveryOptions[ticket.resolution.delivery] : null;
  const decision = ticket.resolution && delivery
    ? `${ticket.resolution.solutionTitle} · ${delivery.title}`
    : "No decision yet";
  const engineerFacts = isReporter() ? "" : `
        <div><dt>Solutions</dt><dd>${escapeHtml(attempts)}</dd></div>
        <div><dt>Decision</dt><dd>${escapeHtml(decision)}</dd></div>`;

  return `
    <section class="ticket-detail" id="ticketDetail-${escapeHtml(ticket.id)}" tabindex="-1" aria-labelledby="ticketDetailHeading-${escapeHtml(ticket.id)}" ${expanded ? "" : "hidden"}>
      <header class="ticket-detail__head">
        <div>
          <span class="mono">${escapeHtml(ticket.id)}</span>
          <h3 id="ticketDetailHeading-${escapeHtml(ticket.id)}">Ticket details</h3>
        </div>
        ${ticketAction(ticket)}
      </header>

      <div class="ticket-description">
        <strong>Description</strong>
        <p>${escapeHtml(ticket.description || "No description was provided.")}</p>
      </div>

      <dl class="ticket-facts">
        <div><dt>Reported by</dt><dd>${escapeHtml(ticket.reporter || "Not recorded")}</dd></div>
        <div><dt>Created</dt><dd>${escapeHtml(formatTicketDate(ticket.createdAt))}</dd></div>
        <div><dt>Repository</dt><dd><code>${escapeHtml(ticket.repository)}</code></dd></div>
        <div><dt>Status</dt><dd>${statusBadge(status.label, status.tone)}</dd></div>
        ${engineerFacts}
      </dl>

      ${clarificationBlock(ticket, { canReply: isReporter() })}
      ${isReporter() ? "" : briefBlock(ticket)}
    </section>
  `;
}

function formatTicketDate(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function solutionTicketPicker(ticket) {
  const hasTickets = state.tickets.length > 0;
  const placeholder = hasTickets ? "Select a ticket" : "No tickets available";

  return `
    <section class="solution-ticket-picker" aria-labelledby="solutionTicketPickerHeading">
      <div class="solution-ticket-picker__copy">
        <h2 id="solutionTicketPickerHeading">Choose the ticket to solve.</h2>
        <p>Solutions are scoped to the ticket you select here. Nothing is chosen automatically.</p>
      </div>
      <div class="field">
        <label class="field-label" for="solutionTicketSelect">Ticket</label>
        <select class="field-select" id="solutionTicketSelect" aria-describedby="solutionTicketSelectHelp" ${hasTickets ? "" : "disabled"}>
          <option value="" ${ticket ? "" : "selected"}>${placeholder}</option>
          ${state.tickets.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === ticket?.id ? "selected" : ""}>${escapeHtml(item.id)} · ${escapeHtml(item.title)}</option>`).join("")}
        </select>
        <span class="field-help" id="solutionTicketSelectHelp">Select a ticket to see its context and attempted solutions.</span>
      </div>
    </section>
  `;
}

function solutionTicketContext(ticket) {
  const status = ticketStatus(ticket);
  const attempts = ticket.solutions.length
    ? `${ticket.solutions.length} ${ticket.solutions.length === 1 ? "attempt" : "attempts"}`
    : "No attempts yet";
  const delivery = ticket.resolution ? deliveryOptions[ticket.resolution.delivery] : null;
  const decision = ticket.resolution && delivery
    ? `${ticket.resolution.solutionTitle} · ${delivery.title}`
    : "No decision yet";

  return `
    <section class="solution-ticket-context" aria-labelledby="solutionTicketHeading">
      <header class="solution-ticket-context__head">
        <div>
          <span class="mono">${escapeHtml(ticket.id)}</span>
          <h2 id="solutionTicketHeading">${escapeHtml(ticket.title)}</h2>
        </div>
        ${statusBadge(status.label, status.tone)}
      </header>

      <div class="solution-ticket-description">
        <strong>Description</strong>
        <p>${escapeHtml(ticket.description || "No description was provided.")}</p>
      </div>

      <dl class="solution-ticket-facts">
        <div><dt>Reported by</dt><dd>${escapeHtml(ticket.reporter || "Not recorded")}</dd></div>
        <div><dt>Created</dt><dd>${escapeHtml(formatTicketDate(ticket.createdAt))}</dd></div>
        <div><dt>Repository</dt><dd><code>${escapeHtml(ticket.repository)}</code></dd></div>
        <div><dt>Attempts</dt><dd>${escapeHtml(attempts)}</dd></div>
        <div><dt>Decision</dt><dd>${escapeHtml(decision)}</dd></div>
      </dl>

      ${clarificationBlock(ticket, { canReply: false })}
      ${briefBlock(ticket)}
    </section>
  `;
}

function solutionsTemplate() {
  const ticket = selectedTicket();
  const ticketPicker = solutionTicketPicker(ticket);

  if (!ticket) {
    return `
      <section class="solutions-selection">
        ${ticketPicker}
        <div class="solution-selection-empty" aria-labelledby="emptySolutionsHeading">
          <div>
            <h3 id="emptySolutionsHeading">No ticket selected.</h3>
            <p>${state.tickets.length ? "Choose a ticket above to review its details and start solving." : "Create a ticket first, then return here to start solving."}</p>
            ${state.tickets.length ? "" : '<button class="btn btn--primary" type="button" data-screen-link="submission">Create ticket</button>'}
          </div>
        </div>
      </section>
    `;
  }

  if (ticket.prPackage) {
    return `
      <section class="solutions-workspace">
        ${ticketPicker}
        ${solutionTicketContext(ticket)}
        ${prPackageBlock(ticket)}
      </section>
    `;
  }

  if (!ticket.solutions.length) {
    const solving = ticket.status === "Solving";
    const ready = ticket.status === "Brief ready";
    const hint = solving
      ? "The orchestrator is planning subtasks, patching the shared workspace and running tests. The PR package will appear here."
      : ready
        ? "The brief is confirmed. Start solving to run the orchestrator (code → tests → docs → review)."
        : "The intake agent has not produced a confirmed brief yet.";
    return `
      <section class="solutions-workspace">
        ${ticketPicker}
        ${solutionTicketContext(ticket)}
        <div class="solution-empty" aria-labelledby="noAttemptsHeading">
          <div class="solution-empty__body">
            <h3 id="noAttemptsHeading">${solving ? "Solving…" : "No PR package yet."}</h3>
            <p>${hint}</p>
            <button class="btn btn--primary" type="button" data-solve-brief="${escapeHtml(ticket.id)}" ${ready ? "" : "disabled"} ${solving ? 'data-state="loading"' : ""}>
              ${solving ? "Orchestrator running" : "Start solving"}
            </button>
          </div>
        </div>
      </section>
    `;
  }

  const solution = ticket.solutions.find((item) => item.id === state.selectedSolutionId) || ticket.solutions[0];
  state.selectedSolutionId = solution.id;
  const currentDecision = ticket.resolution
    ? `<div class="decision-banner" role="status" tabindex="-1"><strong>Current decision</strong><span>${escapeHtml(ticket.resolution.solutionTitle)} · ${escapeHtml(deliveryOptions[ticket.resolution.delivery].title)}</span></div>`
    : "";

  return `
    <section class="solutions-workspace">
      ${ticketPicker}
      ${solutionTicketContext(ticket)}

      ${currentDecision}

      <div class="solution-layout">
        <div class="solution-index" aria-label="Attempted solutions">
          <div class="solution-index__head">
            <h3>Attempts</h3>
            <span>Pick one</span>
          </div>
          ${ticket.solutions.map((item) => solutionOption(item, solution.id)).join("")}
        </div>

        ${solutionDetail(ticket, solution)}
      </div>
    </section>
  `;
}

function solutionOption(solution, selectedId) {
  const selected = solution.id === selectedId;
  return `
    <article class="solution-option" data-selected="${selected}">
      <span class="solution-option__top">
        <button class="solution-pick" type="button" data-solution-id="${escapeHtml(solution.id)}" aria-pressed="${selected}">Candidate ${escapeHtml(solution.label)}</button>
        ${statusBadge(solution.verdict, solution.tone)}
      </span>
      <strong>${escapeHtml(solution.title)}</strong>
      <span>${escapeHtml(solution.summary)}</span>
    </article>
  `;
}

function solutionDetail(ticket, solution) {
  const delivery = deliveryOptions[state.deliveryChoice];
  const branch = `agent/${ticket.id.toLowerCase()}-${solution.label.toLowerCase()}`;

  return `
    <article class="solution-detail" tabindex="-1" aria-labelledby="solutionDetailHeading">
      <header class="solution-detail__head">
        <div>
          <span class="mono">Candidate ${escapeHtml(solution.label)}</span>
          <h3 id="solutionDetailHeading">${escapeHtml(solution.title)}</h3>
        </div>
        ${statusBadge(solution.verdict, solution.tone)}
      </header>

      <p class="solution-summary">${escapeHtml(solution.rationale)}</p>

      <dl class="detail-list">
        <div>
          <dt>Reviewer</dt>
          <dd>${escapeHtml(solution.review)}</dd>
        </div>
        <div>
          <dt>Changed</dt>
          <dd>${escapeHtml(solution.changed)}</dd>
        </div>
        <div>
          <dt>Verification</dt>
          <dd>${escapeHtml(solution.verification)}</dd>
        </div>
        <div>
          <dt>Branch</dt>
          <dd><code>${escapeHtml(branch)}</code></dd>
        </div>
      </dl>

      <form class="delivery-form" id="deliveryForm">
        <fieldset>
          <legend>Decide how to ship it</legend>
          <p>Choose a destination for Candidate ${escapeHtml(solution.label)}.</p>
          <div class="delivery-options">
            ${Object.entries(deliveryOptions).map(([value, option]) => deliveryOption(value, option)).join("")}
          </div>
        </fieldset>
        <button class="btn btn--primary" type="submit">${escapeHtml(ticket.resolution ? "Update decision" : delivery.action)}</button>
      </form>
    </article>
  `;
}

function deliveryOption(value, option) {
  const checked = state.deliveryChoice === value;
  return `
    <label class="delivery-option" data-checked="${checked}">
      <input type="radio" name="delivery" value="${value}" ${checked ? "checked" : ""} />
      <span>
        <strong>${escapeHtml(option.title)}</strong>
        <small>${escapeHtml(option.description)}</small>
      </span>
    </label>
  `;
}

function submissionTemplate() {
  return `
    <section class="submission-screen" aria-labelledby="newTicketHeading">
      <div class="form-intro">
        <button class="back-link" type="button" data-screen-link="tickets">← Tickets</button>
        <h2 id="newTicketHeading">Add work to the queue.</h2>
        <p>Include the observed behavior and expected outcome in one pass.</p>
      </div>

      <form class="ticket-form" id="ticketForm" novalidate>
        <div class="error-summary" id="ticketErrorSummary" role="alert" tabindex="-1" hidden>
          <strong>Add the missing ticket details.</strong>
          <span>Title, description, and reporter are required.</span>
        </div>

        <div class="field">
          <label class="field-label" for="ticketTitle">Ticket title <span class="field-required">Required</span></label>
          <input class="field-input" id="ticketTitle" name="title" type="text" autocomplete="off" aria-required="true" aria-describedby="ticketTitleHelp" placeholder="Checkout action stays disabled" />
          <span class="field-help" id="ticketTitleHelp">Name the observed problem, not the suspected fix.</span>
        </div>

        <div class="field">
          <label class="field-label" for="ticketDescription">Problem and expected outcome <span class="field-required">Required</span></label>
          <textarea class="field-textarea" id="ticketDescription" name="description" aria-required="true" aria-describedby="ticketDescriptionHelp" placeholder="After updating a valid shipping address…"></textarea>
          <span class="field-help" id="ticketDescriptionHelp">Describe what happened, what you expected, and how to reproduce it.</span>
        </div>

        <div class="field">
          <label class="field-label" for="ticketReporter">Reported by <span class="field-required">Required</span></label>
          <input class="field-input" id="ticketReporter" name="reporter" type="text" autocomplete="name" aria-required="true" aria-describedby="ticketReporterHelp" placeholder="Name or team" />
          <span class="field-help" id="ticketReporterHelp">This appears in the ticket details so the team knows who supplied the context.</span>
        </div>

        <div class="field">
          <label class="field-label" for="ticketRepository">Repository</label>
          <select class="field-select" id="ticketRepository" name="repository">
            <option>demo_repo (Notely)</option>
          </select>
          <span class="field-help">The solver reads this repository when the ticket is started.</span>
        </div>

        <div class="form-actions">
          <button class="btn btn--primary" id="createTicket" type="submit">Create ticket</button>
          <button class="btn btn--quiet" type="button" data-screen-link="tickets">Cancel</button>
        </div>
      </form>
    </section>
  `;
}

function setupTemplate() {
  return `
    <section class="setup-screen" aria-labelledby="setupHeading">
      <div class="setup-intro">
        <h2 id="setupHeading">One repository, clear limits.</h2>
        <p>These prototype controls define where tickets run and when the solver stops.</p>
      </div>

      <form class="setup-form" id="setupForm">
        <div class="setting-row">
          <div>
            <strong>GitHub</strong>
            <span>Repository access</span>
          </div>
          ${statusBadge("Prototype connected", "success")}
        </div>

        <div class="setting-row setting-row--field">
          <div>
            <label class="field-label" for="setupRepository">Default repository</label>
            <span>New tickets start here.</span>
          </div>
          <select class="field-select" id="setupRepository">
            <option>demo_repo (Notely)</option>
          </select>
        </div>

        <div class="setting-row setting-row--field">
          <div>
            <label class="field-label" for="attemptLimit">Attempt limit</label>
            <span>Maximum candidate branches per ticket.</span>
          </div>
          <select class="field-select" id="attemptLimit">
            <option>3 attempts</option>
            <option>4 attempts</option>
            <option>5 attempts</option>
          </select>
        </div>

        <div class="setting-row setting-row--field">
          <div>
            <label class="field-label" for="timeLimit">Time limit</label>
            <span>Stop a solve session at this boundary.</span>
          </div>
          <select class="field-select" id="timeLimit">
            <option>20 minutes</option>
            <option>30 minutes</option>
            <option>45 minutes</option>
          </select>
        </div>

        <div class="setup-actions">
          <button class="btn btn--primary" id="saveSetup" type="submit">Save setup</button>
        </div>
      </form>
    </section>
  `;
}

function renderScreen(screenName, { focus = true, updateHash = true } = {}) {
  const normalized = routeAliases[screenName] || screenName;
  const requested = screens[normalized] ? normalized : "tickets";
  const redirected = requested === "solutions" && isReporter();   // reporters have no Solutions screen
  const nextScreen = redirected ? "tickets" : requested;
  state.currentScreen = nextScreen;

  if ((updateHash || redirected) && window.location.hash !== `#${nextScreen}`) {
    window.history.pushState(null, "", `#${nextScreen}`);
  }

  const meta = screens[nextScreen];
  screenTitle.textContent = meta.title;
  screenDescription.textContent = meta.description;
  document.title = "Quorum";

  const templates = {
    tickets: ticketsTemplate,
    solutions: solutionsTemplate,
    submission: submissionTemplate,
    setup: setupTemplate
  };

  screenRoot.innerHTML = templates[nextScreen]();
  document.querySelectorAll(".tab-link").forEach((tab) => {
    const current = tab.dataset.screenLink === nextScreen;
    tab.setAttribute("aria-current", current ? "page" : "false");
  });

  applyRole();
  bindScreen(nextScreen);
  if (focus) main.focus({ preventScroll: true });
}

function bindScreen(screenName) {
  if (screenName === "tickets") bindTickets();
  if (screenName === "solutions") bindSolutions();
  if (screenName === "submission") bindSubmission();
  if (screenName === "setup") bindSetup();
}

function bindTickets() {
  document.querySelectorAll("[data-open-ticket]").forEach((button) => {
    button.addEventListener("click", () => {
      const ticket = state.tickets.find((item) => item.id === button.dataset.openTicket);
      if (!ticket) return;
      const expanding = state.expandedTicketId !== ticket.id;
      state.expandedTicketId = expanding ? ticket.id : null;
      persistState();
      renderScreen("tickets", { focus: false, updateHash: false });
      document.querySelector(`[data-open-ticket="${ticket.id}"]`)?.focus({ preventScroll: true });
    });
  });

  document.querySelectorAll("[data-reply-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const ticket = state.tickets.find((item) => item.id === form.dataset.replyForm);
      const box = form.querySelector("textarea");
      const body = box.value.trim();
      if (!ticket || !body) return;
      form.querySelector("button").disabled = true;
      await api(`/api/tickets/${ticket.id}/comments`, { author: ticket.reporter, body });
      await syncTickets();
      renderScreen("tickets", { focus: false, updateHash: false });
    });
  });

  // Engineer: "Start solving" / "Review solutions" open the Solutions screen (the intake agent has already run).
  document.querySelectorAll("[data-view-solutions]").forEach((button) => {
    button.addEventListener("click", () => {
      const ticket = state.tickets.find((item) => item.id === button.dataset.viewSolutions);
      if (!ticket || isReporter()) return;
      state.selectedTicketId = ticket.id;
      state.selectedSolutionId = ticket.resolution?.solutionId || ticket.solutions[0]?.id || null;
      state.deliveryChoice = ticket.resolution?.delivery || "preview";
      persistState();
      renderScreen("solutions");
    });
  });

  // Manual retry after "Agent error" — POST /solve re-runs the intake agent on the same ticket.
  document.querySelectorAll("[data-retry-agent]").forEach((button) => {
    button.addEventListener("click", async () => {
      const ticket = state.tickets.find((item) => item.id === button.dataset.retryAgent);
      if (!ticket) return;
      button.disabled = true; button.textContent = "Starting agent…";
      try { await api(`/api/tickets/${ticket.id}/solve`, {}); } catch (err) { alert(err.message); }
      state.expandedTicketId = ticket.id;
      await syncTickets();
      renderScreen("tickets", { focus: false, updateHash: false });
    });
  });
}

function bindSolutions() {
  document.querySelector("#solutionTicketSelect")?.addEventListener("change", (event) => {
    const ticket = state.tickets.find((item) => item.id === event.currentTarget.value) || null;
    state.selectedTicketId = ticket?.id || null;
    state.selectedSolutionId = ticket?.resolution?.solutionId || ticket?.solutions[0]?.id || null;
    state.deliveryChoice = ticket?.resolution?.delivery || "preview";
    persistState();
    renderScreen("solutions", { focus: false, updateHash: false });
    document.querySelector("#solutionTicketSelect")?.focus({ preventScroll: true });
  });

  const ticket = selectedTicket();
  if (!ticket) return;

  // Engineer: "Start solving" runs the orchestrator on the confirmed brief (POST /solve-brief).
  // Polling picks up "Solving" → "PR ready" / "Needs human review" and re-renders this screen.
  document.querySelector("[data-solve-brief]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.dataset.state = "loading";
    button.textContent = "Starting orchestrator…";
    try { await api(`/api/tickets/${ticket.id}/solve-brief`, {}); } catch (err) { alert(err.message); }
    await syncTickets();
    renderScreen("solutions", { focus: false, updateHash: false });
  });

  // Engineer: "Open PR on GitHub" applies the stored package diff to a clone of the ticket repo's
  // GitHub remote and opens a real PR (POST /open-pr). Polling picks up "PR opened" + the URL.
  document.querySelector("[data-open-pr]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.dataset.state = "loading";
    button.textContent = "Opening PR…";
    try { await api(`/api/tickets/${ticket.id}/open-pr`, {}); } catch (err) { alert(err.message); }
    await syncTickets();
    renderScreen("solutions", { focus: false, updateHash: false });
  });

  document.querySelectorAll("[data-solution-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedSolutionId = button.dataset.solutionId;
      persistState();
      renderScreen("solutions", { focus: false, updateHash: false });
      document.querySelector(".solution-detail")?.focus({ preventScroll: true });
    });
  });

  document.querySelectorAll('input[name="delivery"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      state.deliveryChoice = radio.value;
      persistState();
      document.querySelectorAll(".delivery-option").forEach((label) => {
        const input = label.querySelector("input");
        label.dataset.checked = String(input.checked);
      });
      const submit = document.querySelector('#deliveryForm button[type="submit"]');
      if (submit && !ticket.resolution) submit.textContent = deliveryOptions[state.deliveryChoice].action;
    });
  });

  document.querySelector("#deliveryForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const solution = ticket.solutions.find((item) => item.id === state.selectedSolutionId);
    if (!solution) return;
    ticket.resolution = {
      solutionId: solution.id,
      solutionTitle: solution.title,
      delivery: state.deliveryChoice
    };
    ticket.status = "Decided";
    persistState();
    renderScreen("solutions", { focus: false, updateHash: false });
    document.querySelector(".decision-banner")?.focus({ preventScroll: true });
  });
}

function bindSubmission() {
  const form = document.querySelector("#ticketForm");
  const title = document.querySelector("#ticketTitle");
  const description = document.querySelector("#ticketDescription");
  const reporter = document.querySelector("#ticketReporter");

  [title, description, reporter].forEach((field) => {
    field.addEventListener("blur", () => validateRequired(field));
    field.addEventListener("input", () => {
      if (field.dataset.touched === "true") validateRequired(field);
    });
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const titleValid = validateRequired(title);
    const descriptionValid = validateRequired(description);
    const reporterValid = validateRequired(reporter);
    const summary = document.querySelector("#ticketErrorSummary");

    if (!titleValid || !descriptionValid || !reporterValid) {
      summary.hidden = false;
      summary.focus({ preventScroll: true });
      return;
    }

    summary.hidden = true;
    const button = document.querySelector("#createTicket");
    button.dataset.state = "loading";
    button.disabled = true;
    button.textContent = "Creating ticket";

    api("/api/tickets", {
      title: title.value.trim(),
      description: description.value.trim(),
      reporter: reporter.value.trim(),
      repository: document.querySelector("#ticketRepository").value
    }).then(async (created) => {
      await syncTickets();
      state.selectedTicketId = null;
      state.expandedTicketId = created.key;
      state.selectedSolutionId = null;
      persistState();
      renderScreen("tickets");
    }).catch((err) => {
      button.dataset.state = ""; button.disabled = false; button.textContent = "Create ticket";
      alert("Could not create ticket: " + err.message);
    });
  });
}

function validateRequired(field) {
  const valid = Boolean(field.value.trim());
  field.dataset.touched = "true";
  field.setAttribute("aria-invalid", String(!valid));
  const helper = document.querySelector(`#${field.getAttribute("aria-describedby")}`);

  if (!valid) {
    helper.setAttribute("role", "alert");
    helper.textContent = {
      ticketTitle: "The ticket needs a title. Name the observed problem.",
      ticketDescription: "The ticket needs context. Add what happened and what you expected.",
      ticketReporter: "The ticket needs a reporter. Add the person or team who supplied it."
    }[field.id];
  } else {
    helper.removeAttribute("role");
    helper.textContent = {
      ticketTitle: "Name the observed problem, not the suspected fix.",
      ticketDescription: "Describe what happened, what you expected, and how to reproduce it.",
      ticketReporter: "This appears in the ticket details so the team knows who supplied the context."
    }[field.id];
  }

  return valid;
}

function bindSetup() {
  document.querySelector("#setupForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const button = document.querySelector("#saveSetup");
    button.dataset.state = "loading";
    button.textContent = "Saving setup";
    window.setTimeout(() => {
      button.dataset.state = "success";
      button.textContent = "Setup saved";
      window.setTimeout(() => {
        button.dataset.state = "";
        button.textContent = "Save setup";
      }, 1500);
    }, 400);
  });
}

function nextTicketId() {
  const highest = state.tickets.reduce((max, ticket) => {
    const number = Number(ticket.id.split("-").pop());
    return Number.isFinite(number) ? Math.max(max, number) : max;
  }, 0);
  return `QT-${String(highest + 1).padStart(3, "0")}`;
}

document.addEventListener("click", (event) => {
  const roleButton = event.target.closest("[data-role]");
  if (roleButton) {
    setRole(roleButton.dataset.role);
    return;
  }
  const link = event.target.closest("[data-screen-link]");
  if (!link) return;
  event.preventDefault();
  if (link.classList.contains("tab-link") && link.dataset.screenLink === "solutions") {
    state.selectedTicketId = null;
    state.selectedSolutionId = null;
    state.deliveryChoice = "preview";
    persistState();
  }
  renderScreen(link.dataset.screenLink);
});

window.addEventListener("hashchange", () => {
  const route = window.location.hash.slice(1);
  const normalized = routeAliases[route] || route;
  if (screens[normalized] && normalized !== state.currentScreen) {
    renderScreen(normalized, { updateHash: false });
  }
});

const initialRoute = window.location.hash.slice(1);
const normalizedInitialRoute = routeAliases[initialRoute] || initialRoute;
if (normalizedInitialRoute === "solutions") {
  state.selectedTicketId = null;
  state.selectedSolutionId = null;
  state.deliveryChoice = "preview";
  persistState();
}
syncTickets().then(() => {
  renderScreen(screens[normalizedInitialRoute] ? normalizedInitialRoute : "tickets", {
    focus: false,
    updateHash: !screens[normalizedInitialRoute]
  });
  startPolling();
});
