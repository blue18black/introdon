// signalがabortされたら即座に打ち切れるsetTimeout。ポーリング間隔待ちに使う。
function _sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (!signal) return;
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        const err = new Error("Aborted");
        err.name = "AbortError";
        reject(err);
      },
      { once: true }
    );
  });
}

// バックエンドAPI呼び出し
const Api = {
  async _get(url, signal) {
    let res;
    try {
      res = await fetch(url, { signal });
    } catch (networkErr) {
      if (networkErr.name === "AbortError") throw networkErr;
      throw new Error("サーバーに接続できませんでした。python app.py が起動しているか確認してください。");
    }
    let data;
    try {
      data = await res.json();
    } catch (parseErr) {
      throw new Error(`サーバーの応答が不正です (${res.status})`);
    }
    if (!res.ok) {
      throw new Error(data.error || `リクエストに失敗しました (${res.status})`);
    }
    return data;
  },

  // サジェストは検索中止(AbortController)や「見つかりませんでした」を呼び出し側で
  // 扱いたいので、共通の_get()は使わずエラー時は空配列を返す。
  async suggestArtist(query, signal) {
    let res;
    try {
      res = await fetch(`/api/artist_suggest?q=${encodeURIComponent(query)}`, { signal });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      return [];
    }
    if (!res.ok) return [];
    try {
      return await res.json();
    } catch (err) {
      return [];
    }
  },

  // 曲取得(全曲だとiTunesとの突き合わせ込みで数十秒かかることがあるため、
  // バックグラウンドジョブとして開始し、完了するまで進捗をポーリングする。
  // onProgress({stage, current, total})は、進捗が更新されるたびに呼ばれる
  // (省略可)。
  async getArtistTracks(name, scope = "all", signal, onProgress) {
    const POLL_INTERVAL_MS = 500;
    const startParams = new URLSearchParams({ name, scope });
    const { jobId } = await this._get(`/api/artist_tracks/start?${startParams.toString()}`, signal);

    try {
      for (;;) {
        await _sleep(POLL_INTERVAL_MS, signal);
        const progress = await this._get(`/api/artist_tracks/progress?jobId=${encodeURIComponent(jobId)}`, signal);
        if (progress.status === "done") return progress.result;
        if (progress.status === "error") throw new Error(progress.error || "曲の取得に失敗しました。");
        if (onProgress) onProgress({ stage: progress.stage, current: progress.current, total: progress.total });
      }
    } catch (err) {
      if (err.name === "AbortError") {
        // 中断時はサーバー側にも通知し、バックグラウンドで続いている
        // (数十秒かかりうる)無駄なYouTube Music/iTunes検索を早めに
        // 切り上げてもらう。signal自体は既にabort済みで使えないため、
        // この通知リクエストには渡さない。結果を待つ必要も無いので
        // 投げっぱなしでよい。
        fetch(`/api/artist_tracks/cancel?jobId=${encodeURIComponent(jobId)}`, { method: "POST" }).catch(() => {});
      }
      throw err;
    }
  },

  // 再生直前に呼ぶ: Deezerのプレビュー音源URLは発行から短時間で失効する
  // 署名付きURLのため、曲一覧取得時点のものではなく常にこれで直前に
  // 取り直したURLを使う。
  async getTrackAudio(trackId, signal) {
    const data = await this._get(`/api/track_audio?id=${encodeURIComponent(trackId)}`, signal);
    return data;
  },
};
