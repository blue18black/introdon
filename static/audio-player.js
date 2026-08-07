// HTML5 <audio>によるプレビュー音源の再生ラッパー。以前のyoutube-player.jsを
// 置き換える。YouTube iframeは(1)MV/ライブ映像が紛れ込む、(2)iOS Safariの
// 自動再生ポリシーやiframe内動画特有の癖により「再生中と表示されるのに
// 実際には無音」という不具合の温床になっていた、という2つの問題があった。
// Deezerのプレビュー音源(30秒程度のmp3)をネイティブの<audio>要素で再生する
// ことで、動画が一切関与しない(MV/ライブ映像混入が構造的に起きない)上、
// iOS Safariでも「ユーザー操作からアンロックした<audio>要素を使い回す」という
// 標準的な手法だけで安定して自動再生できる(YouTube iframeで必要だった
// ミュート状態での自動再生→事後ミュート解除、という回避策が不要になった)。
const AudioPlayers = (() => {
  const pool = []; // HTMLAudioElement[]
  const activeCancel = []; // pool[i]に対応する、進行中のplayOne()を打ち切る関数(なければnull)

  function container() {
    return document.getElementById("audio-players");
  }

  function createPlayer() {
    const audio = document.createElement("audio");
    audio.preload = "auto";
    // iOSでロック画面/コントロールセンターに再生中の曲として現れてしまうのを防ぐ
    // (30秒のプレビュー音源が「曲」として表示されるのは体験として不自然なため)。
    audio.setAttribute("x-webkit-airplay", "deny");
    container().appendChild(audio);
    return audio;
  }

  function ensurePool(count) {
    while (pool.length < count) {
      pool.push(createPlayer());
      activeCancel.push(null);
    }
    return pool.slice(0, count);
  }

  // iOS/Safariは、<audio>要素の最初のplay()がユーザー操作(クリック等)から
  // 直接つながっている場合に限り自動再生を許可する。一度許可された要素は、
  // その後srcを差し替えてplay()を呼んでも(非同期処理を挟んでも)そのまま
  // 再生できる。この「アンロック」を、ユーザーのクリックイベントハンドラの
  // 中で(awaitを挟まず)同期的に全プレーヤーに対して行っておく。
  function unlockOne(audio) {
    try {
      const p = audio.play();
      if (p && typeof p.catch === "function") p.catch(() => { /* noop */ });
      audio.pause();
      audio.currentTime = 0;
    } catch (e) { /* noop */ }
  }

  async function resolveAudioUrl(trackId) {
    return Api.getTrackAudio(trackId);
  }

  const START_TIMEOUT_MS = 7000;

  function playOne(playerIndex, audio, trackId, startSeconds, playSeconds, onStart, onTick) {
    return new Promise((resolve) => {
      if (activeCancel[playerIndex]) {
        activeCancel[playerIndex]();
      }

      let settled = false;
      let started = false;
      let cancelled = false;
      let startTimeoutId = null;
      let segmentTimeoutId = null;
      let tickIntervalId = null;
      let remainingMs = playSeconds * 1000;
      let segmentStartedAt = null;

      const clearStartTimeout = () => {
        if (startTimeoutId) {
          clearTimeout(startTimeoutId);
          startTimeoutId = null;
        }
      };
      const pauseSegmentTimer = () => {
        if (segmentTimeoutId) {
          clearTimeout(segmentTimeoutId);
          segmentTimeoutId = null;
        }
        if (tickIntervalId) {
          clearInterval(tickIntervalId);
          tickIntervalId = null;
        }
        if (segmentStartedAt !== null) {
          remainingMs -= Date.now() - segmentStartedAt;
          segmentStartedAt = null;
        }
      };
      const resumeSegmentTimer = () => {
        if (settled || segmentTimeoutId) return;
        segmentStartedAt = Date.now();
        segmentTimeoutId = setTimeout(finish, Math.max(0, remainingMs));
        // カウントダウンUI(呼び出し側)が、この音声側の残り時間(バッファ
        // リング等での一時停止を差し引いた実際の値)と必ず一致するように、
        // 独自に壁時計で数えるのではなくここから直接残り時間を伝える。
        // これが無いと、バッファリングで音声側の残り時間が延びても
        // カウントダウン表示だけ先に0になり、曲がまだ鳴っているのに
        // 「0」と表示される、というズレが起きる(YouTube Musicのフル尺
        // ストリームはDeezer/iTunesの短いプレビューよりバッファリングが
        // 起きやすく、このズレが顕在化しやすい)。
        if (onTick) {
          const segStartedAt = segmentStartedAt;
          const msAtResume = remainingMs;
          onTick(msAtResume);
          tickIntervalId = setInterval(() => {
            onTick(Math.max(0, msAtResume - (Date.now() - segStartedAt)));
          }, 100);
        }
      };

      const cleanup = () => {
        clearStartTimeout();
        pauseSegmentTimer();
        audio.removeEventListener("playing", onPlaying);
        audio.removeEventListener("canplaythrough", tryDeclareStart);
        audio.removeEventListener("canplay", tryDeclareStart);
        audio.removeEventListener("progress", tryDeclareStart);
        audio.removeEventListener("waiting", onWaiting);
        audio.removeEventListener("stalled", onWaiting);
        audio.removeEventListener("pause", onWaiting);
        audio.removeEventListener("ended", onEnded);
        audio.removeEventListener("error", onError);
        audio.removeEventListener("seeked", onSeeked);
        if (activeCancel[playerIndex] === cancel) activeCancel[playerIndex] = null;
      };

      const finish = () => {
        if (settled) return;
        settled = true;
        cleanup();
        try { audio.pause(); } catch (e) { /* noop */ }
        if (onTick) onTick(0);
        resolve({ error: false });
      };

      const onError = () => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve({ error: true });
      };

      const onEnded = () => {
        // プレビュー音源が短く、要求秒数より先に終端に達した場合。ここまで
        // 鳴っていた実績はあるので失敗扱いにはしない。
        finish();
      };

      // "playing"(あるいは"playing"+timeupdate)だけでは、実際に途切れず
      // 聞こえ始めるより前に発火することがある(出力パイプラインの立ち上がり
      // 待ち・シーク直後の未バッファ区間の再取得待ち等、特にYouTube由来の
      // フルストリームで顕著)。これにより「イントロ再生中…」表示や
      // カウントダウン開始が実際の音より早まるだけでなく、2秒モードのような
      // 短い設定では「実際に聞こえる時間」がタイマーの一部を先取りされて
      // 縮んでしまう実害があった。HAVE_FUTURE_DATA以上(=次に途切れず再生
      // できるだけのデータが既に手元にある状態)を、"playing"に加えて
      // "canplaythrough"/"canplay"/"progress"(データが届くたびに発火)でも
      // 都度確認し、実際に途切れず再生できる状態になって初めて「本当に
      // 鳴り始めた」とみなす(2回目以降のplaying、つまりバッファリング
      // 復帰時はこの確認をせず即座に再開する)。HAVE_FUTURE_DATA(3、次の
      // 1フレーム分だけ保証)ではなく、より安全マージンの大きいHAVE_ENOUGH_
      // DATA(4、この調子で最後まで途切れず再生できるとブラウザが判断した
      // 状態)を要求する。
      const HAVE_ENOUGH_DATA = 4;
      let isPlaying = false;

      const tryDeclareStart = () => {
        if (settled || started || !isPlaying) return;
        if (audio.readyState < HAVE_ENOUGH_DATA) return;
        started = true;
        clearStartTimeout();
        if (onStart) onStart();
        resumeSegmentTimer();
      };

      const onPlaying = () => {
        if (settled) return;
        isPlaying = true;
        if (!started) {
          tryDeclareStart();
          return;
        }
        resumeSegmentTimer();
      };

      const onWaiting = () => {
        // バッファリング等で音が止まっている間は持ち時間から差し引かない。
        isPlaying = false;
        if (started) pauseSegmentTimer();
      };

      const cancel = () => {
        cancelled = true;
        settled = true;
        cleanup();
      };
      activeCancel[playerIndex] = cancel;

      audio.addEventListener("playing", onPlaying);
      audio.addEventListener("canplaythrough", tryDeclareStart);
      audio.addEventListener("canplay", tryDeclareStart);
      audio.addEventListener("progress", tryDeclareStart);
      audio.addEventListener("waiting", onWaiting);
      audio.addEventListener("stalled", onWaiting);
      audio.addEventListener("pause", onWaiting);
      audio.addEventListener("ended", onEnded);
      audio.addEventListener("error", onError);

      startTimeoutId = setTimeout(() => {
        if (!settled && !started) onError();
      }, START_TIMEOUT_MS);

      const startPlayback = () => {
        const playPromise = audio.play();
        if (playPromise && typeof playPromise.catch === "function") {
          playPromise.catch(() => { if (!cancelled) onError(); });
        }
      };

      // 曲の途中(target>0)へのシークは、まだブラウザに何もバッファされて
      // いない1回目の再生時、その位置のデータを新たに取得し終えるまで
      // 完了しない。ここでplay()を即座に呼んでしまうと、シークが完了
      // しないまま(=まだ古い位置のままの状態で)readyState等が一見
      // 「再生できる」ように見えてしまい、"本当に鳴り始めた"判定が実際
      // より早まる・カウントダウンの持ち時間の一部を「まだシーク中の
      // 無音/ズレた区間」に消費されてしまう不具合があった(実例:1回目の
      // 再生だけ2秒のうち1秒程度しか聞こえない。同じ位置を再度シークする
      // 「もう一度再生」ではブラウザ側に既にバッファが残っており問題が
      // 再現しなかった)。"seeked"(シーク完了)を待ってから再生を開始する
      // ことで、1回目から正しい位置のデータで始まるようにする。
      const onSeeked = () => {
        audio.removeEventListener("seeked", onSeeked);
        if (cancelled || settled) return;
        startPlayback();
      };

      resolveAudioUrl(trackId)
        .then((info) => {
          if (cancelled || settled) return;
          audio.src = info.previewUrl;
          audio.currentTime = 0;
          const onLoadedMeta = () => {
            audio.removeEventListener("loadedmetadata", onLoadedMeta);
            if (cancelled || settled) return;
            const maxStart = Math.max(0, (audio.duration || 0) - 0.2);
            const target = Math.min(Math.max(0, startSeconds), maxStart);
            if (target > 0) {
              audio.addEventListener("seeked", onSeeked);
              audio.currentTime = target;
            } else {
              startPlayback();
            }
          };
          audio.addEventListener("loadedmetadata", onLoadedMeta);
          audio.load();
        })
        .catch(() => {
          if (!cancelled) onError();
        });
    });
  }

  return {
    // プレーヤー(<audio>要素)を先行して用意しておく。同期的に呼べるため、
    // 戻り値を待たなくても直後にunlockAll()を呼んでよい。
    prepare(count) {
      ensurePool(count);
      return Promise.resolve(pool.slice(0, count));
    },

    // 現在プール内の全プレーヤーをユーザー操作直後にアンロックする
    // (unlockOne参照)。「スタート!」等のクリックハンドラの先頭、await を
    // 挟む前に同期的に呼び出すこと。
    unmuteAll() {
      pool.forEach(unlockOne);
    },

    // trackIds: string[]・startSecondsList: number[](同じ長さ)、playSeconds: number
    // onStart: 最初の1曲が実際に再生開始した瞬間に一度だけ呼ばれる
    // onTick(remainingMs): 先頭の曲(トラック0)の実際の残り再生時間が
    // 変化するたびに呼ばれる(バッファリング等での一時停止を差し引いた
    // 実測値。カウントダウンUIを独自の壁時計ではなくこの値で駆動すること
    // で、表示と実際の再生終了のズレを防ぐ)。省略可。
    // startPlayerIndex: 使うプレーヤーの開始番号(次の問題の先読み用に2台を交互に使うため)
    async playSegments(trackIds, startSecondsList, playSeconds, onStart, startPlayerIndex = 0, onTick) {
      const players = ensurePool(startPlayerIndex + trackIds.length);
      let started = false;
      const handleStart = () => {
        if (started) return;
        started = true;
        if (onStart) onStart();
      };
      const tasks = trackIds.map((id, i) => {
        const idx = startPlayerIndex + i;
        return playOne(idx, players[idx], id, startSecondsList[i], playSeconds, handleStart, i === 0 ? onTick : undefined);
      });
      return Promise.all(tasks);
    },

    // 指定したプレーヤー番号に、次の問題の音源URLを先読みしておく(実際の
    // 再生トリガーはplaySegments側が常に自前でURLを取り直すため、ここでの
    // 読み込みはネットワーク/デコードの温め目的のヒントに過ぎない)。
    async precue(playerIndex, trackId, startSeconds) {
      const players = ensurePool(playerIndex + 1);
      const audio = players[playerIndex];
      try {
        const info = await resolveAudioUrl(trackId);
        audio.src = info.previewUrl;
        audio.currentTime = 0;
        audio.load();
      } catch (e) { /* noop: 先読みの失敗は実害がない */ }
    },

    // 指定した1台だけを打ち切る。
    stop(playerIndex) {
      if (activeCancel[playerIndex]) {
        activeCancel[playerIndex]();
        activeCancel[playerIndex] = null;
      }
      const audio = pool[playerIndex];
      if (audio) {
        try { audio.pause(); } catch (e) { /* noop */ }
      }
    },

    stopAll() {
      activeCancel.forEach((cancel) => { if (cancel) cancel(); });
      activeCancel.length = 0;
      pool.forEach((a) => {
        try { a.pause(); } catch (e) { /* noop */ }
      });
    },

    destroyAll() {
      activeCancel.forEach((cancel) => { if (cancel) cancel(); });
      activeCancel.length = 0;
      pool.forEach((a) => {
        try {
          a.pause();
          a.removeAttribute("src");
          a.load();
          a.remove();
        } catch (e) { /* noop */ }
      });
      pool.length = 0;
    },
  };
})();
