/* Smart Material AI Chat - Planner agent UI.
 * Talks to the A2A JSON-RPC endpoint (message/send) and renders the Task it returns:
 * narrated markdown per section, CSV FileParts.
 */
(() => {
  "use strict";

  // Same origin when served by FastAPI (/ui); override with ?api=http://host:port
  const params = new URLSearchParams(location.search);
  const API_BASE = params.get("api") || (location.protocol.startsWith("http") ? location.origin : "http://localhost:8000");
  const AGENT_URL = `${API_BASE}/a2a/planner`;

  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"), form: $("composer"), input: $("input"), send: $("send"),
    conn: $("conn"), examples: $("examples"), desc: $("agentDesc"),
    newChat: $("newChat"), stepsPane: $("stepsPane"),
  };

  const DEFAULT_EXAMPLES = [
    ["Work order", "What is short for work order 121060001, and what is aged?"],
    ["Shortage", "Which work orders are short for MRP controller ZMA?"],
    ["Aging", "Show aged material in A301"],
    ["Over-issue", "Any over-issued materials?"],
    ["Control status", "Show non-controlled material"],
    ["Production order", "Production order status 121060010"],
    ["Inventory", "Where is material MTINS29300 and how much is available?"],
    ["Review", "Show machine-head material needing review"],
  ];

  let contextId = null;
  let busy = false;

  // ------------------------------------------------------------------ init
  init();

  async function init() {
    renderExamples(DEFAULT_EXAMPLES);
    try {
      await Promise.all([
        fetchJson(`${AGENT_URL}/.well-known/agent-card.json`),
        fetchJson(`${API_BASE}/health`),
      ]);
      setConn("ok", "Agent online");
      if (params.get("q")) ask(params.get("q")); // deep link: /ui/?q=...
    } catch (e) {
      setConn("err", "Agent offline");
      els.desc.textContent = `Cannot reach ${AGENT_URL}. Start the backend: python -m uvicorn app.main:app --port 8000`;
    }
  }

  function renderExamples(list) {
    els.examples.innerHTML = "";
    for (const [tag, q] of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "example";
      b.innerHTML = `<small>${esc(tag)}</small>${esc(q)}`;
      b.addEventListener("click", () => ask(q));
      els.examples.appendChild(b);
    }
  }

  // ------------------------------------------------------------------ events
  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = els.input.value.trim();
    if (q) ask(q);
  });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); els.form.requestSubmit(); }
  });
  els.input.addEventListener("input", autoGrow);
  els.newChat.addEventListener("click", () => {
    contextId = null;
    els.messages.innerHTML = "";
    els.messages.appendChild(welcome());
    els.stepsPane.innerHTML = `<p class="muted">The steps the agent takes for your latest question show up here.</p>`;
    els.input.focus();
  });

  function autoGrow() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 140) + "px";
  }

  // ------------------------------------------------------------------ A2A call
  async function ask(question) {
    if (busy) return;
    busy = true;
    els.send.disabled = true;
    els.input.value = "";
    autoGrow();
    document.querySelector(".welcome")?.remove();

    addUser(question);
    const pending = addTyping();
    const started = performance.now();

    try {
      const task = await send(question);
      pending.remove();
      contextId = task.contextId;
      setConn("ok", "Agent online");
      await addAgent(task, performance.now() - started, question);
    } catch (e) {
      pending.remove();
      addError(e.message || String(e));
      if (e instanceof TypeError) setConn("err", "Agent offline");
    } finally {
      busy = false;
      els.send.disabled = false;
      els.input.focus();
    }
  }

  /** One A2A message/send; `exportFormat` ("xlsx" or "csv") asks the agent to attach the data as a file. */
  async function send(question, exportFormat = "") {
    const message = {
      kind: "message", role: "user", messageId: uuid(),
      parts: [{ kind: "text", text: question }],
    };
    if (exportFormat) message.parts.push({ kind: "data", data: { export: true, format: exportFormat } });
    if (contextId) message.contextId = contextId;
    const res = await fetchJson(AGENT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: uuid(), method: "message/send", params: { message } }),
    });
    if (res.error) throw new Error(`${res.error.message}${res.error.data?.correlationId ? ` (ref ${res.error.data.correlationId})` : ""}`);
    return res.result;
  }

  const fileParts = (task) => (task.artifacts || []).flatMap((a) => a.parts).filter((p) => p.kind === "file").map((p) => p.file);

  /** "Would you like a report?" prompt under an answer; the button fetches the CSV and downloads it. */
  function reportPrompt(question) {
    const foot = el("div", "card-foot report-prompt");
    foot.appendChild(el("span", "report-q", "Would you like to generate a report of this answer?"));
    for (const [format, label] of [["xlsx", "Excel"], ["csv", "CSV"]]) {
      const btn = el("button", format === "xlsx" ? "btn-report" : "btn-report btn-report-alt", label);
      btn.type = "button";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Generating…";
        try {
          const files = fileParts(await send(question, format));
          if (!files.length) throw new Error("no data to export");
          const day = new Date().toISOString().slice(0, 10);
          files.forEach((f) => download({ ...f, name: f.name.replace(/\.(csv|xlsx)$/, `_report_${day}.$1`) }));
          btn.textContent = "✓ Downloaded";
          setTimeout(() => { btn.textContent = label; btn.disabled = false; }, 2500);
        } catch (e) {
          btn.textContent = label;
          btn.disabled = false;
          addError(e.message || String(e), "Report could not be generated");
        }
      });
      foot.appendChild(btn);
    }
    return foot;
  }

  // ------------------------------------------------------------------ rendering
  function addUser(text) {
    const div = el("div", "msg msg-user");
    div.appendChild(el("div", "bubble", text));
    push(div);
  }

  /** Assistant message row: avatar + bubble. */
  function agentRow(child) {
    const div = el("div", "msg msg-agent");
    div.appendChild(el("div", "avatar", "AI"));
    div.appendChild(child);
    return div;
  }

  function addTyping() {
    const card = el("div", "card");
    card.innerHTML = `<div class="typing"><span></span><span></span><span></span></div>`;
    return push(agentRow(card));
  }

  function addError(text, prefix = "Could not get an answer") {
    push(agentRow(el("div", "error", `${prefix}: ${text}`)));
  }

  async function addAgent(task, ms, question) {
    const arts = task.artifacts || [];
    const textPart = task.status?.message?.parts?.find((p) => p.kind === "text");
    const data = arts.flatMap((a) => a.parts).find((p) => p.kind === "data")?.data || {};
    const files = fileParts(task);
    const results = data.results || [];

    const card = el("div", "card");
    const row = push(agentRow(card));
    if (data.trace?.length) {
      card.innerHTML = `<div class="typing"><span></span><span></span><span></span></div>`;
      await playSteps(data.trace, ms, question);
      card.innerHTML = "";
    }

    const body = el("div", "card-body md answer-reveal");
    const { intro, sections } = splitSections(textPart?.text || "");
    if (intro) body.appendChild(htmlBlock(renderMarkdown(intro)));
    sections.forEach((sec, i) => body.appendChild(renderSection(sec, results[i])));
    if (!sections.length && !intro) body.appendChild(htmlBlock(renderMarkdown(textPart?.text || "")));
    card.appendChild(body);

    // footer: offer a report when the answer has data behind it (or show the files already attached)
    if (files.length) {
      const foot = el("div", "card-foot");
      for (const f of files) {
        const b = el("button", "btn-small", `⬇ Download ${f.name}`);
        b.type = "button";
        b.addEventListener("click", () => download(f));
        foot.appendChild(b);
      }
      card.appendChild(foot);
    } else if (question && results.some((r) => r.rowCount > 0)) {
      card.appendChild(reportPrompt(question));
    }
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // How long each kind of step stays "running" before it ticks off, plus a little jitter.
  const STEP_MS = { plan: 500, data: 750, rule: 650, answer: 550 };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /** Agent steps in the right-hand pane: each step runs (spinner + live label), then completes (tick + result). */
  async function playSteps(steps, ms, question) {
    const pane = els.stepsPane;
    pane.innerHTML = "";
    if (question) pane.appendChild(el("div", "steps-q", question));
    const meta = el("div", "steps-meta", "Working…");
    const list = el("ol", "step-list");
    pane.append(meta, list);

    for (const s of steps) {
      const li = el("li", "step running");
      li.innerHTML = `<span class="step-icon" aria-hidden="true"></span><div class="step-text">
        <div class="step-title">${esc(s.active || s.title)}<span class="step-dots"></span></div>
        <div class="step-src">${esc(s.source)}</div></div>`;
      list.appendChild(li);
      li.scrollIntoView({ behavior: "smooth", block: "nearest" });
      await sleep((STEP_MS[s.kind] || 600) + Math.random() * 350);
      li.classList.replace("running", "done");
      li.querySelector(".step-title").textContent = s.title;
      if (s.detail) li.querySelector(".step-text").appendChild(el("div", "step-detail", s.detail));
    }
    meta.textContent =
      `Done · ${steps.length} steps · ${ms < 1000 ? `${Math.max(1, Math.round(ms))} ms` : `${(ms / 1000).toFixed(1)} s`}`;
  }

  const STATUS = {
    critical: ["Action needed", "red"],
    warning: ["Worth a look", "amber"],
    ok: ["All good", "green"],
  };

  // The chat shows each answer as a short paragraph; the tables stay in the A2A text and CSV export.
  function renderSection(sec, result) {
    const div = el("div", "section");
    const title = el("div", "section-title");
    const st = STATUS[result?.severity];
    title.innerHTML = `<span class="topic">${inline(sec.title)}</span>
      ${st ? `<span class="status status-${st[1]}">${st[0]}</span>` : ""}${result ? resultBadge(result.resultType) : ""}`;
    div.appendChild(title);
    if (result?.headline) {
      // First sentence is the answer in one line; the rest explains it.
      const parts = result.headline.split(/(?<=[.!?])\s+(?=[A-Z0-9"])/);
      div.appendChild(htmlBlock(`<p class="lead">${highlight(parts[0])}</p>`
        + (parts.length > 1 ? `<p class="answer-body">${highlight(parts.slice(1).join(" "))}</p>` : "")));
    } else {
      div.appendChild(htmlBlock(renderMarkdown(sec.body)));
    }
    return div;
  }

  /** Bold the numbers and dates, and show material codes as quiet tags, so key facts stand out. */
  function highlight(text) {
    return esc(text)
      .replace(/\(([A-Z][A-Z0-9-]{5,})\)/g, '<span class="code">$1</span>')
      .replace(/\b(\d{4}-\d{2}-\d{2}|\d[\d,]*(?:\.\d+)?)(%|\s(?:EA|KG|M|CNY|days))?(?![\w-]|\.\d)/g, "<b>$&</b>");
  }

  function resultBadge(type) {
    return {
      deterministic: "",
      needs_review: pill("Needs review", "blue"),
      not_found: pill("Not found", "red"),
      info: "",
    }[type] ?? "";
  }

  // ------------------------------------------------------------------ markdown (subset used by the agent)
  function splitSections(text) {
    const cleaned = text.split("\n")
      .filter((l) => !/^_(Data as of|Attached CSV)/.test(l.trim()))
      .join("\n").trim();
    const parts = cleaned.split(/\n\s*---\s*\n/);
    let intro = "";
    const sections = [];
    for (const p of parts) {
      const idx = p.indexOf("### ");
      if (idx === -1) { intro += p; continue; }
      if (idx > 0) intro += p.slice(0, idx);
      const rest = p.slice(idx + 4);
      const nl = rest.indexOf("\n");
      sections.push({ title: nl === -1 ? rest : rest.slice(0, nl), body: nl === -1 ? "" : rest.slice(nl + 1) });
    }
    return { intro: intro.trim(), sections };
  }

  function renderMarkdown(md) {
    const lines = md.split("\n");
    let html = "";
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim().startsWith("|")) {
        const block = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) block.push(lines[i++]);
        html += table(block);
        continue;
      }
      if (/^\s*-\s+/.test(line)) {
        html += "<ul>";
        while (i < lines.length && /^\s*-\s+/.test(lines[i])) html += `<li>${inline(lines[i++].replace(/^\s*-\s+/, ""))}</li>`;
        html += "</ul>";
        continue;
      }
      if (/^---+$/.test(line.trim())) { html += "<hr>"; i++; continue; }
      if (line.includes(" | ")) { // "Qty 8,000 | Start ... | MRP ZMA" -> fact chips
        html += `<div class="fact-chips">${line.split(" | ").map((f) => `<span class="chip">${inline(f.trim())}</span>`).join("")}</div>`;
        i++;
        continue;
      }
      if (line.trim()) {
        const isNote = /^_.*_\.?$/.test(line.trim());
        html += `<p${isNote ? ' class="note"' : ""}>${inline(line)}</p>`;
      }
      i++;
    }
    return html;
  }

  function table(rows) {
    const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const header = cells(rows[0]);
    const body = rows.slice(1).filter((r) => !/^\|?\s*-{3}/.test(r.trim().replace(/^\|/, "")));
    let h = `<div class="table-wrap"><table><thead><tr>${header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>`;
    for (const r of body) {
      h += "<tr>" + cells(r).map((c) => {
        const num = /^-?[\d,]+(\.\d+)?( [A-Z]{1,3})?$/.test(c) && !/^\d{9}$/.test(c); // 9 digits = order number, not a quantity
        return `<td${num ? ' class="num"' : ""}>${decorate(c)}</td>`;
      }).join("") + "</tr>";
    }
    return h + "</tbody></table></div>";
  }

  // Status words -> coloured pills so the eye finds exceptions quickly.
  const STATUS_PILLS = [
    [/^Short - warehouse insufficient.*/, "red"], [/^Short at line.*/, "amber"], [/^Covered.*/, "green"],
    [/^Over-control \(reasonable.*/, "purple"], [/^Over-issued.*/, "purple"], [/^Over-control \/ over-issued.*/, "red"],
    [/^Non-controlled.*/, "blue"], [/^Available to issue.*/, "green"], [/^Order closed.*|^Order not released.*|^NMVT.*/, "grey"],
    [/^Warehouse stock$/, "grey"], [/^No open demand$/, "grey"],
  ];

  function decorate(c) {
    if (c === "-") return '<span class="muted">–</span>';
    const aged = c.match(/^(.*) \(AGED\)$/);
    if (aged) return `${esc(aged[1])} ${pill("Aged", "amber")}`;
    for (const [re, color] of STATUS_PILLS) if (re.test(c)) return pill(c, color);
    return inline(c);
  }

  function inline(s) {
    return esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|\s)_([^_].*?)_(?=[\s.,]|$)/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  // ------------------------------------------------------------------ utils
  function download(file) {
    const bin = atob(file.bytes);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: file.mimeType || "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: file.name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function fetchJson(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  function setConn(state, text) {
    els.conn.className = `conn conn-${state}`;
    els.conn.title = text;
  }

  function push(node) {
    els.messages.appendChild(node);
    node.scrollIntoView({ behavior: "smooth", block: "start" });
    return node;
  }

  function welcome() {
    const d = el("div", "welcome");
    d.innerHTML = `<h1>What would you like to know?</h1><p>Ask about work order shortage, aged material, over-issued material,
      production orders or material stock. Any answer can be downloaded as a report.</p>`;
    return d;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function htmlBlock(html) {
    const d = document.createElement("div");
    d.innerHTML = html;
    return d;
  }

  function pill(text, color) {
    return `<span class="pill pill-${color}">${esc(text)}</span>`;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
})();
