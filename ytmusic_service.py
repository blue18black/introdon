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



# ytmusicapiは、locationを明示しない場合サーバーのIPアドレスの所在地を
# YouTube Musicへのリクエストの「発信地」として扱う。Render(シンガポール)に
# デプロイした結果、同じアーティスト/プレイリストでもローカル(日本)と
# 配信可否・検索結果が食い違い、特定のアーティストが全く再生できない、
# カタログの内容自体が異なる、といった不具合が多数起きていた。location="JP"を
# 明示することで、サーバーの物理的な所在地に関わらず日本からのリクエストとして
# 扱わせる。
_yt = YTMusic(requests_session=_make_session(), location="JP")
# 英語ロケールのクライアントはアーティスト検索/曲検索結果で日本語アーティスト名を
# ローマ字化してしまう(例: 「超ときめき♡宣伝部」→"Cho Tokimeki Sendenbu")ため、
# 正式表記の解決だけは日本語ロケールのクライアントで行う(lyrics-quizと同じ手法)。
_yt_ja = YTMusic(language="ja", requests_session=_make_session(), location="JP")

INSTRUMENTAL_KEYWORDS = [
    "instrumental", "off vocal", "offvocal", "backing track",
    "インスト", "オフボーカル", "カラオケ",
]

_TRAILING_VARIANT_RE = re.compile(
    r"\s+(?:[a-z0-9.\-']+\s+)*(remix|re-mix|mix|version|ver\.?|remaster(?:ed)?|type\s*\d*)\s*$",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"[\(\[（【]")
_PAIRED_HYPHEN_RE = re.compile(r"-[^-]+-")
# タイトル末尾の"-Moe Shop Remix-"「-TV Size-」のような、ハイフンで挟んだ
# バージョン表記だけを対象にする(文字列途中の"eye-to-eye"のような通常の
# ハイフン使用を誤って削らないよう、末尾に限定する)。
_TRAILING_PAIRED_HYPHEN_RE = re.compile(r"-[^-]+-\s*$")


def is_instrumental(title: str) -> bool:
    t = title.lower()
    return any(k.lower() in t for k in INSTRUMENTAL_KEYWORDS)


_LIVE_TITLE_RE = re.compile(r"live|ライブ", re.IGNORECASE)


def is_live_recording(title: str) -> bool:
    """曲名自体に"live"/"ライブ"のような言葉が含まれるかを見る(アルバム名では
    なく曲名を見る。アルバム名が「REVENGE LIVE」でも曲名自体は普通のことがあり、
    それだけでは除外しない)。"""
    return bool(_LIVE_TITLE_RE.search(title))


def clean_title(title: str) -> str:
    """"日本語タイトル - Romanized Title" のように付与される冗長なローマ字表記を除去する。
    " - "で区切り、非ASCII文字を含む部分(=まだ本来のタイトルの続き)が続く限り残し、
    純ASCIIの部分(=ローマ字化された重複表記)が出た時点で切り捨てる。
    """
    parts = [p for p in title.split(" - ") if p.strip()]
    if not parts:
        return title.strip()
    # 最初の部分が既に純ASCIIなら、そもそも「日本語タイトル - ローマ字重複表記」の
    # パターンではない(例:「ARASHI - Turning Up [Official Music Video]」は
    # アーティスト名も曲名も元から英語表記なだけで、後半はローマ字の重複表記では
    # ない)。誤って曲名ごと切り捨ててしまわないよう、その場合は何もしない。
    if not any(ord(ch) > 127 for ch in parts[0]):
        return title.strip()
    kept = [parts[0]]
    for part in parts[1:]:
        if any(ord(ch) > 127 for ch in part):
            kept.append(part)
        else:
            break
    return " - ".join(kept).strip() or title.strip()


