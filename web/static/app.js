/* Upload the month's folder, watch the comparison run, render the tables. */
"use strict";

const { csrf, limits } = window.ELBASH;
const $ = (id) => document.getElementById(id);

const IMAGE_EXT = ["jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff"];
// Each request carries a few files rather than one, so 1400 photos do not mean
// 1400 round trips; several requests are in flight at once.
const BATCH_BYTES = 24 * 1024 * 1024;
const BATCH_FILES = 12;
const PARALLEL_UPLOADS = 3;

let chosen = [];          // File objects waiting to be uploaded
let jobId = null;
let payload = null;       // last result from the server
let activeTab = "missing";
let poller = null;
let cancelled = false;

const ext = (name) => (name.split(".").pop() || "").toLowerCase();
const isImage = (f) => IMAGE_EXT.includes(ext(f.name));
const isPdf = (f) => ext(f.name) === "pdf";
const mb = (bytes) => (bytes / 1024 / 1024).toFixed(1);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-CSRF-Token": csrf, ...(options.headers || {}) },
  });
  if (response.status === 401) {
    window.location = "/login";
    throw new Error("נדרשת התחברות.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `שגיאה ${response.status}`);
  return body;
}

/* ------------------------------------------------------------- selecting */

// A dropped folder arrives as directory entries, which have to be walked.
function walk(entry, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => { out.push(file); resolve(); }, resolve);
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const next = () => reader.readEntries(async (batch) => {
        if (!batch.length) return resolve();
        await Promise.all(batch.map((e) => walk(e, out)));
        next();
      }, resolve);
      next();
    } else {
      resolve();
    }
  });
}

async function filesFromDrop(dataTransfer) {
  const entries = [...dataTransfer.items]
    .filter((item) => item.kind === "file")
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (!entries.length) return [...dataTransfer.files];

  const out = [];
  await Promise.all(entries.map((entry) => walk(entry, out)));
  return out;
}

function addFiles(files) {
  const usable = files.filter((f) => isImage(f) || isPdf(f));
  const seen = new Set(chosen.map((f) => f.name + f.size));
  for (const file of usable) {
    if (!seen.has(file.name + file.size)) {
      seen.add(file.name + file.size);
      chosen.push(file);
    }
  }
  showSelection();
}

function showSelection() {
  const photos = chosen.filter(isImage);
  const pdfs = chosen.filter(isPdf);
  const bytes = chosen.reduce((sum, f) => sum + f.size, 0);

  $("selection").classList.toggle("hidden", chosen.length === 0);
  $("count-photos").textContent = `${photos.length} תמונות`;
  $("count-pdf").textContent =
    pdfs.length === 1 ? `דוח: ${pdfs[0].name}` :
    pdfs.length === 0 ? "ללא דוח" : `${pdfs.length} קובצי PDF`;
  $("count-size").textContent = `${mb(bytes)} מ״ב`;

  let problem = "";
  if (pdfs.length > 1) problem = "יש לצרף קובץ PDF אחד בלבד של הדוח.";
  else if (chosen.length > limits.max_files) problem = `עד ${limits.max_files} קבצים בכל פעם.`;
  else if (bytes > limits.max_job_mb * 1024 * 1024) problem = `סך הקבצים חורג מ-${limits.max_job_mb} מ״ב.`;

  showError(problem);
  $("run").disabled = Boolean(problem) || pdfs.length !== 1 || photos.length === 0;
}

function showError(text) {
  const box = $("upload-error");
  box.textContent = text || "";
  box.classList.toggle("hidden", !text);
}

/* ------------------------------------------------------------- uploading */

function batches(files) {
  const out = [];
  let current = [], size = 0;
  for (const file of files) {
    if (current.length && (current.length >= BATCH_FILES || size + file.size > BATCH_BYTES)) {
      out.push(current);
      current = []; size = 0;
    }
    current.push(file);
    size += file.size;
  }
  if (current.length) out.push(current);
  return out;
}

async function uploadAll() {
  const groups = batches(chosen);
  let uploaded = 0;
  const rejected = [];
  let next = 0;

  async function worker() {
    while (next < groups.length && !cancelled) {
      const group = groups[next++];
      const form = new FormData();
      for (const file of group) form.append("files", file, file.name);
      const body = await api(`/api/jobs/${jobId}/files`, { method: "POST", body: form });
      rejected.push(...(body.rejected || []));
      uploaded += group.length;
      setProgress(uploaded, chosen.length, `מעלה ${uploaded} מתוך ${chosen.length} קבצים`, "");
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(PARALLEL_UPLOADS, groups.length) }, worker)
  );
  return rejected;
}

