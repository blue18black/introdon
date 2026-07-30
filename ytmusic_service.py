"""
イントロドン バックエンド: ytmusicapiラッパー
「YouTube Music/playlist_builder.py」のインスト除外・重複解決ロジックと、
「lyrics-quiz」のアーティスト名解決(ローマ字化された名前を日本語表記へ戻す)・
検索サジェストのロジックを流用し、クイズ用の曲一覧を返す。
"""
import functools
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from ytmusicapi import YTMusic

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _make_session():
    """ytmusicapiのデフォルトはリクエストタイムアウト30秒で、非公式APIが詰まると
    アーティスト検索の1回の呼び出しだけで30秒(リトライ込みで最大60秒)待たされる
    ことがあった(「検索に時間がかかりすぎる」の原因)。タイムアウトを短くして、
    詰まった呼び出しは早めに諦めて_search_with_retry等のリトライに回す。"""
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=8)
    return session


_yt = YTMusic(requests_session=_make_session())
# 英語ロケールのクライアントはアーティスト検索/曲検索結果で日本語アーティスト名を
# ローマ字化してしまう(例: 「超ときめき♡宣伝部」→"Cho Tokimeki Sendenbu")ため、
# 正式表記の解決だけは日本語ロケールのクライアントで行う(lyrics-quizと同じ手法)。
_yt_ja = YTMusic(language="ja", requests_session=_make_session())

INSTRUMENTAL_KEYWORDS = [
    "instrumental", "off vocal", "offvocal", "backing track",
    "インスト", "オフボーカル", "カラオケ",
]

_TRAILING_VARIANT_RE = re.compile(
    r"\s+(?:[a-z0-9.\-']+\s+)*(remix|re-mix|mix|version|ver\.?|remaster(?:ed)?|type\s*\d*)\s*$",
    re.IGNORECASE,
)
_SPACE_HYPHEN_RE = re.compile(r"\s-")
_BRACKET_RE = re.compile(r"[\(\[（【]")
_PAIRED_HYPHEN_RE = re.compile(r"-[^-]+-")


def is_instrumental(title: str) -> bool:
    t = title.lower()
    return any(k.lower() in t for k in INSTRUMENTAL_KEYWORDS)


def clean_title(title: str) -> str:
    """"日本語タイトル - Romanized Title" のように付与される冗長なローマ字表記を除去する。
    " - "で区切り、非ASCII文字を含む部分(=まだ本来のタイトルの続き)が続く限り残し、
    純ASCIIの部分(=ローマ字化された重複表記)が出た時点で切り捨てる。
    """
    parts = [p for p in title.split(" - ") if p.strip()]
    if not parts:
        return title.strip()
    kept = [parts[0]]
    for part in parts[1:]:
        if any(ord(ch) > 127 for ch in part):
            kept.append(part)
        else:
            break
    return " - ".join(kept).strip() or title.strip()


