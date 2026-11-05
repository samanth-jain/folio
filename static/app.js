const STATUS_OPTIONS = ["New", "In review", "Needs info", "Approved", "Declined"];
const CATEGORIES = ["Identity", "Income", "Bank statement", "Credit report", "Address proof", "Other"];
const PALETTE_SIZE = 6;
const FLAGGED = "__flagged";
const SEVERITIES = ["critical", "warning", "info"];
let allApplications = [];
let categoryCounts = {};
let stats = {};
let activeView = "overview";
let activeStatus = "";
let activeCategory = "";
let toastTimer;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const money = (amount) => new Intl.NumberFormat("en-IN", {
  style: "currency", currency: "INR", maximumFractionDigits: 0,
}).format(amount || 0);
const initials = (name) => String(name || "?").trim().split(/\s+/).slice(0, 2).map((part) => part[0] || "").join("").toUpperCase();
const statusClass = (status) => `status-${String(status || "").toLowerCase().replace(/\s+/g, "-")}`;
const dateLabel = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  const difference = Date.now() - date.getTime();
  if (difference < 60_000) return "Just now";
  if (difference < 3_600_000) return `${Math.max(1, Math.floor(difference / 60_000))}m ago`;
  if (difference < 86_400_000) return `${Math.max(1, Math.floor(difference / 3_600_000))}h ago`;
  return date.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
};

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    location.reload();
    throw new Error("Please sign in.");
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error || `Request failed (${response.status}). Restart the server if you just updated the app.`);
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 3200);
}

async function loadDashboard() {
  const search = $("#search").value.trim();
  const dashboard = await request(`/api/dashboard?search=${encodeURIComponent(search)}`);
  allApplications = dashboard.applications;
  categoryCounts = dashboard.category_counts || {};
  stats = dashboard.stats;
  $("#metric-total").textContent = stats.total_applications;
  $("#metric-review").textContent = stats.in_review;
  $("#metric-attention").textContent = stats.flagged_cases;
  $("#metric-attention-foot").textContent = `${stats.open_flags} open ${stats.open_flags === 1 ? "flag" : "flags"} to review`;
  $("#metric-docs").textContent = stats.documents;
  $("#nav-count").textContent = stats.total_applications;
  $("#queue-total").textContent = stats.queue;
  renderRecent();
  renderPipeline();
  renderCategoryPreview();
  renderStageTabs();
  renderTable();
  renderQueue();
  if (activeView === "documents") loadDocuments().catch((error) => showToast(error.message));
}

function renderPipeline() {
  const counts = {};
  STATUS_OPTIONS.forEach((status) => { counts[status] = 0; });
  allApplications.forEach((application) => { counts[application.status] += 1; });
  const total = allApplications.length;
  $("#pipeline").innerHTML = total ? `<div class="pipeline-bar">${STATUS_OPTIONS.filter((status) => counts[status]).map((status) => `<span class="${statusClass(status)}" data-grow="${counts[status]}"></span>`).join("")}</div>
    <div class="pipeline-legend">${STATUS_OPTIONS.map((status) => `<span class="${statusClass(status)}"><i></i>${status} <b>${counts[status]}</b></span>`).join("")}</div>` : "";
  $$("#pipeline [data-grow]").forEach((segment) => { segment.style.flexGrow = segment.dataset.grow; });
}

function renderRecent() {
  const rows = allApplications.slice(0, 5);
  $("#recent-rows").innerHTML = rows.map((application, index) => `<tr data-open="${escapeHtml(application.id)}">
      <td>${personCell(application, index)}</td>
      <td>${escapeHtml(application.loan_type)}</td><td class="loan-amount">${money(application.amount)}</td>
      <td><span class="status-pill ${statusClass(application.status)}">${escapeHtml(application.status)}</span></td>
      <td class="updated-cell">${dateLabel(application.updated_at)}</td></tr>`).join("");
  $("#recent-empty").classList.toggle("hidden", rows.length > 0);
}

function renderCategoryPreview() {
  $("#category-preview").innerHTML = CATEGORIES.map((category) => `<div><span class="file-type ${categoryClass(category)}">${escapeHtml(category.slice(0, 3).toUpperCase())}</span><span>${escapeHtml(category)}</span><b>${categoryCounts[category] || 0}</b></div>`).join("");
}