def normalize_title(title: str) -> str:
    """重複検出用の正規化キー(小文字化・記号除去など)。
    以前は単純に最初の" -"より前だけを残していたが、これだと「ARASHI - カイト
    [Official Music Video]」のような"アーティスト名 - 曲名"形式のタイトルで
    アーティスト名(「arashi」)だけが残り、同じアーティストの別の曲すべてが
    同一キーに衝突して大量に誤って重複除去されてしまう不具合があった
    (" - "を「日本語タイトル - ローマ字重複表記」の区切りとしか想定していなかった
    ため)。clean_title()の、非ASCII文字を含む部分は本来のタイトルの続きとみなして
    残す、というより安全なロジックを使う。"""
    t = clean_title(title)
    t = t.lower()
    t = re.sub(r"[\(\[（【].*?[\)\]）】]", "", t)
    t = re.sub(r"feat\.?.*", "", t)
    t = _TRAILING_VARIANT_RE.sub("", t)
    t = _TRAILING_PAIRED_HYPHEN_RE.sub("", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_variant_for_display(title: str) -> str:
    """normalize_titleと同じ除去ルールだが、大文字/記号は保持して表示用に整形する。"""
    t = clean_title(title)
    t = re.sub(r"[\(\[（【].*?[\)\]）】]", "", t)
    t = re.sub(r"feat\.?.*", "", t, flags=re.IGNORECASE)
    t = _TRAILING_VARIANT_RE.sub("", t)
    t = _TRAILING_PAIRED_HYPHEN_RE.sub("", t)
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


def _is_omv(track):
    """MUSIC_VIDEO_TYPE_OMV(公式ミュージックビデオ)かどうかを見る。MVは曲が
    始まる前にセリフ・効果音等の別シーンが入っていることがあり、そこが
    「1問目の答え」として流れてしまう(イントロクイズとして不適切)。純粋な
    音源(MUSIC_VIDEO_TYPE_ATV)の方が同じ曲であれば優先したい、との明示的な
    指摘があったため、選定時にこれを避けるようにする。"""
    return track.get("videoType") == "MUSIC_VIDEO_TYPE_OMV"


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
            # MV(OMV)は曲が始まる前に別シーンが入っていることがあるため、
            # 同じ曲であれば音源版(ATV)を優先する。
            0 if _is_omv(t) else 1,
            _year_value(t.get("year")),
            1 if t.get("type") == "シングル・EP" else 0,
            1 if "通常盤" in (t.get("album") or "") else 0,
        ),
    )


def filter_instrumental(tracks):
    return [t for t in tracks if not is_instrumental(t["title"])]


_MEDLEY_TITLE_RE = re.compile(r"\S/\S")


def is_medley_title(title: str) -> bool:
    """「曲A/曲B」のように、1トラックに複数の曲(メドレー/ユニット企画曲など)が
    まとまっている曲を検出する(私立恵比寿中学の「ユニットアルバム」収録曲で
    典型的なパターン)。1トラックの中に別々の曲が混在しており、イントロ当てクイズの
    「1曲を当てる」という前提に合わないため除外する(配信データの信頼性が
    低い傾向があることも合わせて、除外する理由になる)。"""
    return bool(_MEDLEY_TITLE_RE.search(title))


def filter_medley(tracks):
    return [t for t in tracks if not is_medley_title(t["title"])]


def _dedupe_group_key(title):
    """タイトルのグルーピングキー。normalize_titleは"(Live at 会場 日付)"の
    ような括弧書きを装飾情報とみなして丸ごと除去するため、通常版と
    ライブ音源が同じキーに収束してしまうことがあった(例:「ラヴなのっ♡」
    (通常盤・OMV)と「ラヴなのっ (Live at Nakano Sunplaza 2021/12/26)」
    (ATV)が同一グループになり、ATV優先ヒューリスティックにより誤って
    ライブ音源の方が勝者として選ばれてしまっていた)。ライブ音源は別キーに
    退避し、通常版のグループに混ざらないようにする。"""
    key = normalize_title(title)
    if is_live_recording(title):
        key += "__live"
    return key


