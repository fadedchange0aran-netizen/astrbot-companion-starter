const state = {
  books: [],
  selectedBookName: "",
  page: null,
  qqTargetUmo: "",
  callContextMode: "auto",
  actionBusy: false,
  saveBusy: false,
};

function getPluginNameFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  const contentIndex = parts.indexOf("content");
  if (contentIndex !== -1 && parts[contentIndex + 1]) {
    return decodeURIComponent(parts[contentIndex + 1]);
  }
  return "astrbot_plugin_bookshelf";
}

class BookshelfApi {
  constructor() {
    this.bridge = window.AstrBotPluginPage;
    this.pluginName = getPluginNameFromPath();
  }

  async ready() {
    if (!this.bridge || typeof this.bridge.ready !== "function") {
      throw new Error("AstrBot 插件页 bridge 不可用");
    }
    await this.bridge.ready();
  }

  async get(path, params = {}) {
    return this.bridge.apiGet(`page/${path}`, params);
  }

  async post(path, body = {}) {
    return this.bridge.apiPost(`page/${path}`, body);
  }

  async uploadBook(file, bookName) {
    const rawBookName = String(bookName || "").trim();
    const ext = file.name.includes(".")
      ? file.name.slice(file.name.lastIndexOf("."))
      : "";
    const normalizedName = rawBookName
      ? `${rawBookName.replace(/[\\/:*?"<>|]+/g, "_")}${ext}`
      : file.name;
    const fileBase64 = await fileToBase64(file);
    return this.post("upload", {
      book_name: rawBookName,
      file_name: normalizedName,
      file_type: file.type || "application/octet-stream",
      file_base64: fileBase64,
    });
  }
}

const api = new BookshelfApi();

const elements = {
  bookList: document.getElementById("book-list"),
  bookTitle: document.getElementById("book-title"),
  bookMeta: document.getElementById("book-meta"),
  chapterSelect: document.getElementById("chapter-select"),
  chapterTitle: document.getElementById("chapter-title"),
  chapterStats: document.getElementById("chapter-stats"),
  chapterContent: document.getElementById("chapter-content"),
  callFooterHint: document.getElementById("call-footer-hint"),
  qqTargetPanel: document.getElementById("qq-target-panel"),
  qqTargetInput: document.getElementById("qq-target-input"),
  callContextModeSelect: document.getElementById("call-context-mode-select"),
  saveQqTargetButton: document.getElementById("save-qq-target-button"),
  discussionList: document.getElementById("discussion-list"),
  discussionCount: document.getElementById("discussion-count"),
  prevButton: document.getElementById("prev-button"),
  nextButton: document.getElementById("next-button"),
  refreshButton: document.getElementById("refresh-button"),
  callAranButton: document.getElementById("call-aran-button"),
  uploadForm: document.getElementById("upload-form"),
  uploadBookName: document.getElementById("upload-book-name"),
  uploadFile: document.getElementById("upload-file"),
  uploadButton: document.getElementById("upload-button"),
  toast: document.getElementById("toast"),
};

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  elements.toast.style.background = isError ? "rgba(163, 33, 33, 0.92)" : "rgba(21, 32, 51, 0.92)";
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 2600);
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const commaIndex = result.indexOf(",");
      resolve(commaIndex >= 0 ? result.slice(commaIndex + 1) : result);
    };
    reader.onerror = () => {
      reject(new Error("读取上传文件失败"));
    };
    reader.readAsDataURL(file);
  });
}

function syncSettings(settings = {}) {
  if (!settings || typeof settings !== "object") {
    return;
  }
  if (typeof settings.qq_target_umo === "string") {
    state.qqTargetUmo = settings.qq_target_umo;
    elements.qqTargetInput.value = settings.qq_target_umo;
  }
  if (typeof settings.call_context_mode === "string") {
    state.callContextMode = settings.call_context_mode;
  }
}

function getCallModeHint(mode) {
  if (mode === "full") {
    return "当前模式：全文。会把整章正文都发给陪读助手，最完整，但长章会更重。";
  }
  if (mode === "excerpt") {
    return "当前模式：节选。无论长短都只发开头、中段、结尾，最省上下文。";
  }
  return "当前模式：自动。短章会直接喂全文，长章会按开头、中段、结尾折中提供；同一目标会话下还会记住最近几次主动陪读记录。";
}

function setPrimaryActionBusy(isBusy) {
  state.actionBusy = isBusy;
  elements.callAranButton.disabled = isBusy;
}

function setSaveBusy(isBusy) {
  state.saveBusy = isBusy;
  elements.saveQqTargetButton.disabled = isBusy;
}

function renderCallPanel() {
  elements.qqTargetInput.value = state.qqTargetUmo;
  elements.callContextModeSelect.value = state.callContextMode;
  elements.callAranButton.disabled = state.actionBusy || !state.page;
  elements.saveQqTargetButton.disabled = state.saveBusy;
  elements.callFooterHint.textContent = getCallModeHint(state.callContextMode);
}

