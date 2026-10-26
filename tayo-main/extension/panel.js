/* TAYO side-panel controller.
 * All network calls go to the local backend. No third-party scripts.
 */

const DEFAULT_BACKEND = "http://127.0.0.1:8000";
const MAX_TIMEOUT = 1000 * 60 * 10;

const state = {
  backendUrl: DEFAULT_BACKEND,
  virusTotalApiKey: "",
  activeTab: "factcheck",
  imageFile: null,
  imageObjectUrl: null,
  videoFile: null,
  lastHandled: 0,
  locale: "mn",
  lastUrlResult: null,
  claimSources: null,
  claimSourcesMeta: null,
};

const hasChrome = typeof chrome !== "undefined" && Boolean(chrome.storage);

/* ---------------- Tiny DOM helpers ---------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  node.replaceChildren();
  return node;
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "n/a";
  }
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function languageLabel(language) {
  return {
    mn_cyrl: "Монгол кирилл",
    mn_latn: "Монгол латин галиг",
    en: "English",
    other: "Other / unknown",
  }[language] || null;
}

function scopeLabel(scope) {
  return {
    auto: tr("Автомат", "Auto"),
    medical: tr("Эрүүл мэнд", "Medical & public health"),
    fact_check: tr("Баримт шалгалт", "Published fact-checks"),
    all: tr("Бүх эх сурвалж", "All trusted sources"),
  }[scope] || scope || tr("Автомат", "Auto");
}

function relationLabel(relation) {
  return {
    contradicts: tr("Эсэргүүцэж байна", "Contradicts"),
    supports: tr("Дэмжиж байна", "Supports"),
    supports_context: tr("Дэмжих контекст", "Supporting context"),
    fact_check: tr("Баримт шалгалт", "Fact-check"),
    context: tr("Контекст", "Context"),
    related: tr("Холбоотой", "Related"),
    search: tr("Хайлтын холбоос", "Search link"),
  }[relation] || tr("Холбоотой", "Related");
}

function methodLabel(method) {
  return {
    curated_corpus: tr("Курацын корпус", "Curated corpus"),
    published_fact_check: tr("Нийтлэгдсэн баримт шалгалт", "Published fact-check"),
    google_factcheck_tools: "Google Fact Check Tools",
    medical_literature: tr("Эрдэм шинжилгээний эх сурвалж", "Medical literature"),
    multi_source: tr("Олон эх сурвалж", "Multi-source retrieval"),
    indexed_evidence: tr("Индексжсэн эх сурвалж", "Indexed evidence"),
    no_match: tr("Тодорхой дүгнэлтгүй", "No confident match"),
  }[method] || method || "unknown";
}

function fmtBytes(bytes) {
  if (!bytes && bytes !== 0) return "n/a";
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function fmtDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total)) return "n/a";
  const mins = Math.floor(total / 60);
  const secs = Math.round(total % 60);
  return mins ? `${mins}m ${secs}s` : `${secs.toFixed(1)}s`;
}

function fmtUnixDate(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return null;
  return new Intl.DateTimeFormat(state.locale === "mn" ? "mn-MN" : "en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value * 1000));
}

function shortHash(hash) {
  if (!hash || typeof hash !== "string") return "n/a";
  return `${hash.slice(0, 12)}…${hash.slice(-8)}`;
}

function badgeFor(label) {
  const value = String(label || "").toLowerCase();
  let cls = "badge-unsure";
  if (value.includes("not ai") || value.includes("human") || value.includes("authentic")) {
    cls = "badge-real";
  } else if (value.includes("possibly") || value.includes("uncertain")) {
    cls = "badge-unsure";
  } else if (value.includes("likely ai") || value.includes("synthetic") || value.includes("fake")) {
    cls = "badge-high";
  } else if (value === "true") {
    cls = "badge-true";
  } else if (value === "false") {
    cls = "badge-false";
  } else if (value === "not sure") {
    cls = "badge-unsure";
  } else if (value.includes("low risk")) {
    cls = "badge-low";
  } else if (value.includes("medium")) {
    cls = "badge-medium";
  } else if (value.includes("high")) {
    cls = "badge-high";
  }
  return el("span", `badge ${cls}`, label);
}

function addActionRow(card, data) {
  const row = el("div", "result-actions");
  const copy = el("button", "small-button", "Copy JSON");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
      copy.textContent = "Copied";
      setTimeout(() => {
        copy.textContent = "Copy JSON";
      }, 1400);
    } catch {
      copy.textContent = "Copy failed";
    }
  });
  row.append(copy);
  card.append(row);
}

function resultCard(title, extraClass) {
  const card = el("section", `result-card${extraClass ? ` ${extraClass}` : ""}`);
  if (title) card.append(el("h3", null, title));
  return card;
}

function makeDetails(title, bodyNode, open = false) {
  const details = el("details");
  details.open = open;
  const summary = el("summary", null, title);
  details.append(summary, bodyNode);
  return details;
}

function detailsBody(node) {
  const body = el("div", "details-body");
  body.append(node);
  return body;
}

function addLimitations(container, limitations, action) {
  if (Array.isArray(limitations) && limitations.length) {
    const list = el("ul");
    limitations.forEach((item) => list.append(el("li", null, item)));
    container.append(makeDetails("Limitations", detailsBody(list)));
  }
  if (action) {
    const card = resultCard("What to do next");
    card.append(el("p", "summary", action));
    container.append(card);
  }
}

function renderError(container, error) {
  clear(container);
  const card = el("div", "error-card");
  card.append(el("strong", null, "Analysis failed"));
  card.append(el("span", null, error?.message || String(error)));
  card.append(
    el(
      "p",
      "inline-note",
      "If the model is downloading for the first time, wait a minute and try again. Check the backend terminal for details.",
    ),
  );
  container.append(card);
}

function renderLoading(container, message) {
  clear(container);
  const card = el("div", "loading-card");
  card.append(el("span", "spinner"), el("span", null, message));
  container.append(card);
}

function tr(mn, en) {
  return state.locale === "mn" ? mn : en;
}

function setLocale(locale) {
  state.locale = locale === "en" ? "en" : "mn";
  document.documentElement.lang = state.locale;
  document.querySelectorAll("[data-mn][data-en]").forEach((node) => {
    node.textContent = node.dataset[state.locale];
  });
  ["mn", "en"].forEach((code) => {
    const button = document.getElementById(`lang-${code}`);
    if (!button) return;
    const active = code === state.locale;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const output = document.getElementById("url-output");
  if (output && state.lastUrlResult) renderCombinedUrl(output, state.lastUrlResult);
}

/* ---------------- API ---------------- */

