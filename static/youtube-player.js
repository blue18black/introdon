// YouTube IFrame Player APIラッパー。プレーヤーは #yt-players 内に隠して生成する。
const YTPlayers = (() => {
  let apiReadyPromise = null;
  const pool = []; // YT.Player instances
  const activeCancel = []; // pool[i]に対応する、進行中のplayOne()を打ち切る関数(なければnull)
  let nextPlayerIndex = 0;

  function loadApi() {
    if (apiReadyPromise) return apiReadyPromise;
    apiReadyPromise = new Promise((resolve) => {
      if (window.YT && window.YT.Player) {
        resolve();
        return;
      }
      const prevCallback = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        if (typeof prevCallback === "function") prevCallback();
        resolve();
      };
      const tag = document.createElement("script");
      tag.src = "https://www.youtube.com/iframe_api";
      document.head.appendChild(tag);
    });
    return apiReadyPromise;
  }

  function createPlayer() {
    const container = document.getElementById("yt-players");
    const el = document.createElement("div");
    el.id = `yt-player-slot-${nextPlayerIndex++}`;
    container.appendChild(el);
    return new Promise((resolve) => {
      const player = new YT.Player(el.id, {
        width: "160",
        height: "90",
        playerVars: {
          autoplay: 0,
          controls: 0,
          disablekb: 1,
          modestbranding: 1,
          playsinline: 1,
          rel: 0,
          origin: window.location.origin,
        },
        events: {
          onReady: () => resolve(player),
        },
      });
    });
  }

  async function ensurePool(count) {
    await loadApi();
    while (pool.length < count) {
      pool.push(await createPlayer());
    }
    return pool.slice(0, count);
  }

  function playOne(playerIndex, player, videoId, startSeconds, playSeconds, onStart) {
    return new Promise((resolve) => {
      // 同じプレーヤーで前の再生(「もう一度再生」の残りなど)がまだ後片付けされて
      // いなければ、先にそちらのイベント購読/タイマーを止めておく。放置すると、
      // 古い再生のタイマーが後から発火してこの新しい再生を誤ってpauseVideo()し、
      // 「曲が途切れる」原因になる。
      if (activeCancel[playerIndex]) {
        activeCancel[playerIndex]();
      }

      let settled = false;
      let started = false;
      let retried = false;
      let timeoutId = null;

      // playSecondsの計測は「実際に音が鳴り始めた瞬間」を起点にする。loadVideoById()
      // 呼び出し直後ではバッファリング等で数百ms~数秒のずれが出て、画面のカウントダウン
      // と体感の再生タイミングが合わなくなるため。
      const begin = () => {
        if (started || settled) return;
        started = true;
        if (onStart) onStart();
        timeoutId = setTimeout(finish, playSeconds * 1000);
      };

      const onStateChange = (e) => {
        if (e.data === YT.PlayerState.PLAYING) begin();
      };
      const onError = () => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve({ error: true });
      };
      const finish = () => {
        if (settled) return;
        settled = true;
        cleanup();
        try { player.pauseVideo(); } catch (e) { /* noop */ }
        resolve({ error: false });
      };
      const cleanup = () => {
        if (timeoutId) clearTimeout(timeoutId);
        player.removeEventListener("onStateChange", onStateChange);
        player.removeEventListener("onError", onError);
        if (activeCancel[playerIndex] === cancel) activeCancel[playerIndex] = null;
      };
      // 外部(次の再生)から打ち切られた場合: 誰も結果を待っていないのでresolve()は
      // 呼ばず、イベント購読とタイマーの後片付けだけ行う。
      const cancel = () => {
        settled = true;
        if (timeoutId) clearTimeout(timeoutId);
        player.removeEventListener("onStateChange", onStateChange);
        player.removeEventListener("onError", onError);
      };
      activeCancel[playerIndex] = cancel;

      const load = () => {
        try {
          player.loadVideoById({ videoId, startSeconds });
        } catch (e) {
          settled = true;
          cleanup();
          resolve({ error: true });
        }
      };

      player.addEventListener("onStateChange", onStateChange);
      player.addEventListener("onError", onError);
      load();

      // 生成直後のプレーヤーは(特にセッション最初の数曲)YouTube側の初期化待ちで
      // PLAYINGイベントが来ないまま無音で止まっていることがある。少し待っても
      // 反応がなければ読み込み直しを試みる。ただしこの時点で実際にはもう
      // BUFFERING/PLAYINGまで進んでいる(単にイベント通知が遅れているだけの)
      // ケースまで読み込み直すと、鳴り始めた曲を自ら中断させてしまう
      // (「曲が途中で切れる」原因になる)。getPlayerState()で本当に止まって
      // いそうな場合だけ読み込み直す。
      setTimeout(() => {
        if (settled || started || retried) return;
        let state = null;
        try { state = player.getPlayerState(); } catch (e) { /* noop */ }
        if (state === YT.PlayerState.BUFFERING || state === YT.PlayerState.PLAYING) return;
        retried = true;
        load();
      }, 3000);

      // それでも反応がなければ、再生開始とみなして先へ進む(保険)。
      setTimeout(() => {
        if (!settled && !started) begin();
      }, 7000);
    });
  }

  return {
    // プレーヤー生成(YouTube iframe APIの読み込み含む)を先行して始めておく。
    // 「スタート!」クリック直後など、実際の再生より前に呼んでおくことで、
    // 最初の1曲の再生開始が遅れる/反応しない事象を減らす。戻り値は待たなくてよい。
    prepare(count) {
      return ensurePool(count);
    },

    // videoIds: string[], startSecondsList: number[] (同じ長さ), playSeconds: number
    // onStart: 最初の1曲が実際に再生開始した瞬間に一度だけ呼ばれる(カウントダウン表示の起点用)
    // startPlayerIndex: 使うプレーヤーの開始番号(次の問題の先読み用に2台を交互に使うため)
    // 戻り値: [{error:boolean}, ...] videoIdsと同じ順
    async playSegments(videoIds, startSecondsList, playSeconds, onStart, startPlayerIndex = 0) {
      const players = await ensurePool(startPlayerIndex + videoIds.length);
      let started = false;
      const handleStart = () => {
        if (started) return;
        started = true;
        if (onStart) onStart();
      };
      const tasks = videoIds.map((vid, i) => {
        const idx = startPlayerIndex + i;
        return playOne(idx, players[idx], vid, startSecondsList[i], playSeconds, handleStart);
      });
      return Promise.all(tasks);
    },

    // 指定したプレーヤー番号に、再生はせず動画だけ裏側で読み込んでおく
    // (次の問題の曲を先読みし、実際に切り替わった時の再生開始を早める用途)。
    async precue(playerIndex, videoId, startSeconds) {
      const players = await ensurePool(playerIndex + 1);
      try {
        players[playerIndex].cueVideoById({ videoId, startSeconds });
      } catch (e) { /* noop */ }
    },

    stopAll() {
      activeCancel.forEach((cancel) => { if (cancel) cancel(); });
      activeCancel.length = 0;
      pool.forEach((p) => {
        try { p.stopVideo(); } catch (e) { /* noop */ }
      });
    },

    destroyAll() {
      activeCancel.forEach((cancel) => { if (cancel) cancel(); });
      activeCancel.length = 0;
      pool.forEach((p) => {
        try { p.destroy(); } catch (e) { /* noop */ }
      });
      pool.length = 0;
      document.getElementById("yt-players").innerHTML = "";
    },
  };
})();
