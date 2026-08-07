"""
イントロドン2 バックエンド: iTunes Search/Lookup APIラッパー。

1) アーティスト名サジェスト
DeezerのsearchAPIは、日本語アーティストの部分一致検索に弱い(例:
「私立恵比寿中学」は全文一致でしか出てこず、「私立恵比寿」「エビ中」等の
部分一致では無関係な結果しか返らない)。iTunes Search API(認証不要・
公開・country=JP指定可)は同じ部分一致でも正しい候補を返せるため、
サジェスト(入力候補)専用にこちらを使う。

また、iTunes Search APIのartistName自体はローマ字化されていることが多い
(例:「Shiritsu Ebisu Chugaku」)が、artistLinkUrl(Apple Music JPの
アーティストページURL)のパスには日本語表記のスラッグがそのまま
埋め込まれていることが多い。これをデコードすることで、ローマ字化されて
いない現地表記のアーティスト名を取得できる。

2) 曲データの補完取得
曲データ自体の取得は引き続きDeezer(deezer_service.py)が主(動画を
扱わないカタログのためMV混入が起きない、rank(人気度)があるためTop25/
Top50の並び順を作れる、という利点があるため)。ただしDeezerのカタログには
稀に曲そのものが配信されていない欠落があるため、Deezer側で見つからな
かった曲だけをiTunes側から補って埋める(deezer_service.get_artist_tracks
参照)。iTunesにはrankに相当する人気度指標が無いため、Top25/Top50の
並び順の基準には使わず、あくまで「全曲」の穴埋め用。

iTunesの/lookupはentity=songで直接アーティストの曲一覧を取れるが、
1アーティストあたり最大200件という上限がありページングもできない。
Deezerと同じく「アーティスト→アルバム一覧→アルバムごとの曲一覧」の
形で辿ることでこの上限を回避する。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse

import requests

_SEARCH_API_BASE = "https://itunes.apple.com/search"
_LOOKUP_API_BASE = "https://itunes.apple.com/lookup"

_session = requests.Session()
_session.headers.update({"User-Agent": "introdon2/1.0"})

# iTunes側の緩いレート制限に、アルバムごとの全曲取得+曲ごとの個別検索
# フォールバックを合わせるとすぐに達してしまい、実行のたびに結果件数が
# 変わる原因になっていた。スレッド数(ThreadPoolExecutor)で並列数を抑える
# だけでは「一定期間内の総リクエスト数」という制限には効かないため、
# 全スレッド共通で最小間隔を空けるグローバルなスロットルを設ける。
_rate_limit_lock = threading.Lock()
_last_request_at = 0.0
_MIN_REQUEST_INTERVAL_SECONDS = 0.15


def _throttle():
    global _last_request_at
    with _rate_limit_lock:
        now = time.monotonic()
        wait = _last_request_at + _MIN_REQUEST_INTERVAL_SECONDS - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _get(url, params, retries=4):
    """iTunes側は緩いレート制限があり、特にアルバム数の多いアーティストの
    全曲取得時(アルバムごとに何十回もリクエストする)に一部だけ一時的に
    失敗することがあった。これが実行のたびに結果件数が変わる原因になって
    いたため、Deezer側の_getと同様に少し間を置いて再試行する。また、
    レート制限にかかった応答(403等)がエラーにならず空のJSONとして
    静かに返ってくることがあり、それを「結果0件」と誤解して見逃していた
    ため、ステータスコードも明示的に確認する。

    レート制限の解除にかかる時間は環境(サーバーの回線・IP)によって差が
    あり、固定の短い待ち時間だけでは足りずに結局そのアルバム分を
    諦めてしまうことがあった(_ARTIST_MERGE_GROUPSでiTunes側の追加
    アーティストIDを束ねるようになり、1アーティストあたりのリクエスト数が
    倍増したことで発生頻度が上がった)。retriesを増やし、待ち時間も
    リトライのたびに伸ばす(指数バックオフ)ことで、レート制限が長引く
    場合でも回復を待ちやすくする。"""
    last_exc = None
    for attempt in range(retries + 1):
        _throttle()
        try:
            res = _session.get(url, params=params, timeout=10)
            if res.status_code != 200:
                raise requests.HTTPError(f"unexpected status {res.status_code}")
            return res.json()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.6 * (2 ** attempt))
    if last_exc:
        return None
    return None


# artistLinkUrlのスラッグは、♡のようなURLに使えない記号を単純に取り除いて
# ハイフンへ置き換えてしまう(例:「超ときめき♡宣伝部」→「超ときめき-宣伝部」)
# ため、デコードしただけでは正式表記に戻らないアーティストがいる。既知の
# ものはここで正式表記へ補正する。
_SLUG_NAME_OVERRIDES = {
    "超ときめき-宣伝部": "超ときめき♡宣伝部",
    "ときめき-宣伝部": "ときめき♡宣伝部",
}


def _extract_native_name(artist_link_url, fallback_name):
    """artistLinkUrlのパス中のアーティスト名スラッグをデコードして返す。
    スラッグが非ASCII文字(日本語等)を含む場合のみそれを採用し、
    "ayase"のようにローマ字表記が芸名そのものである場合はartistNameを
    そのまま使う(ハイフン区切り等をスペースへ戻す処理はしないため、
    デコード結果をそのまま名前として使えるのは非ASCIIの場合のみ)。"""
    try:
        parts = [p for p in urlparse(artist_link_url).path.split("/") if p]
        slug = parts[parts.index("artist") + 1]
        decoded = unquote(slug)
        if decoded in _SLUG_NAME_OVERRIDES:
            return _SLUG_NAME_OVERRIDES[decoded]
        if any(ord(ch) > 127 for ch in decoded):
            return decoded
    except (ValueError, IndexError):
        pass
    return fallback_name


def search_artist_suggestions(query, limit=8):
    """検索欄への入力に応じたアーティスト名サジェストを返す。失敗時は
    空リストを返す(呼び出し元は入力欄のライブ検索なので、エラーで
    落とすより「候補なし」と同等に扱う方が体験を損なわない)。"""
    query = (query or "").strip()
    if not query:
        return []
    data = _get(
        _SEARCH_API_BASE,
        {"term": query, "entity": "musicArtist", "limit": limit, "country": "JP"},
    )
    if not data:
        return []

    results = data.get("results", [])
    seen = set()
    names = []
    for r in results:
        name = _extract_native_name(r.get("artistLinkUrl"), r.get("artistName"))
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


# ---- 曲データの補完取得(Deezerに無い曲を埋める用) ----

def search_songs(term, limit=5):
    """曲名等でiTunesの曲を直接検索し、結果をそのまま返す(曲名が一致する
    かどうかの判定は呼び出し側で行う)。曲ごとにDeezer側に記録されている
    実際の演奏者名(メンバーソロ名義・旧名義等)で検索することで、1つの
    アーティストページ(find_artist_id)の範囲に縛られず、分裂登録された
    別名義の曲も見つけられる。失敗時は空リスト。"""
    data = _get(
        _SEARCH_API_BASE,
        {"term": term, "entity": "song", "limit": limit, "country": "JP"},
    )
    return (data or {}).get("results", [])


def find_artist_id(display_name):
    """曲データ取得用に、アーティスト名からiTunesのartistIdを解決する。
    検索結果の1件目を採用する(サジェストと違って既に正規化済みの表示名を
    渡す前提のため、単純な最上位一致で十分)。見つからなければNone。"""
    data = _get(
        _SEARCH_API_BASE,
        {"term": display_name, "entity": "musicArtist", "limit": 1, "country": "JP"},
    )
    results = (data or {}).get("results", [])
    return results[0]["artistId"] if results else None


def _fetch_artist_albums(artist_id):
    data = _get(
        _LOOKUP_API_BASE,
        {"id": artist_id, "entity": "album", "limit": 200, "country": "JP"},
    )
    results = (data or {}).get("results", [])
    return [r for r in results if r.get("wrapperType") == "collection"]


def _fetch_album_tracks(album):
    data = _get(
        _LOOKUP_API_BASE,
        {"id": album["collectionId"], "entity": "song", "country": "JP"},
    )
    results = (data or {}).get("results", [])
    tracks = [r for r in results if r.get("wrapperType") == "track"]
    for t in tracks:
        t["_album_title"] = album.get("collectionName")
        t["_album_cover"] = album.get("artworkUrl100")
    return tracks


def fetch_all_tracks_raw(artist_id, on_progress=None):
    """アーティストの全曲を生データのまま返す。1アルバムの取得に失敗しても
    (レート制限等)、そのアルバム分だけ欠けるだけで全体は失敗させない
    (あくまでDeezerの補完用のため、多少取りこぼしても実害が小さい)。
    iTunesの緩いレート制限を考慮し、Deezer側より並列数を抑える。
    on_progress(current, total)は、アルバムの取得が1件終わるたびに呼ばれる
    (進捗表示用)。省略可。"""
    albums = _fetch_artist_albums(artist_id)
    total = len(albums)
    completed = 0
    lock = threading.Lock()

    def handle(album):
        nonlocal completed
        tracks = _fetch_album_tracks(album)
        if on_progress:
            with lock:
                completed += 1
                on_progress(completed, total)
        return tracks

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(handle, albums))

    raw = []
    seen_ids = set()
    for tracks in results:
        for t in tracks:
            if t.get("trackId") in seen_ids:
                continue
            seen_ids.add(t.get("trackId"))
            raw.append(t)
    return raw


def get_track_audio(track_id):
    """再生直前に呼び出し、その場で有効なプレビュー音源URLを取得する
    (Deezer側と同じ「直前に取り直す」設計に合わせる。iTunesのpreviewUrlの
    失効有無は保証されていないため、念のため同じ扱いにしておく)。"""
    data = _get(_LOOKUP_API_BASE, {"id": track_id, "country": "JP"})
    results = (data or {}).get("results", [])
    if not results or not results[0].get("previewUrl"):
        return None
    track = results[0]
    return {
        "previewUrl": track["previewUrl"],
        "durationSeconds": round((track.get("trackTimeMillis") or 0) / 1000),
    }