async function api(path, options = {}) {
  const { method = "GET", body, form, timeout = 120000 } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.min(timeout, MAX_TIMEOUT));
  const headers = {};
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }

  try {
    const response = await fetch(`${state.backendUrl}${path}`, {
      method,
      headers,
      body: payload,
      signal: controller.signal,
    });
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { detail: text };
      }
    }
    if (!response.ok) {
      let detail = data.detail;
      if (Array.isArray(detail)) {
        detail = detail.map((item) => item.msg || String(item)).join("; ");
      }
      throw new Error(detail || `Request failed with HTTP ${response.status}.`);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request timed out. The model may still be loading; try again.");
    }
    if (error instanceof TypeError) {
      throw new Error(
        `Could not reach the local server at ${state.backendUrl}. ` +
          "Start it with ./scripts/run_backend.sh, then check Settings.",
      );
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function checkHealth(announce = false) {
  const status = document.getElementById("server-status");
  const text = document.getElementById("server-status-text");
  status.classList.remove("ok", "down");
  status.classList.add("checking");
  text.textContent = "Checking local server…";

  try {
    const health = await api("/api/health", { timeout: 5000 });
    status.classList.remove("checking");
    status.classList.add("ok");
    const loaded = health.models?.image_loaded?.length || 0;
    const device = health.models?.device || "cpu";
    text.textContent = `Local server ready · ${device.toUpperCase()}${
      loaded ? ` · ${loaded} image model${loaded > 1 ? "s" : ""}` : " · models load on demand"
    }`;
    if (announce) text.textContent += " · reconnected";
    return health;
  } catch (error) {
    status.classList.remove("checking");
    status.classList.add("down");
    text.textContent = "Local server offline · start ./scripts/run_backend.sh";
    if (announce) text.title = error.message;
    return null;
  }
}

/* ---------------- Settings ---------------- */

async function loadSettings() {
  let stored = {};
  if (hasChrome && chrome.storage.local) {
    stored = await chrome.storage.local.get({
      backendUrl: DEFAULT_BACKEND,
      virusTotalApiKey: "",
    });
  } else {
    try {
      stored = {
        backendUrl: localStorage.getItem("tayoBackend") || DEFAULT_BACKEND,
        virusTotalApiKey: localStorage.getItem("tayoVirusTotalApiKey") || "",
      };
    } catch {
      stored = { backendUrl: DEFAULT_BACKEND };
    }
  }
  state.backendUrl = (stored.backendUrl || DEFAULT_BACKEND).replace(/\/+$/, "");
  state.virusTotalApiKey = stored.virusTotalApiKey || "";
  document.getElementById("backend-url").value = state.backendUrl;
  document.getElementById("url-connect-virustotal").hidden = Boolean(
    state.virusTotalApiKey,
  );
}

async function saveSettings(url) {
  state.backendUrl = url.replace(/\/+$/, "");
  if (hasChrome && chrome.storage.local) {
    await chrome.storage.local.set({ backendUrl: state.backendUrl });
  } else {
    try {
      localStorage.setItem("tayoBackend", state.backendUrl);
    } catch {
      /* ignore */
    }
  }
}

async function saveVirusTotalKey(apiKey) {
  state.virusTotalApiKey = apiKey;
  document.getElementById("url-connect-virustotal").hidden = Boolean(apiKey);
  if (hasChrome && chrome.storage.local) {
    if (apiKey) {
      await chrome.storage.local.set({ virusTotalApiKey: apiKey });
    } else {
      await chrome.storage.local.remove("virusTotalApiKey");
    }
  } else {
    try {
      if (apiKey) localStorage.setItem("tayoVirusTotalApiKey", apiKey);
      else localStorage.removeItem("tayoVirusTotalApiKey");
    } catch {
      /* ignore */
    }
  }
}

/* ---------------- Tabs ---------------- */

function switchTab(name) {
  if (!document.getElementById(`panel-${name}`)) return;
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${name}`);
  });
}

/* ---------------- Fact-check ---------------- */

function evidenceSnippet(item) {
  if (state.locale === "mn" && item.snippet_mn) return item.snippet_mn;
  return item.snippet || "";
}

function renderEvidence(evidence) {
  const list = el("div", "evidence-list");
  (evidence || []).forEach((item) => {
    const url = typeof item.url === "string" ? item.url : "";
    const isLink = /^https?:\/\//i.test(url);
    const node = el(isLink ? "a" : "div", "evidence-item");
    if (isLink) {
      node.href = url;
      node.target = "_blank";
      node.rel = "noreferrer noopener";
    }
    const top = el("div", "evidence-top");
    top.append(el("span", "evidence-source", item.source || "Source"));
    if (item.relation) {
      top.append(
        el(
          "span",
          `evidence-relation relation-${item.relation}`,
          relationLabel(item.relation),
        ),
      );
    }
    node.append(top);
    node.append(el("span", "evidence-title", item.title || item.url || "Untitled evidence"));
    const snippet = evidenceSnippet(item);
    if (snippet) node.append(el("span", "evidence-snippet", snippet));
    const meta = el("div", "evidence-meta");
    const rating = item.published_verdict || item.rating;
    if (rating) {
      meta.append(el("span", "evidence-rating", tr(`Нийтэлсэн үнэлгээ: ${rating}`, `Published rating: ${rating}`)));
    }
    if (item.journal) meta.append(el("span", null, item.journal));
    if (item.published) meta.append(el("span", null, item.published));
    if (item.tier) meta.append(el("span", null, item.tier));
    if (item.is_medical) meta.append(el("span", "evidence-medical", tr("Эрүүл мэнд", "Medical")));
    if (Number(item.relevance) > 0) meta.append(el("span", null, tr(`Хамаарал ${pct(item.relevance)}`, `Relevance ${pct(item.relevance)}`)));
    if (meta.childElementCount) node.append(meta);
    list.append(node);
  });
  return list;
}

function renderQueries(data) {
  const analysis = data.analysis || {};
  const statusRows = Array.isArray(data.queries) ? data.queries : [];
  const statusByQuery = new Map(statusRows.map((row) => [row.query, row]));
  const queries =
    Array.isArray(analysis.queries) && analysis.queries.length
      ? analysis.queries
      : statusRows;
  const groups = [
    ["fact_check", tr("Баримт шалгалтын хайлт", "Fact-check queries")],
    ["medical", tr("Эрдэм шинжилгээний хайлт", "Medical queries")],
    ["web", tr("Эх сурвалжийн хайлт", "Trusted-source queries")],
  ];
  const wrap = el("div", "query-list");
  groups.forEach(([purpose, label]) => {
    const rows = queries.filter((query) => query.purpose === purpose);
    if (!rows.length) return;
    const group = el("div", "query-group");
    group.append(el("h4", "query-group-title", label));
    rows.forEach((query) => {
      const status = statusByQuery.get(query.query) || {};
      const row = el("div", "query-row");
      row.append(el("code", "query-text", query.query));
      const bits = [];
      if (Array.isArray(query.providers) && query.providers.length) {
        bits.push(query.providers.join(", "));
      }
      const count =
        status.result_count !== undefined ? status.result_count : query.result_count;
      if (count !== undefined) {
        bits.push(tr(`${count} үр дүн`, `${count} result(s)`));
      }
      if (status.status === "error") bits.push(tr("алдаа", "error"));
      if (bits.length) row.append(el("span", "query-meta", bits.join(" · ")));
      group.append(row);
    });
    wrap.append(group);
  });
  return wrap.childElementCount ? wrap : null;
}

function renderAnalyzedClaim(data) {
  const analysis = data.analysis;
  if (!analysis) return null;
  const card = resultCard(tr("Хэрхэн ойлгосон", "How the claim was read"));
  card.append(el("blockquote", "checked-claim", analysis.claim || data.claim || ""));
  const meta = el("div", "meta-line");
  meta.append(
    el(
      "span",
      null,
      `${tr("Хүрээ", "Scope")}: ${scopeLabel(analysis.effective_scope || data.source_scope)}`,
    ),
  );
  const language = languageLabel(analysis.language || data.language);
  if (language) meta.append(el("span", null, `${tr("Бичиг", "Language")}: ${language}`));
  if (analysis.domain) {
    meta.append(el("span", null, `${tr("Төрөл", "Domain")}: ${analysis.domain}`));
  }
  if (Array.isArray(analysis.claim_forms) && analysis.claim_forms.length) {
    meta.append(el("span", null, `${tr("Хэлбэр", "Form")}: ${analysis.claim_forms.join(", ")}`));
  }
  card.append(meta);

  if (Array.isArray(analysis.concepts) && analysis.concepts.length) {
    const block = el("div", "analysis-block");
    block.append(el("h4", "card-subtitle", tr("Илрүүлсэн ойлголтууд", "Detected concepts")));
    const chips = el("div", "chip-row");
    analysis.concepts.forEach((concept) => {
      const label =
        state.locale === "mn"
          ? concept.label_mn || concept.key
          : concept.label_en || concept.key;
      chips.append(el("span", "chip", label));
    });
    block.append(chips);
    card.append(block);
  }

  if (Array.isArray(analysis.keywords) && analysis.keywords.length) {
    const block = el("div", "analysis-block");
    block.append(el("h4", "card-subtitle", tr("Түлхүүр үгс", "Keyword fallback")));
    const chips = el("div", "chip-row");
    analysis.keywords.slice(0, 10).forEach((keyword) => {
      chips.append(el("span", "chip chip-muted", keyword));
    });
    block.append(chips);
    card.append(block);
  }

  const queries = renderQueries(data);
  if (queries) {
    card.append(
      makeDetails(
        tr("Хайлтын мөрүүд ба үр дүн", "Search queries and results"),
        detailsBody(queries),
      ),
    );
  }
  return card;
}

function renderAssessmentCard(data) {
  const card = resultCard(tr("Дүгнэлт", "Assessment"));
  const head = el("div", "score-head");
  head.append(badgeFor(data.verdict || "Not Sure"));
  head.append(el("span", "score-sub", tr(`Итгэл ${pct(data.confidence)}`, `Confidence ${pct(data.confidence)}`)));
  card.append(head);
  card.append(el("p", "summary", data.summary || "No summary returned."));

  if (data.reported_verdict) {
    const reported = data.reported_verdict;
    const box = el("div", "reported-verdict");
    box.append(el("span", "reported-label", tr("Нийтлэгчийн дүгнэлт", "Publisher's verdict")));
    box.append(el("strong", null, reported.rating || reported.verdict || ""));
    if (reported.source) box.append(el("span", null, reported.source));
    if (Number(reported.match_score) > 0) {
      box.append(el("span", null, tr(`Тохирол ${pct(reported.match_score)}`, `Match ${pct(reported.match_score)}`)));
    }
    if (typeof reported.url === "string" && /^https?:\/\//i.test(reported.url)) {
      const link = el("a", "small-button external-link", tr("Нийтлэлийг нээх", "Open review"));
      link.href = reported.url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      box.append(link);
    }
    if (reported.note) box.append(el("p", "inline-note", reported.note));
    card.append(box);
  }

  if (data.matched_claim) {
    card.append(
      el(
        "p",
        "match-line",
        tr(
          `Тохирсон мэдэгдэл: “${data.matched_claim}” · ${pct(data.match_score)}`,
          `Matched claim: “${data.matched_claim}” · ${pct(data.match_score)}`,
        ),
      ),
    );
  }

  const meta = el("div", "meta-line");
  meta.append(el("span", null, `${tr("Арга", "Method")}: ${methodLabel(data.method)}`));
  if (data.source_scope) {
    meta.append(el("span", null, `${tr("Хүрээ", "Scope")}: ${scopeLabel(data.source_scope)}`));
  }
  const language = languageLabel(data.language);
  if (language) meta.append(el("span", null, `${tr("Бичиг", "Language")}: ${language}`));
  if (data.topic) meta.append(el("span", null, `${tr("Сэдэв", "Topic")}: ${data.topic}`));
  if (data.summary_source) {
    meta.append(el("span", null, `${tr("Хураангуй", "Summary")}: ${data.summary_source}`));
  }
  if (data.index_status) {
    const index = data.index_status;
    meta.append(
      el(
        "span",
        null,
        tr(
          `Локал индекс: ${index.available ? `${index.indexed_items} бичлэг` : "хоосон"}`,
          `Local index: ${index.available ? `${index.indexed_items} items` : "empty"}`,
        ),
      ),
    );
  }
  card.append(meta);
  addActionRow(card, data);
  return card;
}

function renderEvidenceGroup(title, items, open = false) {
  if (!Array.isArray(items) || !items.length) return null;
  const details = el("details", "evidence-group");
  details.open = open;
  const summary = el("summary");
  summary.append(el("span", null, title));
  summary.append(el("span", "group-count", String(items.length)));
  details.append(summary, detailsBody(renderEvidence(items)));
  return details;
}

function renderSourceStatus(data) {
  const items = Array.isArray(data.source_status) ? data.source_status : [];
  if (!items.length) return null;
  const body = el("div", "source-status-list");
  items.forEach((item) => {
    const row = el("div", "source-status-row");
    row.append(el("span", "source-status-provider", item.provider || "source"));
    const stateValue = item.status || "ok";
    row.append(el("span", `source-status-state is-${stateValue}`, stateValue));
    if (item.query) row.append(el("code", "source-status-query", item.query));
    row.append(
      el(
        "span",
        "source-status-note",
        item.note || tr(`${item.results ?? 0} үр дүн`, `${item.results ?? 0} result(s)`),
      ),
    );
    body.append(row);
  });
  return makeDetails(tr("Эх сурвалж бүрийн хариу", "How each source responded"), detailsBody(body));
}

function renderFactcheck(container, data) {
  clear(container);
  const analyzed = renderAnalyzedClaim(data);
  if (analyzed) container.append(analyzed);
  container.append(renderAssessmentCard(data));

  const groups = data.evidence_groups || {};
  const grouped = [
    [
      tr("Нийтлэгдсэн баримт шалгалтууд", "Published fact-checks"),
      groups.fact_checks,
      true,
    ],
    [
      tr("Эрдэм шинжилгээний эх сурвалж (контекст)", "Medical literature (context)"),
      groups.medical,
      false,
    ],
    [
      tr("Холбоотой эх сурвалж", "Related trusted sources"),
      groups.related,
      false,
    ],
    [
      tr("Эдгээр эх сурвалжаас хайх", "Search these trusted sources"),
      groups.source_links,
      false,
    ],
  ]
    .map(([title, items, open]) => renderEvidenceGroup(title, items, open))
    .filter(Boolean);

  if (grouped.length) {
    const card = resultCard(tr("Нотолгоо, эх сурвалж", "Evidence and sources"));
    grouped.forEach((node) => card.append(node));
    const status = renderSourceStatus(data);
    if (status) card.append(status);
    container.append(card);
  } else if (Array.isArray(data.evidence) && data.evidence.length) {
    const card = resultCard(
      data.method === "curated_corpus"
        ? tr("Нотолгоо ба эх сурвалж", "Evidence & sources")
        : tr("Баримт шалгалт ба хайлтын холбоос", "Fact-checks & search links"),
    );
    card.append(renderEvidence(data.evidence));
    const status = renderSourceStatus(data);
    if (status) card.append(status);
    container.append(card);
  } else {
    const status = renderSourceStatus(data);
    if (status) {
      const card = resultCard(tr("Эх сурвалжийн шалгалт", "Source checks"));
      card.append(status);
      container.append(card);
    }
  }

  addLimitations(container, data.limitations, data.recommended_action);
}

/* ---------------- Source browser ---------------- */

function sourceFilterText(source) {
  return [
    source.name,
    source.domain,
    source.note,
    source.category,
    source.category_label_en,
    source.category_label_mn,
    ...(source.scopes || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function renderSourceItem(source) {
  const item = el("article", "source-item");
  const head = el("div", "source-item-head");
  head.append(el("span", "source-name", source.name || source.domain || "Source"));
  const category =
    state.locale === "mn" ? source.category_label_mn : source.category_label_en;
  if (category) head.append(el("span", "source-tag", category));
  if (source.medical) {
    head.append(el("span", "source-tag source-tag-medical", tr("Эрүүл мэнд", "Medical")));
  }
  if (source.language === "mn") {
    head.append(el("span", "source-tag source-tag-mn", "MN"));
  }
  item.append(head);
  if (source.note) item.append(el("p", "source-note", source.note));
  const meta = el("div", "source-meta");
  if (source.domain) meta.append(el("span", null, source.domain));
  if (source.tier) meta.append(el("span", null, source.tier));
  if (source.feed_verified) meta.append(el("span", null, tr("RSS баталгаажсан", "RSS verified")));
  const homepage = typeof source.homepage === "string" ? source.homepage : "";
  if (/^https?:\/\//i.test(homepage)) {
    const link = el("a", "source-link", tr("Сайт", "Site"));
    link.href = homepage;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    meta.append(link);
  }
  if (typeof source.search_url === "string" && source.search_url) {
    const claimInput = document.getElementById("claim-input");
    const claim = claimInput ? claimInput.value.trim() : "";
    const link = el("a", "source-link", tr("Хайх", "Search"));
    link.href = source.search_url.replace("{query}", encodeURIComponent(claim));
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    meta.append(link);
  }
  if (meta.childElementCount) item.append(meta);
  return item;
}

function renderSourceList(sources, filter = "") {
  const list = document.getElementById("claim-source-list");
  if (!list) return;
  const needle = filter.trim().toLowerCase();
  const visible = (sources || []).filter(
    (source) => !needle || sourceFilterText(source).includes(needle),
  );
  list.replaceChildren();
  if (!visible.length) {
    list.append(el("div", "empty-state", tr("Тохирох эх сурвалж олдсонгүй.", "No matching source.")));
    return;
  }
  visible.forEach((source) => list.append(renderSourceItem(source)));
}

async function loadClaimSources() {
  const list = document.getElementById("claim-source-list");
  const summary = document.getElementById("claim-source-summary");
  const count = document.getElementById("claim-source-count");
  const scopeSelect = document.getElementById("claim-source-scope");
  if (!list) return;
  const requestScope =
    scopeSelect && scopeSelect.value !== "auto" ? scopeSelect.value : "all";
  list.replaceChildren(
    el("div", "loading-card", tr("Эх сурвалжийг ачаалж байна…", "Loading trusted sources…")),
  );
  try {
    const data = await api(
      `/api/factcheck/sources?scope=${encodeURIComponent(requestScope)}`,
      { timeout: 30000 },
    );
    state.claimSources = Array.isArray(data.sources) ? data.sources : [];
    state.claimSourcesMeta = data;
    const counts = data.counts || {};
    if (count) {
      count.textContent = tr(
        `${data.total ?? state.claimSources.length} эх сурвалж · ${counts.mongolian ?? 0} монгол`,
        `${data.total ?? state.claimSources.length} sources · ${counts.mongolian ?? 0} Mongolian`,
      );
    }
    if (summary) summary.textContent = data.notice || "";
    const search = document.getElementById("claim-source-search");
    renderSourceList(state.claimSources, search ? search.value : "");
  } catch (error) {
    list.replaceChildren();
    const card = el("div", "error-card");
    card.append(el("strong", null, tr("Эх сурвалж ачаалсангүй", "Could not load sources")));
    card.append(el("span", null, error?.message || String(error)));
    list.append(card);
  }
}

/* ---------------- Text ---------------- */

function statGrid(stats, language) {
  const grid = el("div", "stat-grid");
  const entries = [
    ["Words", stats.words],
    ["Sentences", stats.sentences],
    ["Characters", stats.characters],
    ["Avg sentence", `${stats.avg_sentence_words ?? "n/a"} words`],
    ["Sentence CV", stats.sentence_length_cv],
    ["Type–token ratio", stats.type_token_ratio],
    ["Trigram repetition", stats.trigram_repetition],
  ];
  if (!languageLabel(language)) entries.push(["AI-style phrases", stats.ai_phrase_hits]);
  entries.forEach(([label, value]) => {
    const stat = el("div", "stat");
    stat.append(el("span", "stat-label", label));
    stat.append(el("span", "stat-value", value ?? "n/a"));
    grid.append(stat);
  });
  return grid;
}

function signalList(signals) {
  const list = el("div", "signal-list");
  (signals || []).forEach((signal) => {
    const item = el("div", "signal");
    item.dataset.direction = signal.direction || "neutral";
    const head = el("div", "signal-head");
    head.append(el("span", null, signal.name || "signal"));
    head.append(el("span", "signal-weight", signal.weight || ""));
    item.append(head, el("p", null, signal.explanation || ""));
    list.append(item);
  });
  return list;
}

function qualityLabel(status) {
  return {
    experimental: "Experimental",
    unvalidated_language: "Unvalidated language",
    insufficient_input: "Insufficient input",
    model_unavailable: "Model unavailable",
  }[status] || status || "Unknown";
}

function qualityBadge(status) {
  let cls = "badge-neutral";
  if (status === "experimental") cls = "badge-info";
  else if (status === "unvalidated_language" || status === "insufficient_input") {
    cls = "badge-medium";
  } else if (status === "model_unavailable") {
    cls = "badge-unsure";
  }
  return el("span", `badge ${cls}`, qualityLabel(status));
}

function formatCheckValue(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function renderCheckList(checks) {
  const statuses = {
    measured: "Measured",
    insufficient: "Insufficient",
    unavailable: "Unavailable",
  };
  const list = el("div", "check-list");
  checks.forEach((check) => {
    const row = el("div", "check-row");
    const head = el("div", "check-head");
    head.append(el("strong", null, check.name || check.id || "Check"));
    head.append(el("span", "check-status", statuses[check.status] || check.status || ""));
    row.append(head);
    const value = formatCheckValue(check.value);
    if (value !== null) row.append(el("pre", "check-value", value));
    if (check.explanation) row.append(el("p", null, check.explanation));
    list.append(row);
  });
  return list;
}

function scorePair(aiProbability) {
  const wrap = el("div", "score-pair");
  [
    { label: "Human-like", value: 1 - aiProbability, cls: "score-human" },
    { label: "AI-like", value: aiProbability, cls: "score-ai" },
  ].forEach(({ label, value, cls }) => {
    const box = el("div", `score-box ${cls}`);
    box.append(el("span", "score-label", label));
    box.append(el("strong", "score-value", pct(value)));
    wrap.append(box);
  });
  return wrap;
}

function coverageText(coverage) {
  if (!coverage) return null;
  const analyzed = Number(coverage.analyzed_words);
  const total = Number(coverage.total_whitespace_words);
  if (!Number.isFinite(analyzed) || !Number.isFinite(total)) return null;
  return `${analyzed.toLocaleString()} / ${total.toLocaleString()} words analyzed`;
}

function renderText(container, data) {
  clear(container);
  const aiProbability = data.ai_probability;
  const hasModelScore = aiProbability !== null && aiProbability !== undefined;
  const quality = data.quality || {};
  const card = resultCard();
  const head = el("div", "score-head");
  head.append(badgeFor(data.label));
  head.append(
    el(
      "span",
      "score-sub",
      hasModelScore
        ? `AI-like ${pct(aiProbability)}`
        : quality.status === "insufficient_input"
          ? "Not enough input for a model score"
          : "AI score unavailable",
    ),
  );
  card.append(head);

  if (hasModelScore) {
    card.append(scorePair(aiProbability));
    card.append(
      el(
        "p",
        "inline-note",
        "These are opposing model scores, not the probability that a person or an AI wrote the text.",
      ),
    );
  }
  card.append(el("p", "summary", data.summary || ""));

  (quality.warnings || []).forEach((warning) => {
    card.append(el("p", "quality-warning", warning));
  });

  const meta = el("div", "meta-line");
  if (quality.status) meta.append(qualityBadge(quality.status));
  if (data.model_used) meta.append(el("span", null, `Model: ${data.model_used}`));
  const coverage = coverageText(data.coverage);
  if (coverage) meta.append(el("span", null, `Coverage: ${coverage}`));
  if (languageLabel(data.language)) {
    meta.append(el("span", null, `Бичиг: ${languageLabel(data.language)}`));
  }
  if (data.chunks?.length) meta.append(el("span", null, `${data.chunks.length} chunk(s)`));
  card.append(meta);
  addActionRow(card, data);
  container.append(card);

  const statsCard = resultCard("Style statistics");
  statsCard.append(statGrid(data.text_stats || {}, data.language));
  if (data.text_stats?.ai_phrases_found?.length) {
    statsCard.append(
      el("p", "inline-note", `Phrases found: ${data.text_stats.ai_phrases_found.join(", ")}`),
    );
  }
  container.append(statsCard);

  if (Array.isArray(data.checks) && data.checks.length) {
    const checksCard = resultCard("Checks (14 groups, 18 checks)");
    checksCard.append(renderCheckList(data.checks));
    container.append(checksCard);
  }

  if (data.signals?.length) {
    const signalsCard = resultCard("Signals");
    signalsCard.append(signalList(data.signals));
    container.append(signalsCard);
  }

  if (data.chunks?.length) {
    const chunksCard = resultCard("Per-chunk model scores");
    data.chunks.forEach((chunk) => {
      chunksCard.append(
        el(
          "p",
          "inline-note",
          `Chunk ${chunk.index + 1}: ${chunk.words} words · AI-like ${pct(
            chunk.ai_probability,
          )}${chunk.truncated ? " · token limit reached" : ""}`,
        ),
      );
    });
    if (data.coverage?.partial) {
      chunksCard.append(
        el(
          "p",
          "quality-warning",
          "Not all text was covered. Do not apply chunk scores to the whole text.",
        ),
      );
    }
    container.append(chunksCard);
  }
  if (data.model_error) {
    container.append(el("p", "inline-note", `Model note: ${data.model_error}`));
  }

  addLimitations(container, data.limitations, data.recommended_action);
}

/* ---------------- Image ---------------- */

function metadataGrid(metadata) {
  const grid = el("dl", "kv");
  const exif = metadata.exif || {};
  const add = (key, value) => {
    if (value === undefined || value === null || value === "") return;
    grid.append(el("dt", null, key));
    grid.append(el("dd", null, String(value)));
  };
  add("Format", metadata.format);
  add("Dimensions", metadata.width && metadata.height ? `${metadata.width} × ${metadata.height}` : null);
  add("Color mode", metadata.mode);
  add("EXIF present", metadata.exif_present ? "yes" : "no");
  add("Camera make", exif.Make);
  add("Camera model", exif.Model);
  add("Software", metadata.software || exif.Software);
  add("C2PA manifest", metadata.c2pa_present ? "detected (not verified)" : "not detected");
  add("AI software hints", (metadata.ai_software_hints || []).join(", "));
  add("Metadata flags", (metadata.metadata_flags || []).join(", "));
  return grid;
}

function renderImage(container, data) {
  clear(container);
  if (data.error) {
    renderError(container, new Error(data.error));
    return;
  }

  const card = resultCard();
  const head = el("div", "score-head");
  head.append(badgeFor(data.label));
  head.append(
    el(
      "span",
      "score-sub",
      data.ai_probability !== null && data.ai_probability !== undefined
        ? `AI-like ${pct(data.ai_probability)}`
        : "No classifier score",
    ),
  );
  card.append(head);
  const meter = el("div", "meter");
  const bar = el("span");
  bar.style.width = `${Math.round((data.ai_probability || 0) * 100)}%`;
  meter.append(bar);
  card.append(meter);
  card.append(el("p", "summary", data.summary || ""));
  const meta = el("div", "meta-line");
  meta.append(el("span", null, `Confidence: ${pct(data.confidence)}`));
  meta.append(el("span", null, `Size: ${fmtBytes(data.file_size_bytes)}`));
  meta.append(el("span", null, `SHA-256: ${shortHash(data.sha256)}`));
  card.append(meta);
  addActionRow(card, data);
  container.append(card);

  if (data.models?.length) {
    const modelsCard = resultCard("Classifier output");
    data.models.forEach((model) => {
      const row = el("div", "signal");
      row.dataset.direction = model.ai_probability >= 0.5 ? "AI-like" : "real-like";
      const rowHead = el("div", "signal-head");
      rowHead.append(el("span", null, model.model_id));
      rowHead.append(el("span", "signal-weight", `${pct(model.ai_probability)} AI-like`));
      row.append(rowHead);
      row.append(
        el(
          "p",
          null,
          `Predicted: ${model.predicted_label} · ${model.latency_ms} ms · ${model.device}`,
        ),
      );
      modelsCard.append(row);
    });
    container.append(modelsCard);
  }

  const metadataCard = resultCard("Metadata & provenance");
  metadataCard.append(metadataGrid(data.metadata || {}));
  if (data.metadata && Object.keys(data.metadata).length) {
    const pre = el("pre", null, JSON.stringify(data.metadata, null, 2));
    metadataCard.append(makeDetails("Raw metadata", detailsBody(pre)));
  }
  container.append(metadataCard);

  if (data.signals?.length) {
    const signalsCard = resultCard("Signals");
    signalsCard.append(signalList(data.signals));
    container.append(signalsCard);
  }

  addLimitations(container, data.limitations, data.recommended_action);
}

/* ---------------- Video ---------------- */

function renderVideo(container, data) {
  clear(container);
  const card = resultCard();
  const head = el("div", "score-head");
  head.append(badgeFor(data.label));
  head.append(
    el(
      "span",
      "score-sub",
      data.ai_probability !== null && data.ai_probability !== undefined
        ? `Mean AI-like ${pct(data.ai_probability)}`
        : "No frame score",
    ),
  );
  card.append(head);
  if (data.ai_probability !== null && data.ai_probability !== undefined) {
    const meter = el("div", "meter");
    const bar = el("span");
    bar.style.width = `${Math.round(data.ai_probability * 100)}%`;
    meter.append(bar);
    card.append(meter);
  }
  card.append(el("p", "summary", data.summary || ""));
  const meta = el("div", "meta-line");
  meta.append(el("span", null, `Frames: ${data.frames_analyzed}/${data.frames_requested}`));
  meta.append(el("span", null, `Duration: ${fmtDuration(data.duration_seconds)}`));
  meta.append(el("span", null, `Size: ${fmtBytes(data.file_size_bytes)}`));
  if (data.max_frame_probability !== null) {
    meta.append(el("span", null, `Max frame: ${pct(data.max_frame_probability)}`));
  }
  card.append(meta);
  addActionRow(card, data);
  container.append(card);

  if (data.frame_results?.length) {
    const frames = resultCard("Sampled frames");
    const list = el("div", "signal-list");
    data.frame_results.forEach((frame) => {
      const item = el("div", "signal");
      item.dataset.direction = frame.ai_probability >= 0.5 ? "AI-like" : "real-like";
      const row = el("div", "signal-head");
      row.append(el("span", null, `Frame ${frame.index + 1} · ${fmtDuration(frame.timestamp_seconds)}`));
      row.append(el("span", "signal-weight", pct(frame.ai_probability)));
      item.append(row);
      list.append(item);
    });
    frames.append(list);
    container.append(frames);
  }

  if (data.metadata) {
    const metaCard = resultCard("Video metadata");
    const pre = el("pre", null, JSON.stringify(data.metadata, null, 2));
    metaCard.append(pre);
    container.append(metaCard);
  }

  if (data.signals?.length) {
    const cardSignals = resultCard("Signals");
    cardSignals.append(signalList(data.signals));
    container.append(cardSignals);
  }

  if (data.frame_error) {
    container.append(el("p", "inline-note", `Frame extraction note: ${data.frame_error}`));
  }
  addLimitations(container, data.limitations, data.recommended_action);
}

/* ---------------- URL ---------------- */

function riskClass(level) {
  const value = String(level || "").toLowerCase();
  if (value.includes("high")) return "var(--red)";
  if (value.includes("medium")) return "var(--amber)";
  if (value.includes("low")) return "var(--green)";
  return "var(--muted)";
}

function addKeyValues(card, pairs) {
  const grid = el("dl", "kv");
  pairs.forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    const display = typeof value === "object" ? JSON.stringify(value) : String(value);
    grid.append(el("dt", null, key), el("dd", null, display));
  });
  card.append(grid);
}

function renderUrl(container, data) {
  clear(container);
  const card = resultCard();
  const risk = el("div", "risk-card");
  const gauge = el("div", "risk-gauge");
  gauge.style.setProperty("--score", String(data.risk_score ?? 0));
  gauge.style.setProperty("--risk-color", riskClass(data.risk_level));
  const gaugeText = el("span", null, String(data.risk_score ?? 0));
  gaugeText.append(el("small", null, "/100"));
  gauge.append(gaugeText);
  const copy = el("div", "risk-copy");
  copy.append(badgeFor(data.risk_level || "Unknown"));
  copy.append(el("p", "summary", data.summary || ""));
  risk.append(gauge, copy);
  card.append(risk);
  const meta = el("div", "meta-line");
  meta.append(el("span", null, `Host: ${data.host || "n/a"}`));
  meta.append(el("span", null, `Domain: ${data.registered_domain || "n/a"}`));
  card.append(meta);
  addActionRow(card, data);
  container.append(card);

  if (data.reasons?.length) {
    const reasonsCard = resultCard("Why this score");
    const list = el("ul", "reason-list");
    data.reasons.forEach((reason) => list.append(el("li", null, reason)));
    reasonsCard.append(list);
    container.append(reasonsCard);
  }

  const checks = data.checks || {};

  if (checks.offline?.length) {
    const list = el("div", "signal-list");
    checks.offline.forEach((signal) => {
      const item = el("div", "signal");
      item.dataset.direction = signal.triggered ? (signal.weight > 0 ? "risk" : "neutral") : "safe";
      const head = el("div", "signal-head");
      head.append(el("span", null, signal.name));
      head.append(
        el(
          "span",
          "signal-weight",
          signal.triggered ? (signal.weight > 0 ? `+${signal.weight}` : "ok") : "clear",
        ),
      );
      item.append(head, el("p", null, signal.detail));
      list.append(item);
    });
    const flagged = checks.offline.filter((signal) => signal.triggered && signal.weight > 0).length;
    container.append(
      makeDetails(
        `Offline URL heuristics (${checks.offline.length} checks, ${flagged} flagged)`,
        detailsBody(list),
      ),
    );
  }

  if (checks.dns) {
    const body = el("div");
    addKeyValues(body, [
      ["DNS status", checks.dns.status === 0 ? "NOERROR" : `status ${checks.dns.status}`],
      ["DNSSEC validated", checks.dns.dnssec?.validated ? "yes" : "no / not validated"],
      ["DS record", checks.dns.dnssec?.ds_present ? "present" : "absent"],
      ["A records", (checks.dns.answers?.A || []).map((a) => a.data).join(", ")],
      ["AAAA records", (checks.dns.answers?.AAAA || []).map((a) => a.data).join(", ")],
      ["NS records", (checks.dns.answers?.NS || []).map((a) => a.data).join(", ")],
      ["MX records", (checks.dns.answers?.MX || []).map((a) => a.data).join(", ")],
    ]);
    body.append(el("p", "inline-note", checks.dns.dnssec?.note || ""));
    container.append(makeDetails("DNS & DNSSEC", detailsBody(body)));
  }

  if (checks.tls) {
    const body = el("div");
    const cert = checks.tls.certificate || {};
    addKeyValues(body, [
      ["Connected", checks.tls.connected ? "yes" : "no"],
      ["Valid certificate", checks.tls.valid === true ? "yes" : checks.tls.valid === false ? "no" : "n/a"],
      ["TLS version", checks.tls.tls_version],
      ["Cipher", checks.tls.cipher],
      ["Subject", cert.subject],
      ["Issuer", cert.issuer],
      ["Validation type", cert.validation_type],
      ["Valid from", cert.not_before],
      ["Valid until", cert.not_after],
      ["Days until expiry", cert.days_until_expiry],
      ["SANs", (cert.san || []).join(", ")],
      ["Error", checks.tls.error],
    ]);
    body.append(
      el(
        "p",
        "inline-note",
        "A valid certificate only proves control of the domain, not that the site is trustworthy.",
      ),
    );
    container.append(makeDetails("TLS certificate", detailsBody(body)));
  }

  if (checks.rdap) {
    const body = el("div");
    const rdap = checks.rdap;
    addKeyValues(body, [
      ["Found", rdap.found ? "yes" : "no"],
      ["Registrar", rdap.registrar],
      ["Created", rdap.created],
      ["Expires", rdap.expires],
      ["Age (days)", rdap.age_days],
      ["Status", Array.isArray(rdap.status) ? rdap.status.join(", ") : rdap.status],
      ["Nameservers", Array.isArray(rdap.nameservers) ? rdap.nameservers.join(", ") : rdap.nameservers],
      ["Note", rdap.note],
      ["Error", rdap.error],
    ]);
    container.append(makeDetails("Registration data (RDAP)", detailsBody(body)));
  }

  const intel = checks.threat_intel || {};
  const vt = intel.virustotal || {};
  const vtStats = vt.stats || {};
  const vtResult = vt.error
    ? `error: ${vt.error}`
    : !vt.enabled
      ? vt.note
      : vt.status === "completed"
        ? `${vtStats.malicious || 0} malicious / ${vtStats.suspicious || 0} suspicious / ${vtStats.harmless || 0} harmless`
        : vt.note || vt.status || "submitted";
  const intelCard = resultCard("Threat intelligence");
  addKeyValues(intelCard, [
    ["Google Safe Browsing", intel.safe_browsing?.matched ? "MATCH" : intel.safe_browsing?.enabled ? "no match" : intel.safe_browsing?.note],
    ["VirusTotal", vtResult],
    ["VirusTotal submission", vt.submitted ? "submitted for a fresh scan" : undefined],
    ["VirusTotal status", vt.status],
    ["URLhaus", intel.urlhaus?.query_status || intel.urlhaus?.note],
  ]);
  if (Array.isArray(vt.detections) && vt.detections.length) {
    const detections = el("ul");
    vt.detections.forEach((item) => {
      detections.append(
        el("li", null, `${item.engine || "Unknown engine"}: ${item.result || item.category}`),
      );
    });
    intelCard.append(
      makeDetails("VirusTotal malicious/suspicious detections", detailsBody(detections), true),
    );
  }
  const intelRaw = el("pre", null, JSON.stringify(intel, null, 2));
  intelCard.append(makeDetails("Raw threat-intel output", detailsBody(intelRaw)));
  container.append(intelCard);

  addLimitations(container, data.limitations, data.recommended_action);
}

function securityPresentation(security) {
  const status = security.security_status || "unknown";
  return {
    "safe-looking": {
      label: tr("Аюул багатай харагдаж байна", "Safe-looking"),
      cls: "status-safe",
      explanation: tr(
        "Одоогийн шалгалтаар өндөр эрсдэл илрээгүй. Гэхдээ энэ нь аюулгүй гэсэн баталгаа биш.",
        "No high-risk signal was found, but this is not proof that the site is safe.",
      ),
    },
    suspicious: {
      label: tr("Сэжигтэй", "Suspicious"),
      cls: "status-warning",
      explanation: tr(
        "Эрсдэлийн шинж илэрсэн тул хувийн мэдээлэл оруулахгүй, линкийг нягтална уу.",
        "Risk indicators were found. Do not enter personal information until the site is verified.",
      ),
    },
    malicious: {
      label: tr("Хортой", "Malicious"),
      cls: "status-danger",
      explanation: tr(
        "Аюулын мэдээллийн сан энэ линкийг хортой гэж тэмдэглэсэн. Веб хуудсыг татаж аваагүй.",
        "Threat intelligence flagged this URL. The website was not downloaded.",
      ),
    },
    unknown: {
      label: tr("Тодорхойгүй", "Unknown"),
      cls: "status-neutral",
      explanation: tr(
        "Линкийг бүрэн шалгаж чадсангүй. Үр дүнг аюулгүй гэж ойлгож болохгүй.",
        "The URL could not be fully checked. Do not interpret this result as safe.",
      ),
    },
  }[status] || { label: status, cls: "status-neutral", explanation: "" };
}

function addShareButton(card, data) {
  const button = el("button", "button secondary share-button", tr("Үр дүн хуваалцах", "Share results"));
  button.type = "button";
  button.addEventListener("click", async () => {
    const security = securityPresentation(data.security || {});
    const text = `TAYO — ${data.url}\n${security.label}\n${data.factcheck?.verdict || tr("Баримт шалгалтгүй", "No fact-check result")}`;
    try {
      if (navigator.share) await navigator.share({ title: "TAYO URL check", text });
      else await navigator.clipboard.writeText(text);
      button.textContent = tr("Хуваалцлаа", "Shared");
    } catch (error) {
      if (error?.name !== "AbortError") button.textContent = tr("Хуулж чадсангүй", "Could not copy");
    }
  });
  card.append(button);
}

function renderWebsiteAppearance(website) {
  const frame = el("div", "site-preview-frame");
  const chromeBar = el("div", "site-preview-chrome");
  const dots = el("span", "site-preview-dots");
  const address = el("span", "site-preview-address", website.url || "");
  chromeBar.append(dots, address);

  const page = el("div", "site-preview-page");
  const theme = website.appearance?.theme_color;
  if (/^#[0-9a-f]{3,8}$/i.test(theme || "")) {
    page.style.setProperty("--site-accent", theme);
  }
  if (website.preview_data_url) {
    const image = el("img", "website-preview");
    image.src = website.preview_data_url;
    image.alt = tr("Сайтаас авсан зураг", "Image supplied by the website");
    page.append(image);
  }
  const content = el("div", "site-preview-content");
  content.append(el("strong", "site-preview-title", website.title || tr("Гарчиггүй", "Untitled website")));
  if (website.description) content.append(el("p", null, website.description));
  const headings = (website.appearance?.headings || []).filter(Boolean).slice(0, 3);
  if (headings.length) {
    const list = el("ul", "site-preview-headings");
    headings.forEach((heading) => list.append(el("li", null, heading)));
    content.append(list);
  }
  page.append(content);
  frame.append(chromeBar, page);
  const wrapper = el("div", "site-preview-wrap");
  wrapper.append(frame, el("p", "preview-caption", tr(
    "Статик хуудасны мэдээллээр сэргээсэн аюулгүй харагдац — бодит screenshot биш.",
    "Safe reconstruction from static page data — not a live screenshot.",
  )));
  return wrapper;
}

function renderPageFactcheck(result, index, total) {
  const item = el("section", "claim-result");
  const verdict = result.verdict || "Not Sure";
  const status = verdict === "True"
    ? tr("Баталгаажсан", "Verified")
    : verdict === "False"
      ? tr("Худал", "False")
      : tr("Баталгаажаагүй", "Unverified");
  const label = el("div", "claim-label", tr(
    `Шалгасан мэдэгдэл ${index + 1}/${total}`,
    `Checked claim ${index + 1} of ${total}`,
  ));
  const claim = el("blockquote", "checked-claim", result.claim || "");
  const head = el("div", "workflow-head");
  head.append(badgeFor(verdict), el("span", "score-sub", status));
  item.append(label, claim, head, el("p", "summary", result.summary || ""));
  if (Array.isArray(result.evidence) && result.evidence.length) {
    item.append(el("h4", "card-subtitle", tr("Эх сурвалж ба нотолгоо", "Sources and evidence")));
    item.append(renderEvidence(result.evidence));
  }
  addKeyValues(item, [
    [tr("Арга", "Method"), result.method],
    [tr("Итгэлцүүр", "Confidence"), pct(result.confidence)],
  ]);
  return item;
}

function safetyClass(level) {
  return {
    do_not_open: "status-danger",
    sandbox_only: "status-warning",
    caution: "status-warning",
    unknown: "status-neutral",
    no_strong_signal: "status-safe",
  }[level] || "status-neutral";
}

function severityClass(severity) {
  return {
    critical: "finding-critical",
    high: "finding-high",
    caution: "finding-caution",
    info: "finding-info",
  }[severity] || "finding-info";
}

function appendExternalTools(card, safety) {
  const tools = safety?.external_tools || {};
  const links = [
    ["VirusTotal report", tools.virustotal_report],
    [tr("urlscan.io дээр гүнзгий шалгах", "Deep-scan on urlscan.io"), tools.urlscan],
  ].filter(([, href]) => href);
  if (!links.length) return;
  const row = el("div", "external-tools");
  links.forEach(([label, href]) => {
    const link = el("a", "small-button external-link", label);
    link.href = href;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    row.append(link);
  });
  card.append(row);
  card.append(el("p", "inline-note", tr(
    "urlscan.io линкийг олон нийтэд нээлттэйгээр шалгаж болно. Нууц, хувийн линкийг тэнд бүү илгээнэ үү.",
    "urlscan.io may inspect the URL publicly. Do not submit private or secret links.",
  )));
}

function renderSafetyCard(data) {
  const safety = data.safety;
  if (!safety) return null;
  const card = resultCard(tr("1 · Нээх эсвэл зайлсхийх", "1 · Open or avoid"), "workflow-card safety-card");
  card.dataset.level = safety.level || "unknown";
  const head = el("div", "workflow-head");
  head.append(el("span", `workflow-status ${safetyClass(safety.level)}`, safety.label || safety.level));
  head.append(el("span", "score-sub", `${safety.risk_score ?? "—"}/100`));
  card.append(head, el("p", "summary", safety.summary || ""));

  if (safety.reasons?.length) {
    const list = el("ul", "reason-list");
    safety.reasons.forEach((reason) => list.append(el("li", null, reason)));
    card.append(
      makeDetails(
        tr("Яагаад ийм дүгнэлт гарсан", "Why this recommendation"),
        detailsBody(list),
        safety.level === "do_not_open" || safety.level === "sandbox_only",
      ),
    );
  }

  if (safety.actions?.length) {
    card.append(el("h4", "card-subtitle", tr("Аюулгүй байх алхмууд", "Safer next steps")));
    const actions = el("ul", "safety-actions");
    safety.actions.forEach((action) => actions.append(el("li", null, action)));
    card.append(actions);
  }

  addKeyValues(card, [
    ["VirusTotal", safety.threat_intel?.virustotal],
    ["Google Safe Browsing", safety.threat_intel?.safe_browsing],
    ["URLhaus", safety.threat_intel?.urlhaus],
  ]);

  if (data.destination_security) {
    addKeyValues(card, [
      [tr("Хүлээн авах эцсийн хаяг", "Final destination"), data.destination_security.url],
      [tr("Эцсийн хаягийн эрсдэл", "Final destination risk"), data.destination_security.risk_level],
      [tr("Эцсийн хаягийн төлөв", "Final destination status"), data.destination_security.security_status],
    ]);
    if (data.destination_security.security_status === "malicious") {
      card.append(el("p", "verification-warning", tr(
        "⚠ Линк өөр домэйн руу шилжиж, эцсийн хаяг хортой гэж тэмдэглэгдсэн.",
        "⚠ The link redirects to another domain and the final destination is flagged as malicious.",
      )));
    }
  }
  if (data.destination_scan_error) {
    card.append(el("p", "inline-note", `${tr("Эцсийн хаяг шалгаагүй", "Final destination not checked")}: ${data.destination_scan_error}`));
  }

  appendExternalTools(card, safety);
  card.append(el("p", "zero-click-note", safety.zero_click_note || ""));
  if (safety.note) card.append(el("p", "inline-note", safety.note));
  return card;
}

function renderUrlSecurityCard(data) {
  const security = data.security || {};
  const presentation = securityPresentation(security);
  const vt = security.checks?.threat_intel?.virustotal || {};
  const stats = vt.stats || {};
  const detected = Number(stats.malicious || 0) + Number(stats.suspicious || 0);
  const vendors = Object.values(stats).reduce((sum, value) => sum + Number(value || 0), 0);

  const card = resultCard(tr("2 · URL ба домэйны аюулгүй байдал", "2 · URL & domain security"), "workflow-card");
  const head = el("div", "workflow-head");
  head.append(el("span", `workflow-status ${presentation.cls}`, presentation.label));
  head.append(el("span", "score-sub", `${security.risk_score ?? "—"}/100`));
  card.append(head, el("p", "summary", presentation.explanation));

  const vtState = vt.report_state === "scanned"
    ? detected > 0
      ? tr(`${detected} нийлүүлэгч эрсдэл илрүүлсэн`, `${detected} vendor${detected === 1 ? "" : "s"} detected a threat`)
      : tr("Аюул илрээгүй", "No threats detected")
    : vt.report_state === "pending"
      ? tr("VirusTotal шалгалт хүлээгдэж байна", "VirusTotal scan pending")
      : tr("VirusTotal-д шалгагдаагүй", "Not scanned by VirusTotal");
  addKeyValues(card, [
    ["VirusTotal", vtState],
    [tr("Шалгасан нийлүүлэгч", "Security vendors"), vt.report_state === "scanned" ? vendors : undefined],
    [tr("Хортой", "Malicious"), vt.report_state === "scanned" ? stats.malicious || 0 : undefined],
    [tr("Сэжигтэй", "Suspicious"), vt.report_state === "scanned" ? stats.suspicious || 0 : undefined],
    [tr("Илрээгүй", "Undetected"), vt.report_state === "scanned" ? stats.undetected || 0 : undefined],
    [tr("Тайлан", "Report"), vt.report_source === "existing"
      ? tr("VirusTotal-ийн өмнөх тайлан", "Existing VirusTotal report")
      : vt.submitted ? tr("Шинэ шинжилгээ", "Fresh analysis") : undefined],
    [tr("Сүүлд шинжилсэн", "Last analyzed"), fmtUnixDate(vt.last_analysis_date)],
    ["Host", security.host],
    [tr("Бүртгэгдсэн домэйн", "Registered domain"), security.registered_domain],
  ]);
  if (!security.fully_verified) {
    card.append(el("p", "verification-warning", tr(
      "⚠ Энэ линк бүрэн баталгаажаагүй. “Шалгагдаагүй” нь “аюулгүй” гэсэн үг биш.",
      "⚠ This URL was not fully verified. “Not scanned” does not mean “safe.”",
    )));
  }
  if (vt.error) card.append(el("p", "inline-note", vt.error));
  addShareButton(card, data);
  return card;
}

function renderOverviewCard(data) {
  const website = data.website;
  if (!website) return null;
  const overview = data.overview || {};
  const summary = overview.summary || website.summary || {};
  const card = resultCard(tr("3 · Хуудасны тойм", "3 · Page overview"), "workflow-card");
  card.append(el("h2", "website-title", overview.title || website.title || tr("Гарчиггүй", "Untitled website")));
  const source = el("a", "source-url", overview.final_url || website.url || data.url);
  source.href = overview.final_url || website.url || data.url;
  source.target = "_blank";
  source.rel = "noreferrer noopener";
  card.append(source);

  addKeyValues(card, [
    [tr("Хуудасны төрөл", "Page type"), overview.page_type || website.inspection?.page_type],
    [tr("Эх линк", "Original URL"), website.original_url || data.url],
    [tr("Сайт", "Site"), overview.metadata?.site_name || website.metadata?.site_name],
    [tr("Зохиогч", "Author"), overview.metadata?.author || website.metadata?.author],
    [tr("Нийтэлсэн", "Published"), overview.metadata?.published_time || website.metadata?.published_time],
    [tr("Хэл", "Language"), overview.language || website.metadata?.language],
    [tr("Унших хугацаа", "Reading time"), summary.reading_time_minutes ? `${summary.reading_time_minutes} min` : undefined],
    [tr("Үгийн тоо", "Word count"), summary.word_count],
  ]);

  if (summary.brief) card.append(el("p", "summary overview-summary", summary.brief));
  if (summary.key_points?.length) {
    card.append(el("h4", "card-subtitle", tr("Гол санаа", "Key points")));
    const list = el("ul", "key-points");
    summary.key_points.forEach((point) => list.append(el("li", null, point)));
    card.append(list);
  }
  if (summary.topics?.length) {
    const topics = el("div", "topic-chips");
    summary.topics.forEach((topic) => topics.append(el("span", "topic-chip", topic)));
    card.append(topics);
  }
  if (summary.source) {
    card.append(el("p", "inline-note", `${tr("Тоймын эх сурвалж", "Summary source")}: ${summary.source}`));
  }

  const preview = renderWebsiteAppearance(website);
  card.append(
    makeDetails(
      tr("Хуудасны харагдацын тойм", "Page appearance preview"),
      detailsBody(preview),
    ),
  );
  return card;
}

function renderInspectionCard(data) {
  const website = data.website;
  const inspection = website?.inspection;
  if (!inspection) return null;
  const card = resultCard(tr("4 · Статик хуудасны шинжилгээ", "4 · Static page inspection"), "workflow-card");
  const forms = inspection.forms || [];
  const downloads = inspection.downloads || [];
  const frames = inspection.frames || [];
  const scripts = inspection.scripts || {};
  const redirects = inspection.redirects || website.fetch?.redirects || [];

  addKeyValues(card, [
    [tr("Хуудасны төрөл", "Page type"), inspection.page_type],
    [tr("Статик эрсдэл", "Static risk"), inspection.risk_level],
    [tr("Маягт", "Forms"), forms.length],
    [tr("Нууц үгийн талбар", "Password fields"), forms.filter((form) => form.password_field).length],
    [tr("Татаж авах линк", "Download links"), downloads.length],
    [tr("Дотоод скрипт", "Inline scripts"), scripts.inline_count],
    [tr("Хүрээ / iframe", "Frames / iframes"), frames.length],
    [tr("Дахин чиглүүлэлт", "Redirects"), redirects.length],
  ]);

  const findings = inspection.findings || [];
  if (findings.length) {
    card.append(el("h4", "card-subtitle", tr("Илэрсэн шинжүүд", "Static findings")));
    const list = el("div", "finding-list");
    findings.forEach((finding) => {
      const item = el("div", `finding ${severityClass(finding.severity)}`);
      const head = el("div", "finding-head");
      head.append(el("strong", null, finding.title || finding.id));
      head.append(el("span", "finding-severity", finding.severity || "info"));
      item.append(head, el("p", null, finding.detail || ""));
      if (finding.evidence) {
        const evidence = typeof finding.evidence === "string"
          ? finding.evidence
          : JSON.stringify(finding.evidence, null, 2);
        item.append(el("pre", "finding-evidence", evidence));
      }
      list.append(item);
    });
    card.append(list);
  } else {
    card.append(el("p", "inline-note", tr(
      "Статик шинжилгээгээр тодорхой эрсдэлийн шинж олдсонгүй.",
      "No specific static risk pattern was found.",
    )));
  }

  if (redirects.length) {
    const body = el("ol", "redirect-chain");
    redirects.forEach((hop) => {
      body.append(el(
        "li",
        null,
        `HTTP ${hop.status} → ${hop.to}${hop.cross_host ? tr(" (өөр домэйн)", " (different domain)") : ""}`,
      ));
    });
    card.append(makeDetails(tr("Дахин чиглүүлэлтийн хэлхээ", "Redirect chain"), detailsBody(body)));
  }

  if (forms.length) {
    const body = el("div", "inspection-list");
    forms.forEach((form, index) => {
      const item = el("div", "inspection-item");
      item.append(el("strong", null, `${tr("Маягт", "Form")} ${index + 1}: ${form.method.toUpperCase()}`));
      item.append(el("p", null, form.action));
      if (form.password_field) item.append(el("p", "inline-note", tr("Нууц үгийн талбар агуулна", "Contains a password field")));
      if (form.external_action) item.append(el("p", "inline-note", tr("Өөр домэйн руу илгээнэ", "Submits to a different domain")));
      if (form.sensitive_fields?.length) item.append(el("p", "inline-note", `${tr("Мэдрэмтгий талбар", "Sensitive fields")}: ${form.sensitive_fields.join(", ")}`));
      body.append(item);
    });
    card.append(makeDetails(tr("Маягтын шинжилгээ", "Form inspection"), detailsBody(body)));
  }

  if (downloads.length) {
    const body = el("div", "inspection-list");
    downloads.forEach((download) => {
      const item = el("div", "inspection-item");
      item.append(el("strong", null, `${download.severity === "high" ? "⚠ " : ""}${download.extension || tr("файл", "file")}`));
      item.append(el("p", null, download.url));
      if (download.explicit_download) item.append(el("p", "inline-note", tr("download атрибуттай", "Uses the download attribute")));
      body.append(item);
    });
    card.append(makeDetails(tr("Татаж авах файлууд", "Downloadable files"), detailsBody(body), true));
  }

  if (scripts.flags?.length || scripts.external?.length) {
    const body = el("div");
    addKeyValues(body, [
      [tr("Скриптийн шинж", "Script patterns"), (scripts.flags || []).join(", ")],
      [tr("Гадаад скрипт", "External scripts"), (scripts.external || []).length],
      [tr("Дотоод скриптийн хэмжээ", "Inline characters"), scripts.inline_characters],
    ]);
    if (scripts.external?.length) {
      const list = el("ul", "reason-list");
      scripts.external.forEach((url) => list.append(el("li", null, url)));
      body.append(list);
    }
    card.append(makeDetails(tr("Скриптийн шинжилгээ", "Script inspection"), detailsBody(body)));
  }

  if (inspection.meta_refresh || inspection.base_href || inspection.mixed_content?.length || frames.length) {
    const body = el("div");
    addKeyValues(body, [
      [tr("Автомат шилжилт", "Meta refresh"), inspection.meta_refresh?.target],
      [tr("Суурь хаяг", "Base href"), inspection.base_href],
      [tr("Холимог агуулга", "Mixed content"), inspection.mixed_content?.length],
      [tr("Гадаад хүрээ", "External frames"), frames.filter((frame) => frame.external).length],
    ]);
    if (frames.length) {
      const list = el("ul", "reason-list");
      frames.forEach((frame) => list.append(el("li", null, `${frame.kind}: ${frame.src || "inline"}${frame.hidden ? " (hidden)" : ""}`)));
      body.append(list);
    }
    card.append(makeDetails(tr("Бусад хуудасны зан төлөв", "Other page behavior"), detailsBody(body)));
  }

  const headers = inspection.security_headers || {};
  if (Object.keys(headers).length) {
    const body = el("div", "header-checks");
    Object.values(headers).forEach((header) => {
      const row = el("div", "header-check");
      row.append(el("span", null, header.label));
      row.append(el("span", header.present ? "header-present" : "header-missing", header.present ? "present" : "missing"));
      body.append(row);
    });
    card.append(makeDetails(tr("Аюулгүй байдлын HTTP header", "Security headers"), detailsBody(body)));
  }

  if (inspection.note) card.append(el("p", "zero-click-note", inspection.note));
  return card;
}

function renderFactcheckCard(data) {
  const card = resultCard(tr("5 · Баримт шалгалтын үр дүн", "5 · Fact-check results"), "workflow-card");
  const overview = data.factcheck_overview;
  if (overview) {
    addKeyValues(card, [
      [tr("Шалгасан мэдэгдэл", "Claims checked"), overview.checked],
      [tr("Баталгаажсан", "Supported"), overview.true],
      [tr("Худал", "Contradicted"), overview.false],
      [tr("Тодорхойгүй", "Unresolved"), overview.not_sure],
    ]);
    if (overview.headline) card.append(el("p", "summary", overview.headline));
    if (overview.note) card.append(el("p", "inline-note", overview.note));
  }
  const checks = Array.isArray(data.factchecks) && data.factchecks.length
    ? data.factchecks
    : data.factcheck ? [data.factcheck] : [];
  if (checks.length) {
    card.append(el("p", "inline-note", tr(
      "Хуудас бүхэлдээ үнэн эсвэл худал гэж дүгнэхгүй. Гол мэдэгдэл бүрийг тусад нь шалгав.",
      "The page is not labeled true or false as a whole. Its main claims are checked separately.",
    )));
    checks.forEach((result, index) => card.append(renderPageFactcheck(result, index, checks.length)));
  } else {
    card.append(el("div", "empty-state", data.factcheck_error || (
      data.workflow_status === "blocked_malicious"
        ? tr("Хортой линкээс агуулга татаагүй тул баримт шалгаагүй.", "Fact-check skipped because malicious content was not fetched.")
        : tr("Шалгах агуулга олдсонгүй.", "No content was available to fact-check.")
    )));
  }
  return card;
}

function renderCombinedUrl(container, data) {
  state.lastUrlResult = data;
  clear(container);

  const safetyCard = renderSafetyCard(data);
  if (safetyCard) container.append(safetyCard);

  const securityCard = renderUrlSecurityCard(data);
  if (securityCard) container.append(securityCard);

  const overviewCard = renderOverviewCard(data);
  if (overviewCard) container.append(overviewCard);

  const inspectionCard = renderInspectionCard(data);
  if (inspectionCard) container.append(inspectionCard);

  container.append(renderFactcheckCard(data));

  const security = data.security || {};
  addLimitations(container, security.limitations);
}

/* ---------------- Form wiring ---------------- */

async function runAnalysis({ containerId, button, loadingText, task, render }) {
  const container = document.getElementById(containerId);
  button.classList.add("is-loading");
  const wasDisabled = button.disabled;
  button.disabled = true;
  renderLoading(container, loadingText);
  try {
    const data = await task();
    render(container, data);
  } catch (error) {
    renderError(container, error);
  } finally {
    button.classList.remove("is-loading");
    button.disabled = wasDisabled;
  }
}

function wireFactcheck() {
  const form = document.getElementById("factcheck-form");
  const input = document.getElementById("claim-input");
  const language = document.getElementById("claim-language");
  const sourceScope = document.getElementById("claim-source-scope");
  const sourceBrowser = document.getElementById("claim-source-browser");
  const sourceSearch = document.getElementById("claim-source-search");
  const sourceReload = document.getElementById("claim-source-reload");

  if (sourceBrowser) {
    sourceBrowser.addEventListener("toggle", () => {
      if (sourceBrowser.open && !state.claimSources) loadClaimSources();
    });
  }
  if (sourceSearch) {
    sourceSearch.addEventListener("input", () => {
      renderSourceList(state.claimSources || [], sourceSearch.value);
    });
  }
  if (sourceReload) {
    sourceReload.addEventListener("click", () => loadClaimSources());
  }
  if (sourceScope) {
    sourceScope.addEventListener("change", () => {
      state.claimSources = null;
      if (sourceBrowser && sourceBrowser.open) loadClaimSources();
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const claim = input.value.trim();
    if (claim.length < 3) {
      input.focus();
      return;
    }
    const button = form.querySelector("button[type=submit]");
    await runAnalysis({
      containerId: "factcheck-output",
      button,
      loadingText: tr(
        "Мэдэгдлийг ойлгож, эх сурвалжуудаас хайж байна…",
        "Reading the claim and searching trusted sources…",
      ),
      task: () =>
        api("/api/factcheck", {
          method: "POST",
          body: {
            claim,
            language: language.value,
            source_scope: sourceScope ? sourceScope.value : "auto",
          },
          timeout: 120000,
        }),
      render: renderFactcheck,
    });
  });
}

function wireText() {
  const form = document.getElementById("text-form");
  const input = document.getElementById("text-input");
  const language = document.getElementById("text-language");
  const counter = document.getElementById("text-count");
  const updateCount = () => {
    const text = input.value;
    const words = text.trim() ? text.trim().split(/\s+/u).length : 0;
    counter.textContent = `${words.toLocaleString()} words · ${text.length.toLocaleString()} characters`;
  };
  input.addEventListener("input", updateCount);
  updateCount();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) {
      input.focus();
      return;
    }
    const button = form.querySelector("button[type=submit]");
    await runAnalysis({
      containerId: "text-output",
      button,
      loadingText: "Running text classifier… first run may download ~300 MB.",
      task: () => api("/api/analyze/text", { method: "POST", body: { text, language: language.value }, timeout: 600000 }),
      render: renderText,
    });
  });
}

function setupDropzone(zone, input, onFile) {
  ["dragenter", "dragover"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragover");
    });
  });
  zone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) {
      input.files = event.dataTransfer.files;
      onFile(file);
    }
  });
  input.addEventListener("change", () => {
    if (input.files?.[0]) onFile(input.files[0]);
  });
}

function wireImage() {
  const form = document.getElementById("image-form");
  const urlForm = document.getElementById("image-url-form");
  const urlInput = document.getElementById("image-url-input");
  const fileInput = document.getElementById("image-file");
  const dropzone = document.getElementById("image-dropzone");
  const submit = document.getElementById("image-submit");
  const previewWrap = document.getElementById("image-preview-wrap");
  const preview = document.getElementById("image-preview");
  const clearButton = document.getElementById("image-clear");

  const resetFile = () => {
    state.imageFile = null;
    fileInput.value = "";
    submit.disabled = true;
    previewWrap.hidden = true;
    preview.removeAttribute("src");
    if (state.imageObjectUrl) {
      URL.revokeObjectURL(state.imageObjectUrl);
      state.imageObjectUrl = null;
    }
  };

  setupDropzone(dropzone, fileInput, (file) => {
    if (!file.type.startsWith("image/")) return;
    resetFile();
    state.imageFile = file;
    state.imageObjectUrl = URL.createObjectURL(file);
    preview.src = state.imageObjectUrl;
    previewWrap.hidden = false;
    submit.disabled = false;
  });

  clearButton.addEventListener("click", (event) => {
    event.preventDefault();
    resetFile();
  });

  urlForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;
    await analyzeImageUrl(url);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.imageFile) return;
    const formData = new FormData();
    formData.append("file", state.imageFile, state.imageFile.name);
    await runAnalysis({
      containerId: "image-output",
      button: submit,
      loadingText: "Reading metadata and running the image classifier…",
      task: () =>
        api("/api/analyze/image", { method: "POST", form: formData, timeout: 600000 }),
      render: renderImage,
    });
  });
}

async function analyzeImageUrl(url, fallbackFrame = null) {
  await runAnalysis({
    containerId: "image-output",
    button: document.querySelector("#image-url-form button[type=submit]"),
    loadingText: "Fetching and inspecting the image…",
    task: async () => {
      try {
        return await api("/api/analyze/image-url", {
          method: "POST", body: { url }, timeout: 180000,
        });
      } catch (error) {
        if (!fallbackFrame) throw error;
        const data = await scoreVisibleImageFrame(fallbackFrame);
        data.image_fetch_error = error.message;
        return data;
      }
    },
    render: (container, data) => data.capture_scope === "visible_image_screenshot"
      ? renderVisibleImageFrame(container, data)
      : renderImage(container, data),
  });
}

async function scoreVisibleImageFrame(request) {
  const blob = await cropVisibleMediaFrame(request);
  const form = new FormData();
  form.append("file", blob, "visible-image.jpg");
  const data = await api("/api/analyze/image", {
    method: "POST", form, timeout: 600000,
  });
  data.capture_scope = "visible_image_screenshot";
  data.limitations = [
    "This is a screenshot of the displayed image. Original EXIF and C2PA metadata are unavailable.",
    ...(data.limitations || []),
  ];
  return data;
}

function renderVisibleImageFrame(container, data) {
  renderImage(container, data);
  const notice = el("div", "notice");
  notice.append(el("strong", null, "Displayed image only. "));
  notice.append(el("span", null,
    data.image_fetch_error ? `Original image download failed: ${data.image_fetch_error} ` : ""));
  notice.append(el("span", null,
    "TAYO inspected a screenshot. Original image metadata and full resolution were not available."));
  container.prepend(notice);
}

async function analyzeVisibleImageFrame(request) {
  await runAnalysis({
    containerId: "image-output",
    button: document.getElementById("image-submit"),
    loadingText: "Inspecting the displayed image…",
    task: () => scoreVisibleImageFrame(request),
    render: renderVisibleImageFrame,
  });
}

function wireVideo() {
  const form = document.getElementById("video-form");
  const urlForm = document.getElementById("video-url-form");
  const urlInput = document.getElementById("video-url-input");
  const fileInput = document.getElementById("video-file");
  const dropzone = document.getElementById("video-dropzone");
  const fileName = document.getElementById("video-file-name");
  const submit = document.getElementById("video-submit");

  urlForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;
    await analyzeVideoUrl(url);
  });

  setupDropzone(dropzone, fileInput, (file) => {
    if (!file.type.startsWith("video/")) return;
    state.videoFile = file;
    fileName.textContent = `${file.name} · ${fmtBytes(file.size)}`;
    fileName.hidden = false;
    submit.disabled = false;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.videoFile) return;
    const formData = new FormData();
    formData.append("file", state.videoFile, state.videoFile.name);
    await runAnalysis({
      containerId: "video-output",
      button: submit,
      loadingText: "Extracting frames and scoring them… this can take a while on CPU.",
      task: () => api("/api/analyze/video", { method: "POST", form: formData, timeout: 900000 }),
      render: renderVideo,
    });
  });
}

async function analyzeVideoUrl(url, fallbackFrame = null) {
  await runAnalysis({
    containerId: "video-output",
    button: document.querySelector("#video-url-form button[type=submit]"),
    loadingText: "Fetching the public video and sampling its frames…",
    task: async () => {
      try {
        return await api("/api/analyze/video-url", {
          method: "POST", body: { url }, timeout: 600000,
        });
      } catch (error) {
        if (!fallbackFrame) throw error;
        const data = await scoreVisibleVideoFrame(fallbackFrame);
        data.video_fetch_error = error.message;
        return data;
      }
    },
    render: (container, data) => {
      if (data.capture_scope === "single_visible_video_frame") {
        renderVisibleVideoFrame(container, data);
      } else {
        renderVideo(container, data);
      }
    },
  });
}

async function cropVisibleMediaFrame(request) {
  const image = new Image();
  image.src = request.screenshot;
  await image.decode();
  const rect = request.rect;
  const left = Math.max(0, rect.left);
  const top = Math.max(0, rect.top);
  const right = Math.min(rect.viewportWidth, rect.left + rect.width);
  const bottom = Math.min(rect.viewportHeight, rect.top + rect.height);
  if (right - left < 32 || bottom - top < 32) {
    throw new Error("The media is not visible enough to inspect.");
  }
  const scaleX = image.naturalWidth / rect.viewportWidth;
  const scaleY = image.naturalHeight / rect.viewportHeight;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round((right - left) * scaleX);
  canvas.height = Math.round((bottom - top) * scaleY);
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Could not prepare the captured media.");
  context.drawImage(
    image,
    left * scaleX, top * scaleY,
    (right - left) * scaleX, (bottom - top) * scaleY,
    0, 0, canvas.width, canvas.height,
  );
  return await new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("Could not encode the captured media.")),
      "image/jpeg", 0.88,
    );
  });
}

async function scoreVisibleVideoFrame(request) {
  const blob = await cropVisibleMediaFrame(request);
  const form = new FormData();
  form.append("file", blob, "visible-video-frame.jpg");
  const data = await api("/api/analyze/image", {
    method: "POST", form, timeout: 600000,
  });
  data.capture_scope = "single_visible_video_frame";
  data.limitations = [
    "Only one visible frame was inspected. This is not a video-wide result.",
    ...(data.limitations || []),
  ];
  return data;
}

function renderVisibleVideoFrame(container, data) {
  renderImage(container, data);
  const notice = el("div", "notice");
  notice.append(el("strong", null, "Current frame only. "));
  notice.append(el("span", null,
    data.video_fetch_error
      ? `Full video download failed: ${data.video_fetch_error} `
      : "This player has no direct video file URL. "));
  notice.append(el("span", null,
    "TAYO inspected one visible frame, not the full video. Upload the original file for multi-frame analysis."));
  container.prepend(notice);
}

async function analyzeVisibleVideoFrame(request) {
  await runAnalysis({
    containerId: "video-output",
    button: document.getElementById("video-submit"),
    loadingText: "Inspecting the visible video frame…",
    task: () => scoreVisibleVideoFrame(request),
    render: renderVisibleVideoFrame,
  });
}

function wireUrl() {
  const form = document.getElementById("url-form");
  const input = document.getElementById("url-input");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = input.value.trim();
    if (!url) return;
    const button = form.querySelector("button[type=submit]");
    await runAnalysis({
      containerId: "url-output",
      button,
      loadingText: tr(
        "Аюулгүй байдлыг шалгаж, зөвшөөрөгдвөл агуулгыг задлан баримт шалгаж байна…",
        "Checking security, then extracting and fact-checking permitted content…",
      ),
      task: () =>
        api("/api/check/url", {
          method: "POST",
          body: {
            url,
            language: "auto",
            source_scope: document.getElementById("url-source-scope")?.value || "auto",
            ...(state.virusTotalApiKey
              ? { virustotal_api_key: state.virusTotalApiKey }
              : {}),
          },
          timeout: 120000,
        }),
      render: renderCombinedUrl,
    });
  });
}

/* ---------------- Context-menu requests ---------------- */

async function handleExternalRequest(request) {
  if (!request || !request.type) return;
  if (Date.now() - (request.createdAt || 0) > 120000) return;
  if (Date.now() - state.lastHandled < 800) return;
  state.lastHandled = Date.now();

  if (request.type === "factcheck") {
    switchTab("factcheck");
    const input = document.getElementById("claim-input");
    input.value = request.value || "";
    document.getElementById("factcheck-form").requestSubmit();
  } else if (request.type === "url") {
    switchTab("url");
    document.getElementById("url-input").value = request.value || "";
    document.getElementById("url-form").requestSubmit();
  } else if (request.type === "image") {
    switchTab("image");
    document.getElementById("image-url-input").value = request.value || "";
    await analyzeImageUrl(request.value || "", request.frame);
  } else if (request.type === "image-frame") {
    switchTab("image");
    await analyzeVisibleImageFrame(request);
  } else if (request.type === "image-error") {
    switchTab("image");
    renderError(document.getElementById("image-output"), new Error(request.message));
  } else if (request.type === "text") {
    switchTab("text");
    const input = document.getElementById("text-input");
    input.value = request.value || "";
    input.dispatchEvent(new Event("input"));
    if (input.value.trim()) {
      document.getElementById("text-form").requestSubmit();
    } else {
      renderError(document.getElementById("text-output"),
        new Error("No text was selected to analyze."));
    }
  } else if (request.type === "text-error") {
    switchTab("text");
    renderError(document.getElementById("text-output"), new Error(request.message));
  } else if (request.type === "video") {
    switchTab("video");
    document.getElementById("video-url-input").value = request.value || "";
    await analyzeVideoUrl(request.value || "", request.frame);
  } else if (request.type === "video-frame") {
    switchTab("video");
    await analyzeVisibleVideoFrame(request);
  } else if (request.type === "video-error") {
    switchTab("video");
    renderError(document.getElementById("video-output"), new Error(request.message));
  }
}

async function consumePending() {
  if (!hasChrome || !chrome.storage.session) return;
  try {
    const stored = await chrome.storage.session.get("tayoPending");
    if (stored?.tayoPending) {
      await chrome.storage.session.remove("tayoPending");
      await handleExternalRequest(stored.tayoPending);
    }
  } catch (error) {
    console.warn("Could not read pending request:", error);
  }
}

/* ---------------- Settings dialog ---------------- */

function wireSettings() {
  const dialog = document.getElementById("settings-dialog");
  const button = document.getElementById("settings-button");
  const form = document.getElementById("settings-form");
  const urlInput = document.getElementById("backend-url");
  const status = document.getElementById("settings-status");
  const virusTotalInput = document.getElementById("virustotal-api-key");
  const virusTotalStatus = document.getElementById("virustotal-status");
  const connectVirusTotal = document.getElementById("connect-virustotal");
  const disconnectVirusTotal = document.getElementById("disconnect-virustotal");

  const showVirusTotalStatus = (message, connected = false) => {
    virusTotalStatus.textContent = message;
    virusTotalStatus.classList.toggle("is-connected", connected);
    disconnectVirusTotal.hidden = !connected;
    virusTotalInput.value = "";
    virusTotalInput.placeholder = connected ? "Saved key ••••••••" : "Paste API key";
  };

  button.addEventListener("click", () => {
    urlInput.value = state.backendUrl;
    status.textContent = "";
    showVirusTotalStatus(
      state.virusTotalApiKey ? "Connected to VirusTotal" : "Not connected",
      Boolean(state.virusTotalApiKey),
    );
    dialog.showModal();
  });

  connectVirusTotal.addEventListener("click", async () => {
    const apiKey = virusTotalInput.value.trim();
    if (!apiKey) {
      virusTotalStatus.textContent = state.virusTotalApiKey
        ? "Already connected. Paste a new key to replace it."
        : "Paste your VirusTotal API key first.";
      virusTotalInput.focus();
      return;
    }
    connectVirusTotal.disabled = true;
    virusTotalStatus.textContent = "Checking key with VirusTotal…";
    try {
      const result = await api("/api/virustotal/validate", {
        method: "POST",
        body: { api_key: apiKey },
        timeout: 30000,
      });
      if (!result.valid) {
        virusTotalStatus.textContent = result.message || "VirusTotal rejected this key.";
        return;
      }
      await saveVirusTotalKey(apiKey);
      showVirusTotalStatus(result.message || "VirusTotal connected successfully.", true);
    } catch (error) {
      virusTotalStatus.textContent = error.message;
    } finally {
      connectVirusTotal.disabled = false;
    }
  });

  virusTotalInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      connectVirusTotal.click();
    }
  });

  disconnectVirusTotal.addEventListener("click", async () => {
    await saveVirusTotalKey("");
    showVirusTotalStatus("Disconnected. Saved key removed.", false);
  });

  dialog.querySelectorAll("[data-close-dialog]").forEach((node) => {
    node.addEventListener("click", () => dialog.close());
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = urlInput.value.trim();
    if (!/^https?:\/\//i.test(url)) {
      status.textContent = "Enter a full http:// or https:// URL.";
      return;
    }
    await saveSettings(url);
    status.textContent = "Saved. Checking connection…";
    const health = await checkHealth(true);
    status.textContent = health
      ? `Connected to backend v${health.version || "?"} on ${health.models?.device || "cpu"}.`
      : "Saved, but the backend did not respond.";
    setTimeout(() => dialog.close(), 900);
  });

  document.getElementById("clear-data").addEventListener("click", async () => {
    if (hasChrome && chrome.storage.local) {
      await chrome.storage.local.clear();
      if (chrome.storage.session) await chrome.storage.session.clear();
    }
    try {
      localStorage.removeItem("tayoBackend");
      localStorage.removeItem("tayoVirusTotalApiKey");
    } catch {
      /* ignore */
    }
    state.backendUrl = DEFAULT_BACKEND;
    state.virusTotalApiKey = "";
    urlInput.value = DEFAULT_BACKEND;
    showVirusTotalStatus("Disconnected. Saved key removed.", false);
    status.textContent = "Saved data cleared. Backend URL reset.";
    checkHealth();
  });

  document.getElementById("retry-health").addEventListener("click", () => checkHealth(true));
  document.getElementById("url-connect-virustotal").addEventListener("click", () => {
    button.click();
    setTimeout(() => virusTotalInput.focus(), 0);
  });
}

/* ---------------- Init ---------------- */

async function init() {
  document.getElementById("lang-mn").addEventListener("click", () => setLocale("mn"));
  document.getElementById("lang-en").addEventListener("click", () => setLocale("en"));
  setLocale("mn");
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  wireFactcheck();
  wireText();
  wireImage();
  wireVideo();
  wireUrl();
  wireSettings();

  await loadSettings();
  await checkHealth();
  await consumePending();

  if (hasChrome && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === "session" && changes.tayoPending?.newValue) {
        const pending = changes.tayoPending.newValue;
        chrome.storage.session.remove("tayoPending").catch(() => {});
        handleExternalRequest(pending);
      }
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