def normalize_title(title: str) -> str:
    """重複検出用の正規化キー(小文字化・記号除去など)。"""
    t = title.lower()
    t = re.sub(r"[\(\[（【].*?[\)\]）】]", "", t)
    t = re.sub(r"feat\.?.*", "", t)
    t = _SPACE_HYPHEN_RE.split(t, maxsplit=1)[0]
    t = _TRAILING_VARIANT_RE.sub("", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_variant_for_display(title: str) -> str:
    """normalize_titleと同じ除去ルールだが、大文字/記号は保持して表示用に整形する。"""
    t = re.sub(r"[\(\[（【].*?[\)\]）】]", "", title)
    t = re.sub(r"feat\.?.*", "", t, flags=re.IGNORECASE)
    t = _SPACE_HYPHEN_RE.split(t, maxsplit=1)[0]
    t = _TRAILING_VARIANT_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or title.strip()


def _has_variant_qualifier(title: str) -> bool:
    return (
        bool(_BRACKET_RE.search(title))
        or bool(_TRAILING_VARIANT_RE.search(title))
        or bool(_PAIRED_HYPHEN_RE.search(title))
    )


def _year_value(year):
    try:
        return -int(year)
    except (TypeError, ValueError):
        return float("-inf")


def _pick_winner(unique):
    plain = [t for t in unique if not _has_variant_qualifier(t["title"])]
    candidates = plain if plain else unique
    return max(
        candidates,
        key=lambda t: (
            # シングル/アルバムとしてカタログ登録されている曲を、動画セクション
            # (MV等)由来の曲より常に優先する。MVは音源が別ミックスのことがあり、
            # タイトルの表記が同じでどちらを残すか決め手がない場合、動画セクション
            # 由来のtype=""(fetch_video_tracks参照)が先頭に来て誤って勝ってしまう
            # ことがあった。
            1 if t.get("type") in ("シングル・EP", "アルバム") else 0,
            _year_value(t.get("year")),
            1 if t.get("type") == "シングル・EP" else 0,
            1 if "通常盤" in (t.get("album") or "") else 0,
        ),
    )


def filter_instrumental(tracks):
    return [t for t in tracks if not is_instrumental(t["title"])]


def resolve_duplicates(tracks):
    """タイトルの表記ゆれ重複を検出し、優先順位ルールで1曲に絞り込む(playlist_builder.pyと同ロジック)。"""
    key_by_video_id = {}
    groups = defaultdict(list)
    for t in tracks:
        key = normalize_title(t["title"])
        key_by_video_id[t["videoId"]] = key
        groups[key].append(t)

    winners = {}
    for norm, group in groups.items():
        unique = list({t["videoId"]: t for t in group}.values())
        if len(unique) <= 1:
            continue
        winner = _pick_winner(unique)
        winners[norm] = winner["videoId"]

    result = []
    for t in tracks:
        key = key_by_video_id[t["videoId"]]
        if key in winners:
            if t["videoId"] == winners[key]:
                result.append(t)
        else:
            result.append(t)
    return result


def _primary_artist_name(track_artists):
    if not track_artists:
        return "不明なアーティスト"
    return "、".join(a["name"] for a in track_artists if a.get("name"))


def _to_quiz_track(raw, fallback_album=None):
    duration = raw.get("duration_seconds")
    if not duration or duration <= 0:
        return None
    thumbs = raw.get("thumbnails") or []
    thumb = thumbs[-1]["url"] if thumbs else None
    return {
        "videoId": raw["videoId"],
        "title": raw.get("title") or "不明な曲",
        "artist": _primary_artist_name(raw.get("artists")),
        "album": raw.get("album") or fallback_album,
        "durationSeconds": duration,
        "thumbnail": thumb,
    }


def _clean_and_dedupe(tracks):
    """is_instrumental除外→clean_titleでローマ字重複表記を除去→normalize_titleでグルーピングし
    重複バージョンを1曲に絞り込む→最後にstrip_variant_for_displayで表示用タイトルに整形する
    (lyrics-quizの_dedupe_tracksと同じ処理順)。"""
    tracks = [t for t in tracks if t.get("videoId") and t.get("title")]
    tracks = filter_instrumental(tracks)

    cleaned = []
    for t in tracks:
        t = dict(t)
        t["title"] = clean_title(t["title"])
        cleaned.append(t)

    tracks = resolve_duplicates(cleaned)

    for t in tracks:
        t["title"] = strip_variant_for_display(t["title"])

    # 同じ曲がアルバムごとに邦題/英題など別表記でクレジットされていると、
    # normalize_title()のグルーピングをすり抜けて同一videoIdが複数グループの
    # 「勝者」として残ることがある。最後にvideoId単位で最終的な重複除去を行う。
    seen_ids = set()
    deduped = []
    for t in tracks:
        if t["videoId"] in seen_ids:
            continue
        seen_ids.add(t["videoId"])
        deduped.append(t)
    return deduped


_EMBEDDABLE_CACHE = {}


def _is_embeddable(video_id):
    """YouTubeのoEmbedエンドポイントで、動画が埋め込み再生できるかを事前確認する。
    アップロード側が埋め込みを許可していない動画は401、削除/非公開になった動画は
    404を返す--これがイントロクイズで「曲が流れない」の大半の原因(youtube-player.js
    のonErrorで検出しているのと同じ制約)。ここで弾いておくことで、出題時にその
    問題に当たる頻度そのものを減らす。
    タイムアウトなど判定不能な場合は誤って曲を減らさないよう埋め込み可能とみなす
    (本当に再生できない曲は、クイズ側の差し替えロジックが最終的な保険になる)。
    """
    if video_id in _EMBEDDABLE_CACHE:
        return _EMBEDDABLE_CACHE[video_id]
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    ok = True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as res:
            ok = res.status == 200
    except urllib.error.HTTPError as e:
        # 401(埋め込み不可)/404(動画が存在しない)は確実な「再生不可」シグナル。
        ok = e.code not in (401, 404)
    except Exception:
        ok = True
    _EMBEDDABLE_CACHE[video_id] = ok
    return ok


def _filter_embeddable(tracks):
    if not tracks:
        return tracks
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda t: _is_embeddable(t["videoId"]), tracks))
    return [t for t, ok in zip(tracks, results) if ok]