function unwrapResponse(response) {
  if (!response) {
    throw new Error("空响应");
  }
  if (response.status === "error") {
    throw new Error(response.message || "请求失败");
  }
  if (Object.prototype.hasOwnProperty.call(response, "data")) {
    return response.data;
  }
  return response;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function setLoadingChapter(message) {
  elements.chapterContent.textContent = message;
  elements.chapterContent.classList.remove("empty");
}

function renderBookList() {
  if (!state.books.length) {
    elements.bookList.innerHTML = '<div class="empty">书架还是空的，先上传一本书吧。</div>';
    return;
  }
  elements.bookList.innerHTML = state.books
    .map((book) => {
      const active = book.name === state.selectedBookName ? "active" : "";
      return `
        <div class="book-item ${active}">
          <button type="button" data-book-name="${escapeHtml(book.name)}">
            <div class="book-name">《${escapeHtml(book.name)}》</div>
            <div class="muted small">第 ${book.current_chapter}/${book.total_chapters} 章 · ${book.progress_percent}%</div>
          </button>
        </div>
      `;
    })
    .join("");
}

function renderPage() {
  renderBookList();
  if (!state.page) {
    elements.bookTitle.textContent = "还没有选书";
    elements.bookMeta.textContent = "先上传一本书，或者从左边书架里选一本。";
    elements.chapterTitle.textContent = "章节内容";
    elements.chapterStats.textContent = "0 字";
    elements.chapterContent.textContent = "请选择一本书。";
    elements.chapterContent.classList.add("empty");
    elements.chapterSelect.innerHTML = "";
    renderCallPanel();
    elements.discussionList.innerHTML = '<div class="empty">还没有笔记或读后感。</div>';
    elements.discussionCount.textContent = "0";
    elements.prevButton.disabled = true;
    elements.nextButton.disabled = true;
    return;
  }

  const book = state.page.book;
  const chapter = state.page.current_chapter;
  const discussion = state.page.discussion;

  elements.bookTitle.textContent = `《${book.name}》`;
  elements.bookMeta.textContent = `第 ${book.current_chapter}/${book.total_chapters} 章 · ${book.progress_percent}%`;
  elements.chapterTitle.textContent = `${chapter.title}`;
  elements.chapterStats.textContent = `${chapter.chars} 字`;
  elements.chapterContent.textContent = chapter.content || "这一章现在没有内容。";
  elements.chapterContent.classList.remove("empty");
  renderCallPanel();
  elements.discussionCount.textContent = String(
    (discussion.recent_items || []).length,
  );

  elements.chapterSelect.innerHTML = (state.page.chapters || [])
    .map((item) => {
      const selected = item.is_current ? "selected" : "";
      return `<option value="${item.no}" ${selected}>第 ${item.no} 章 · ${escapeHtml(item.title)}</option>`;
    })
    .join("");

  if (!discussion.recent_items || !discussion.recent_items.length) {
    elements.discussionList.innerHTML = '<div class="empty">还没有笔记或读后感。</div>';
  } else {
    elements.discussionList.innerHTML = discussion.recent_items
      .map((item) => {
        const kind = item.kind === "thought" ? "读后感" : "笔记";
        return `
          <div class="discussion-item">
            <div><strong>${kind}</strong> · 第 ${item.chapter} 章</div>
            <div class="muted small">${escapeHtml(item.author)} · ${escapeHtml(item.time)}</div>
            <div>${escapeHtml(item.content)}</div>
          </div>
        `;
      })
      .join("");
  }

  elements.prevButton.disabled = !chapter.has_prev;
  elements.nextButton.disabled = !chapter.has_next;
}

async function refreshBooks(preferredBookName = "") {
  const data = unwrapResponse(await api.get("books"));
  state.books = data.books || [];
  state.selectedBookName = preferredBookName || data.selected_book_name || "";
  syncSettings(data.settings);
  renderBookList();
  if (!state.selectedBookName && state.books.length) {
    state.selectedBookName = state.books[0].name;
  }
}

async function loadBook(bookName = "") {
  const targetName = bookName || state.selectedBookName;
  if (!targetName) {
    state.page = null;
    renderPage();
    return;
  }
  setLoadingChapter("正在加载章节内容...");
  const data = unwrapResponse(await api.get("book", { book_name: targetName }));
  state.books = data.books || state.books;
  state.selectedBookName = data.selected_book_name || targetName;
  state.page = data.page || null;
  syncSettings(data.settings);
  renderPage();
}

async function changeChapter(chapterNo) {
  if (!state.selectedBookName) {
    return;
  }
  setLoadingChapter("正在切换章节...");
  state.page = unwrapResponse(
    await api.post("chapter", {
      book_name: state.selectedBookName,
      chapter_no: Number(chapterNo),
    }),
  );
  await refreshBooks(state.selectedBookName);
  renderPage();
}

async function handleUpload(event) {
  event.preventDefault();
  const file = elements.uploadFile.files?.[0];
  if (!file) {
    showToast("先选一个 txt 或 epub 文件。", true);
    return;
  }
  elements.uploadButton.disabled = true;
  try {
    const response = unwrapResponse(
      await api.uploadBook(file, elements.uploadBookName.value.trim()),
    );
    state.books = response.books || [];
    state.selectedBookName = response.selected_book_name || "";
    state.page = response.page || null;
    elements.uploadForm.reset();
    renderPage();
    showToast(`已导入《${state.selectedBookName}》`);
  } catch (error) {
    const detail = error.message || "上传失败";
    showToast(detail, true);
  } finally {
    elements.uploadButton.disabled = false;
  }
}

async function saveQqTarget() {
  const qqTargetUmo = elements.qqTargetInput.value.trim();
  const callContextMode = elements.callContextModeSelect.value;
  setSaveBusy(true);
  try {
    const data = unwrapResponse(
      await api.post("settings", {
        qq_target_umo: qqTargetUmo,
        call_context_mode: callContextMode,
      }),
    );
    syncSettings(data);
    showToast(qqTargetUmo ? "已记住 QQ 目标和上下文模式" : "已清空默认 QQ 目标");
    renderCallPanel();
  } finally {
    setSaveBusy(false);
  }
}

async function triggerQqDiscussion() {
  if (!state.page?.book?.name || !state.page?.current_chapter?.chapter_no) {
    showToast("先选一本书和章节。", true);
    return;
  }
  const qqTargetUmo = elements.qqTargetInput.value.trim();
  if (!qqTargetUmo) {
    showToast("先填写一个 QQ 目标会话 UMO。", true);
    return;
  }
  const callContextMode = elements.callContextModeSelect.value;
  setPrimaryActionBusy(true);
  try {
    const data = unwrapResponse(
      await api.post("call_aran", {
        book_name: state.page.book.name,
        chapter_no: state.page.current_chapter.chapter_no,
        qq_target_umo: qqTargetUmo,
        call_context_mode: callContextMode,
      }),
    );
    state.qqTargetUmo = data.qq_target_umo || qqTargetUmo;
    state.callContextMode = data.call_context_mode || callContextMode;
    elements.qqTargetInput.value = state.qqTargetUmo;
    showToast(`已发到目标会话：${state.qqTargetUmo}`);
    renderCallPanel();
  } finally {
    setPrimaryActionBusy(false);
  }
}

async function boot() {
  try {
    await api.ready();
    await refreshBooks();
    await loadBook();
    renderPage();
  } catch (error) {
    console.error(error);
    showToast(error.message || "插件页初始化失败", true);
  }
}

elements.bookList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-book-name]");
  if (!button) {
    return;
  }
  const bookName = button.getAttribute("data-book-name") || "";
  try {
    await loadBook(bookName);
  } catch (error) {
    showToast(error.message || "加载书籍失败", true);
  }
});

