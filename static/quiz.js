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
    const correctIds = new Set(correctTracks.map((t) => t.videoId));
    const remaining = shuffle(pool.filter((t) => !correctIds.has(t.videoId)));
    const distractorCount = Math.max(0, Math.min(numChoices - correctTracks.length, remaining.length));
    return shuffle(correctTracks.concat(remaining.slice(0, distractorCount)));
  }

  function buildQuestions({ pool, mode, numQuestions, tracksPerQuestion }) {
    const bag = makeBag(pool);
    const questions = [];
    for (let q = 0; q < numQuestions; q++) {
      const picked = [];
      const seenIds = new Set();
      let guard = 0;
      while (picked.length < tracksPerQuestion && guard < pool.length * 4 + 20) {
        guard++;
        const track = bag.next();
        if (seenIds.has(track.videoId)) continue;
        seenIds.add(track.videoId);
        picked.push(track);
      }
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

      // selectedVideoIds: string[] (単曲モードは要素1個)
      submitAnswer(selectedVideoIds) {
        const q = this.currentQuestion;
        const actualIds = q.tracks.map((t) => t.videoId);
        const selectedSet = new Set(selectedVideoIds);
        const correctCount = actualIds.filter((id) => selectedSet.has(id)).length;
        const extraWrong = selectedVideoIds
          .filter((id) => !actualIds.includes(id))
          .map((id) => this.pool.find((t) => t.videoId === id))
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
      // excludeVideoIds: 候補から除外するvideoId(再生できないと分かっている曲など)。
      // 差し替え先が見つかればtrue、pool内に候補が残っていなければfalseを返す。
      replaceCurrentTrack(excludeVideoIds = []) {
        const excluded = new Set(excludeVideoIds);
        const candidates = pool.filter((t) => !excluded.has(t.videoId));
        if (candidates.length === 0) return false;
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