_PLAYLIST_ID_RE = re.compile(r"[?&]list=([\w-]+)")


def _extract_playlist_id(url_or_id):
    """YouTube Music/YouTubeのプレイリストURL(またはID自体)からプレイリストIDを取り出す。
    アーティストページ経由の発見ロジックでは辿れない曲(公式ページ非掲載のベスト盤収録曲
    など)でも、ユーザーが直接プレイリストのリンクを貼れば取得できるようにするための入口。
    """
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = _PLAYLIST_ID_RE.search(s)
    if m:
        return m.group(1)
    # URL形式でなく、IDそのものが渡された場合
    if re.fullmatch(r"[\w-]+", s):
        return s
    return None


def _fetch_playlist_by_id(playlist_id, limit=200):
    """get_playlist()は渡すIDの形式(先頭にVLが要るかどうか)がプレイリストの種類に
    よって異なるため、両方試す。英語ロケールのクライアントだとアーティスト名が
    ローマ字化される(例:「アンジュルム」→"ANGERME")ため、日本語ロケールの
    _yt_jaで取得する(アーティスト名解決と同じ理由)。"""
    variants = [playlist_id]
    variants.append(playlist_id[2:] if playlist_id.startswith("VL") else "VL" + playlist_id)
    for pid in variants:
        try:
            playlist = _yt_ja.get_playlist(pid, limit=limit)
            if playlist and playlist.get("tracks"):
                return playlist
        except Exception:
            continue
    return None


def get_playlist_tracks(url_or_id):
    """YouTube Music/YouTubeのプレイリストのURL(またはID)から曲一覧を取得する。
    見つからない場合はNoneを返す。"""
    playlist_id = _extract_playlist_id(url_or_id)
    if not playlist_id:
        return None

    playlist = _fetch_playlist_by_id(playlist_id)
    if not playlist:
        return None

    raw_tracks = _clean_and_dedupe(playlist.get("tracks") or [])
    raw_tracks = _filter_embeddable(raw_tracks)

    tracks = []
    for raw in raw_tracks:
        qt = _to_quiz_track(raw)
        if qt:
            tracks.append(qt)

    return {"playlistTitle": playlist.get("title") or "プレイリスト", "tracks": tracks}


def _is_pure_katakana(s):
    return bool(s) and all("゠" <= ch <= "ヿ" or ch in " ・-.'" for ch in s)


def _is_ascii(s):
    return bool(s) and all(ord(ch) < 128 for ch in s)


_DOMINANT_SONG_VOTES = 8
# 完全一致するアーティストの票数がこれ未満の場合、「曲検索側が単にこの語で
# ヒットしにくいだけ」とみなし、多数決側による上書きを許さない
# (find_target_artist内のsong_dominates_exactのコメント参照)。
_EXACT_MATCH_CONTEST_FLOOR = 2