def _dedupe_by_video_id_prefer_native(tracks):
    """同じvideoIdに対して、カタログ側が日本語タイトルとローマ字/英語/他言語の
    別題を別々の行として持っていることがある(1動画に複数の言語違いの題名が
    クレジットされているケース)。タイトルベースの重複解決より前にvideoId単位で
    1行に絞り込んでおかないと、後段のタイトル正規化ではまるで別の曲であるかの
    ように扱われてしまい、稀に非日本語表記の方が勝者として残ってしまうことが
    あった(例:「すきっ！」が「SUKI! English ver.」として表示される)。
    日本語(非ASCII文字を含む)タイトルを優先し、無ければ最初に出てきたものを使う。"""
    best_by_id = {}
    order = []
    for t in tracks:
        vid = t["videoId"]
        if vid not in best_by_id:
            best_by_id[vid] = t
            order.append(vid)
            continue
        current = best_by_id[vid]
        current_native = any(ord(ch) > 127 for ch in current["title"])
        candidate_native = any(ord(ch) > 127 for ch in t["title"])
        if candidate_native and not current_native:
            best_by_id[vid] = t
    return [best_by_id[vid] for vid in order]


def resolve_duplicates(tracks):
    """タイトルの表記ゆれ重複を検出し、優先順位ルールで1曲に絞り込む(playlist_builder.pyと同ロジック)。"""
    key_by_video_id = {}
    groups = defaultdict(list)
    for t in tracks:
        key = _dedupe_group_key(t["title"])
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
    tracks = filter_medley(tracks)

    cleaned = []
    for t in tracks:
        t = dict(t)
        t["title"] = clean_title(t["title"])
        cleaned.append(t)

    cleaned = _dedupe_by_video_id_prefer_native(cleaned)

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


def _is_embeddable(video_id):
    """YouTubeのoEmbedエンドポイントで、動画が埋め込み再生できるかを事前確認する。
    アップロード側が埋め込みを許可していない動画は401、削除/非公開になった動画は
    404を返す--これがイントロクイズで「曲が流れない」の大半の原因(youtube-player.js
    のonErrorで検出しているのと同じ制約)。ここで弾いておくことで、出題時にその
    問題に当たる頻度そのものを減らす。
    一度get_song()(playabilityStatus)ベースに切り替えたが、曲数の多い
    アーティストで大量に並列実行するとYouTube側の(HTTPエラーにはならない
    "ソフトな")レート制限に引っかかり、全曲が判定不能になって0曲扱いに
    なってしまう不具合が実際に起きた(私立恵比寿中学で発生)。oEmbedは
    もっと軽量で、大量チェックでもこの問題が起きなかったため元に戻す。
    401/404以外の失敗(タイムアウト等)は、曲数の多いアーティストを一気に並列
    チェックした時にYouTube側から一時的にレート制限され、本来「再生不可」なはずの
    曲まで「念のため再生可能扱い」になってしまうことがあった。取得が遅くなっても
    正確さを優先し、間を置いて1回だけ再試行してから、それでも判定できない場合だけ
    誤って曲を減らさないよう埋め込み可能とみなす(それでも本当に再生できない曲は、
    クイズ側の差し替えロジックが最終的な保険になる)。
    """
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"

    def attempt():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as res:
            return res.status == 200

    ok = True
    for i in range(2):
        try:
            ok = attempt()
            break
        except urllib.error.HTTPError as e:
            # 401(埋め込み不可)/404(動画が存在しない)は確実な「再生不可」シグナルなので
            # 再試行しても意味がない。
            ok = e.code not in (401, 404)
            break
        except Exception:
            if i == 0:
                time.sleep(0.5)
                continue
            ok = True
    return ok


def _filter_embeddable(tracks):
    if not tracks:
        return tracks
    # get_song()のplayabilityStatusによる二次チェックを試したことがあるが、
    # Render環境からはYouTube側のbot対策(playabilityStatus="LOGIN_REQUIRED")に
    # ほぼ即座に引っかかり、ローカルからは正しく判定できる動画も含めて全件が
    # 判定不能になることを実機で確認した(データセンターIPに対する認証要求で、
    # 待つ・並列数を減らす等では回避できない)。そのため個別の動画の実再生可否は
    # サーバー側では確実に検証できないと判断し、oEmbed(埋め込み許可の有無のみ)
    # による軽量な事前チェックに留める。oEmbedをすり抜ける「メタデータは残って
    # いるが実際には削除/非公開になった動画」は、実際の再生時にクライアント側の
    # 自動リトライ・再生不可リストの永続化(static/app.js)で追って除外する。
    # 並列数を減らし、大量アクセスによる一時的なレート制限(→誤った再生可能判定)を
    # 起きにくくする。取得は遅くなるが、正確さを優先する。
    with ThreadPoolExecutor(max_workers=4) as pool:
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


