// クイズの出題・採点ロジック
const Quiz = (() => {
  const SIMULTANEOUS_PLAY_SECONDS = 10;

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function makeBag(pool) {
    let bag = shuffle(pool);
    let idx = 0;
    return {
      next() {
        if (idx >= bag.length) {
          bag = shuffle(pool);
          idx = 0;
        }
        return bag[idx++];
      },
    };
  }

  function pickChoices(pool, correctTracks, numChoices) {
    const correctIds = new Set(correctTracks.map((t) => t.trackId));
    const remaining = shuffle(pool.filter((t) => !correctIds.has(t.trackId)));
    const distractorCount = Math.max(0, Math.min(numChoices - correctTracks.length, remaining.length));
    return shuffle(correctTracks.concat(remaining.slice(0, distractorCount)));
  }

  function buildQuestions({ pool, mode, numQuestions, tracksPerQuestion }) {
    const bag = makeBag(pool);
    const questions = [];
    // ゲーム全体を通して同じ曲が何度も出題されないようにする。プールのユニークな
    // 曲数がnumQuestions分に足りている限り、一度出した曲は避け続ける。プールが
    // 足りない場合のみ(setup-errorで警告済み)、やむを得ず重複を許可する。
    const usedTrackIds = new Set();
    const uniqueCount = new Set(pool.map((t) => t.trackId)).size;

    for (let q = 0; q < numQuestions; q++) {
      const picked = [];
      const seenIds = new Set();
      let guard = 0;
      while (picked.length < tracksPerQuestion && guard < pool.length * 8 + 40) {
        guard++;
        const track = bag.next();
        if (seenIds.has(track.trackId)) continue;
        if (usedTrackIds.has(track.trackId) && usedTrackIds.size < uniqueCount) continue;
        seenIds.add(track.trackId);
        picked.push(track);
      }
      picked.forEach((t) => usedTrackIds.add(t.trackId));
      const numChoices = Math.min(pool.length, tracksPerQuestion * 2 + 2);
      const choices = pickChoices(pool, picked, numChoices);
      questions.push({ tracks: picked, choices });
    }
    return questions;
  }

  function startSecondsFor(track, mode, seconds) {
    if (mode === "outro") {
      return Math.max(0, Math.round(track.durationSeconds) - seconds);
    }
    return 0;
  }

  function createSession({ mode, pool, numQuestions, seconds, simultaneousCount }) {
    const tracksPerQuestion = mode === "mix" ? simultaneousCount : 1;
    const playSeconds = mode === "mix" ? SIMULTANEOUS_PLAY_SECONDS : seconds;
    const questions = buildQuestions({ pool, mode, numQuestions, tracksPerQuestion });

    return {
      mode,
      pool,
      playSeconds,
      seconds,
      questions,
      currentIndex: 0,
      log: [],

      get total() { return questions.length; },
      get currentQuestion() { return questions[this.currentIndex]; },
      get isLastQuestion() { return this.currentIndex >= questions.length - 1; },

      getPlaybackPlan() {
        const q = this.currentQuestion;
        return q.tracks.map((t) => ({
          track: t,
          startSeconds: startSecondsFor(t, mode, seconds),
        }));
      },

      // selectedTrackIds: string[] (単曲モードは要素1個)
      submitAnswer(selectedTrackIds) {
        const q = this.currentQuestion;
        const actualIds = q.tracks.map((t) => t.trackId);
        const selectedSet = new Set(selectedTrackIds);
        const correctCount = actualIds.filter((id) => selectedSet.has(id)).length;
        const extraWrong = selectedTrackIds
          .filter((id) => !actualIds.includes(id))
          .map((id) => this.pool.find((t) => t.trackId === id))
          .filter(Boolean);

        const entry = {
          tracks: q.tracks,
          correctCount,
          total: q.tracks.length,
          extraWrong,
        };
        this.log.push(entry);
        return entry;
      },

      advance() {
        this.currentIndex++;
      },

      // 現在の問題の曲が再生できなかった場合に、別の曲へ静かに差し替える。
      // excludeTrackIds: 候補から除外するtrackId(再生できないと分かっている曲など)。
      // 差し替え先が見つかればtrue、pool内に候補が残っていなければfalseを返す。
      //
      // 以前は「他の問題と重複しない候補が尽きたら、重複を許容してどれでも選ぶ」
      // フォールバックだったが、これだと再生できる曲が少ないプールで特定の1曲
      // (たまたま再生できた曲)に選択が集中し、同じ曲が何度も出題されてしまう
      // 不具合があった。再生できない曲(excludeTrackIds)だけは絶対に避けた上で、
      // 「他の問題で使われている回数が最も少ない曲」を選ぶことで、やむを得ず
      // 重複する場合でも特定の1曲に偏らず均等に分散するようにする。
      replaceCurrentTrack(excludeTrackIds = []) {
        const broken = new Set(excludeTrackIds);
        const nonBroken = pool.filter((t) => !broken.has(t.trackId));
        if (nonBroken.length === 0) return false;

        const usageCount = new Map();
        questions.forEach((q, i) => {
          if (i === this.currentIndex) return;
          q.tracks.forEach((t) => {
            usageCount.set(t.trackId, (usageCount.get(t.trackId) || 0) + 1);
          });
        });

        const minUsage = Math.min(...nonBroken.map((t) => usageCount.get(t.trackId) || 0));
        const candidates = nonBroken.filter((t) => (usageCount.get(t.trackId) || 0) === minUsage);

        const newTrack = candidates[Math.floor(Math.random() * candidates.length)];
        const numChoices = Math.min(pool.length, tracksPerQuestion * 2 + 2);
        questions[this.currentIndex] = {
          tracks: [newTrack],
          choices: pickChoices(pool, [newTrack], numChoices),
        };
        return true;
      },

      getResultSummary() {
        const totalCorrect = this.log.reduce((s, e) => s + e.correctCount, 0);
        const totalCount = this.log.reduce((s, e) => s + e.total, 0);
        return { totalCorrect, totalCount, log: this.log, mode };
      },
    };
  }

  return { createSession, SIMULTANEOUS_PLAY_SECONDS };
})();