def find_target_artist(artist: str):
    """(表記ゆれのある)アーティスト名を正式な (名前, channelId) に解決する(lyrics-quizと同じ手法)。

    2つの独立したシグナルを組み合わせる:
    1) 多数決: 曲検索結果("songs"フィルタ)の中で最も多く登場するメインアーティスト。
       YouTube Musicは検索クエリと異なる表記/ローマ字でアーティストを登録していること
       が多い(例: 「私立恵比寿中学」が"Shiritsu Ebisu Chugaku"扱いなど)ため、名前の
       文字列一致では解決できないが、これなら曖昧な単語のクエリ(例:「SEKAI」→実際に
       曲がヒットしているSEKAI NO OWARI)も正しく解決できる。
    2) YouTube Music自身のアーティスト検索("artists"フィルタ)の1位候補。
       「ミスチル」→Mr.Childrenのような愛称/略称に強い。

    両者が一致すればそれを採用。食い違う場合、(1)は絶対票数のしきい値を超え、かつ
    2位候補に大差をつけている場合のみ信頼し、そうでなければ(2)を採用する。
    さらに、クエリと完全一致(大文字小文字無視)するアーティスト名がある場合はそれを
    強いシグナルとして扱い、別アーティストの曲が2倍以上の票数で圧倒しない限り採用する
    (「aiko」のような名前衝突を決定的に解決するため)。
    """
    def _search_songs():
        return _yt.search(artist, filter="songs", limit=25)

    def _search_artists():
        try:
            return _yt.search(artist, filter="artists", limit=10)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        songs_future = pool.submit(_search_songs)
        artists_future = pool.submit(_search_artists)
        results = songs_future.result()
        artist_results = artists_future.result()

    counts = {}
    for r in results:
        artists = r.get("artists") or []
        if not artists:
            continue
        artist_id = artists[0].get("id")
        if artist_id:
            counts[artist_id] = counts.get(artist_id, 0) + 1
    sorted_counts = sorted(counts.values(), reverse=True)
    song_vote_id = max(counts, key=counts.get) if counts else None
    song_vote_count = sorted_counts[0] if sorted_counts else 0
    runner_up_count = sorted_counts[1] if len(sorted_counts) > 1 else 0

    artist_search_id = artist_results[0].get("browseId") if artist_results else None

    query_norm = artist.strip().lower()
    exact_match_ids = [
        r["browseId"] for r in artist_results
        if r.get("browseId") and (r.get("artist") or "").strip().lower() == query_norm
    ]
    exact_match_id = None
    if exact_match_ids:
        # 同名の別チャンネル(ホモニム)が複数ある場合、曲検索側の票データに
        # 実際に出てくるものを優先する(実カタログを持つ本物である可能性が高い)。
        exact_match_id = max(exact_match_ids, key=lambda aid: counts.get(aid, 0))
    exact_match_votes = counts.get(exact_match_id, 0) if exact_match_id else 0

    is_dominant = song_vote_count >= _DOMINANT_SONG_VOTES and song_vote_count >= runner_up_count * 1.5

    if exact_match_id:
        song_dominates_exact = (
            song_vote_id
            and song_vote_id != exact_match_id
            and song_vote_count >= _DOMINANT_SONG_VOTES
            and exact_match_votes >= _EXACT_MATCH_CONTEST_FLOOR
            and song_vote_count >= exact_match_votes * 2
        )
        artist_id = song_vote_id if song_dominates_exact else exact_match_id
    elif song_vote_id and (song_vote_id == artist_search_id or is_dominant):
        artist_id = song_vote_id
    elif artist_search_id:
        artist_id = artist_search_id
    else:
        artist_id = song_vote_id

    if not artist_id:
        return None

    # 英語ロケールの検索結果は日本語アーティスト名をローマ字化し、逆に日本語ロケールは
    # 欧米アーティスト名をカタカナ化してしまうため、両方取得して使い分ける。
    def _get_ja_name():
        try:
            return _yt_ja.get_artist(artist_id).get("name")
        except Exception:
            return None

    def _get_en_name():
        try:
            return _yt.get_artist(artist_id).get("name")
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        ja_future = pool.submit(_get_ja_name)
        en_future = pool.submit(_get_en_name)
        ja_name = ja_future.result()
        en_name = en_future.result()

    if ja_name and _is_pure_katakana(ja_name) and en_name and _is_ascii(en_name):
        name = en_name
    else:
        name = ja_name or en_name or artist
    return name, artist_id


_SUGGESTION_NAME_CACHE = {}