def _fetch_playlist_by_id(playlist_id, limit=500):
    """get_playlist()は渡すIDの形式(先頭にVLが要るかどうか)がプレイリストの種類に
    よって異なるため、両方試す。英語ロケールのクライアントだとアーティスト名が
    ローマ字化される(例:「アンジュルム」→"ANGERME")ため、日本語ロケールの
    _yt_jaで取得する(アーティスト名解決と同じ理由)。
    継続トークンを使ったページ送りが、回線都合で時々ページの途中で静かに
    打ち切られ、本来のトラック数より大幅に少ない結果を返すことがあった。
    さらに、playlist自身が申告するtrackCountや、どのトラックが
    videoId欠落(isAvailable=false)として返るかも、取得のたびに結果が
    ばらつくことが実際に確認できた(同じプレイリストへの複数回の取得で
    有効曲数が66件/186件のように変動した)。trackCountの申告値を信用して
    早期に打ち切るのではなく、実際に再生に使える(videoId/titleが揃っている)
    トラック数が多かった方を採用する。
    Renderには(gunicorn自体のtimeout設定とは別に)プラットフォーム側の
    ゲートウェイに30秒前後のハードタイムアウトがあり、逐次に何度も取得し
    直すと合計の所要時間がそれを超えて「読み込みに失敗しました」になって
    しまう(ローカルでは取得できるのにRenderでは失敗する、という形で
    報告があった)。2つのID形式を逐次ではなく並列に1回ずつ試すことで、
    リトライの安全性を保ちつつ所要時間を増やさないようにする。"""
    variants = [playlist_id]
    variants.append(playlist_id[2:] if playlist_id.startswith("VL") else "VL" + playlist_id)

    def _attempt(pid):
        try:
            playlist = _yt_ja.get_playlist(pid, limit=limit)
        except Exception:
            return None
        if not playlist or not playlist.get("tracks"):
            return None
        return playlist

    with ThreadPoolExecutor(max_workers=len(variants)) as pool:
        results = list(pool.map(_attempt, variants))

    best = None
    best_valid_count = -1
    for playlist in results:
        if not playlist:
            continue
        valid_count = sum(1 for t in playlist["tracks"] if t.get("videoId") and t.get("title"))
        if valid_count > best_valid_count:
            best = playlist
            best_valid_count = valid_count
    return best


def _dedupe_playlist_tracks(tracks):
    """プレイリストは基本的にユーザーが選んだ通りの曲を使いたいので、
    _clean_and_dedupeのような「表記ゆれで同じ曲とみなして1曲に絞り込む」
    (=結果的に別のバージョンに差し替わることがある)処理はしない。
    プレイリスト内にたまたま同じ動画が2回入っている場合だけ、その完全一致分を除く。"""
    tracks = [t for t in tracks if t.get("videoId") and t.get("title")]
    seen_ids = set()
    deduped = []
    for t in tracks:
        if t["videoId"] in seen_ids:
            continue
        seen_ids.add(t["videoId"])
        deduped.append(t)
    return deduped


_SYMBOL_ONLY_DIFF_RE = re.compile(r"[^\w]", re.UNICODE)


def _normalize_artist_symbol_variants(tracks):
    """プレイリストの曲についているアーティスト名の表記ゆれのうち、記号の
    有無だけの違い(例:「ときめき宣伝部」と「ときめき♡宣伝部」)だけを統一する。
    改名前後のように文字そのものが違う表記(例:「ときめき♡宣伝部」→「超ときめき
    ♡宣伝部」)は別のアーティスト名として区別したままにする(曲がリリースされた
    当時の名義を尊重するため)。曲の中身(videoId)には一切触れない。
    find_target_artist()のようなアーティスト解決は行わない(ネットワーク不要で、
    改名を「同一アーティストへの統合」に潰してしまう問題も起きない)。"""
    variants_by_key = defaultdict(list)
    for t in tracks:
        name = t.get("artist") or ""
        if not name:
            continue
        key = _SYMBOL_ONLY_DIFF_RE.sub("", name).lower()
        variants_by_key[key].append(name)

    # 記号が付いている方(情報量が多い方)を代表表記として選ぶ。
    canonical_by_key = {key: max(names, key=len) for key, names in variants_by_key.items()}

    for t in tracks:
        name = t.get("artist") or ""
        if not name:
            continue
        key = _SYMBOL_ONLY_DIFF_RE.sub("", name).lower()
        t["artist"] = canonical_by_key[key]
    return tracks