/* --------------------------------------------------------------- running */

function setProgress(done, total, message, detail) {
  $("progress-message").textContent = message || "";
  $("progress-detail").textContent = detail || "";
  $("progress-count").textContent = total ? `${done} / ${total}` : "";
  $("progress-bar").style.width = total ? `${(100 * done) / total}%` : "0%";
}

async function run() {
  cancelled = false;
  showError("");
  $("results").classList.add("hidden");
  $("progress-panel").classList.remove("hidden");
  $("run").disabled = true;
  setProgress(0, chosen.length, "מתחיל בהעלאה…", "");

  try {
    jobId = (await api("/api/jobs", { method: "POST" })).id;
    const rejected = await uploadAll();
    if (cancelled) return;
    if (rejected.length) {
      showError(`${rejected.length} קבצים נדחו: ${rejected.slice(0, 3).map((r) => r.name).join(", ")}`);
    }

    setProgress(0, 0, "מעבד…", "");
    await api(`/api/jobs/${jobId}/start`, { method: "POST" });
    poller = setInterval(poll, 600);
  } catch (error) {
    $("progress-panel").classList.add("hidden");
    $("run").disabled = false;
    showError(error.message);
  }
}

async function poll() {
  let state;
  try {
    state = await api(`/api/jobs/${jobId}`);
  } catch (error) {
    clearInterval(poller);
    showError(error.message);
    return;
  }

  setProgress(state.done, state.total, state.message, state.detail);

  if (state.status === "done") {
    clearInterval(poller);
    $("progress-panel").classList.add("hidden");
    payload = state.result;
    render();
  } else if (state.status === "error" || state.status === "stopped") {
    clearInterval(poller);
    $("progress-panel").classList.add("hidden");
    $("run").disabled = false;
    showError(state.error || "ההשוואה הופסקה.");
  }
}

/* --------------------------------------------------------------- results */

const COLUMNS = {
  missing: [
    { key: "number", label: "מס' תעודה", cls: "num" },
    { key: "date", label: "תאריך", cls: "num" },
    { key: "kind", label: "סוג" },
    { key: "route", label: "מסלול" },
    { key: "page", label: "עמוד", cls: "num" },
  ],
  matched: [
    { key: "number", label: "מס' תעודה", cls: "num" },
    { key: "date", label: "תאריך", cls: "num" },
    { key: "kind", label: "סוג" },
    { key: "route", label: "מסלול" },
    { key: "photos", label: "תמונה", photo: true },
    { key: "confidence", label: "ודאות" },
  ],
  problems: [
    { key: "file", label: "תמונה", photo: true },
    { key: "number", label: "מספר משלוח", cls: "num" },
    { key: "reason", label: "סיבה" },
    { key: "detail", label: "פירוט" },
  ],
  by_date: [
    { key: "date", label: "תאריך", cls: "num" },
    { key: "have", label: "יש", cls: "num" },
    { key: "missing", label: "חסר", cls: "num" },
    { key: "total", label: 'סה״כ', cls: "num" },
    { key: "bar", label: "", bar: true },
  ],
};

const TAB_LABEL = { missing: "חסר", matched: "נמצא", problems: "בעיות", by_date: "לפי תאריך" };

function render() {
  if (!payload) return;
  const s = payload.summary;

  // The dropzone has done its job; "השוואה חדשה" brings it back.
  $("upload-panel").classList.add("hidden");
  $("results").classList.remove("hidden");
  $("c-rows").textContent = s.rows;
  $("c-photos").textContent = s.photos;
  $("c-matched").textContent = s.matched;
  $("c-coverage").textContent = `${s.coverage}%`;

  const missing = $("c-missing");
  missing.textContent = s.missing;
  missing.className = "card-value " + (s.missing ? "bad" : "good");
  const problems = $("c-problems");
  problems.textContent = s.problems;
  problems.className = "card-value " + (s.problems ? "bad" : "good");

  const verdict = $("verdict");
  if (s.complete) {
    verdict.textContent = "✓ הושלם – לכל מס' תעודה בדוח יש תמונה תואמת.";
    verdict.className = "verdict good";
  } else {
    const bits = [];
    if (s.missing) bits.push(`${s.missing} תעודות ללא תמונה`);
    if (s.problems) bits.push(`${s.problems} תמונות דורשות בדיקה`);
    if (s.duplicates) bits.push(`${s.duplicates} מספרים צולמו פעמיים`);
    verdict.textContent = "⚠ " + bits.join(" · ");
    verdict.className = "verdict bad";
  }

  for (const tab of document.querySelectorAll(".tab")) {
    const key = tab.dataset.tab;
    tab.textContent = `${TAB_LABEL[key]} (${(payload[key] || []).length})`;
  }
  renderTable();
}