def _search_with_retry(client, query, **kwargs):
    """並列リクエストが集中すると、YT Music(非公式API)の一部が一時的にリジェクト
    されることがある。1回だけ間を置いて再試行すると大半は回復する
    (アーティストサジェストは候補ごとに最大7並列で叩くため起きやすい。
    「何回か検索しないとヒットしない」の主因はこれだった)。"""
    for attempt in range(2):
        try:
            return client.search(query, **kwargs)
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
    return []


def _get_search_suggestions_with_retry(query):
    for attempt in range(2):
        try:
            return _yt.get_search_suggestions(query)
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
    return []


def _resolve_suggestion_name(raw):
    """get_search_suggestionsが返す断片的な語句(例:「ときめき宣伝部」)や
    「アーティスト名 曲名」形式の候補を、アーティストの正式名に解決する。
    find_target_artistより軽量(多数決の曲検索はせず、直接のアーティスト検索のみ)にし、
    候補(最大6件)を1文字入力するたびに解決してもレスポンスが遅くならないようにする。"""
    if raw in _SUGGESTION_NAME_CACHE:
        return _SUGGESTION_NAME_CACHE[raw]
    results = _search_with_retry(_yt_ja, raw, filter="artists", limit=1)
    resolved = results[0].get("artist") if results else None
    _SUGGESTION_NAME_CACHE[raw] = resolved
    return resolved


def _direct_artist_matches(query):
    """get_search_suggestions自体の補完候補だけでは短い/一般的なクエリ
    (例:「超」→「超ときめき♡宣伝部」)でアーティストを取りこぼすことがあるため、
    入力文字列そのものでも直接アーティスト検索する。"""
    results = _search_with_retry(_yt_ja, query, filter="artists", limit=5)
    return [r["artist"] for r in results[:5] if r.get("artist")]


def get_artist_suggestions(query):
    """検索欄への入力に応じたアーティスト名サジェストを返す(lyrics-quizの
    /api/suggestと同じロジック)。

    非公式APIは呼び出し1回あたり1〜2秒程度かかるため、直列に呼ぶと体感速度が
    大きく悪化する。get_search_suggestions(候補語句取得)とdirect_artist_matches
    (入力文字列そのものでの直接検索)はお互いに依存しないので同時に投げ、
    候補語句が返ってきた分だけ都度アーティスト名解決を追加で投げる。また
    解決対象の候補語句は多いほど並列数・レイテンシが増えるため4件に絞る
    (元は6件だったが、体感が遅いというフィードバックを受けて削減)。"""
    with ThreadPoolExecutor(max_workers=6) as pool:
        raw_future = pool.submit(_get_search_suggestions_with_retry, query)
        direct_future = pool.submit(_direct_artist_matches, query)

        raw = raw_future.result()
        seen = set()
        candidates = []
        for text in raw:
            if text in seen:
                continue
            seen.add(text)
            candidates.append(text)
        candidates = candidates[:4]

        resolve_futures = [pool.submit(_resolve_suggestion_name, c) for c in candidates]
        resolved = [f.result() for f in resolve_futures]
        direct_matches = direct_future.result()

    seen_resolved = set()
    names = []
    for name in resolved + direct_matches:
        if not name or name in seen_resolved:
            continue
        seen_resolved.add(name)
        names.append(name)
    return names


def _collect_discography_entries(yt, artist, artist_id, section_key):
    section = artist.get(section_key, {})
    if not section:
        return []
    params = section.get("params")
    results = section.get("results", [])
    if not params:
        return results
    browse_id = section.get("browseId") or artist_id
    try:
        full = yt.get_artist_albums(browse_id, params)
        if len(full) >= len(results):
            return full
        return results
    except Exception:
        return results


_ARTIST_TRACKS_CACHE = {}