def get_playlist_tracks(url_or_id):
    """YouTube Music/YouTubeのプレイリストのURL(またはID)から曲一覧を取得する。
    見つからない場合はNoneを返す。プレイリストに実際に入っている曲をそのまま
    使う――検索による別バージョンへの差し替えは行わない(「そのまま読み込めば
    いい」という明示的な要望、かつ検索ベースの差し替えは曲名にバージョン
    表記の無い曲がLess Vocal版に化けてしまう等の不具合を繰り返し起こしていた
    ため)。ストリーミング配信カタログに無くvideoId自体が欠落している曲
    (isAvailable=false)は、差し替え候補を探そうとせず素直に諦める(無理に
    別の曲を採用するとプレイリストの意図しない内容になるため)。
    埋め込み再生できない動画(_filter_embeddable、検索は伴わない軽量な
    oEmbedチェックのみ)は除外する――これを省いていた時期があったが、
    「再生できませんでした」が連発してまともに遊べなくなる方が、多少
    取りこぼしがあっても事前に弾く方より悪影響が大きいと判断した
    (この事前確認自体は「差し替え」ではなく「除外」なので、プレイリストの
    中身を勝手に別バージョンへ化けさせることはない)。
    アーティスト名の表示は、記号の有無だけの表記ゆれだけを統一する
    (改名前後の別名義はそのまま区別する)。"""
    playlist_id = _extract_playlist_id(url_or_id)
    if not playlist_id:
        return None

    playlist = _fetch_playlist_by_id(playlist_id)
    if not playlist:
        return None

    all_raw = playlist.get("tracks") or []
    have_vid = [t for t in all_raw if t.get("videoId") and t.get("title")]

    raw_tracks = _dedupe_playlist_tracks(have_vid)
    raw_tracks = _filter_embeddable(raw_tracks)

    tracks = []
    for raw in raw_tracks:
        qt = _to_quiz_track(raw)
        if qt:
            tracks.append(qt)
    tracks = _normalize_artist_symbol_variants(tracks)

    return {"playlistTitle": playlist.get("title") or "プレイリスト", "tracks": tracks}


_DOMINANT_SONG_VOTES = 8


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
            # 完全一致判定(exact_match_id)に使うため、クエリと同じ表記体系で
            # 結果が返る日本語ロケールのクライアントで検索する。英語ロケールだと
            # 日本語のクエリ(例:「いぎなり東北産」)がヒットしても、結果の
            # artist名がローマ字化されて("THE MADE IN TOHOKU"など)一致判定に
            # 使えなくなってしまう。
            return _yt_ja.search(artist, filter="artists", limit=10)
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

    is_dominant = song_vote_count >= _DOMINANT_SONG_VOTES and song_vote_count >= runner_up_count * 1.5

    # 以前は、曲検索側の票数が圧倒的な場合(song_dominates_exact)、完全一致する
    # アーティスト名があってもそちらを上書きしていた。しかし「aiko」のような
    # クエリでは、この曲名検索が本人(和製シンガー、11票)よりも「Jhené Aiko」
    # (名前に"aiko"を含む米国の別アーティスト、23票)の方を多く拾ってしまい、
    # 完全一致する本人チャンネルがあるにもかかわらず上書きされ、無関係な
    # アーティストの曲ばかりになってしまう不具合があった。クエリと完全一致する
    # アーティスト名がある場合は、それを最優先で信頼する(曲名検索側の票数による
    # 上書きはしない)。
    if exact_match_id:
        artist_id = exact_match_id
    elif song_vote_id and (song_vote_id == artist_search_id or is_dominant):
        artist_id = song_vote_id
    elif artist_search_id:
        artist_id = artist_search_id
    else:
        artist_id = song_vote_id

    if not artist_id:
        return None

    # 以前はget_artist()経由でロケールごとの正式名称を取得し直していたが、
    # 日本語ロケール側の取得がライブラリ内部のパースエラーで失敗して英語名に
    # フォールバックする(「いぎなり東北産」→"THE MADE IN TOHOKU")、カタカナ
    # 名義を外国人アーティストの音訳と誤判定してローマ字化する(「アンジュルム」
    # →"ANGERME")、「aiko」のような曖昧な名前で見当違いのアーティストの
    # 表記に化ける、といった不具合を繰り返し起こしていた。ユーザーが入力/
    # 選択した表記をそのまま正式名称として使う方が確実(「アーティスト名を
    # そのまま採用する」との明示的な指摘)。artist_idの解決(検索結果の多数決)
    # だけは引き続き行い、曲を取得するチャンネル自体は正しく特定する。
    return artist.strip(), artist_id


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
    results = _search_with_retry(_yt_ja, raw, filter="artists", limit=1)
    resolved = results[0].get("artist") if results else None
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