function renderTable() {
  const columns = COLUMNS[activeTab];
  const needle = $("filter").value.trim().toLowerCase();
  let rows = payload[activeTab] || [];
  if (needle) {
    rows = rows.filter((row) =>
      columns.some((c) => String(row[c.key] ?? "").toLowerCase().includes(needle))
    );
  }

  const table = $("table");
  table.tHead.innerHTML = "";
  const head = table.tHead.insertRow();
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column.label;
    head.appendChild(th);
  }

  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = body.insertRow();
    for (const column of columns) {
      const td = tr.insertCell();
      if (column.cls) td.className = column.cls;

      if (column.bar) {
        const filled = row.total ? Math.round((100 * row.have) / row.total) : 0;
        td.innerHTML = `<span class="minibar"><span style="width:${filled}%"></span></span>`;
      } else if (column.photo) {
        const names = [].concat(row[column.key] || []).filter(Boolean);
        td.append(...names.flatMap((name, index) => {
          const link = document.createElement("span");
          link.className = "photo-link ltr";
          link.textContent = name;
          link.onclick = () => openPhoto(name);
          return index ? [document.createTextNode(", "), link] : [link];
        }));
      } else {
        td.textContent = row[column.key] ?? "";
      }
    }
  }
  table.replaceChild(body, table.tBodies[0]);

  const total = (payload[activeTab] || []).length;
  $("table-foot").textContent = needle
    ? `${rows.length} מתוך ${total} שורות`
    : `${total} שורות`;

  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = columns.length;
    cell.className = "empty";
    cell.textContent = needle ? "אין תוצאות לחיפוש." : "אין רשומות בלשונית זו.";
  }
}

function openPhoto(name) {
  $("lightbox-img").src = `/api/jobs/${jobId}/photo/${encodeURIComponent(name)}`;
  $("lightbox-name").textContent = name;
  $("lightbox").classList.remove("hidden");
}

function reset() {
  chosen = [];
  payload = null;
  jobId = null;
  $("results").classList.add("hidden");
  $("progress-panel").classList.add("hidden");
  $("upload-panel").classList.remove("hidden");
  $("file-input").value = "";
  $("folder-input").value = "";
  showSelection();
  showError("");
}

/* ----------------------------------------------------------------- wiring */

const dropzone = $("dropzone");
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("over");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", async (e) => {
  e.preventDefault();
  dropzone.classList.remove("over");
  addFiles(await filesFromDrop(e.dataTransfer));
});
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); }
});

$("pick-files").onclick = () => $("file-input").click();
$("pick-folder").onclick = () => $("folder-input").click();
$("file-input").onchange = (e) => addFiles([...e.target.files]);
$("folder-input").onchange = (e) => addFiles([...e.target.files]);

$("clear").onclick = reset;
$("restart").onclick = () => { reset(); window.scrollTo({ top: 0, behavior: "smooth" }); };
$("run").onclick = run;

$("stop").onclick = async () => {
  cancelled = true;
  if (jobId) await api(`/api/jobs/${jobId}/stop`, { method: "POST" }).catch(() => {});
};

$("export-excel").onclick = () => { window.location = `/api/jobs/${jobId}/export/excel`; };
$("export-csv").onclick = () => { window.location = `/api/jobs/${jobId}/export/csv`; };

$("tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  document.querySelector(".tab.active")?.classList.remove("active");
  tab.classList.add("active");
  activeTab = tab.dataset.tab;
  renderTable();
});

let filterTimer = null;
$("filter").addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(renderTable, 150);
});

$("lightbox").onclick = () => $("lightbox").classList.add("hidden");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("lightbox").classList.add("hidden");
});