def fetch_all_tracks(artist_id):
    """playlist_builder.py の get_artist_tracks を踏襲: アーティストの全アルバム/シングル
    収録曲を取得する。アルバムが多いアーティストほど逐次取得が遅くなるため、
    lyrics-quizと同様に並列fetch+キャッシュで高速化する。"""
    if artist_id in _ARTIST_TRACKS_CACHE:
        return _ARTIST_TRACKS_CACHE[artist_id]

    artist = _yt.get_artist(artist_id)

    album_entries = _collect_discography_entries(_yt, artist, artist_id, "albums")
    single_entries = _collect_discography_entries(_yt, artist, artist_id, "singles")

    album_entries = list(reversed(album_entries))
    single_entries = list(reversed(single_entries))

    for entry in single_entries:
        entry["_type_rank"] = 0
    for entry in album_entries:
        entry["_type_rank"] = 1

    def entry_year(entry):
        try:
            return int(entry.get("year"))
        except (TypeError, ValueError):
            return 9999

    all_entries = sorted(
        single_entries + album_entries,
        key=lambda e: (entry_year(e), e["_type_rank"]),
    )

    seen_album_ids = set()
    ordered_entries = []
    for entry in all_entries:
        album_id = entry.get("browseId")
        if not album_id or album_id in seen_album_ids:
            continue
        seen_album_ids.add(album_id)
        ordered_entries.append(entry)

    def fetch_album(entry):
        try:
            return entry, _yt.get_album(entry["browseId"])
        except Exception:
            return entry, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_album, ordered_entries))

    raw_tracks = []
    for entry, album in results:
        if not album:
            continue
        for track in album.get("tracks", []):
            if not track.get("videoId"):
                continue
            track = dict(track)
            track["type"] = "シングル・EP" if entry["_type_rank"] == 0 else "アルバム"
            track["year"] = entry.get("year") or "年不明"
            raw_tracks.append(track)

    # 「仮契約のシンデレラ」のように、アルバム/シングルという形でカタログ登録されて
    # いない動画限定の曲は、上記のディスコグラフィ走査だけでは一切拾えない
    # (人気・不人気とは無関係に、そもそも取得元データに含まれていない)。
    # アーティストページの「動画」セクションからも曲を拾って合流させる。
    existing_ids = {t["videoId"] for t in raw_tracks}
    for t in fetch_video_tracks(artist_id):
        if t["videoId"] not in existing_ids:
            raw_tracks.append(t)
            existing_ids.add(t["videoId"])

    _ARTIST_TRACKS_CACHE[artist_id] = raw_tracks
    return raw_tracks


def _fetch_view_count(video_id):
    try:
        details = _yt.get_song(video_id).get("videoDetails") or {}
        return int(details.get("viewCount") or 0)
    except Exception:
        return 0


_ARTIST_RANKED_CACHE = {}


def fetch_ranked_tracks(artist_id):
    """Top25/Top50用のランキング。YouTube Music公式の「人気の曲」プレイリストは
    使わず、全曲(fetch_all_tracks)について曲ごとに再生回数を取得し、
    その再生回数の多い順に独自にランキングする。

    通常版/ショートバージョンのような表記ゆれ重複は、_clean_and_dedupe(全曲一覧用)
    が使う「カッコ書きのないプレーンなタイトルを優先する」という表示用ヒューリスティック
    ではなく、実際の再生回数が高い方を勝者(=ランキング算出に採用する動画)として残す。
    ただし表示するタイトルは勝者の実タイトルではなく、strip_variant_for_displayで
    整形したプレーンタイトルにする(例: 「仮契約のシンデレラ（ショートバージョン）」が
    勝者でも、表示上は「仮契約のシンデレラ」にする)。"""
    if artist_id in _ARTIST_RANKED_CACHE:
        return _ARTIST_RANKED_CACHE[artist_id]

    raw = [t for t in fetch_all_tracks(artist_id) if t.get("videoId") and t.get("title")]
    raw = filter_instrumental(raw)
    tracks = []
    for t in raw:
        t = dict(t)
        t["title"] = clean_title(t["title"])
        tracks.append(t)

    with ThreadPoolExecutor(max_workers=8) as pool:
        view_counts = list(pool.map(_fetch_view_count, [t["videoId"] for t in tracks]))
    for t, vc in zip(tracks, view_counts):
        t["_viewCount"] = vc

    groups = defaultdict(list)
    for t in tracks:
        groups[normalize_title(t["title"])].append(t)

    winners = []
    for group in groups.values():
        unique = list({t["videoId"]: t for t in group}.values())
        winner = dict(max(unique, key=lambda t: t["_viewCount"]))
        winner["title"] = strip_variant_for_display(winner["title"])
        winners.append(winner)

    seen_ids = set()
    deduped = []
    for t in winners:
        if t["videoId"] in seen_ids:
            continue
        seen_ids.add(t["videoId"])
        deduped.append(t)

    ranked = sorted(deduped, key=lambda t: t["_viewCount"], reverse=True)
    _ARTIST_RANKED_CACHE[artist_id] = ranked
    return ranked