def fetch_all_tracks(artist_id):
    """playlist_builder.py の get_artist_tracks を踏襲: アーティストの全アルバム/シングル
    収録曲を取得する。アルバムが多いアーティストほど逐次取得が遅くなるため、
    lyrics-quizと同様に並列fetchで高速化する。以前はプロセス内キャッシュも
    持っていたが、修正の反映確認中に古い結果がいつまでも返り続けて紛らわしい
    (サーバーはブラウザのページを閉じても生き続けるため、キャッシュも消えない)
    との指摘があったため撤去した(速度より正確さの確認しやすさを優先)。"""
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

    return raw_tracks


def _fetch_song_details(video_id):
    """view_countとlengthSecondsをまとめて1回のget_song()で取得する
    (再生回数ランキングと、カタログの申告時間との不整合チェックの両方で使う)。"""
    try:
        details = _yt.get_song(video_id).get("videoDetails") or {}
        view_count = int(details.get("viewCount") or 0)
        length_raw = details.get("lengthSeconds")
        length_seconds = int(length_raw) if length_raw else None
        return view_count, length_seconds
    except Exception:
        return 0, None


_DURATION_MISMATCH_THRESHOLD = 20  # 秒。これ以上ズレていたらカタログの動画紐付けが壊れているとみなす


def _is_duration_mismatched(declared_seconds, actual_seconds):
    return bool(
        actual_seconds is not None
        and declared_seconds
        and abs(actual_seconds - declared_seconds) > _DURATION_MISMATCH_THRESHOLD
    )


def _duration_str_to_seconds(s):
    if not s:
        return None
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


_ARTIST_NAME_SYMBOL_RE = re.compile(r"[^\w]", re.UNICODE)


def _artist_name_key(name):
    return _ARTIST_NAME_SYMBOL_RE.sub("", name or "").lower()


def _candidate_matches_artist(target_name, candidate_artists):
    """検索結果が本当に目的のアーティストの曲かどうかを確認する。曲名だけで
    一致判定すると、「フレ!フレ!」のような汎用的な曲名で全く別のアーティストの
    曲に取り違えてしまうことがあった(タイトルと申告時間がたまたま近い曲は
    別アーティストにも普通に存在しうる)。"""
    target_key = _artist_name_key(target_name)
    if not target_key:
        return True
    for a in candidate_artists or []:
        key = _artist_name_key(a.get("name"))
        if key and (key == target_key or key in target_key or target_key in key):
            return True
    return False