function renderStageTabs() {
  const flagged = allApplications.filter((application) => application.open_flags).length;
  const tabs = [["", "All", allApplications.length], ...STATUS_OPTIONS.map((status) => [status, status, allApplications.filter((application) => application.status === status).length]), [FLAGGED, "⚑ Flagged", flagged]];
  $("#stage-tabs").innerHTML = tabs.map(([value, label, count]) => `<button role="tab" class="stage-tab ${value === activeStatus ? "active" : ""}" data-stage="${escapeHtml(value)}">${escapeHtml(label)}<span>${count}</span></button>`).join("");
}

function personCell(application, index) {
  return `<div class="person-cell"><span class="person-avatar palette-${index % PALETTE_SIZE}">${escapeHtml(initials(application.applicant_name))}</span><span class="person-copy"><strong>${escapeHtml(application.applicant_name)}${flagBadge(application)}</strong><span>${escapeHtml(application.id)} · ${escapeHtml(application.city || "City not set")}</span></span></div>`;
}

function flagBadge(item) {
  if (!item.open_flags) return "";
  const tone = item.critical_flags ? "critical" : "warning";
  const label = `${item.open_flags} open ${item.open_flags === 1 ? "flag" : "flags"}${item.critical_flags ? `, ${item.critical_flags} critical` : ""}`;
  return `<span class="flag-badge flag-${tone}" title="${label}">⚑ ${item.open_flags}</span>`;
}

function categoryClass(category) {
  return { Identity: "pdf", "Bank statement": "bank", "Credit report": "credit" }[category] || "other";
}

function scoreCell(score) {
  if (score == null) return `<span class="muted-cell">—</span>`;
  const tone = score >= 750 ? "good" : score >= 700 ? "fair" : "low";
  return `<span class="score score-${tone}">${score}</span>`;
}

function filteredApplications() {
  if (activeStatus === FLAGGED) return allApplications.filter((application) => application.open_flags);
  return activeStatus ? allApplications.filter((application) => application.status === activeStatus) : allApplications;
}

function renderTable() {
  const rows = filteredApplications();
  $("#application-rows").innerHTML = rows.map((application, index) => `<tr data-open="${escapeHtml(application.id)}">
      <td>${personCell(application, index)}</td>
      <td>${escapeHtml(application.loan_type)}</td><td class="loan-amount">${money(application.amount)}</td>
      <td>${application.monthly_income == null ? `<span class="muted-cell">—</span>` : money(application.monthly_income)}</td>
      <td>${scoreCell(application.credit_score)}</td>
      <td><span class="docs-count"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2h5l3 3v9H4zM9 2v3h3"/></svg>${application.document_count} files</span></td>
      <td><span class="status-pill ${statusClass(application.status)}">${escapeHtml(application.status)}</span></td>
      <td class="updated-cell">${dateLabel(application.updated_at)}</td>
    </tr>`).join("");
  $("#empty-state").classList.toggle("hidden", rows.length > 0);
  $("#table-count").textContent = `${rows.length} ${rows.length === 1 ? "application" : "applications"} shown`;
}