_ARTIST_VIDEO_CACHE = {}
_VIDEO_TITLE_RE = re.compile(r"[「『]([^」』]+)[」』]")
_LIVE_VIDEO_RE = re.compile(r"live|tour|ライブ|ツアー", re.IGNORECASE)


def fetch_video_tracks(artist_id):
    """アルバム/シングルがYouTube Musicのカタログに登録されていないアーティスト向けの
    フォールバック: アーティストページの「動画」セクション(公式MVなど)のタイトル
    "<アーティスト>「<曲名>」..." から曲名を推測する(lyrics-quizと同じ手法)。"""
    if artist_id in _ARTIST_VIDEO_CACHE:
        return _ARTIST_VIDEO_CACHE[artist_id]

    artist = _yt.get_artist(artist_id)
    videos_section = artist.get("videos") or {}
    browse_id = videos_section.get("browseId")

    raw_tracks = videos_section.get("results", [])
    if browse_id:
        try:
            playlist = _yt.get_playlist(browse_id, limit=100)
            if playlist.get("tracks"):
                raw_tracks = playlist["tracks"]
        except Exception:
            pass

    tracks = []
    for t in raw_tracks:
        raw_title = t.get("title")
        if not raw_title or not t.get("videoId"):
            continue
        if _LIVE_VIDEO_RE.search(raw_title):
            continue
        m = _VIDEO_TITLE_RE.search(raw_title)
        if not m:
            continue
        track = dict(t)
        track["title"] = m.group(1).strip()
        track["type"] = ""
        track["year"] = "年不明"
        tracks.append(track)

    _ARTIST_VIDEO_CACHE[artist_id] = tracks
    return tracks


def get_artist_tracks(artist_name, scope="all"):
    """アーティスト名(表記ゆれ・ローマ字表記でも可)から曲一覧を取得する。見つからない
    場合はNoneを返す。歌詞データが不要なlyrics-quizと異なり、scope="all"で全曲、
    "top25"/"top50"で再生回数ランキング上位に絞り込んで取得できる。"""
    target = find_target_artist(artist_name)
    if not target:
        return None
    canonical_name, artist_id = target

    if scope in ("top25", "top50"):
        ranked = fetch_ranked_tracks(artist_id)  # 全曲を重複解決済み・再生回数降順
        nominal = 25 if scope == "top25" else 50
        tracks = _filter_embeddable(ranked[:nominal])
        if len(tracks) < nominal:
            # 埋め込み不可な曲が混ざっていて規定数に満たない場合、ランキングの
            # 続き(次のnominal件)から補充する。
            have_ids = {t["videoId"] for t in tracks}
            for t in _filter_embeddable(ranked[nominal:nominal * 2]):
                if len(tracks) >= nominal:
                    break
                if t["videoId"] not in have_ids:
                    tracks.append(t)
                    have_ids.add(t["videoId"])
    else:
        # fetch_all_tracks自体がアルバム/シングル未登録の動画限定曲も含めて
        # 返すため、ここで別途動画セクションへフォールバックする必要はない。
        tracks = _filter_embeddable(_clean_and_dedupe(fetch_all_tracks(artist_id)))

    quiz_tracks = []
    for raw in tracks:
        qt = _to_quiz_track(raw, fallback_album=raw.get("album"))
        if qt:
            # 曲ごとのartists表記はリリース元アルバム/動画のロケールやコラボ
            # クレジットによって「Shiritsu Ebisu Chugaku」「TWINKLE WINK」のように
            # 一貫しないことがある。アーティスト指定で取得した曲は全曲この
            # アーティストのものなので、指定時に解決した正式表記に統一する。
            qt["artist"] = canonical_name
            quiz_tracks.append(qt)
    return {"artistName": canonical_name, "tracks": quiz_tracks}
