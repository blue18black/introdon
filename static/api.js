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

  async getArtistTracks(name, scope = "all", signal) {
    const params = new URLSearchParams({ name, scope });
    const data = await this._get(`/api/artist_tracks?${params.toString()}`, signal);
    return data;
  },

  async getPlaylistTracks(url) {
    const data = await this._get(`/api/playlist_tracks?url=${encodeURIComponent(url)}`);
    return data;
  },
};