def _find_studio_alternative(artist_name, track):
    """ライブ音源判定/カタログの申告時間との不整合で除外対象になった曲について、
    除外する前に同じ曲の別バージョン(スタジオ版など)が無いか直接検索して探す。
    見つかればそちらの情報(videoId/タイトル/再生時間/再生回数)を積んで返し、
    見つからなければNoneを返す(呼び出し側で除外する)。"""
    raw_title = track.get("title", "")
    title_key = normalize_title(raw_title)
    if not title_key:
        return None
    query_title = strip_variant_for_display(clean_title(raw_title))
    query = f"{artist_name} {query_title}".strip()
    try:
        results = _yt_ja.search(query, filter="songs", limit=10)
    except Exception:
        results = []
    for r in results:
        vid = r.get("videoId")
        if not vid or vid == track.get("videoId"):
            continue
        cand_title = r.get("title") or ""
        if normalize_title(cand_title) != title_key or is_live_recording(cand_title):
            continue
        if not _candidate_matches_artist(artist_name, r.get("artists")):
            continue
        view_count, actual_len = _fetch_song_details(vid)
        declared = _duration_str_to_seconds(r.get("duration")) or actual_len
        if _is_duration_mismatched(declared, actual_len):
            continue
        new_track = dict(track)
        new_track["videoId"] = vid
        new_track["title"] = cand_title
        if declared:
            new_track["duration_seconds"] = declared
        return new_track, view_count, actual_len
    return None


def _resolve_track_quality(artist_name, track):
    """ライブ音源(曲名にlive/ライブ等を含む)や、カタログの申告時間と実際の動画の
    長さが大きくズレている(データ不整合で別テイクに紐付いている)曲を検出し、
    除外する前にスタジオ版などの代わりを検索して差し替えを試みる。
    戻り値は (曲, 再生回数) のタプルで、除外する場合は (None, 0)。"""
    view_count, actual_len = _fetch_song_details(track.get("videoId"))
    flagged = is_live_recording(track.get("title", "")) or _is_duration_mismatched(
        track.get("duration_seconds"), actual_len
    )
    if not flagged:
        return track, view_count
    alt = _find_studio_alternative(artist_name, track)
    if not alt:
        return None, 0
    new_track, alt_view_count, _ = alt
    return new_track, alt_view_count


_VIEWS_STR_RE = re.compile(r"([\d.,]+)\s*([KM]?)", re.IGNORECASE)


def _parse_views_str(s):
    """カタログのtrack辞書に既に入っている"views"表記(例:"268K plays"、
    "6.2M plays"、"1,234 plays")を概算の整数に変換する。曲数の多い
    アーティストで曲ごとに再生回数を取得し直す(1曲1通信)のは重すぎ、
    Render環境ではタイムアウトして0曲になってしまうことがあった
    (私立恵比寿中学のTop25/Top50で発生)。この概算値を使えば通信無しで
    ランキングの絞り込みができ、実際の通信(_resolve_track_quality)は
    絞り込んだ後の候補だけで済む。"""
    if not s:
        return 0
    m = _VIEWS_STR_RE.search(s)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    suffix = m.group(2).upper()
    if suffix == "K":
        num *= 1_000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


# 再生回数(概算)による絞り込み後、実際に通信して正確な再生回数・品質確認を
# 行う候補数の上限。Top25/Top50どちらでもこれだけあれば、埋め込み不可な曲の
# 補充分も含めて十分足りる。
_RANKED_QUALITY_CHECK_LIMIT = 120