async function loadDocuments() {
  const search = $("#doc-search").value.trim();
  const data = await request(`/api/documents?search=${encodeURIComponent(search)}&category=${encodeURIComponent(activeCategory)}`);
  const total = Object.values(data.category_counts).reduce((sum, count) => sum + count, 0);
  $("#category-tabs").innerHTML = [["", "All", total], ...CATEGORIES.map((category) => [category, category, data.category_counts[category] || 0])]
    .map(([value, label, count]) => `<button role="tab" class="stage-tab ${value === activeCategory ? "active" : ""}" data-category="${escapeHtml(value)}">${escapeHtml(label)}<span>${count}</span></button>`).join("");
  $("#document-rows").innerHTML = data.documents.map((document) => {
    const extension = (document.name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
    const size = document.has_file ? `${Math.max(1, Math.round(document.size / 1024))} KB` : "Record only";
    const action = document.has_file ? `<a class="document-download" target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(document.id)}/view">Open</a> · <a class="document-download" href="/api/documents/${encodeURIComponent(document.id)}/download">Download</a>` : "";
    const read = document.has_extraction ? `<span class="extract-pill">Data extracted</span>` : `<span class="extract-pill empty">Not read yet</span>`;
    return `<tr data-open="${escapeHtml(document.application_id)}">
      <td><div class="person-cell"><span class="document-icon">${escapeHtml(extension)}</span><span class="person-copy"><strong>${escapeHtml(document.name)}${flagBadge(document)}</strong><span>${escapeHtml(document.notes || document.id)}</span></span></div></td>
      <td><span class="category-pill ${categoryClass(document.category)}">${escapeHtml(document.category)}</span></td>
      <td>${escapeHtml(document.applicant_name)}<br><span class="muted-cell">${escapeHtml(document.application_id)}</span></td>
      <td>${size}<br>${read}</td><td class="updated-cell">${dateLabel(document.created_at)}</td><td>${action}</td></tr>`;
  }).join("");
  $("#doc-empty").classList.toggle("hidden", data.documents.length > 0);
  $("#doc-count").textContent = `${data.documents.length} ${data.documents.length === 1 ? "document" : "documents"} shown`;
}

function renderQueue() {
  const queue = allApplications.filter((application) => ["New", "In review", "Needs info"].includes(application.status)).slice(0, 4);
  $("#queue-list").innerHTML = queue.length ? queue.map((application, index) => {
    const task = application.status === "Needs info" ? "Waiting for information" : application.status === "New" ? "Ready for first review" : "Document verification";
    return `<div class="queue-item" data-open="${escapeHtml(application.id)}"><span class="queue-avatar palette-${(index + 2) % PALETTE_SIZE}">${escapeHtml(initials(application.applicant_name))}</span><span class="queue-person"><strong>${escapeHtml(application.applicant_name)}</strong><span>${task}</span></span><span class="queue-mark">›</span></div>`;
  }).join("") : `<div class="queue-empty">Nothing waiting for review. Nice work.</div>`;
}

function setView(view) {
  activeView = view;
  activeStatus = "";
  activeCategory = "";
  const labels = {
    overview: ["Overview", "Underwriting desk", "A clear view of every application, from first document to final review."],
    applications: ["Applications", "All applications", "Every case, with stage, income and credit score side by side."],
    documents: ["Documents", "Document library", "Every file across all cases, organized by category."],
    mcp: ["MCP setup", "Your Claude, connected.", "Give Claude the tools to organize your lending workflow."],
  };
  const [crumb, title, subtitle] = labels[view] || labels.overview;
  $("#crumb").textContent = crumb;
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $$("[data-page]").forEach((section) => section.classList.toggle("hidden", section.dataset.page !== view));
  $("#new-application").classList.toggle("hidden", view === "mcp");
  $$(".nav-link").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  renderStageTabs();
  renderTable();
  if (view === "documents") loadDocuments().catch((error) => showToast(error.message));
}

async function loadMcpUrl() {
  const info = await request("/api/mcp-info");
  $("#mcp-endpoint").textContent = info.url;
}

function closeDrawer() {
  $("#drawer-backdrop").classList.add("hidden");
  $("#drawer").innerHTML = "";
}

function showDrawer(content) {
  $("#drawer").innerHTML = content;
  $("#drawer-backdrop").classList.remove("hidden");
  $("#drawer").focus();
}

function categoryOptions(selected) {
  return CATEGORIES.map((category) => `<option ${category === selected ? "selected" : ""}>${escapeHtml(category)}</option>`).join("");
}

function documentMarkup(document) {
  const id = escapeHtml(document.id);
  const extension = (document.name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
  const fieldCount = Object.keys(document.extracted_data || {}).length;
  const read = fieldCount ? `<span class="extract-pill">${fieldCount} ${fieldCount === 1 ? "field" : "fields"} extracted</span>` : document.has_text ? `<span class="extract-pill">Text extracted</span>` : `<span class="extract-pill empty">Not read yet</span>`;
  const links = document.has_file ? `<a class="document-download" target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(document.id)}/view">Open file</a><a class="document-download" href="/api/documents/${encodeURIComponent(document.id)}/download">Download</a>` : "";
  return `<div class="document-item" data-document="${id}">
    <div class="document-row">
      <span class="document-icon">${escapeHtml(extension)}</span>
      <span class="document-copy"><strong>${escapeHtml(document.name)}</strong><span>${escapeHtml(document.category)} · ${document.has_file ? `${Math.max(1, Math.round(document.size / 1024))} KB stored` : "Record only, no file yet"}</span></span>
      <label class="field"><select class="document-category" aria-label="Category for ${escapeHtml(document.name)}">${categoryOptions(document.category)}</select></label>
    </div>
    <div class="document-actions">${read}<button type="button" class="link-button" data-doc-toggle="${id}">View extracted data</button>${links}</div>
    <div class="document-detail hidden" id="doc-detail-${id}"></div>
  </div>`;
}

function documentDetailMarkup(document) {
  const id = escapeHtml(document.id);
  const rows = Object.entries(document.extracted_data || {}).map(([key, value]) => `<div class="extract-row"><span class="extract-key">${escapeHtml(key)}</span><input class="extract-value" data-key="${escapeHtml(key)}" value="${escapeHtml(value)}" aria-label="${escapeHtml(key)}"></div>`).join("");
  const preview = document.has_file && ["image/png", "image/jpeg", "image/webp"].includes(document.mime_type) ? `<img class="doc-preview" alt="Preview of ${escapeHtml(document.name)}" src="/api/documents/${encodeURIComponent(document.id)}/view">` : "";
  return `${preview}
    <h4>Extracted fields</h4>
    ${rows ? `<div class="extract-grid">${rows}</div>` : `<p class="muted-note">No fields yet. In Claude, ask: “Read document ${id} and save the extracted fields.”</p>`}
    <div class="extract-add"><input id="new-field-name-${id}" placeholder="New field name" aria-label="New field name"><input id="new-field-value-${id}" placeholder="Value" aria-label="New field value"></div>
    <div class="extract-actions"><button type="button" class="secondary-button" data-doc-save="${id}">Save fields</button><span class="muted-note">Clear a value to remove the field.${document.extracted_at ? ` Last updated ${dateLabel(document.extracted_at)}.` : ""}</span></div>
    <h4>Document text</h4>
    ${document.extracted_text ? `<pre class="extracted-text">${escapeHtml(document.extracted_text)}</pre>` : `<p class="muted-note">${document.has_file ? "No text read from this file yet. CSV, DOCX and XLSX are read automatically; PDFs and images are read by Claude." : "Record only. No file has been uploaded."}</p>`}`;
}

async function toggleDocumentDetail(id) {
  const panel = $(`#doc-detail-${CSS.escape(id)}`);
  if (!panel) return;
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  panel.textContent = "Loading...";
  try {
    panel.innerHTML = documentDetailMarkup(await request(`/api/documents/${encodeURIComponent(id)}`));
  } catch (error) {
    panel.textContent = error.message;
  }
}

async function saveDocumentFields(id) {
  const panel = $(`#doc-detail-${CSS.escape(id)}`);
  const fields = {};
  $$(".extract-value", panel).forEach((input) => {
    if (input.value.trim()) fields[input.dataset.key] = input.value.trim();
  });
  const name = $(`#new-field-name-${CSS.escape(id)}`).value.trim();
  const value = $(`#new-field-value-${CSS.escape(id)}`).value.trim();
  if (name && value) fields[name] = value;
  try {
    const document = await request(`/api/documents/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extracted_data: fields }),
    });
    panel.innerHTML = documentDetailMarkup(document);
    showToast("Extracted data saved.");
    loadDashboard().catch(() => {});
  } catch (error) {
    showToast(error.message);
  }
}

function flagItemMarkup(flag) {
  const id = escapeHtml(flag.id);
  const where = flag.document_name ? `on ${escapeHtml(flag.document_name)} · ` : "";
  return `<div class="flag-item flag-${escapeHtml(flag.severity)} ${flag.resolved ? "resolved" : ""}">
    <span class="flag-icon">⚑</span>
    <div class="flag-copy"><strong>${escapeHtml(flag.title)}</strong>${flag.detail ? `<p>${escapeHtml(flag.detail)}</p>` : ""}<span>${escapeHtml(flag.severity)} · ${where}${flag.source === "claude" ? "Raised by Claude" : "Added by reviewer"} · ${dateLabel(flag.created_at)}${flag.resolved ? " · Resolved" : ""}</span></div>
    <div class="flag-actions"><button type="button" class="link-button" data-flag-resolve="${id}" data-resolved="${flag.resolved ? "0" : "1"}">${flag.resolved ? "Reopen" : "Resolve"}</button><button type="button" class="link-button danger-link" data-flag-delete="${id}">Delete</button></div>
  </div>`;
}

function flagsSection(application) {
  const flags = application.flags || [];
  const open = flags.filter((flag) => !flag.resolved).length;
  const documentOptions = (application.documents || []).map((document) => `<option value="${escapeHtml(document.id)}">${escapeHtml(document.name)}</option>`).join("");
  return `<section class="drawer-section"><div class="drawer-section-head"><h3>Flags for review</h3><span>${open} open${flags.length > open ? ` · ${flags.length - open} resolved` : ""}</span></div>
    <div class="flag-list">${flags.map(flagItemMarkup).join("") || `<div class="no-documents">No flags. Claude raises them while reading documents, and you can add your own.</div>`}</div>
    <div class="flag-add">
      <input id="flag-title" placeholder="Add a flag, e.g. Name differs from PAN" maxlength="200" aria-label="Flag title">
      <select id="flag-severity" aria-label="Severity">${SEVERITIES.map((severity) => `<option ${severity === "warning" ? "selected" : ""}>${severity}</option>`).join("")}</select>
      <select id="flag-document" aria-label="Document"><option value="">Whole case</option>${documentOptions}</select>
      <button type="button" class="secondary-button" id="add-flag">Add flag</button>
    </div></section>`;
}

async function addFlag() {
  const title = $("#flag-title").value.trim();
  if (!title) {
    showToast("Describe the flag first.");
    return;
  }
  const applicationId = $("#application-form").dataset.id;
  try {
    await request(`/api/loans/${encodeURIComponent(applicationId)}/flags`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, severity: $("#flag-severity").value, document_id: $("#flag-document").value }),
    });
    await loadDashboard();
    await openApplication(applicationId);
    showToast("Flag added.");
  } catch (error) {
    showToast(error.message);
  }
}

async function changeFlag(flagId, options) {
  const applicationId = $("#application-form").dataset.id;
  try {
    await request(`/api/flags/${encodeURIComponent(flagId)}`, options);
    await loadDashboard();
    await openApplication(applicationId);
  } catch (error) {
    showToast(error.message);
  }
}

function detailForm(application) {
  const documents = application.documents || [];
  const statusOptions = STATUS_OPTIONS.map((status) => `<option ${status === application.status ? "selected" : ""}>${status}</option>`).join("");
  const documentRows = documents.map(documentMarkup).join("");
  return `<div class="drawer-top"><div><span class="drawer-kicker">${application.id ? "APPLICATION FILE" : "NEW APPLICATION"}</span></div><div class="drawer-top-actions">${application.id ? `<span class="status-pill ${statusClass(application.status)}">${escapeHtml(application.status)}</span>` : ""}<button class="drawer-close" aria-label="Close dialog">×</button></div></div>
    <form id="application-form" class="drawer-content" data-id="${escapeHtml(application.id || "")}">
      <div class="drawer-title-row"><div><h2>${application.id ? escapeHtml(application.applicant_name) : "Create application"}</h2><p>${application.id ? "Update applicant details and keep the case file in order." : "Add the applicant and requested loan details."}</p></div></div>
      ${application.id ? `<div class="drawer-meta"><span>${escapeHtml(application.id)}</span><span>Created ${dateLabel(application.created_at)}</span><label class="field"><select name="status" aria-label="Application status">${statusOptions}</select></label></div>` : ""}
      ${application.id ? flagsSection(application) : ""}
      <section class="drawer-section"><div class="drawer-section-head"><h3>Applicant details</h3>${application.id ? `<span>${documents.length} documents</span>` : ""}</div>
        <div class="form-grid">
          <div class="field full"><label for="applicant_name">Full name *</label><input id="applicant_name" name="applicant_name" required maxlength="200" value="${escapeHtml(application.applicant_name || "")}" placeholder="e.g. Priya Shah"></div>
          <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" value="${escapeHtml(application.email || "")}" placeholder="name@example.com"></div>
          <div class="field"><label for="phone">Phone</label><input id="phone" name="phone" value="${escapeHtml(application.phone || "")}" placeholder="+91"></div>
          <div class="field"><label for="city">City</label><input id="city" name="city" value="${escapeHtml(application.city || "")}" placeholder="City"></div>
          <div class="field"><label for="loan_type">Loan type *</label><select id="loan_type" name="loan_type" required><option value="">Select type</option>${["Personal loan", "Business loan", "Home loan", "Vehicle loan", "Other"].map((type) => `<option ${application.loan_type === type ? "selected" : ""}>${type}</option>`).join("")}</select></div>
          <div class="field"><label for="amount">Requested amount (₹) *</label><input id="amount" name="amount" type="number" min="1" step="1" required value="${escapeHtml(application.amount ?? "")}" placeholder="400000"></div>
          <div class="field"><label for="monthly_income">Monthly income (₹)</label><input id="monthly_income" name="monthly_income" type="number" min="0" step="1" value="${escapeHtml(application.monthly_income ?? "")}" placeholder="Optional"></div>
          <div class="field"><label for="credit_score">Credit score</label><input id="credit_score" name="credit_score" type="number" min="0" max="900" step="1" value="${escapeHtml(application.credit_score ?? "")}" placeholder="Optional"></div>
          <div class="field full"><label for="purpose">Loan purpose</label><input id="purpose" name="purpose" value="${escapeHtml(application.purpose || "")}" placeholder="What is the loan for?"></div>
          <div class="field full"><label for="reviewer_note">Reviewer note</label><textarea id="reviewer_note" name="reviewer_note" placeholder="Internal notes for the review team...">${escapeHtml(application.reviewer_note || "")}</textarea></div>
        </div>
      </section>
      ${application.id ? `<section class="drawer-section"><div class="drawer-section-head"><h3>Case documents</h3><span>${documents.length} filed</span></div>
        <div class="document-list">${documentRows || `<div class="no-documents">No documents filed yet.<br>Upload files below or ask Claude to register a checklist item.</div>`}</div>
        <div class="upload-box"><div class="upload-controls"><label class="field"><span>Category for selected files</span><select id="upload-category">${categoryOptions("Identity")}</select></label><label class="field"><span>Choose files</span><input id="upload-files" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.webp,.docx,.xlsx,.csv"></label><button type="button" class="secondary-button" id="upload-button">Upload</button></div><p class="upload-help">PDF, image, DOCX, XLSX or CSV · Up to 15 MB total · Files are stored in the database. CSV, DOCX and XLSX text is read automatically; Claude reads PDFs and images through MCP.</p></div>
      </section>` : ""}
      <p class="form-error hidden" id="form-error"></p>
    </form>
    <div class="drawer-footer">${application.id ? `<button type="button" class="danger-button" id="delete-application">Delete application</button>` : ""}<button type="button" class="secondary-button drawer-close-button">Cancel</button><button type="submit" form="application-form" class="save-button">${application.id ? "Save changes" : "Create application"}</button></div>`;
}

async function openApplication(id) {
  try {
    const application = await request(`/api/loans/${encodeURIComponent(id)}`);
    showDrawer(detailForm(application));
  } catch (error) {
    showToast(error.message);
  }
}

function openNewApplication() {
  showDrawer(detailForm({}));
}

async function saveApplication(event) {
  event.preventDefault();
  const form = event.target;
  const formData = new FormData(form);
  const data = Object.fromEntries(formData.entries());
  for (const field of ["amount", "monthly_income", "credit_score"]) {
    if (data[field] === "") delete data[field];
    else if (data[field] !== undefined) data[field] = Number(data[field]);
  }
  const id = form.dataset.id;
  try {
    const application = await request(id ? `/api/loans/${encodeURIComponent(id)}` : "/api/loans", {
      method: id ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    closeDrawer();
    await loadDashboard();
    showToast(id ? "Application updated." : `Application ${application.id} created.`);
    if (!id) openApplication(application.id);
  } catch (error) {
    const message = $("#form-error");
    message.textContent = error.message;
    message.classList.remove("hidden");
  }
}

async function uploadDocuments() {
  const files = $("#upload-files")?.files;
  if (!files?.length) {
    showToast("Choose one or more files first.");
    return;
  }
  const applicationId = $("#application-form").dataset.id;
  const body = new FormData();
  body.append("category", $("#upload-category").value);
  for (const file of files) body.append("files", file);
  const button = $("#upload-button");
  button.disabled = true;
  button.textContent = "Uploading...";
  try {
    await request(`/api/loans/${encodeURIComponent(applicationId)}/documents`, { method: "POST", body });
    await loadDashboard();
    await openApplication(applicationId);
    showToast("Documents uploaded and added to the case file.");
  } catch (error) {
    button.disabled = false;
    button.textContent = "Upload";
    showToast(error.message);
  }
}

async function updateDocumentCategory(select) {
  const row = select.closest("[data-document]");
  try {
    await request(`/api/documents/${encodeURIComponent(row.dataset.document)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category: select.value }),
    });
    showToast("Document category updated.");
  } catch (error) {
    showToast(error.message);
    await openApplication($("#application-form").dataset.id);
  }
}

