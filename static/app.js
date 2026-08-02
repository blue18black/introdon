// 画面遷移・イベントバインド
(() => {
  // 現状はイントロドンのみ対応(アウトロドン/同時再生クイズは一旦なし)。
  const MODE = "intro";

  const State = {
    source: "artist",
    pool: null,
    selectedArtists: [], // 複数指定できるアーティスト名のリスト
    artistPool: null, // 選択中の全アーティストをマージした曲プール
    artistTrackCache: {}, // `${name}::${scope}` -> {artistName, tracks}
    playlistPool: null, // 読み込んだプレイリストの曲プール
    lastPlaylistTitle: "",
    savedPool: null, // 保存済みデータセットから読み込んだ曲プール
    savedActiveId: null,
    numQuestions: 10,
    seconds: 2,
    session: null,
    countdownTimer: null,
    brokenVideoIds: new Set(), // 再生できないと判明した曲(このセッション中は避ける)
  };

  function $(id) { return document.getElementById(id); }

  function showScreen(id) {
    document.querySelectorAll(".screen").forEach((el) => el.classList.remove("is-active"));
    $(id).classList.add("is-active");
  }

  function tracksPerQuestion() { return 1; }

  // ---------------- ホーム画面(イントロドン設定) ----------------

  $("source-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    document.querySelectorAll("#source-tabs .tab").forEach((t) => t.classList.remove("is-active"));
    tab.classList.add("is-active");
    State.source = tab.dataset.source;
    document.querySelectorAll(".source-pane").forEach((p) => p.classList.remove("is-active"));
    $(`source-${State.source}`).classList.add("is-active");
    // 保存済みタブでは「この曲リストを保存」は不要(保存済みのものを保存し直すだけになるため)。
    $("save-dataset-row").classList.toggle("hidden", State.source === "saved");

    if (State.source === "artist") {
      State.pool = State.artistPool;
      updateStartButtonState();
    } else if (State.source === "playlist") {
      State.pool = State.playlistPool;
      updateStartButtonState();
    } else if (State.source === "saved") {
      renderSavedDatasetsList();
      State.pool = State.savedPool;
      updateStartButtonState();
    }
  });

  // ---- プレイリストのURL指定 ----
  const playlistInput = $("playlist-input");

  async function loadPlaylistPool() {
    const url = playlistInput.value.trim();
    const statusEl = $("playlist-status");
    if (!url) {
      statusEl.textContent = "プレイリストのURLを入力してください";
      statusEl.classList.add("is-error");
      return;
    }
    statusEl.textContent = "読み込み中...";
    statusEl.classList.remove("is-error");
    State.playlistPool = null;
    if (State.source === "playlist") State.pool = null;
    updateStartButtonState();
    try {
      const result = await Api.getPlaylistTracks(url);
      if (!result.tracks || result.tracks.length === 0) {
        statusEl.textContent = "曲が見つかりませんでした";
        statusEl.classList.add("is-error");
        return;
      }
      State.playlistPool = result.tracks;
      State.lastPlaylistTitle = result.playlistTitle;
      if (State.source === "playlist") State.pool = State.playlistPool;
      statusEl.textContent = `${result.playlistTitle}: ${result.tracks.length}曲 取得しました`;
    } catch (err) {
      statusEl.textContent = `取得に失敗しました: ${err.message}`;
      statusEl.classList.add("is-error");
    }
    updateStartButtonState();
  }

  $("playlist-load-btn").addEventListener("click", loadPlaylistPool);
  playlistInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      loadPlaylistPool();
    }
  });

  // 間違ったURLを入力してしまった時に、入力欄と読み込み済みの内容をクリアできるようにする。
  const playlistClearBtn = $("playlist-clear-btn");
  playlistInput.addEventListener("input", () => {
    playlistClearBtn.classList.toggle("hidden", playlistInput.value.trim().length === 0);
  });
  playlistClearBtn.addEventListener("mousedown", (e) => {
    e.preventDefault();
    playlistInput.value = "";
    playlistClearBtn.classList.add("hidden");
    playlistInput.focus();
    State.playlistPool = null;
    State.lastPlaylistTitle = "";
    if (State.source === "playlist") State.pool = null;
    const statusEl = $("playlist-status");
    statusEl.textContent = "";
    statusEl.classList.remove("is-error");
    updateStartButtonState();
  });

  // ---- アーティスト検索(サジェスト付きライブ検索、複数指定可) ----
  const artistInput = $("artist-input");
  const artistClearBtn = $("artist-clear-btn");
  const artistSuggestionsEl = $("artist-suggestions");

  const SCOPE_OPTIONS = [
    { value: "top25", label: "Top25" },
    { value: "top50", label: "Top50" },
    { value: "all", label: "全曲" },
    { value: "mix", label: "ミックス" },
  ];
  const SCOPE_LABEL = { top25: "Top25", top50: "Top50", all: "全曲", mix: "公式ミックス" };

  let suggestAbortController = null;
  let suggestRequestId = 0;
  let suggestDebounceTimer = null;
  let activeSuggestionIndex = -1;

  function renderSuggestionStatus(text) {
    artistSuggestionsEl.innerHTML = "";
    activeSuggestionIndex = -1;
    const li = document.createElement("li");
    li.textContent = text;
    li.className = "suggestion-status";
    artistSuggestionsEl.appendChild(li);
    artistSuggestionsEl.classList.remove("hidden");
  }

  function hideSuggestions() {
    artistSuggestionsEl.innerHTML = "";
    activeSuggestionIndex = -1;
    artistSuggestionsEl.classList.add("hidden");
  }

  function renderArtistSuggestions(items) {
    artistSuggestionsEl.innerHTML = "";
    activeSuggestionIndex = -1;

    if (!items || items.length === 0) {
      renderSuggestionStatus("見つかりませんでした");
      return;
    }

    items.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      li.addEventListener("mousedown", (e) => {
        // mousedown(clickではなく)なので、inputのblurで隠れるより先に発火する
        e.preventDefault();
        confirmArtist(text);
      });
      artistSuggestionsEl.appendChild(li);
    });

    artistSuggestionsEl.classList.remove("hidden");
  }

  function highlightSuggestion(delta) {
    const items = Array.from(artistSuggestionsEl.children);
    // 「検索中…」「見つかりませんでした」プレースホルダ表示中は選択できない
    if (items.length === 0 || items[0].classList.contains("suggestion-status")) return;

    items[activeSuggestionIndex]?.classList.remove("active");
    activeSuggestionIndex = (activeSuggestionIndex + delta + items.length) % items.length;
    const active = items[activeSuggestionIndex];
    active.classList.add("active");
    artistInput.value = active.textContent;
    artistClearBtn.classList.remove("hidden");
  }

  async function fetchArtistSuggestions(query) {
    // 前のリクエストが残っていれば中止し、古い(短い)クエリの遅い応答が
    // 新しい(より具体的な)クエリの結果を上書きしないようにする。
    if (suggestAbortController) suggestAbortController.abort();
    const controller = new AbortController();
    suggestAbortController = controller;
    const requestId = ++suggestRequestId;

    try {
      const names = await Api.suggestArtist(query, controller.signal);
      if (requestId !== suggestRequestId) return;
      renderArtistSuggestions(names);
    } catch (err) {
      if (requestId !== suggestRequestId) return;
      if (err.name !== "AbortError") renderArtistSuggestions([]);
    }
  }

  artistInput.addEventListener("input", () => {
    const query = artistInput.value.trim();
    artistClearBtn.classList.toggle("hidden", query.length === 0);
    clearTimeout(suggestDebounceTimer);

    if (query.length < 1) {
      if (suggestAbortController) suggestAbortController.abort();
      hideSuggestions();
      return;
    }

    renderSuggestionStatus("検索中…");
    // バックエンド1回の応答に数百ms〜数秒かかるため、デバウンスが短すぎると
    // (以前は100ms)、特に日本語IME変換中の連続inputイベントで毎回前の
    // リクエストが中断され、いつまでも結果が表示されない状態になっていた。
    suggestDebounceTimer = setTimeout(() => fetchArtistSuggestions(query), 400);
  });

  artistClearBtn.addEventListener("mousedown", (e) => {
    e.preventDefault();
    artistInput.value = "";
    artistClearBtn.classList.add("hidden");
    hideSuggestions();
    artistInput.focus();
  });

  // 入力欄からフォーカスが外れただけでは候補を隠さない(文字が入っている限り
  // 「検索中」の状態を保つ)。実際に隠すのは、検索欄と無関係な場所をクリック
  // した時だけにする。
  document.addEventListener("click", (e) => {
    if (e.target.closest(".artist-input-wrap") || e.target.closest("#artist-suggestions")) return;
    // 検索バーに文字が残っている限り、フォーカスが外れていても・他の場所を
    // クリックしても「検索中…」/候補/結果なしの表示を消さない。
    if (artistInput.value.trim().length > 0) return;
    hideSuggestions();
  });

  artistInput.addEventListener("focus", () => {
    if (artistSuggestionsEl.children.length > 0) {
      artistSuggestionsEl.classList.remove("hidden");
    }
  });

  artistInput.addEventListener("keydown", (e) => {
    const suggestionsVisible = !artistSuggestionsEl.classList.contains("hidden");

    if (e.key === "ArrowDown" && suggestionsVisible) {
      e.preventDefault();
      highlightSuggestion(1);
    } else if (e.key === "ArrowUp" && suggestionsVisible) {
      e.preventDefault();
      highlightSuggestion(-1);
    } else if (e.key === "Escape") {
      artistSuggestionsEl.classList.add("hidden");
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (suggestionsVisible && activeSuggestionIndex >= 0) {
        const items = Array.from(artistSuggestionsEl.children);
        confirmArtist(items[activeSuggestionIndex].textContent);
      }
      // それ以外(サジェストを選ばずEnter)は何もしない: 一覧から選ばれた
      // アーティスト名だけを有効な指定として扱う。
    }
  });

  // アーティストを1件、取得範囲(既定Top25)付きで選択リストに追加する。
  // 複数回呼べば複数アーティストを指定できる(同名は追加しない)。
  function confirmArtist(name) {
    artistInput.value = "";
    artistClearBtn.classList.add("hidden");
    hideSuggestions();
    artistInput.focus();
    if (State.selectedArtists.some((a) => a.name === name)) return;
    State.selectedArtists.push({ name, scope: "mix" });
    renderArtistEntries();
    refreshArtistPool();
  }

  let openPreviewName = null; // クリックでプレビューを開いているアーティスト名
  let openSavedPreviewId = null; // クリックでプレビューを開いている保存済みデータセットのid
  const loadingArtistKeys = new Set(); // `${name}::${scope}` 現在取得中のもの

  // プレビューを開いている時、それ以外の場所をクリックしたら閉じる
  // (流行・アーティスト一覧・保存済み一覧の確認ボタンで共通)。
  document.addEventListener("click", (e) => {
    if (e.target.closest(".artist-entry-preview") || e.target.closest(".artist-entry-confirm")) return;
    if (openPreviewName) {
      openPreviewName = null;
      renderArtistEntries();
    }
    if (openSavedPreviewId) {
      openSavedPreviewId = null;
      renderSavedDatasetsList();
    }
  });

  function renderArtistEntries() {
    const wrap = $("artist-chips");
    wrap.innerHTML = "";
    State.selectedArtists.forEach((entry) => {
      const li = document.createElement("li");
      li.className = "artist-entry";

      // 取得中はこのアーティストの行だけにステータスを重ねて表示する
      // (他のアーティストの行やスタート画面の高さには影響させない)。
      const entryKey = `${entry.name}::${entry.scope}`;
      if (loadingArtistKeys.has(entryKey)) {
        const loadingEl = document.createElement("div");
        loadingEl.className = "artist-entry-loading";
        const loadingText = document.createElement("span");
        loadingText.className = "artist-entry-loading-text";
        loadingText.textContent = "曲を取得中...(曲数が多いと時間がかかります)";
        loadingEl.appendChild(loadingText);

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "artist-entry-loading-cancel";
        cancelBtn.textContent = "中断";
        cancelBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          cancelArtistFetch(entry.name, entry.scope);
        });
        loadingEl.appendChild(cancelBtn);

        li.appendChild(loadingEl);
      }
      // 中断済みでも特別な画面(再開/削除だけ)には固定せず、通常のアーティスト
      // 行の表示(名前・確認・スコープ選択・×)をそのまま出す。以前は中断後に
      // オーバーレイ(position:absolute; inset:0)がスコープボタンや×ボタンを
      // 覆ってしまい、再開ボタン以外で操作できず、実質そのアーティストで
      // 遊べなくなっていた不具合があった。再取得したい場合はスコープボタンを
      // 押せば(下のクリックハンドラでcancelledArtistKeysを解除して)取得し直せる。

      const nameEl = document.createElement("div");
      nameEl.className = "artist-entry-name";

      const nameText = document.createElement("span");
      nameText.className = "artist-entry-name-text";
      nameText.textContent = entry.name;
      nameEl.appendChild(nameText);

      // アーティストごとに「確認」ボタンを押すと、取得できた曲一覧をプレビュー表示する
      // (ホバーだとPC以外で使えないため、タップでも開閉できるボタン方式にする)。
      const cached = State.artistTrackCache[`${entry.name}::${entry.scope}`];
      let confirmBtn = null;
      let preview = null;
      if (cached && cached.tracks && cached.tracks.length > 0) {
        preview = document.createElement("div");
        // 位置の基準は行全体(li.artist-entry)にする。アーティスト名部分(nameEl)は
        // 横幅が狭いため、そこを基準にするとプレビューが画面外へはみ出していた。
        preview.className = "artist-entry-preview";
        if (openPreviewName === entry.name) preview.classList.add("is-open");
        const items = cached.tracks.map((t) => `<li>${escapeHtml(t.title)}</li>`).join("");
        preview.innerHTML = `<strong>${cached.tracks.length}曲取得済み</strong><ul>${items}</ul>`;

        confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "artist-entry-confirm";
        confirmBtn.textContent = "確認";
        confirmBtn.addEventListener("click", () => {
          openPreviewName = openPreviewName === entry.name ? null : entry.name;
          renderArtistEntries();
        });
      }

      const scopeRow = document.createElement("div");
      scopeRow.className = "artist-entry-scope";
      SCOPE_OPTIONS.forEach((opt) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "mini-choice" + (entry.scope === opt.value ? " is-active" : "");
        btn.textContent = opt.label;
        btn.addEventListener("click", () => {
          const wasCancelled = cancelledArtistKeys.has(`${entry.name}::${entry.scope}`);
          // 同じスコープを押した場合も、中断状態からの取得し直し(明示的な
          // 操作なので再開してよい)として扱う。スコープが変わらない限り
          // 何も起きないままだと、中断済みで詰んだ状態から抜け出せないため。
          if (entry.scope === opt.value && !wasCancelled) return;
          cancelledArtistKeys.delete(`${entry.name}::${entry.scope}`);
          entry.scope = opt.value;
          renderArtistEntries();
          refreshArtistPool();
        });
        scopeRow.appendChild(btn);
      });

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "artist-entry-remove";
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", () => {
        removeArtistEntry(entry);
      });

      li.appendChild(nameEl);
      if (confirmBtn) li.appendChild(confirmBtn);
      li.appendChild(scopeRow);
      li.appendChild(removeBtn);
      if (preview) li.appendChild(preview);
      wrap.appendChild(li);
    });
  }

  const artistFetchControllers = {}; // `${name}::${scope}` -> AbortController(中断ボタン用)
  const cancelledArtistKeys = new Set(); // `${name}::${scope}` 中断済み(明示的な操作があるまで自動では再取得しない)

  function removeArtistEntry(entry) {
    State.selectedArtists = State.selectedArtists.filter((a) => a.name !== entry.name);
    cancelledArtistKeys.delete(`${entry.name}::${entry.scope}`);
    if (openPreviewName === entry.name) openPreviewName = null;
    renderArtistEntries();
    refreshArtistPool();
  }

  function cancelArtistFetch(name, scope) {
    const key = `${name}::${scope}`;
    const controller = artistFetchControllers[key];
    if (controller) controller.abort();
    cancelledArtistKeys.add(key);
    renderArtistEntries();
  }

  // 中断済みのアーティストは、他のアーティストを新たに追加/変更した時にも
  // refreshArtistPool()がまとめて全アーティストを再取得しようとする巻き添えで
  // 勝手に取得が再開してしまっていた(明示的に再取得を選んでいないのに読み込みが
  // 始まる不具合)。このアーティストのスコープボタンを押す等、明示的な操作で
  // cancelledArtistKeysから削除されない限り、ここで取得をスキップする。
  async function fetchArtistPool(name, scope) {
    const key = `${name}::${scope}`;
    // 以前はここで既に取得済みなら再取得をスキップしていたが、修正の反映確認中に
    // 古い結果がいつまでも表示され続けて紛らわしい(「正しく反映されているか
    // 確認できない」)との指摘があったため、毎回必ず取得し直すようにした
    // (取得済みの一覧はState.artistTrackCacheに残し、「確認」プレビュー表示にのみ使う)。
    if (cancelledArtistKeys.has(key)) return null;
    loadingArtistKeys.add(key);
    const controller = new AbortController();
    artistFetchControllers[key] = controller;
    renderArtistEntries();
    try {
      const result = await Api.getArtistTracks(name, scope, controller.signal);
      State.artistTrackCache[key] = result;
      return result;
    } finally {
      loadingArtistKeys.delete(key);
      delete artistFetchControllers[key];
    }
  }

  // 選択中の全アーティストを、それぞれ指定された範囲で取得してマージする。
  // 取得中の表示は全体ではなく、fetchArtistPool側でアーティストの行ごとに出す。
  async function refreshArtistPool() {
    const statusEl = $("artist-status");
    if (State.selectedArtists.length === 0) {
      State.artistPool = null;
      if (State.source === "artist") State.pool = null;
      statusEl.textContent = "";
      statusEl.classList.remove("is-error");
      updateStartButtonState();
      return;
    }

    statusEl.textContent = "";
    statusEl.classList.remove("is-error");
    if (State.source === "artist") State.pool = null;
    updateStartButtonState();

    const entries = State.selectedArtists.slice();
    const results = await Promise.allSettled(
      entries.map((entry) => fetchArtistPool(entry.name, entry.scope))
    );

    const merged = [];
    const seenIds = new Set();
    const resolvedNames = [];
    let failCount = 0;
    results.forEach((r, i) => {
      if (r.status === "fulfilled" && r.value && r.value.tracks && r.value.tracks.length > 0) {
        resolvedNames.push(r.value.artistName);
        r.value.tracks.forEach((t) => {
          if (!seenIds.has(t.videoId)) {
            seenIds.add(t.videoId);
            merged.push(t);
          }
        });
      } else if (!cancelledArtistKeys.has(`${entries[i].name}::${entries[i].scope}`)) {
        // 中断済みでまだ取得し直していないアーティストは、失敗扱いにはしない
        // (スコープボタンを押す等、自分から取得を始めるまでは黙って除外するだけでよい)。
        failCount++;
      }
    });

    State.artistPool = merged.length > 0 ? merged : null;
    if (State.source === "artist") State.pool = State.artistPool;

    if (merged.length === 0) {
      statusEl.textContent = "曲が見つかりませんでした";
      statusEl.classList.add("is-error");
    } else {
      let msg = `${resolvedNames.join("、")}: 合計${merged.length}曲 取得しました`;
      if (failCount > 0) msg += `(${failCount}件取得できませんでした)`;
      statusEl.textContent = msg;
    }
    updateStartButtonState();
    // 取得できた曲一覧をアーティスト名のホバープレビューに反映する。
    renderArtistEntries();
  }

  document.querySelectorAll(".choice-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      const btn = e.target.closest(".choice");
      if (!btn) return;
      row.querySelectorAll(".choice").forEach((c) => c.classList.remove("is-active"));
      btn.classList.add("is-active");
      row.dataset.value = btn.dataset.value;
      if (row.id === "choice-questions") State.numQuestions = parseInt(btn.dataset.value, 10);
      if (row.id === "choice-seconds") State.seconds = parseFloat(btn.dataset.value);
      // 問題数を変えた時、「曲数が少ないため重複することがある」の警告が
      // 古い問題数のまま表示され続けていた(問題数変更時にstart button側の
      // 再評価が呼ばれていなかったため)。
      updateStartButtonState();
    });
  });

  function updateStartButtonState() {
    const errEl = $("setup-error");
    const startBtn = $("setup-start");
    const saveBtn = $("save-dataset-btn");
    const need = tracksPerQuestion();
    errEl.textContent = "";
    saveBtn.disabled = !State.pool || State.pool.length === 0;
    if (!State.pool || State.pool.length === 0) {
      startBtn.disabled = true;
      return;
    }
    if (State.pool.length < need) {
      errEl.textContent = `この設定には最低${need}曲必要です(現在${State.pool.length}曲)`;
      startBtn.disabled = true;
      return;
    }
    if (State.pool.length < need * State.numQuestions) {
      errEl.textContent = "曲数が少ないため、同じ曲が複数回出題される場合があります";
    }
    startBtn.disabled = false;
  }

  // ---- 曲リストの端末保存(localStorage) ----
  const DATASET_STORAGE_KEY = "introdon.datasets.v1";

  function loadDatasetList() {
    try {
      const raw = localStorage.getItem(DATASET_STORAGE_KEY);
      const list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function saveDatasetList(list) {
    try {
      localStorage.setItem(DATASET_STORAGE_KEY, JSON.stringify(list));
      return true;
    } catch (e) {
      return false;
    }
  }

  function suggestDatasetLabel() {
    if (State.source === "artist") {
      return State.selectedArtists.map((a) => `${a.name}(${SCOPE_LABEL[a.scope]})`).join("+");
    }
    if (State.source === "playlist" && State.lastPlaylistTitle) {
      return `${State.lastPlaylistTitle}(${State.pool.length}曲)`;
    }
    return `曲リスト(${State.pool.length}曲)`;
  }

  function saveCurrentPoolAsDataset() {
    if (!State.pool || State.pool.length === 0) return;
    const defaultLabel = suggestDatasetLabel();
    const input = window.prompt("この曲リストの名前を入力してください", defaultLabel);
    if (input === null) return; // キャンセル
    const label = input.trim() || defaultLabel;

    const list = loadDatasetList();
    list.unshift({
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      label,
      trackCount: State.pool.length,
      tracks: State.pool,
      savedAt: new Date().toISOString(),
    });
    if (!saveDatasetList(list)) {
      window.alert("保存に失敗しました。ブラウザのストレージ容量が不足している可能性があります。");
      return;
    }
    window.alert(`「${label}」として保存しました。`);
    if (State.source === "saved") renderSavedDatasetsList();
  }

  $("save-dataset-btn").addEventListener("click", saveCurrentPoolAsDataset);

  function renderSavedDatasetsList() {
    const list = loadDatasetList();
    const ul = $("saved-datasets");
    const statusEl = $("saved-status");
    ul.innerHTML = "";

    if (list.length === 0) {
      statusEl.textContent = "保存済みの曲リストはまだありません。";
      statusEl.classList.remove("is-error");
      return;
    }
    statusEl.textContent = "読み込む曲リストを選んでください。";
    statusEl.classList.remove("is-error");

    list.forEach((ds) => {
      const li = document.createElement("li");
      li.className = "saved-item";
      if (State.savedActiveId === ds.id) li.classList.add("is-selected");

      const main = document.createElement("div");
      main.className = "saved-item-main";
      const savedDate = new Date(ds.savedAt);
      const dateLabel = isNaN(savedDate.getTime()) ? "" : savedDate.toLocaleDateString("ja-JP");
      main.innerHTML = `<strong>${escapeHtml(ds.label)}</strong><small>${ds.trackCount}曲・${dateLabel}保存</small>`;
      main.addEventListener("click", () => selectSavedDataset(ds));

      // 保存済みの曲リストも、確認ボタンで中身の曲一覧をプレビューできるようにする。
      // 位置の基準は行全体(li.saved-item)にする(confirmWrapは横幅が狭く、
      // そこを基準にするとプレビューが画面外へはみ出すことがあるため)。
      const confirmWrap = document.createElement("div");
      confirmWrap.className = "saved-item-confirm-wrap";

      const preview = document.createElement("div");
      preview.className = "artist-entry-preview";
      if (openSavedPreviewId === ds.id) preview.classList.add("is-open");
      const items = ds.tracks.map((t) => `<li>${escapeHtml(t.title)}</li>`).join("");
      preview.innerHTML = `<strong>${ds.tracks.length}曲</strong><ul>${items}</ul>`;

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "artist-entry-confirm";
      confirmBtn.textContent = "確認";
      confirmBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openSavedPreviewId = openSavedPreviewId === ds.id ? null : ds.id;
        renderSavedDatasetsList();
      });
      confirmWrap.appendChild(confirmBtn);

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "saved-item-remove";
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSavedDataset(ds.id);
      });

      li.appendChild(main);
      li.appendChild(confirmWrap);
      li.appendChild(removeBtn);
      li.appendChild(preview);
      ul.appendChild(li);
    });
  }

  function selectSavedDataset(ds) {
    State.savedActiveId = ds.id;
    State.savedPool = ds.tracks;
    State.pool = ds.tracks;
    renderSavedDatasetsList();
    $("saved-status").textContent = `${ds.label}: ${ds.tracks.length}曲を読み込みました`;
    updateStartButtonState();
  }

  function deleteSavedDataset(id) {
    const list = loadDatasetList().filter((d) => d.id !== id);
    saveDatasetList(list);
    if (State.savedActiveId === id) {
      State.savedActiveId = null;
      State.savedPool = null;
      if (State.source === "saved") State.pool = null;
    }
    if (openSavedPreviewId === id) openSavedPreviewId = null;
    renderSavedDatasetsList();
    updateStartButtonState();
  }

  // 「読み込みが多すぎてしらける」を避けるため、プレーヤーの準備と最初の曲の
  // 先読みはゲーム画面に切り替える前(ホーム画面/リザルト画面にいる間)に
  // 済ませておく。1問目→2問目の切り替えでもラグが出ないよう、2問目分もここで
  // 先読みしておく(3問目以降は毎問、答え合わせ中に次を先読みする)。
  async function startGame(triggerBtn) {
    // クリックイベントハンドラの中で(awaitを挟む前に)呼ぶことで、ブラウザに
    // 「ユーザー操作に基づくミュート解除」だと認識させる(下記prepareの
    // 事前呼び出しにより、この時点で既にプレーヤーが存在しているはず)。
    YTPlayers.unmuteAll();
    const originalLabel = triggerBtn.textContent;
    triggerBtn.disabled = true;
    triggerBtn.textContent = "準備中...";
    activeSlot = 0;
    State.session = Quiz.createSession({
      mode: MODE,
      pool: State.pool,
      numQuestions: State.numQuestions,
      seconds: State.seconds,
    });
    loadCurrentQuestionPlan();
    try {
      await YTPlayers.prepare(2);
      await YTPlayers.precue(0, currentPlaybackPlan.videoIds[0], currentPlaybackPlan.startSecondsList[0]);
      if (State.session.questions.length > 1) {
        const secondTrack = State.session.questions[1].tracks[0];
        await YTPlayers.precue(1, secondTrack.videoId, 0);
      }
    } catch (e) { /* noop: 失敗しても通常のplayCurrentClip側の読み込みに任せる */ }
    triggerBtn.disabled = false;
    triggerBtn.textContent = originalLabel;
    showScreen("screen-game");
    renderQuestion();
  }

  $("setup-start").addEventListener("click", () => startGame($("setup-start")));

  // ---------------- ゲーム画面 ----------------

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function goHome() {
    stopCountdown();
    YTPlayers.destroyAll();
    State.session = null;
    currentPlaybackPlan = null;
    activeSlot = 0;
    showScreen("screen-home");
  }

  $("game-home").addEventListener("click", goHome);

  let currentPlaybackPlan = null; // {videoIds, startSecondsList, playSeconds}
  let playbackBusy = false;
  let selectedChoiceIndex = -1; // 十字キーでの選択位置
  let focusedActionBtn = null; // 十字キーでの選択位置(選択肢ではなく"replay"/"skip"にいる場合)
  let introSkipOffset = 0; // 「続きから再生」で加算される開始位置のオフセット(秒)
  // 2つのプレーヤーを交互に使う。片方で今の問題を再生している間、もう片方に
  // 次の問題の曲を裏側で読み込んでおく(precue)ことで、実際に切り替わった時の
  // 読み込み開始を早める。
  let activeSlot = 0;

  function loadCurrentQuestionPlan() {
    const session = State.session;
    const plan = session.getPlaybackPlan();
    currentPlaybackPlan = {
      videoIds: plan.map((p) => p.track.videoId),
      startSecondsList: plan.map((p) => {
        // 「続きから再生」で進めた分、開始位置を後ろにずらす。
        // ただし曲の終わり際まで行かないよう、再生できる長さの分は手前で止める。
        const duration = p.track.durationSeconds || 0;
        const maxStart = Math.max(0, Math.floor(duration - session.playSeconds - 1));
        return Math.min(p.startSeconds + introSkipOffset, maxStart);
      }),
      playSeconds: session.playSeconds,
    };
  }

  function renderQuestion() {
    const session = State.session;
    $("game-qnum").textContent = session.currentIndex + 1;
    $("game-qtotal").textContent = session.total;
    $("game-score").textContent = scoreLabel(session);

    $("answer-choices").innerHTML = "";
    $("answer-area").classList.remove("is-active");
    $("answer-next").classList.add("hidden");
    // 直前の問題の「もう一度再生」がまだ再生中でも、次の問題へは必ず進めるように
    // busyフラグを強制的に解除する(古い再生はYTPlayers側で自動的に打ち切られる)。
    playbackBusy = false;
    introSkipOffset = 0;

    loadCurrentQuestionPlan();
    // 最初の再生に失敗した場合、この回数まで別の曲に差し替えて再挑戦する。
    // 「再生できませんでした」の表示自体をなるべく出したくないため、プール内の
    // 候補が尽きるまで(replaceCurrentTrackがfalseを返すまで)粘れるよう、
    // 実用上十分に大きい回数にしておく。
    playCurrentClip({ isFirstPlay: true, retriesLeft: 20 });
  }

  // 現在の問題のクリップを(再生ボタン/少し先から再生ボタンからの再再生も含めて)再生する。
  function playCurrentClip({ isFirstPlay = false, retriesLeft = 0 } = {}) {
    if (playbackBusy || !currentPlaybackPlan) return;
    playbackBusy = true;
    const { videoIds, startSecondsList, playSeconds } = currentPlaybackPlan;
    const visual = $("player-visual");
    const replayBtn = $("replay-btn");
    const skipBtn = $("skip-ahead-btn");
    replayBtn.disabled = true;
    skipBtn.disabled = true;
    // 「次へ」は再生中でも常に押せるようにする(押した場合は再生を打ち切って
    // 次の問題へ進む。answer-nextのクリックハンドラ側で処理する)。

    // 実際に音が鳴り始めるまでは「再生中」等の状態を名乗らない(待っているだけなのに
    // 再生中と表示するのは実態と違うため)。「読み込み中」「待機中」のような文言は
    // 出さないが、何の反応もないと固まって見えて不安になるため、「•」を軽く
    // 脈動させるだけの控えめな動きで「進行中」であることを示す。
    visual.classList.add("is-idle");
    $("game-caption").textContent = "";
    const countdownEl = $("game-countdown");
    countdownEl.textContent = "•";
    countdownEl.classList.add("is-waiting");

    YTPlayers.playSegments(videoIds, startSecondsList, playSeconds, () => {
      visual.classList.remove("is-idle");
      countdownEl.classList.remove("is-waiting");
      $("game-caption").textContent = captionText(true);
      startCountdown(playSeconds);
    }, activeSlot).then((results) => {
      stopCountdown();
      countdownEl.classList.remove("is-waiting");
      visual.classList.add("is-idle");
      const anyError = results.some((r) => r.error);
      if (anyError) videoIds.forEach((id) => State.brokenVideoIds.add(id));

      // 最初の再生に失敗した曲は、エラー表示はせず別の曲へ静かに差し替えて
      // 出題し直す(ユーザーには再生できない曲があったことを見せない)。
      if (anyError && isFirstPlay) {
        playbackBusy = false;
        if (retriesLeft > 0 && State.session.replaceCurrentTrack([...State.brokenVideoIds])) {
          introSkipOffset = 0; // 別の曲に差し替わったので、開始位置のずらしもリセットする
          loadCurrentQuestionPlan();
          playCurrentClip({ isFirstPlay: true, retriesLeft: retriesLeft - 1 });
          return;
        }
        // 差し替え候補が尽きた場合のみ、やむを得ずこの曲のまま進める。
      }

      // 差し替えず(または差し替え候補が尽きて)そのまま進めることになった場合は、
      // 無言で「再生中」表示のまま終わらせず、再生できなかったことがわかるようにする。
      $("game-caption").textContent = anyError
        ? "再生できませんでした。もう一度再生をお試しください"
        : captionText(false);
      replayBtn.disabled = false;
      skipBtn.disabled = false;
      playbackBusy = false;
      if (isFirstPlay) {
        openAnswerArea();
        precueNextQuestion();
      }
    });
  }

  // 次の問題の曲を、今使っていない方のプレーヤーへ裏側で読み込んでおく
  // (再生のトリガーは常にplayCurrentClip側のloadVideoByIdのままで、ここでは
  // 事前のヒントとしてcueVideoById()するだけ)。失敗しても実害はないので
  // 念のためtry/catchで無視する。
  function precueNextQuestion() {
    const session = State.session;
    if (!session || session.isLastQuestion) return;
    const nextQuestion = session.questions[session.currentIndex + 1];
    if (!nextQuestion) return;
    const track = nextQuestion.tracks[0];
    if (!track) return;
    try {
      YTPlayers.precue(1 - activeSlot, track.videoId, 0);
    } catch (e) { /* noop */ }
  }

  function flashPressed(btn) {
    btn.classList.add("is-pressed");
    setTimeout(() => btn.classList.remove("is-pressed"), 200);
  }

  $("replay-btn").addEventListener("click", (e) => {
    flashPressed(e.currentTarget);
    YTPlayers.unmuteAll();
    playCurrentClip();
  });

  // イントロが無音だった場合に、直前に流した秒数分だけ開始位置を進めて再生し
  // 直せるようにする(実際の音量を検知しての自動スキップはIFrame埋め込みの仕組み上
  // できないため、手動でずらせるようにする代替策)。飛び飛びにならず曲の頭から
  // 順番に聞いていけるよう、ずらす量は「今まで流した秒数分」ちょうどにする。
  $("skip-ahead-btn").addEventListener("click", (e) => {
    flashPressed(e.currentTarget);
    YTPlayers.unmuteAll();
    introSkipOffset += currentPlaybackPlan.playSeconds;
    loadCurrentQuestionPlan();
    playCurrentClip();
  });

  function captionText(playing) {
    return playing ? "イントロ再生中…" : "この曲は何でしょう?";
  }

  // setIntervalで1回ごとに引き算する方式は、タブの負荷などでずれが積み重なり
  // 実際の再生終了(こちらもタイマー駆動)との体感差につながっていた。
  // 経過時間を都度Date.nowから計算し直す方式にして、ずれを抑える。
  // 100ms間隔でチェックすることで、0.5秒/1.5秒のような1秒未満の設定にも対応する。
  function startCountdown(totalSeconds) {
    const totalMs = Math.round(totalSeconds * 1000);
    const startedAt = Date.now();
    const el = $("game-countdown");

    const tick = () => {
      const remainingMs = totalMs - (Date.now() - startedAt);
      if (remainingMs <= 0) {
        el.textContent = "0";
        stopCountdown();
        return;
      }
      el.textContent = Math.ceil(remainingMs / 1000);
    };

    stopCountdown();
    tick();
    State.countdownTimer = setInterval(tick, 100);
  }
  function stopCountdown() {
    if (State.countdownTimer) {
      clearInterval(State.countdownTimer);
      State.countdownTimer = null;
    }
  }

  function openAnswerArea() {
    $("answer-area").classList.add("is-active");
    renderChoices();
  }

  function renderChoices() {
    const session = State.session;
    const wrap = $("answer-choices");
    wrap.innerHTML = "";
    selectedChoiceIndex = -1;
    focusedActionBtn = null;
    session.currentQuestion.choices.forEach((track) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "choice-btn";
      btn.dataset.videoId = track.videoId;
      btn.innerHTML = `<strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist)}</small>`;
      btn.addEventListener("click", () => selectAnswer(track, btn));
      wrap.appendChild(btn);
    });
    updateActionBtnHighlight();
  }

  function updateChoiceHighlight() {
    Array.from($("answer-choices").children).forEach((btn, i) => {
      btn.classList.toggle("keyboard-selected", i === selectedChoiceIndex);
    });
  }

  function updateActionBtnHighlight() {
    $("replay-btn").classList.toggle("keyboard-selected", focusedActionBtn === "replay");
    $("skip-ahead-btn").classList.toggle("keyboard-selected", focusedActionBtn === "skip");
    $("answer-next").classList.toggle("keyboard-selected", focusedActionBtn === "next");
  }

  // 選択肢の並び/もう一度再生・続きから再生/次への間を、ひとつながりの
  // 環状リストとして扱う: 最後の選択肢から下へ行くとボタン行(もう一度再生)へ、
  // ボタン行から下へ行くと先頭の選択肢へ戻る(上はその逆順)。解答済みで選択肢が
  // 選べない状態でも、もう一度再生/続きから再生へは移動できるようにする。
  // 解答済みで次へが表示されている場合は、もう一度再生/続きから再生から下で
  // 次へへ移動でき、次への上下はどちらももう一度再生に戻る。
  function moveChoiceSelection(delta) {
    const items = Array.from($("answer-choices").children);
    if (items.length === 0) return;
    const nextBtn = $("answer-next");
    const nextVisible = !nextBtn.classList.contains("hidden");

    if (focusedActionBtn === "next") {
      focusedActionBtn = "replay";
      updateActionBtnHighlight();
      return;
    }

    if (focusedActionBtn) {
      if (delta > 0 && nextVisible) {
        focusedActionBtn = "next";
        updateActionBtnHighlight();
        return;
      }
      focusedActionBtn = null;
      selectedChoiceIndex = delta > 0 ? 0 : items.length - 1;
      updateChoiceHighlight();
      updateActionBtnHighlight();
      return;
    }

    if (items[0].disabled) {
      selectedChoiceIndex = -1;
      focusedActionBtn = "replay";
      updateChoiceHighlight();
      updateActionBtnHighlight();
      return;
    }

    const next = selectedChoiceIndex + delta;
    if (next < 0 || next >= items.length) {
      selectedChoiceIndex = -1;
      focusedActionBtn = "replay";
      updateChoiceHighlight();
      updateActionBtnHighlight();
      return;
    }
    selectedChoiceIndex = next;
    updateChoiceHighlight();
  }

  // もう一度再生⇄続きから再生の間の左右移動。次へにいる時は対象外。
  function moveActionBtnSelection(delta) {
    if (focusedActionBtn !== "replay" && focusedActionBtn !== "skip") return;
    focusedActionBtn = delta > 0 ? "skip" : "replay";
    updateActionBtnHighlight();
  }

  // 選択肢をタップした時点でそのまま解答として確定し、即座に正誤判定する。
  // 画面遷移はせず、4つの選択肢ボタンの上でそのまま正解/不正解を示す。
  function selectAnswer(track, btn) {
    const session = State.session;
    const entry = session.submitAnswer([track.videoId]);
    $("game-score").textContent = scoreLabel(session);

    const correctId = entry.tracks[0].videoId;
    Array.from($("answer-choices").children).forEach((b) => {
      b.disabled = true;
      if (b.dataset.videoId === correctId) {
        b.classList.add("is-correct");
      } else if (b === btn) {
        b.classList.add("is-wrong");
      }
    });
    $("game-caption").textContent = track.videoId === correctId
      ? "正解!"
      : `残念…正解は「${entry.tracks[0].title}」`;

    const nextBtn = $("answer-next");
    nextBtn.textContent = session.isLastQuestion ? "結果を見る" : "次へ";
    nextBtn.classList.remove("hidden");
  }

  function scoreLabel(session) {
    const s = session.getResultSummary();
    return `${s.totalCorrect} / ${s.totalCount}`;
  }

  $("answer-next").addEventListener("click", () => {
    YTPlayers.unmuteAll();
    const session = State.session;
    // もう一度再生/続きから再生で曲が流れている最中でも、次へを押したら
    // その再生を打ち切ってすぐ次の問題に進む。
    if (playbackBusy) {
      YTPlayers.stop(activeSlot);
      stopCountdown();
      playbackBusy = false;
    }
    if (session.isLastQuestion) {
      showResult(session);
    } else {
      session.advance();
      activeSlot = 1 - activeSlot; // precueNextQuestion()で先読みしておいた側に切り替える
      renderQuestion();
    }
  });

  // 十字キーで選択肢/もう一度再生・続きから再生を移動、Enterで確定。
  document.addEventListener("keydown", (e) => {
    if (!$("screen-game").classList.contains("is-active")) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      moveChoiceSelection(1);
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      moveChoiceSelection(-1);
      return;
    }
    if (e.key === "ArrowRight") {
      if (!focusedActionBtn) return;
      e.preventDefault();
      moveActionBtnSelection(1);
      return;
    }
    if (e.key === "ArrowLeft") {
      if (!focusedActionBtn) return;
      e.preventDefault();
      moveActionBtnSelection(-1);
      return;
    }
    if (e.key !== "Enter") return;

    // Enterは今キーボードでフォーカスしているものを確定する。もう一度再生/
    // 続きから再生にいる場合はそちらを優先し、それ以外(選択肢を選んでいる/
    // 何も選んでいない)の場合だけ次へにフォールバックする。
    if (focusedActionBtn === "replay") {
      e.preventDefault();
      const btn = $("replay-btn");
      if (!btn.disabled) btn.click();
      return;
    }
    if (focusedActionBtn === "skip") {
      e.preventDefault();
      const btn = $("skip-ahead-btn");
      if (!btn.disabled) btn.click();
      return;
    }

    const items = Array.from($("answer-choices").children);
    const chosen = items[selectedChoiceIndex];
    if (chosen && !chosen.disabled) {
      e.preventDefault();
      chosen.click();
      return;
    }

    const nextBtn = $("answer-next");
    if (!nextBtn.classList.contains("hidden")) {
      e.preventDefault();
      nextBtn.click();
    }
  });

  // ---------------- リザルト画面 ----------------

  function showResult(session) {
    const summary = session.getResultSummary();
    $("result-score-num").textContent = `${summary.totalCorrect} / ${summary.totalCount}`;
    $("result-score-label").textContent = "問正解";

    const list = $("result-list");
    list.innerHTML = "";
    summary.log.forEach((entry, idx) => {
      const li = document.createElement("li");
      const isCorrect = entry.correctCount === entry.total;
      const badgeClass = isCorrect ? "is-correct" : "is-wrong";
      li.className = `result-item ${badgeClass}`;
      const badgeText = isCorrect ? "正解" : "不正解";
      const t = entry.tracks[0];

      li.innerHTML = `
        <div class="result-item-head"><span>第${idx + 1}問</span><span class="badge ${badgeClass}">${badgeText}</span></div>
        <div class="result-item-songs"><div class="result-song-row">${escapeHtml(t.title)} / ${escapeHtml(t.artist)}</div></div>
      `;
      list.appendChild(li);
    });

    YTPlayers.stopAll();
    showScreen("screen-result");
  }

  $("result-home").addEventListener("click", goHome);
  // 同じ設定(曲プール・問題数・秒数)のままもう一度遊べるようにする。
  $("result-retry").addEventListener("click", () => startGame($("result-retry")));

  // ページ読み込み時点でプレーヤー(iframe API読み込み含む)を先に用意しておく。
  // 「スタート!」クリックのタイミングでゼロから生成すると、iframe APIの
  // 読み込み待ちなどでawaitを挟むことになり、クリックという「ユーザー操作」との
  // 直接のつながりが失われてしまう。ブラウザの自動再生ポリシーは、ユーザー
  // 操作から直接つながっていない音声付き自動再生を許可しないことが多く、これが
  // 「再生中と表示されるのに実際には無音のまま」の不具合の原因だった。事前に
  // プレーヤーを用意しておけば、クリックハンドラ内でawait無しにunmuteAll()を
  // 呼べる(YTPlayers.unmuteAll呼び出し箇所を参照)。戻り値は待たなくてよい。
  YTPlayers.prepare(2);
})();
