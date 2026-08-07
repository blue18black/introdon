"""
イントロドン2: 再生音源をYouTube Music(ytmusicapi + yt-dlp)から取得する
試験的な実装。

Deezer/iTunesの30秒プレビューは、曲によっては(配信元がDDEXメタデータで
指定したクリップ切り出し位置の都合で)曲の途中から始まることがある。
DeezerとiTunesで同じ曲が同じように途中から始まる事例が確認されており、
配信元側のメタデータに起因する可能性が高い。YouTube Music上の同一曲を
フル尺で再生できれば、こちらで再生開始位置(0:00)を確実に制御できる。

- 検索はfilter="songs"を指定し、MV扱いの結果("videos")を除外する
  (以前ytmusicapiから離れた理由はMV/ライブ映像混入だったが、
  filter="songs"はそもそも動画扱いの結果を除外できる)。ただしfilter=
  "songs"の結果内にも、同じ曲のMusic Video版(videoType=
  MUSIC_VIDEO_TYPE_OMV)とArt Track版(videoType=MUSIC_VIDEO_TYPE_ATV、
  音声のみ)が別videoIdとして混在することがあるため、ATVかどうかの判定は
  呼び出し側(deezer_service)で行う。ライブ・インストゥルメンタル等の
  除外も呼び出し側の既存のキーワード判定を再利用する。
  曲ごとに「曲名+収録アルバム名(そのシングル/アルバム/EP名)」または
  「曲名+演奏者名」で個別検索する方式を使う(アーティストの全リリースを
  クロールしてトラックリストのvideoIdを拾う方式は、MVのvideoIdしか
  掲載されていないことがあり信頼できないため主要な検索方式としては
  採用していない)。
  ただし、曲名自体は普通の表記でも検索インデックス側の事情でfilter=
  "songs"の検索結果に一切出てこない曲がまれにある(実例:いぎなり東北産
  「線の物語」「ニュートロ」「テキーナ」)。この場合の最終手段として、
  対象の1枚のアルバムだけをfilter="albums"で特定してトラックリストを
  取得し、その中から曲名が一致するトラックを探す(search_albums/
  get_album_tracks)。アーティストの全リリースを対象にした曲の長さだけ
  による突き合わせ(過去に試して誤爆したため廃止済み)とは異なり、
  「対象アルバム1枚に絞った上でなお曲名一致を要求する」ため精度は保てる。
  ただしこの方式で拾ったトラックの中にもMV(videoType=
  MUSIC_VIDEO_TYPE_OMV)が混在することがある(上記いぎなり東北産の例でも
  同アルバム収録の別の曲はMVのvideoIdしか無かった)ため、呼び出し側で
  必ずATV判定を行うこと。
- 再生はyt-dlpでストリームURLをその場で取り直す(Deezer/iTunes同様、
  署名付きURLの失効に対応するため)。SafariがWebM/Opusを再生できない
  ため、m4a(AAC)形式を優先する。

注意: YouTubeから音声ストリームを直接抽出して自前のプレーヤーで再生する
ことは、YouTubeの利用規約上グレーゾーン(公式な用途ではない)。個人利用の
範囲内での試験的な実装であることを理解した上で使うこと。また、YouTube側の
内部仕様変更でyt-dlpが追従できず突然動かなくなることがある(Deezer/iTunes
の公開APIより壊れやすい)。
"""
import threading
import time

from ytmusicapi import YTMusic
import yt_dlp

# location未指定だとリクエスト元IPのジオロケーションから国を推測するため、
# 海外データセンター(Render等)からだと日本限定配信の曲がカタログに出ず
# 検索でヒットしなくなる。サーバーの場所に関係なく日本のカタログで検索
# されるよう明示する。
_yt = YTMusic(language="ja", location="JP")

# Deezer/iTunes側と同様、ytmusicapiの内部リクエストも一時的なネットワーク
# エラー/レート制限で失敗することがあるため、失敗時は間を置いて再試行する。
# 全曲取得時はThreadPoolExecutorで曲ごとに並列検索するため、スレッド数×
# 曲あたり最大数回という短時間の大量リクエストになり、iTunes側で以前
# 経験したのと同じ「一部だけ一時的に失敗し、実行のたびに結果(どの曲が
# YouTube Musicで見つかるか)がブレる」問題が起きていた。iTunes側の_get
# と同様、全スレッド共通で最小間隔を空けるグローバルなスロットルを設ける。
_rate_limit_lock = threading.Lock()
_last_request_at = 0.0
_MIN_REQUEST_INTERVAL_SECONDS = 0.25


def _throttle():
    global _last_request_at
    with _rate_limit_lock:
        now = time.monotonic()
        wait = _last_request_at + _MIN_REQUEST_INTERVAL_SECONDS - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry(fn, retries=4, delay=1.0, default=None):
    """delayは初回リトライ時の待ち時間。レート制限の解除にかかる時間は
    環境(サーバーの回線・IP)によって差があり、固定の短い待ち時間だけでは
    足りずに結局諦めてしまうことがあったため、iTunes側(itunes_service._get)
    と同様にリトライのたびに待ち時間を伸ばす(指数バックオフ)。"""
    for attempt in range(retries + 1):
        _throttle()
        try:
            return fn()
        except Exception:
            if attempt < retries:
                time.sleep(delay * (2 ** attempt))
    return default


def search_songs(query, limit=8):
    """曲名等でYouTube Musicの"songs"(動画扱いのMVを除いた音声トラック)を
    検索する。失敗時は空リスト。"""
    return _retry(lambda: _yt.search(query, filter="songs", limit=limit), default=[]) or []


def search_albums(query, limit=3):
    """アーティスト名+アルバム名でYouTube Musicのアルバム(シングル/EP含む)
    を検索する。曲名検索で見つからない曲の最終手段(対象アルバムのトラック
    リストを直接見に行く)用。失敗時は空リスト。"""
    return _retry(lambda: _yt.search(query, filter="albums", limit=limit), default=[]) or []


def get_album_tracks(browse_id):
    """アルバムのbrowseIdからトラックリストを取得する。失敗時は空リスト。"""
    album = _retry(lambda: _yt.get_album(browse_id))
    return (album or {}).get("tracks") or []


_YDL_OPTS = {
    # SafariはWebM/Opusを再生できないため、AAC(m4a)を優先する。
    "format": "bestaudio[ext=m4a]/bestaudio",
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
}


def get_track_audio(video_id):
    """再生直前に呼び出し、その場で有効なストリームURLを取得する
    (Deezer/iTunes側と同じ「直前に取り直す」設計に合わせる)。"""
    try:
        with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
            info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False)
    except Exception:
        return None
    if not info or not info.get("url"):
        return None
    return {
        "previewUrl": info["url"],
        "durationSeconds": info.get("duration") or 0,
    }