async function deleteApplication() {
  const form = $("#application-form");
  const name = $("#applicant_name").value || "this applicant";
  if (!confirm(`Permanently delete ${name} and all their documents? This cannot be undone.`)) return;
  try {
    await request(`/api/loans/${encodeURIComponent(form.dataset.id)}`, { method: "DELETE" });
    closeDrawer();
    await loadDashboard();
    showToast("Application deleted.");
  } catch (error) {
    showToast(error.message);
  }
}

function initialize() {
  setView("overview");
  $("#date-label").textContent = new Intl.DateTimeFormat("en-IN", { weekday: "long", day: "numeric", month: "long" }).format(new Date()).toUpperCase();
  loadDashboard().catch((error) => showToast(error.message));
  loadMcpUrl().catch(() => {});
  request("/api/me").then((me) => {
    $("#profile-name").textContent = me.username;
    $$(".avatar").forEach((avatar) => { avatar.textContent = initials(me.username); });
  }).catch(() => {});

  document.addEventListener("click", (event) => {
    if (event.target.closest("a.document-download")) return;
    const toggle = event.target.closest("[data-doc-toggle]");
    if (toggle) return void toggleDocumentDetail(toggle.dataset.docToggle);
    const saveFields = event.target.closest("[data-doc-save]");
    if (saveFields) return void saveDocumentFields(saveFields.dataset.docSave);
    const resolve = event.target.closest("[data-flag-resolve]");
    if (resolve) return void changeFlag(resolve.dataset.flagResolve, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ resolved: resolve.dataset.resolved === "1" }) });
    const removeFlag = event.target.closest("[data-flag-delete]");
    if (removeFlag) return void changeFlag(removeFlag.dataset.flagDelete, { method: "DELETE" });
    if (event.target.closest("#add-flag")) return void addFlag();
    const stageTab = event.target.closest("[data-stage]");
    if (stageTab) {
      activeStatus = stageTab.dataset.stage;
      renderStageTabs();
      renderTable();
      return;
    }
    const categoryTab = event.target.closest("[data-category]");
    if (categoryTab) {
      activeCategory = categoryTab.dataset.category;
      loadDocuments().catch((error) => showToast(error.message));
      return;
    }
    const viewButton = event.target.closest("[data-view]");
    if (viewButton) {
      setView(viewButton.dataset.view);
      return;
    }
    const applicationRow = event.target.closest("[data-open]");
    if (applicationRow) {
      openApplication(applicationRow.dataset.open);
      return;
    }
    if (event.target.closest("#new-application")) openNewApplication();
    if (event.target.closest(".drawer-close, .drawer-close-button") || event.target === $("#drawer-backdrop")) closeDrawer();
    if (event.target.closest("#upload-button")) uploadDocuments();
    if (event.target.closest("#delete-application")) deleteApplication();
    if (event.target.closest("#signout")) {
      fetch("/api/logout", { method: "POST" }).then(() => location.reload());
    }
    if (event.target.closest("#copy-endpoint")) {
      navigator.clipboard.writeText($("#mcp-endpoint").textContent).then(() => showToast("MCP URL copied.")).catch(() => showToast("Copy the URL shown above."));
    }
  });
  document.addEventListener("submit", (event) => {
    if (event.target.id === "application-form") saveApplication(event);
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches(".document-category")) updateDocumentCategory(event.target);
  });
  let searchTimer;
  $("#search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadDashboard().catch((error) => showToast(error.message)), 180);
  });
  let docSearchTimer;
  $("#doc-search").addEventListener("input", () => {
    clearTimeout(docSearchTimer);
    docSearchTimer = setTimeout(() => loadDocuments().catch((error) => showToast(error.message)), 180);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      (activeView === "documents" ? $("#doc-search") : $("#search")).focus();
    }
  });
}

initialize();
