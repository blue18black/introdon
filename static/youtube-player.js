// YouTube IFrame Player APIラッパー。プレーヤーは #yt-players 内に隠して生成する。
const YTPlayers = (() => {
  let apiReadyPromise = null;
  const pool = []; // YT.Player instances
  const activeCancel = []; // pool[i]に対応する、進行中のplayOne()を打ち切る関数(なければnull)
  let nextPlayerIndex = 0;
  const warmedUpPlayers = new Set(); // 一度でも実際に再生を試みたplayerIndex

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
          // ブラウザの自動再生ポリシー対策(下記unmuteAll参照)で、まずは
          // ミュート状態で確実に再生できるようにしておく。
          mute: 1,
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

  // 動画の途中(startSeconds > 0)へシークした直後は、YouTube側の内部処理で
  // 実際に音が鳴り始めるまで数百ms〜1秒ほどの遅延/こま切れが起きることがある。
  // これまではPLAYING状態への遷移とgetCurrentTime()の進行だけを見て「開始」と
  // みなしていたため、この遅延の間もカウントダウン(=出題時間)が進んでしまい、
  // 特に2秒モードのような短い設定では音が鳴らないまま持ち時間の半分近くを
  // 失うことがあった(致命的な不具合)。本来の開始位置より少し手前から静かに
  // 読み込みを始め、実際にgetCurrentTime()が本来の開始位置に追いついてから
  // 初めて「開始」とみなすことで、この遅延を持ち時間の外に追い出す。
  const PRE_ROLL_SECONDS = 1.2;

  function playOne(playerIndex, player, videoId, startSeconds, playSeconds, onStart) {
    return new Promise((resolve) => {
      // 同じプレーヤーで前の再生(「もう一度再生」の残りなど)がまだ後片付けされて
      // いなければ、先にそちらのイベント購読/タイマーを止めておく。放置すると、
      // 古い再生のタイマーが後から発火してこの新しい再生を誤ってpauseVideo()し、
      // 「曲が途切れる」原因になる。
      if (activeCancel[playerIndex]) {
        activeCancel[playerIndex]();
      }

      // 「無音のまま終わり、もう一度再生すると鳴る」という報告が繰り返しあり、
      // 絶対に起きてはならないとの明確な指摘があった。startGame()側での
      // 一括ミュート解除(下記unmuteAll)だけでは、その後に同じプレーヤーで
      // loadVideoById()/cueVideoById()するたびにミュート状態が再び効いて
      // しまうケースを防ぎきれなかった可能性があるため、念のため過剰なくらい
      // 冗長に、この再生の対象プレーヤーを毎回ここでも明示的にミュート解除する。
      try {
        player.unMute();
        player.setVolume(100);
      } catch (e) { /* noop */ }

      let settled = false;
      let started = false;
      let retried = false;
      let timeoutId = null;
      let confirmTimer = null;
      let confirmChecks = 0;
      let confirmLastTime = null;
      // playSecondsの計測は「実際にPLAYING状態だった時間の合計」で行う。
      // 再生開始後に回線都合等で一瞬BUFFERINGへ戻ることがあり、以前は最初の
      // PLAYINGから壁時計時間で一度きり計測していたため、その無音の間も
      // カウントが進んでしまい、指定した秒数より短い音しか流せていなかった。
      // BUFFERING等に入ったら残り時間を確定させてタイマーを止め、PLAYINGに
      // 戻ったら残り時間からタイマーを再開する。
      let remainingMs = playSeconds * 1000;
      let segmentStartedAt = null;
      const preRoll = startSeconds > 0 ? Math.min(PRE_ROLL_SECONDS, startSeconds) : 0;
      const loadStartSeconds = Math.max(0, startSeconds - preRoll);

      const pauseTimer = () => {
        if (timeoutId) {
          clearTimeout(timeoutId);
          timeoutId = null;
        }
        if (segmentStartedAt !== null) {
          remainingMs -= Date.now() - segmentStartedAt;
          segmentStartedAt = null;
        }
      };

      const resumeTimer = () => {
        if (settled || timeoutId) return;
        segmentStartedAt = Date.now();
        timeoutId = setTimeout(finish, Math.max(0, remainingMs));
      };

      const begin = () => {
        if (settled) return;
        if (!started) {
          started = true;
          if (onStart) onStart();
        }
        resumeTimer();
      };

      const clearConfirm = () => {
        if (confirmTimer) {
          clearInterval(confirmTimer);
          confirmTimer = null;
        }
      };

      // PLAYING状態は、実際に音が聞こえ始めるより一瞬早く報告されることがある
      // (カウントダウンは始まっているのにまだ無音、という体感のズレの原因)。
      // 最初の開始判定だけは、getCurrentTime()の進みを確認してからbegin()する。
      // preRollを入れている場合(本来の開始位置より手前から読み込んでいる場合)は、
      // 単に「進んでいる」だけでは不十分(それはまだpreRoll区間を再生中なだけ
      // かもしれない)なので、実際に本来の開始位置まで追いついたことを確認する。
      // startSeconds=0(イントロモード)はシークが無いのでpreRollを付けようが
      // なく、以前は上限600ms(6回)で見切りをつけていたが、これだと動画を
      // 初めて読み込む「コールド」な状態でのバッファリング遅延がこの時間を
      // 超えた場合、まだ音が鳴っていないのにカウントダウンが始まってしまう
      // (2秒モードで持ち時間の大半を無音のまま失う致命的な不具合の主因)。
      // 待たされる分には構わないので、上限を余裕を持って引き上げる。
      const maxConfirmChecks = 20 + Math.ceil((preRoll * 1000) / 100);
      // getCurrentTime()が進み始めた/本来の開始位置に追いついた直後でも、実際に
      // 音が耳に聞こえ始めるまでYouTube側の音声パイプラインがさらに一瞬(数百ms
      // 程度)遅れることがある(進み始めた通知の方が実際の発声より早いことが
      // ある)。この余白も持ち時間の外に追い出すため、確認が取れてから即座に
      // begin()するのではなく、少しだけ間を置く。
      const SETTLE_DELAY_MS = 300;
      let settleTimer = null;
      const clearSettle = () => {
        if (settleTimer) {
          clearTimeout(settleTimer);
          settleTimer = null;
        }
      };
      const confirmReallyPlaying = () => {
        if (settled || started || confirmTimer || settleTimer) return;
        confirmChecks = 0;
        confirmLastTime = null;
        confirmTimer = setInterval(() => {
          confirmChecks++;
          let t = null;
          try { t = player.getCurrentTime(); } catch (e) { /* noop */ }
          const reachedTarget = preRoll > 0 && t != null && t >= startSeconds - 0.1;
          const advancing = preRoll === 0 && t != null && confirmLastTime != null && t > confirmLastTime + 0.05;
          if (reachedTarget || advancing) {
            clearConfirm();
            // getCurrentTime()の進行はミュート状態でも起こりうる(=無音のまま
            // 「再生成功」判定されてしまう)ため、成功と扱う直前に最後の保険として
            // 必ずミュート解除しておく。
            try {
              player.unMute();
              player.setVolume(100);
            } catch (e) { /* noop */ }
            settleTimer = setTimeout(() => {
              settleTimer = null;
              begin();
            }, SETTLE_DELAY_MS);
            return;
          }
          if (confirmChecks >= maxConfirmChecks) {
            // ここまで(待ち時間を大きく取った上で)確認が取れなかった場合、
            // 実際には本当に止まっている(再生できていない)可能性の方が高い。
            // 以前はここでbegin()して「再生成功」扱いにしていたが、これだと
            // 本当は無音のまま何も鳴らずに終わった曲が失敗として検出されず、
            // 「再生できませんでした」の表示にも別の曲への差し替えにも
            // つながらないまま、カウントダウンだけが空虚に進む不具合があった。
            // 再生失敗として扱い、呼び出し側の差し替えロジックに任せる。
            clearConfirm();
            onError();
            return;
          }
          confirmLastTime = t;
        }, 100);
      };

      const onStateChange = (e) => {
        if (e.data === YT.PlayerState.PLAYING) {
          if (started) {
            resumeTimer();
          } else {
            confirmReallyPlaying();
          }
        } else if (e.data === YT.PlayerState.ENDED) {
          clearConfirm();
          finish();
        } else if (started) {
          // BUFFERING/PAUSEDなどで音が止まっている間は、その分を持ち時間から
          // 差し引かないようタイマーを一時停止する。
          pauseTimer();
        } else {
          // まだ本当の開始判定中にBUFFERING等へ戻った場合は確認をリセットする
          // (再生確認済みで待機中の余白タイマーも、ここで途切れたなら白紙に戻す)。
          clearConfirm();
          clearSettle();
        }
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
        clearConfirm();
        clearSettle();
        player.removeEventListener("onStateChange", onStateChange);
        player.removeEventListener("onError", onError);
        if (activeCancel[playerIndex] === cancel) activeCancel[playerIndex] = null;
      };
      // 外部(次の再生)から打ち切られた場合: 誰も結果を待っていないのでresolve()は
      // 呼ばず、イベント購読とタイマーの後片付けだけ行う。
      const cancel = () => {
        settled = true;
        if (timeoutId) clearTimeout(timeoutId);
        clearConfirm();
        clearSettle();
        player.removeEventListener("onStateChange", onStateChange);
        player.removeEventListener("onError", onError);
      };
      activeCancel[playerIndex] = cancel;

      const load = () => {
        try {
          player.unMute();
          player.setVolume(100);
        } catch (e) { /* noop */ }
        try {
          player.loadVideoById({ videoId, startSeconds: loadStartSeconds });
        } catch (e) {
          settled = true;
          cleanup();
          resolve({ error: true });
        }
      };

      const startPlayback = () => {
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

        // それでも(PLAYING状態への遷移すら)反応がなければ、本当に再生できて
        // いない可能性が高いので失敗として扱う(以前はここでbegin()して
        // 「再生成功」扱いにしていたが、無音のまま何も鳴らずに終わる曲を
        // 検出できず、差し替えが働かない不具合があった)。
        setTimeout(() => {
          if (!settled && !started) onError();
        }, 7000);
      };

      player.addEventListener("onStateChange", onStateChange);
      player.addEventListener("onError", onError);

      // 「無音のまま曲が終わったのに、もう一度再生すると適切に流れる」という
      // 報告が繰り返しあった。1回のcueVideoById()+バッファ確認待ちでは
      // まだ不十分なケースが残っていた(getPlayerState()/getVideoLoadedFraction()
      // が「準備できた」ように見えても、実際にはまだ音が出ないことがある =
      // これらの信号自体があまり信頼できない)。そこで、本番の読み込みに入る前に
      // cueVideoById()を複数回繰り返し、実質的に「もう一度再生」を何度も
      // 行ってから始める(待たされても構わないとのことなので、1問目は
      // このプレーヤーが初めて使われる時に限りとりわけ多め(10回)、
      // 2問目以降(既にこのプレーヤーで再生実績がある)は5回)。
      const PREBUFFER_CYCLES = warmedUpPlayers.has(playerIndex) ? 5 : 10;
      const PREBUFFER_CYCLE_INTERVAL_MS = 600;
      warmedUpPlayers.add(playerIndex);
      const runPrebufferCycle = (remaining) => {
        if (settled) return;
        if (remaining <= 0) {
          startPlayback();
          return;
        }
        try {
          player.unMute();
          player.setVolume(100);
        } catch (e) { /* noop */ }
        try {
          player.cueVideoById({ videoId, startSeconds: loadStartSeconds });
        } catch (e) { /* noop */ }
        setTimeout(() => runPrebufferCycle(remaining - 1), PREBUFFER_CYCLE_INTERVAL_MS);
      };
      runPrebufferCycle(PREBUFFER_CYCLES);
    });
  }

  return {
    // プレーヤー生成(YouTube iframe APIの読み込み含む)を先行して始めておく。
    // 「スタート!」クリック直後など、実際の再生より前に呼んでおくことで、
    // 最初の1曲の再生開始が遅れる/反応しない事象を減らす。戻り値は待たなくてよい。
    prepare(count) {
      return ensurePool(count);
    },

    // 現在プール内に存在する全プレーヤーのミュートを解除する。ブラウザの
    // 自動再生ポリシーは、ユーザー操作(クリック等)から直接つながっていない
    // 場面での音声付き自動再生を許可しないことが多く、これまでプレーヤーは
    // 「PLAYING状態になり時間も進んでいるのに実際には無音」という状態に
    // なっていた(「スタート!」クリック→非同期でiframe API読み込み→
    // プレーヤー生成→再生、という間にawaitが挟まり、クリックとの直接の
    // つながりが失われてしまうため)。プレーヤー自体は最初からミュートで
    // 確実に自動再生できるようにしておき(createPlayerのmute:1)、実際の
    // クリックイベントハンドラの中で(awaitを挟まず)この関数を呼んで
    // ミュート解除することで、ブラウザにユーザー操作に基づく解除だと
    // 認識させる。一度ミュート解除すれば、以降そのプレーヤーインスタンスで
    // loadVideoById()するだけの後続の曲でも音が鳴り続ける。
    unmuteAll() {
      pool.forEach((p) => {
        try {
          p.unMute();
          p.setVolume(100);
        } catch (e) { /* noop */ }
      });
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
    // (次の問題の曲を先読みし、実際に切り替わった時の読み込み開始を早める用途)。
    // 以前はこの後の実際の再生時にplayVideo()へ切り替えて二重読み込みを避けようと
    // したが、それが原因で再生できなくなる不具合が起きた。今回は実際の再生トリガー
    // は常にloadVideoById(playOne側は変更なし)のままにし、cueVideoById()は
    // あくまで事前のヒントとしてのみ使う(失敗しても実害がないようtry/catchで無視)。
    async precue(playerIndex, videoId, startSeconds) {
      const players = await ensurePool(playerIndex + 1);
      try {
        players[playerIndex].cueVideoById({ videoId, startSeconds });
      } catch (e) { /* noop */ }
    },

    // 指定した1台だけを打ち切る(「次へ」を再生中に押した時、その曲だけ止めて
    // 次の問題に進むための用途)。他のプレーヤー(先読み中の側)には触れない。
    stop(playerIndex) {
      if (activeCancel[playerIndex]) {
        activeCancel[playerIndex]();
        activeCancel[playerIndex] = null;
      }
      const player = pool[playerIndex];
      if (player) {
        try { player.pauseVideo(); } catch (e) { /* noop */ }
      }
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