elements.chapterSelect.addEventListener("change", async (event) => {
  try {
    await changeChapter(event.target.value);
  } catch (error) {
    showToast(error.message || "切换章节失败", true);
  }
});

elements.prevButton.addEventListener("click", async () => {
  if (!state.page?.current_chapter?.chapter_no) {
    return;
  }
  try {
    await changeChapter(state.page.current_chapter.chapter_no - 1);
  } catch (error) {
    showToast(error.message || "切换章节失败", true);
  }
});

elements.nextButton.addEventListener("click", async () => {
  if (!state.page?.current_chapter?.chapter_no) {
    return;
  }
  try {
    await changeChapter(state.page.current_chapter.chapter_no + 1);
  } catch (error) {
    showToast(error.message || "切换章节失败", true);
  }
});

elements.refreshButton.addEventListener("click", async () => {
  try {
    await refreshBooks(state.selectedBookName);
    await loadBook(state.selectedBookName);
    showToast("已刷新书架");
  } catch (error) {
    showToast(error.message || "刷新失败", true);
  }
});

elements.callAranButton.addEventListener("click", () => {
  if (!state.page?.book?.name) {
    showToast("先选一本书和章节。", true);
    return;
  }
  triggerQqDiscussion().catch((error) => {
    showToast(error.message || "呼叫陪读助手失败", true);
  });
});

elements.saveQqTargetButton.addEventListener("click", () => {
  saveQqTarget().catch((error) => {
    showToast(error.message || "保存 QQ 目标失败", true);
  });
});

elements.qqTargetInput.addEventListener("change", () => {
  state.qqTargetUmo = elements.qqTargetInput.value.trim();
});

elements.callContextModeSelect.addEventListener("change", () => {
  state.callContextMode = elements.callContextModeSelect.value;
  renderCallPanel();
});

elements.uploadForm.addEventListener("submit", handleUpload);

boot();