def fetch_ranked_tracks(artist_id, artist_name):
    """Top25/Top50用のランキング。YouTube Music公式の「人気の曲」プレイリストは
    使わず、全曲(fetch_all_tracks)について曲ごとに再生回数を取得し、
    その再生回数の多い順に独自にランキングする。

    通常版/ショートバージョンのような表記ゆれ重複は、_clean_and_dedupe(全曲一覧用)
    が使う「カッコ書きのないプレーンなタイトルを優先する」という表示用ヒューリスティック
    ではなく、実際の再生回数が高い方を勝者(=ランキング算出に採用する動画)として残す。
    ただし表示するタイトルは勝者の実タイトルではなく、strip_variant_for_displayで
    整形したプレーンタイトルにする(例: 「仮契約のシンデレラ（ショートバージョン）」が
    勝者でも、表示上は「仮契約のシンデレラ」にする)。

    曲名にlive/ライブを含む曲や、カタログの申告時間と実際の動画の長さが大きく
    ズレている曲(データ不整合)は、_resolve_track_qualityでスタジオ版などの
    代わりへの差し替えを試み、見つからなければ除外する。

    正確な再生回数の取得(1曲ごとに通信)は、曲数の多いアーティストだと全曲分
    行うには重すぎるため、まずカタログに既に入っている概算のviews表記だけで
    絞り込んでから、その上位候補だけ正確な値を取りに行く2段階構成にしている。"""
    raw = [t for t in fetch_all_tracks(artist_id) if t.get("videoId") and t.get("title")]
    raw = filter_instrumental(raw)
    raw = filter_medley(raw)
    candidates = []
    for t in raw:
        t = dict(t)
        t["title"] = clean_title(t["title"])
        t["_approxViewCount"] = _parse_views_str(t.get("views"))
        candidates.append(t)

    candidates = _dedupe_by_video_id_prefer_native(candidates)

    # 概算の再生回数だけでグルーピング・仮の勝者選出を行う(通信不要)。
    # MV(OMV)は曲が始まる前に別シーンが入っていることがあるため、同じ曲で
    # あれば音源版(ATV)を優先する(再生回数より優先する明示的な指摘があった)。
    approx_groups = defaultdict(list)
    for t in candidates:
        approx_groups[_dedupe_group_key(t["title"])].append(t)
    approx_winners = [
        max(
            {t["videoId"]: t for t in group}.values(),
            # Remix/TV Sizeなどのバージョン表記が付いた動画より、素のタイトルの
            # 動画を優先する(同じ曲が"-Moe Shop Remix-"等の別動画として重複した
            # まま残ってしまう不具合の対策)。
            key=lambda t: (0 if _is_omv(t) else 1, 0 if _has_variant_qualifier(t["title"]) else 1, t["_approxViewCount"]),
        )
        for group in approx_groups.values()
    ]
    approx_winners.sort(key=lambda t: t["_approxViewCount"], reverse=True)
    shortlist = approx_winners[:_RANKED_QUALITY_CHECK_LIMIT]

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolved = list(pool.map(lambda t: _resolve_track_quality(artist_name, t), shortlist))

    tracks = []
    for new_track, view_count in resolved:
        if new_track is None:
            continue
        new_track["_viewCount"] = view_count
        tracks.append(new_track)

    groups = defaultdict(list)
    for t in tracks:
        groups[_dedupe_group_key(t["title"])].append(t)

    winners = []
    for group in groups.values():
        unique = list({t["videoId"]: t for t in group}.values())
        winner = dict(max(unique, key=lambda t: (0 if _is_omv(t) else 1, 0 if _has_variant_qualifier(t["title"]) else 1, t["_viewCount"])))
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
    return ranked


def _resolve_all_tracks_quality(tracks, artist_name):
    """全曲モード用。ライブ音源(曲名にlive/ライブ等を含む)や、カタログの申告時間と
    実際の動画の長さが大きくズレている曲を検出し、除外する前にスタジオ版などの
    代わりを検索して差し替えを試みる(fetch_ranked_tracks内のロジックと同様)。"""
    def resolve_one(t):
        _, actual_len = _fetch_song_details(t.get("videoId"))
        flagged = is_live_recording(t.get("title", "")) or _is_duration_mismatched(
            t.get("duration_seconds"), actual_len
        )
        if not flagged:
            return t
        alt = _find_studio_alternative(artist_name, t)
        if not alt:
            return None
        new_track, _, _ = alt
        return new_track

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolved = list(pool.map(resolve_one, tracks))

    seen_ids = set()
    result = []
    for t in resolved:
        if not t or t["videoId"] in seen_ids:
            continue
        seen_ids.add(t["videoId"])
        result.append(t)
    return result


_VIDEO_TITLE_RE = re.compile(r"[「『]([^」』]+)[」』]")
_LIVE_VIDEO_RE = re.compile(r"live|tour|ライブ|ツアー", re.IGNORECASE)


def fetch_video_tracks(artist_id):
    """アルバム/シングルがYouTube Musicのカタログに登録されていないアーティスト向けの
    フォールバック: アーティストページの「動画」セクション(公式MVなど)のタイトル
    "<アーティスト>「<曲名>」..." から曲名を推測する(lyrics-quizと同じ手法)。"""
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
        ranked = fetch_ranked_tracks(artist_id, canonical_name)  # 全曲を重複解決済み・再生回数降順
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
        tracks = _resolve_all_tracks_quality(_clean_and_dedupe(fetch_all_tracks(artist_id)), canonical_name)
        tracks = _filter_embeddable(tracks)

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
