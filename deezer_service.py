"""
イントロドン2 バックエンド: Deezer公開APIラッパー。

以前はytmusicapi(YouTube Musicの非公式API)を使っていたが、YouTube Music側の
カタログにはミュージックビデオ(MV)やライブ映像が音源と同列に混ざっており、
「曲が始まる前に別シーンが入るMV」や「歓声・演奏ノイズの混じるライブ音源」が
出題されてしまうことがあった。Deezerは音楽ストリーミングサービスの公開API
(認証不要・無料・公開ドキュメントあり)で、そもそも動画を一切扱わない音声のみの
カタログのため、MVが紛れ込む問題が構造的に起きない。また各トラックにはタイトル
本体と版表記(Live/Remix等)が"title"/"title_version"として別フィールドで
分離されているため、ライブ/インスト等の版違いも文字列パースに頼らず確実に検出できる。
"""
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

import itunes_service
import ytmusic_service

_API_BASE = "https://api.deezer.com"


def _make_session():
    session = requests.Session()
    # Accept-Languageを指定しないと、リクエスト元IPのジオロケーションから
    # 言語/カタログ版を推測するため、海外データセンター(Render等)からだと
    # 日本語表記があるはずの曲がローマ字表記(国際流通版)で返ってくることが
    # ある(例:超ときめき♡宣伝部「青春アンセム」がSeishun Anthem表記になる)。
    # サーバーの場所に関係なく日本語表記を優先させる。
    session.headers.update({
        "User-Agent": "introdon2/1.0",
        "Accept-Language": "ja-JP,ja;q=0.9",
    })
    return session


_session = _make_session()


def _get(path, params=None, retries=2):
    """DeezerのAPIは短時間に集中アクセスするとレート制限で一時的に失敗することが
    あるため、間を置いて再試行する。レート制限時はHTTPレベルの例外ではなく
    {"error": ...}という成功扱いのJSON応答が返ってくることがあり、以前は
    これを例外側の再試行ループに乗せずに即座に諦めていた(実行のたびに
    取得できるアルバム・曲数が変わる原因になっていた)。エラー応答も
    再試行対象に含める。"""
    url = f"{_API_BASE}{path}"
    for attempt in range(retries + 1):
        try:
            res = _session.get(url, params=params, timeout=8)
            data = res.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("error"):
            data = None
        if data is not None:
            return data
        if attempt < retries:
            time.sleep(0.4)
    return None


UNWANTED_VERSION_KEYWORDS = [
    "live", "ライブ", "instrumental", "インスト", "off vocal", "offvocal",
    "オフボーカル", "karaoke", "カラオケ", "backing track", "acapella", "a cappella",
    "アカペラ", "less vocal", "lessvocal", "interlude", "インタールード",
    "english ver", "korean ver",
]

# アイドルグループの曲名装飾によく使われる記号(グループ名自体に♡を含む
# 「超ときめき♡宣伝部」等)。配信元によってこの手の記号が入っていたり
# 抜けていたりする表記ゆれがある(実例:「季節外れのときめき♡サマー」
# (iTunes)と「季節外れのときめきサマー」(Deezer)が別の曲として重複
# 登録されてしまっていた)。他の記号と違いこれらは単語の区切りとしての
# 意味を持たない(前後にスペースが入るとは限らない)装飾目的の記号なので、
# 通常の記号(スペースに置換)とは別に、正規化キーからは完全に取り除く。
_DECORATIVE_SYMBOLS_RE = re.compile(r"[♡★☆♪♫✩✧]")
_LIVE_TITLE_RE = re.compile(r"\blive\b|ライブ", re.IGNORECASE)
# "LET'S LIVE！"(つばきファクトリー)のように、"live"が単語境界的には
# ライブ版表記に見えてしまうが実際は曲名そのものの一部というケースがある。
# 一般化した判定は誤爆リスクが高いため、確認済みの曲名だけを個別に除外する。
_LIVE_KEYWORD_FALSE_POSITIVES = {"LET'S LIVE！", "LET'S LIVE!", "Let's Live！", "Let's Live!"}
_MEDLEY_TITLE_RE = re.compile(r"\S/\S")
# "メドレー1(曲A・曲B)"(Juice=Juice)や"メドレー(曲A･曲B)(コンサートツアー...)"
# (つばきファクトリー)のように、"メドレー"の直後が数字または括弧のものを
# まとめて検出する。
_MEDLEY_NUMBERED_RE = re.compile(r"^メドレー[\d\(（]")
# "曲名(Concert 2025 ...)"や"曲名(コンサートツアー 2023秋 ...)"、
# "曲名 (BLACKPINK ARENA TOUR 2018 ...)"のように、括弧内にConcert/Tour表記
# (英語表記・カタカナ表記どちらも)があるものはコンサート/ツアー(ライブ)
# 音源の版であることを示す。"/ ROSE (...TOUR...)"のようにスラッシュ
# クレジットが挟まる場合もあるが、括弧の中身自体を見るのでどちらでも拾える。
# _LIVE_TITLE_REの"live"/"ライブ"だけでは拾えないため別途判定する。tourは
# "detour"等への誤爆を避けるため単語境界付きで判定する。
_CONCERT_IN_BRACKETS_RE = re.compile(
    r"[\(\[（【][^)\]）】]*(?:\bconcert\b|コンサート|\btour\b)[^)\]）】]*[\)\]）】]", re.IGNORECASE
)
# "曲名(2023)"や"曲名(2023)／メンバー名・メンバー名"のように、末尾に
# 西暦年だけの括弧(+任意でその年に歌ったメンバーのクレジット)が付いた
# 歌い直し版(Juice=Juiceの周年記念アルバム"Juicetory"に実例あり)。
# 素のタイトルが別途カタログに存在するアニバーサリー再録版のため除外する。
_YEAR_VERSION_SUFFIX_RE = re.compile(r"\(\d{4}\)(／.+)?$")
# "曲名 - メンバー別ミックス名/リミックス名 -"や"曲名 -TV Size-"、
# "曲名-Moe Shop Remix-"のように、版表記がtitle_versionではなくtitle_short
# 本体の末尾に直接埋め込まれていることがある(アイドルグループのメンバー別
# ソロミックス、TVサイズ版、リミックス版等でよく見られる)。区切りのハイフン
# の前後のスペースの有無は表記ゆれがある("- Remix -" "- Remix-" "-Remix-"
# 全パターンが実在する)ため、いずれも任意とする。末尾が"-"で終わる場合に
# 限り、この部分を版表記とみなす。
_TRAILING_VERSION_SUFFIX_RE = re.compile(r"-\s*.+-\s*$")
# "季節外れのときめき♡サマー ときくり盤"のように、CDシングルの複数プレス
# 版(いわゆる"○○盤")がスペース区切りでtitle_short末尾に付与されている
# ことがある。同じリード曲が"ぴょんぴょん盤"「どんどん盤」等プレス版の数だけ
# 重複して登録されるため、この部分も版表記とみなす。エディション名はかな
# 表記が定番のため、誤爆を避けるためひらがな・カタカナのみに限定する。
_EDITION_SUFFIX_RE = re.compile(r"\s+[ぁ-んァ-ヶー]+盤\s*$")
# "BATTER UP -JP Ver."のように、閉じ側のハイフンが無い"-XX Ver."形式の版表記
# (BABYMONSTERに実例あり)。_TRAILING_VERSION_SUFFIX_REは両端ハイフンの
# 形式のみ対応のため、この形式は素通りしてしまい、重複排除時に別の曲として
# 扱われてしまっていた(例:"BATTER UP"と"BATTER UP -JP Ver."が別グループに
# なり、ランキング(rank)がたまたま高いJP Ver.の方が誤って残ってしまう)。
_TRAILING_LANG_VER_SUFFIX_RE = re.compile(r"\s*-\s*[A-Za-z]{2,6}\s*ver\.?\s*$", re.IGNORECASE)


# clean_title/extract_english_titleの「日本語→英語表記の重複」判定は、
# 続く部分が"純ASCII"かどうかで英題/ローマ字表記を見分けている。しかし
# YouTube Music側の英題には"It's"のような気の利いた引用符(カーブ
# クォート、U+2019等)が使われることがあり、これはASCII範囲外のため
# 誤って「まだ日本語タイトルの続き」と判定され、切り捨てられずに残って
# しまっていた(実例:いぎなり東北産の「さいしょのシングルです！ - It's
# our first single!」で"'"がU+2019だったため一致確認に失敗していた)。
# こうした定番の装飾記号はASCII相当として扱う。
_SMART_PUNCTUATION = "‘’“”–—…"


def _is_ascii_like(ch):
    return ord(ch) <= 127 or ch in _SMART_PUNCTUATION


def _is_ascii_like_text(text):
    return all(_is_ascii_like(ch) for ch in text)


def _is_bracket_balanced(text):
    counts = {"(": 0, ")": 0, "（": 0, "）": 0, "[": 0, "]": 0}
    for ch in text:
        if ch in counts:
            counts[ch] += 1
    return counts["("] == counts[")"] and counts["（"] == counts["）"] and counts["["] == counts["]"]


def clean_title(title):
    """Deezerのtitle_shortには、"ピースサイン - Peace Sign"のように日本語
    タイトルに英題/ローマ字表記が" - "区切りで付与されたまま入っていることが
    ある。" - "で区切り、非ASCII文字を含む部分(=まだ本来のタイトルの続き)が
    続く限り残し、純ASCIIの部分(=英題/ローマ字の重複表記)が出た時点で
    切り捨てる。

    ただし"(Memorial Edit - Instrumental)"のように、括弧の中にたまたま
    " - "が含まれる版表記もある。この場合に単純split木するとparts[0]が
    括弧を閉じないまま切れてしまい("...(Memorial Edit")、括弧内の
    "Instrumental"等の重要なキーワードごと失われてしまう
    (_has_unwanted_keywordがinstrumental版を検出できなくなる実害があった)。
    parts[0]の時点で括弧の対応が崩れている場合は、日本語→英語の重複表記
    ではなく括弧内の区切りとみなし、切り捨てを行わず元のタイトルのまま
    返す。"""
    if not title:
        return title
    parts = [p for p in title.split(" - ") if p.strip()]
    if not parts:
        return title.strip()
    if _is_ascii_like_text(parts[0]):
        return title.strip()
    if not _is_bracket_balanced(parts[0]):
        return title.strip()
    kept = [parts[0]]
    for part in parts[1:]:
        if not _is_ascii_like_text(part):
            kept.append(part)
        else:
            break
    return " - ".join(kept).strip() or title.strip()


def extract_english_title(title):
    """clean_title()が切り捨てる" - "以降の英題/ローマ字表記部分を取り出す
    (例:"ピースサイン - Peace Sign"→"Peace Sign")。YouTube Music側に
    日本語タイトルではなくこちらの英題/ローマ字表記でしか曲が登録されて
    いない場合、通常の日本語タイトルでの検索では見つからない。そうした
    曲向けの検索クエリ用フォールバックとして使う。無ければNone。"""
    if not title:
        return None
    parts = [p for p in title.split(" - ") if p.strip()]
    if len(parts) < 2:
        return None
    if _is_ascii_like_text(parts[0]):
        return None
    kept_count = 1
    for part in parts[1:]:
        if not _is_ascii_like_text(part):
            kept_count += 1
        else:
            break
    dropped = parts[kept_count:]
    return " - ".join(dropped).strip() or None


def _has_unwanted_keyword(text):
    """"(instrumental)"のような版表記が、title_version(別フィールド)ではなく
    title_short(曲名本体)に直接埋め込まれたまま入っていることがあるため、
    title_version・title_short どちらに対しても同じキーワード判定を使う。
    liveは"olive"等への誤爆を避けるため単語境界付きの正規表現で、それ以外は
    部分一致でよい(誤爆しにくい語のみを対象にしているため)。"""
    if not text:
        return False
    if text.strip() in _LIVE_KEYWORD_FALSE_POSITIVES:
        return False
    if _LIVE_TITLE_RE.search(text):
        return True
    t = text.lower()
    return any(k in t for k in UNWANTED_VERSION_KEYWORDS)


def _title_version_is_unwanted(title_version):
    return _has_unwanted_keyword(title_version)


def _base_title_is_unwanted(title):
    return _has_unwanted_keyword(title)


def _has_concert_in_brackets(title):
    return bool(_CONCERT_IN_BRACKETS_RE.search(title or ""))


def _has_year_version_suffix(title):
    return bool(_YEAR_VERSION_SUFFIX_RE.search(title or ""))


def _album_indicates_live(album_title):
    """曲名自体には版表記が一切無いのに、収録アルバムがライブ盤/コンサート・
    ツアー音源であることがある(実例:BLACKPINKの"Don't Know What To Do"が
    "BLACKPINK 2019-2020 WORLD TOUR IN YOUR AREA -TOKYO DOME- (Live)"という
    アルバムに、版表記の無いスタジオ版と全く同じtitle_shortで収録されて
    いた)。この場合、曲名ベースの判定(_has_unwanted_keyword等)だけでは
    ライブ版だと検出できず、しかも重複排除時に人気度(rank)がたまたま
    スタジオ版より高いとライブ版の方が誤って選ばれてしまう。そのため
    アルバム名自体も確認する。

    アルバム名中の"TOUR"は、曲名の場合と違い括弧で囲まれているとは限らない
    (例:"2025 BABYMONSTER 1st WORLD TOUR <HELLO MONSTERS> IN JAPAN ...")。
    アルバム名がツアー/コンサート音源そのものを指す場合、"TOUR"は括弧の
    有無によらずほぼ確実にライブ盤を意味するため、_has_concert_in_brackets
    (括弧内のみ判定)とは別に、アルバム名全体に対して単語境界付きで判定する。
    """
    if not album_title:
        return False
    if _LIVE_TITLE_RE.search(album_title):
        return True
    if re.search(r"\btour\b", album_title, re.IGNORECASE):
        return True
    return _has_concert_in_brackets(album_title)


def is_medley_title(title):
    """「曲A/曲B」のように1トラックに複数曲がまとまっているものを除外する。
    「メドレー1(曲A・曲B・…)」「メドレー2(…)」のように"・"区切りで複数曲を
    まとめたコンサート編集版も同様に除外する(Juice=Juiceのコンサート
    アルバムに実例あり)。"""
    title = title or ""
    return bool(_MEDLEY_TITLE_RE.search(title)) or bool(_MEDLEY_NUMBERED_RE.match(title))


def strip_trailing_version_suffix(title):
    """title_short末尾の"- ... -"形式やCDプレス版("○○盤")の版表記を
    取り除いた形を返す(表示用のタイトルそのものは書き換えない、重複判定・
    素のタイトル判定専用)。"""
    if not title:
        return title
    for pattern in (_TRAILING_VERSION_SUFFIX_RE, _EDITION_SUFFIX_RE, _TRAILING_LANG_VER_SUFFIX_RE):
        m = pattern.search(title)
        if m:
            return title[: m.start()].strip()
    return title


def title_has_embedded_version_marker(title):
    """"サクラ・ゴーラウンド(アニメver.)"や"曲名 - TV Size -"のように、
    版表記がtitle_version(別フィールド)ではなくtitle_short本体に
    括弧付き・ハイフン付きで直接埋め込まれているかどうかを判定する。
    normalize_key側は重複グループ化のためにこれらを取り除くが、
    _pick_winnerではこの判定を使って「素のタイトル」を優先する。"""
    if not title:
        return False
    if strip_trailing_version_suffix(title) != title.strip():
        return True
    return bool(re.search(r"[\(\[（【].*?[\)\]）】]", title))


def normalize_key(title_short):
    """重複検出用の正規化キー。Deezerのtitle_shortは既にバージョン表記が
    title_versionへ分離済みのことが多いが、念のため記号・大小文字ゆれを吸収する。
    括弧内容ごと取り除くため、同一アーティストの版違い(インスト・アニメver.等)を
    まとめる用途には適するが、他アーティストの原曲と誤って同一視してしまう
    リスクがある(検索結果の一致確認にはsearch_match_keyを使うこと)。

    比較前にUnicode正規化形式をNFCに揃える。iTunes Search APIは濁点・
    半濁点付きの仮名を結合文字(基底文字+濁点記号)のNFD形式で返すことが
    あり、Deezer側の合成済み文字(NFC)と見た目は同じでも文字列としては
    一致しない(実例:「地団駄ダンス」がDeezer/iTunesで別の曲として重複
    登録されてしまっていた)。

    記号は削除ではなくスペースに置換してから連続スペースを畳む。単純削除
    すると、例えば"Love,Maybe"(カンマの後にスペース無し)は"lovemaybe"に、
    "Love, Maybe"(スペース有り)は"love maybe"になり、どちらも同じ曲なのに
    別のキーになってしまっていた(実例:BABYMONSTERの"Love,Maybe"がDeezer
    とYouTube Musicの表記ゆれで不一致になっていた)。

    ♡等の装飾記号(_DECORATIVE_SYMBOLS_RE参照)は、他の記号と違いスペースに
    置換せず完全に削除する(実例:超ときめき♡宣伝部の「季節外れのときめき
    ♡サマー」(iTunes)と「季節外れのときめきサマー」(Deezer)が別の曲として
    重複登録されてしまっていた)。

    西暦+verの間のスペース有無も表記ゆれがある(実例:いぎなり東北産の
    「2020ver.」(Deezer)と「2020 ver.」(iTunes)が別の曲として重複登録
    されてしまっていた)ため、数字の直後に"ver"が続く場合は必ずスペースを
    挟むよう正規化する。"""
    t = unicodedata.normalize("NFC", title_short or "").lower()
    t = re.sub(r"[\(\[（【].*?[\)\]）】]", "", t)
    t = re.sub(r"feat\.?.*", "", t)
    t = strip_trailing_version_suffix(t)
    t = _DECORATIVE_SYMBOLS_RE.sub("", t)
    t = re.sub(r"(\d)(ver)\b", r"\1 \2", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def search_match_key(title_short):
    """外部サービス(YouTube Music/iTunes)の検索結果が、探している曲と
    本当に一致するかどうかの確認専用の正規化キー。normalize_keyと違い、
    括弧内の版表記(例:"なにがなんでも(エビ中ver.)"の"エビ中ver."部分)は
    保持したまま比較する。normalize_keyの括弧除去は、"同一アーティストの
    版違い"をまとめる分にはよいが、検索結果の一致確認にそのまま使うと、
    括弧書きのカバー版表記が失われて無関係な原曲アーティストの同名異曲
    (括弧無しタイトル)を誤って一致させてしまうことがある(実例:「なにが
    なんでも(エビ中ver.)」で検索した際、原曲アーティスト「PAN」の
    「なにがなんでも」を誤って一致させていた)。normalize_key同様、比較前に
    Unicode正規化形式をNFCに揃える(iTunesが濁点付き仮名をNFD=結合文字で
    返すことがあるため)。normalize_key同様、記号は削除ではなくスペースに
    置換してから畳む(カンマ等の前後のスペース有無による表記ゆれ対策)。
    ♡等の装飾記号もnormalize_key同様、完全に削除する。西暦+verの間のスペース
    有無もnormalize_key同様に正規化する。"""
    t = unicodedata.normalize("NFC", title_short or "").lower()
    t = re.sub(r"feat\.?.*", "", t)
    t = strip_trailing_version_suffix(t)
    t = _DECORATIVE_SYMBOLS_RE.sub("", t)
    t = re.sub(r"(\d)(ver)\b", r"\1 \2", t)
    t = re.sub(r"[^\w\s]", " ", t)  # 括弧の記号自体は消えるが、中の文字は残る
    t = re.sub(r"\s+", " ", t).strip()
    return t


_UNIT_ALBUM_TITLE_MARKER = "ユニットアルバム"
_UNIT_ALBUM_CREDIT_SUFFIX_RE = re.compile(r"^(.+?)/\S.*$")


def _strip_unit_album_credit_suffix(title):
    """私立恵比寿中学の「エビ中のユニットアルバム」シリーズでは、1トラックの
    タイトルが"曲名/演奏ユニット名(またはメンバー名)"の形式になっている
    (例: "いつかのメイドインジャピャ〜ン/くっつきブンブン" は2曲を繋いだ
    メドレーではなく、「くっつきブンブン」というユニットが歌う単独曲)。
    is_medley_titleの"曲A/曲B"判定に誤って引っかかってしまうため、この
    アルバムシリーズに限り"/"以降のクレジット部分を取り除いて曲名のみ残す。"""
    m = _UNIT_ALBUM_CREDIT_SUFFIX_RE.match(title or "")
    return m.group(1).strip() if m else title


def _clean_titles_in_place(tracks):
    for t in tracks:
        # iTunes Search APIは濁点・半濁点付きの仮名をNFD(結合文字)で返す
        # ことがあり、Deezer側のNFC(合成済み文字)表記と見た目は同じでも
        # 文字列としては不一致になる(normalize_key/search_match_key参照)。
        # 表示用のtitle_shortもここでNFCへ揃えておく。
        raw_title_short = unicodedata.normalize("NFC", t.get("title_short") or "")
        t["_english_title"] = extract_english_title(raw_title_short)
        title = clean_title(raw_title_short)
        if _UNIT_ALBUM_TITLE_MARKER in (t.get("_album_title") or ""):
            title = _strip_unit_album_credit_suffix(title)
        t["title_short"] = title


# Deezerが"readable: false"を返すが、実際にはpreview URLも人気度(rank)も
# 正常に入っている(単なるDeezer側のフラグ誤り/地域配信設定の一時的な
# 不整合と見られる)ため、readableチェックを個別に無効化するtrack_id
# (文字列)。実例:BABYMONSTERの"BATTER UP"(album="BATTER UP"、素の版)が
# readable=falseとなっており、そのままだと重複排除の際にreadable扱いされる
# "BATTER UP -JP Ver."の方が誤って残ってしまっていた。
_FORCE_READABLE_TRACK_IDS = {
    "2553454952",  # BATTER UP (BABYMONSTER, album="BATTER UP")
}


def _track_is_usable(track):
    if not track.get("id") or not track.get("title_short"):
        return False
    if not track.get("preview"):
        return False
    if track.get("readable") is False and str(track.get("id")) not in _FORCE_READABLE_TRACK_IDS:
        return False
    if _title_version_is_unwanted(track.get("title_version")):
        return False
    if _base_title_is_unwanted(track.get("title_short")):
        return False
    if is_medley_title(track.get("title_short")):
        return False
    if _has_concert_in_brackets(track.get("title_short")):
        return False
    if _has_year_version_suffix(track.get("title_short")):
        return False
    if _album_indicates_live(track.get("_album_title")):
        return False
    return True


def _pick_winner(group):
    """同じ曲の重複候補から1つを選ぶ。版表記(Remix/TV Size、アニメver.、
    メンバー別ミックス等)が無い素のタイトルを優先し、その中では人気度
    (rank)が高い方を優先する。"""
    def sort_key(t):
        title = t.get("title_short") or ""
        plain = 1 if not t.get("title_version") and not title_has_embedded_version_marker(title) else 0
        return (plain, t.get("rank") or 0)
    return max(group, key=sort_key)


def _dedupe_tracks(tracks):
    groups = defaultdict(list)
    for t in tracks:
        key = normalize_key(t.get("title_short"))
        groups[key].append(t)

    winners = []
    for group in groups.values():
        unique_by_id = list({t["id"]: t for t in group}.values())
        winners.append(_pick_winner(unique_by_id) if len(unique_by_id) > 1 else unique_by_id[0])

    seen_ids = set()
    result = []
    for t in winners:
        if t["id"] in seen_ids:
            continue
        seen_ids.add(t["id"])
        result.append(t)
    return result


# Deezerのプレビュー音源は、曲によっては0:00からではなくサビ等
# 「代表区間」とみなされた曲の途中から30秒切り出されていることがある
# (Deezer側のプレビュー生成アルゴリズムの仕様で、API側に開始位置を示す
# フィールドが無いため機械的な検出・補正はできない)。曲データ・ランキング
# (rank)は引き続きDeezerのものを使いつつ、再生する音源は基本的に全曲
# iTunes側の同一曲のプレビューへ差し替える(_apply_itunes_audio_override
# 参照)。ただし自動突き合わせは、iTunes側でもメンバーソロ名義など別
# artistIdに分裂登録されている曲までは追えない(_fetch_itunes_catalogは
# canonical_nameで見つかる1アーティストの範囲しか見ない)。そうした
# 曲や、自動突き合わせが誤って別の曲・版(instrumental等)を拾ってしまう
# 場合の手動上書き用に、Deezerのtrack_id(文字列)→iTunesのtrackId
# (文字列)のマッピングをここに追加する。
_FORCE_ITUNES_AUDIO = {
    "3445348181": "1824410659",  # ハニートリガー(坂井仁香、iTunes側も別artistId)
    "3496195161": "1831461921",  # 来世でも誇れる人生をっ!(吉川ひより、同上)
    "476735762": "1362942576",  # プリンセスプリンセスプリンセス(旧名義時代の曲、iTunes側の本体カタログに含まれず)
}


def _to_quiz_track(raw, artist_name, album_title=None, album_cover=None):
    return {
        # HTML data-*属性は常に文字列になるため(answer-choicesボタンの
        # dataset.trackIdとの比較などで型不一致にならないよう)、ここで
        # 文字列化しておく。
        "trackId": str(raw["id"]),
        "title": raw.get("title_short") or raw.get("title") or "不明な曲",
        "artist": artist_name,
        "album": album_title or (raw.get("album") or {}).get("title"),
        "durationSeconds": raw.get("duration") or 0,
        "thumbnail": album_cover or (raw.get("album") or {}).get("cover_medium"),
        # YouTube Music音源がどう見つかったか("album"=Deezer/iTunesランキングと
        # 同じシングル/アルバム名の検索で発見、"artist"=演奏者名だけでの検索で
        # 発見(別版の可能性あり)、"english"=英題/ローマ字表記フォールバックで
        # 発見、"forced"=手動確認済み)。フロントエンドの確認画面ハイライト用
        # (_apply_ytmusic_audio_override参照)。ytmusic:以外(iTunes/Deezerの
        # ままの曲)ではNone。
        "ytmusicMatchTier": raw.get("_ytmusic_match_tier"),
    }


# ---- アーティスト検索・解決 ----

def _search_artists(query, limit=10):
    data = _get("/search/artist", {"q": query, "limit": limit})
    return (data or {}).get("data", [])


def find_target_artist(artist_name):
    """アーティスト名からDeezerのartist_idを解決する。同名の空スタブ
    チャンネル(アルバム0件のダミー的な重複エントリ)と本物を区別するため、
    アルバム数(nb_album)が最大のものを採用する(playlist_builder.py系の
    「多数決」に相当する簡易版)。

    クエリ文字列と完全一致する名前を素朴に最優先すると、Deezerでは主要な
    本体側のアーティスト名がローマ字表記で登録されており、逆に日本語表記
    そのままの方がアルバム0件の空スタブだった、というケースで誤って空の
    方を選んでしまう(例:「米津玄師」で検索すると、本体は"Kenshi Yonezu"
    (41アルバム)として登録されており、"米津玄師"という完全一致エントリは
    0アルバムのスタブ)。そのため、完全一致であってもアルバム数が0の候補は
    信用せず、その場合はアルバム数が最大の候補を採用する。"""
    results = _search_artists(artist_name, limit=10)
    if not results:
        return None
    query_norm = artist_name.strip().lower()
    exact_populated = [
        r for r in results
        if (r.get("name") or "").strip().lower() == query_norm and r.get("nb_album", 0) > 0
    ]
    if exact_populated:
        candidates = exact_populated
    else:
        populated = [r for r in results if r.get("nb_album", 0) > 0]
        candidates = populated if populated else results
    best = max(candidates, key=lambda r: (r.get("nb_album", 0), r.get("nb_fan", 0)))
    return best["id"]


# 改名・表記ゆれにより、Deezer上で同一アーティストの曲が複数の別artist_id
# へ分裂登録されていることがある(例: 改名前の名義が完全一致優先で薄い
# スタブ側を掴んでしまい、本体側の曲が取得できない)。ここに正規表示名→
# 統合するartist_idのリストを登録しておくと、いずれかのIDに解決された
# 時点で他のIDの曲もまとめて取得し、常に正規表示名で表示する。
_ARTIST_MERGE_GROUPS = [
    {
        # 改名前「ときめき♡宣伝部」名義(2表記に分裂: ローマ字期+日本語表記期)
        # のみ。改名後の「超ときめき♡宣伝部」の曲は含めない。
        "display_name": "ときめき♡宣伝部",
        # trigger_ids: この中のいずれかのidにfind_target_artistが解決したら
        # このグループを使う。
        "trigger_ids": [238203301, 11300134],
        # fetch_ids: 実際に曲を取得するartist_idの一覧。
        "fetch_ids": [238203301, 11300134],
    },
    {
        # 改名後「超ときめき♡宣伝部」で検索した場合は、旧名義「ときめき♡
        # 宣伝部」の曲もあわせて含める。メンバーソロ曲が"メンバー名
        # (超ときめき♡宣伝部)"という別artist_idで個別登録されている分も統合。
        "display_name": "超ときめき♡宣伝部",
        "trigger_ids": [
            229486035,
            334225451,  # 坂井仁香 (超ときめき♡宣伝部)
            293591331,  # 小泉遥香 (超ときめき♡宣伝部)
            339946441,  # 吉川ひより (超ときめき♡宣伝部)
            361398382,  # 菅田愛貴 (超ときめき♡宣伝部)
            323086021,  # 辻野かなみ (超ときめき♡宣伝部)
            295264651,  # 杏ジュリア (超ときめき♡宣伝部)
        ],
        "fetch_ids": [
            229486035, 238203301, 11300134,
            334225451, 293591331, 339946441, 361398382, 323086021, 295264651,
        ],
        # iTunes側の自動解決(find_artist_id)は「超ときめき♡宣伝部」で検索
        # して見つかる1アーティストページ(新名義)しか見ない。旧名義
        # 「ときめき宣伝部」時代の曲(Deezer側のfetch_ids 238203301/11300134
        # に相当)はiTunes上では別アーティストID(1166362525)に分裂登録されて
        # いるため、ここに追加して_fetch_itunes_catalogで束ねて取得する。
        "itunes_extra_artist_ids": [1166362525],
        # メンバー別ユニット曲が"曲名／メンバー名"の形式でタイトルに直接
        # クレジットされている(コンサート音源は"曲名／メンバー名(Concert
        # ...)"の形式)。私立恵比寿中学のユニットアルバムと似ているが、
        # こちらはコンサート版だけ除外したい、という別ルールが必要。
        "strip_slash_credit": True,
        # 改名後の現体制で歌い直した"～超ver～"/"~西暦ver~"版と無印の旧版が
        # 両方存在する曲がある(例:「すきっ！」と「すきっ！～超ver～」、
        # 「むてきのうた」と「むてきのうた~2021ver~」)。歌い直し版だけを残す。
        "prefer_new_ver": True,
        # グループ名自体が曲名に含まれているため、通常の版表記除去(超ver等)
        # では同一曲と判定できない特殊ケース。「(超)ときめき♡宣伝部の
        # VICTORY STORY」は改名前後で表記が違うだけの同じテーマソング。
        "duplicate_titles": [
            {
                "keep": "超ときめき♡宣伝部のVICTORY STORY",
                "drop": [
                    "ときめき（白抜きのハート記号）宣伝部のVICTORY STORY",
                    "ときめき♡宣伝部のVICTORY STORY",
                ],
            },
        ],
        # Deezer/iTunesどちらにも登録が無いためこれまでのフローでは出題
        # 対象に含められなかった曲。YouTube Music側の曲名+演奏者名検索
        # (_find_ytmusic_audio_for_track)で実際に見つかることを確認済み
        # なので、手動で曲一覧に加えて同じ仕組みで音源を探させる。
        "manual_extra_tracks": [
            {"title": "ツヨクなる", "artist": "辻野かなみ"},
        ],
    },
    {
        # 完全一致優先で直近4アルバムのみの薄いスタブ(いぎなり東北産)を
        # 掴んでしまい、本体カタログ(Deezer上は"THE MADE IN TOHOKU"名義、
        # 32アルバム)を取りこぼしていたため統合。
        "display_name": "いぎなり東北産",
        "trigger_ids": [345384721, 117762712],
        "fetch_ids": [345384721, 117762712],
    },
    {
        # 完全一致優先で3アルバムのみの薄いスタブ(つばきファクトリー)を
        # 掴んでしまい、本体カタログ(Deezer上は"TSUBAKI Factory"名義、
        # 26アルバム)を取りこぼしていたため統合。
        "display_name": "つばきファクトリー",
        "trigger_ids": [375971061, 367607822],
        "fetch_ids": [375971061, 367607822],
    },
    {
        # 「私が言う前に抱きしめなきゃね」「五月雨美女がさ乱れる」は、無印版
        # と(MEMORIAL EDIT)版の両方がDeezerに登録されている。通常の重複排除
        # (_pick_winner)は版表記の無い無印版を優先するが、この2曲に限っては
        # (MEMORIAL EDIT)版を残したいとの指定のため、無印版の方を明示的に
        # 除外する(exclude_exact_titles、_track_is_usable適用前に完全一致で
        # 除去。normalize_keyは括弧を取り除くため(MEMORIAL EDIT)版と衝突して
        # しまい使えない)。
        "display_name": "Juice=Juice",
        "trigger_ids": [13261347],
        "fetch_ids": [13261347],
        "exclude_exact_titles": [
            "私が言う前に抱きしめなきゃね",
            "五月雨美女がさ乱れる",
        ],
        # "terzo"アルバム等のサブユニット曲で"曲名／メンバー名・メンバー名…"
        # のようにメンバークレジットがtitle_short本体に埋め込まれている
        # (例:「プラトニック・プラネット(Ultimate Juice Ver.)／金澤朋子、
        # 高木紗友希、…」)。クレジット無しのiTunes側と正規化キーが一致せず
        # 重複登録されてしまっていたため、超ときめき♡宣伝部と同じくクレジット
        # 部分を取り除く(コンサート版は既存のConcert除外ルールでも弾かれる)。
        "strip_slash_credit": True,
    },
]
_ARTIST_ID_TO_MERGE_GROUP = {
    trigger_id: group
    for group in _ARTIST_MERGE_GROUPS
    for trigger_id in group["trigger_ids"]
}

# "曲名／メンバー名"や"曲名／メンバー名(Concert ...)"のように、演奏メンバーの
# クレジットがtitle_short本体に"/"(全角"／"も含む)区切りで直接埋め込まれて
# いることがある。全角スラッシュはis_medley_titleの"曲A/曲B"判定(半角のみ)
# には掛からないため、こちらは別の正規表現で検出する。
_SLASH_CREDIT_RE = re.compile(r"^(.+?)[/／](\S.*)$")


def _split_slash_credit(title):
    m = _SLASH_CREDIT_RE.match(title or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _apply_slash_credit_rule(tracks):
    """スラッシュ以降のクレジット部分に"Concert"が含まれる場合はコンサート
    音源(ライブ)とみなして除外し、それ以外はクレジット部分を取り除いて
    曲名のみ残す(ユニット曲であり、メドレーではないため)。"""
    kept = []
    for t in tracks:
        split = _split_slash_credit(t.get("title_short"))
        if split is None:
            kept.append(t)
            continue
        song_title, credit = split
        if "concert" in credit.lower():
            continue
        t["title_short"] = song_title
        kept.append(t)
    return kept


# 改名後の現体制で歌い直した版が"すきっ！～超ver～"「初恋サイクリング -
# 超ver」「むてきのうた~2021ver~」のように、"超ver"や"(西暦)ver"という
# 語を区切り記号(全角/半角チルダ・ハイフン)に挟んで曲名末尾へ付与する形で
# 表される。この歌い直し版と無印の版が両方存在する場合、歌い直し版の方を
# 正式版として残し、無印の方を除外する(通常のバージョン違い統合とは逆に、
# こちらは新しい版を優先したいため)。
_NEW_VER_RE = re.compile(r"[\s～~\-]+(?:超|\d{4})\s*ver\s*[～~]?\s*$", re.IGNORECASE)


def _apply_prefer_new_ver_rule(tracks):
    """歌い直し版が優先されて除外される通常版について、その通常版自体の
    収録アルバム名・演奏者名を残す歌い直し版トラックへ_plain_version_*と
    して控えておく。YouTube Music側で歌い直し版が見つからず通常版で
    フォールバック検索する際(_find_ytmusic_audio_for_track参照)に、
    「Deezer/iTunesで実際に除外されたのと同じ通常版」を正しく指定する
    ために使う(歌い直し版自身のアルバム名で通常版を探すと、無関係な
    別収録の通常版を拾ってしまう可能性があるため)。"""
    def base_key(title):
        return normalize_key(_NEW_VER_RE.sub("", title or ""))

    plain_info_by_key = {}
    for t in tracks:
        if not _NEW_VER_RE.search(t.get("title_short") or ""):
            key = base_key(t.get("title_short"))
            if key not in plain_info_by_key:
                plain_info_by_key[key] = {
                    "_album_title": t.get("_album_title"),
                    "artist": t.get("artist"),
                }

    new_ver_keys = set(plain_info_by_key.keys()) & {
        base_key(t.get("title_short"))
        for t in tracks
        if _NEW_VER_RE.search(t.get("title_short") or "")
    }

    kept = []
    for t in tracks:
        title = t.get("title_short") or ""
        if _NEW_VER_RE.search(title):
            plain_info = plain_info_by_key.get(base_key(title))
            if plain_info:
                t["_plain_version_album_title"] = plain_info["_album_title"]
                t["_plain_version_artist"] = plain_info["artist"]
            kept.append(t)
        elif base_key(title) not in new_ver_keys:
            kept.append(t)
    return kept


def _apply_duplicate_titles_rule(tracks, duplicate_titles):
    """グループ名自体が曲名に含まれているなど、通常の版表記除去では
    同一曲と判定できない特殊ケース用の手動重複統合。duplicate_titlesは
    [{"keep": 残すタイトル, "drop": [除外するタイトル, ...]}, ...]の形式
    (_ARTIST_MERGE_GROUPSのdisplay_nameごとの設定参照)。"""
    if not duplicate_titles:
        return tracks
    present_titles = {t.get("title_short") for t in tracks}
    drop_titles = set()
    for group in duplicate_titles:
        if group["keep"] in present_titles:
            drop_titles.update(group["drop"])
    if not drop_titles:
        return tracks
    return [t for t in tracks if t.get("title_short") not in drop_titles]


# Deezer/iTunesどちらにも登録が無い曲を手動で曲一覧に加える際に使う
# プレースホルダーのID接頭辞。YouTube Music側の検索で実際の音源が
# 見つかった場合はこのIDごと差し替わる(_apply_ytmusic_audio_override
# 側は接頭辞を見ずに常に検索を試みるため)。最後まで見つからなかった
# 場合はDeezer/iTunesのフォールバック先も無いので出題対象から除外する。
_MANUAL_EXTRA_ID_PREFIX = "manual:"


def _build_manual_extra_tracks(manual_extra_tracks, existing_keys):
    """設定(merge_groupのmanual_extra_tracks)から、既存の曲一覧には無い
    曲だけを合成トラックとして返す。rankは常に0(iTunes補完同様、常に
    末尾寄りになる)。"""
    tracks = []
    for i, entry in enumerate(manual_extra_tracks or []):
        title = entry.get("title")
        if not title or normalize_key(title) in existing_keys:
            continue
        tracks.append({
            "id": f"{_MANUAL_EXTRA_ID_PREFIX}{i}",
            "title_short": title,
            "title_version": "",
            "rank": 0,
            "artist": {"name": entry.get("artist")},
            "_album_title": entry.get("album"),
            "_album_cover": None,
        })
    return tracks


def resolve_artist(artist_name):
    """アーティスト名から、取得対象のartist_idリスト・表示名・グループ設定
    (strip_slash_credit等の追加ルールのフラグを持つdict)を解決する。通常は
    単一のartist_id・ユーザー入力そのままの表示名・空dictを返すが、
    _ARTIST_MERGE_GROUPSに登録されたアーティストの場合はグループ内の全
    artist_idと正規表示名、そのグループの設定dictを返す。"""
    artist_id = find_target_artist(artist_name)
    if artist_id is None:
        return None, None, {}
    group = _ARTIST_ID_TO_MERGE_GROUP.get(artist_id)
    if group:
        return group["fetch_ids"], group["display_name"], group
    return [artist_id], artist_name.strip(), {}


# ---- 全曲取得(scope="all") ----

def _paginate(path, params, max_items):
    items = []
    index = 0
    limit = min(100, max_items)
    while len(items) < max_items:
        page = _get(path, {**params, "limit": limit, "index": index})
        if not page or not page.get("data"):
            break
        items.extend(page["data"])
        index += limit
        if not page.get("next") or len(page["data"]) < limit:
            break
    return items[:max_items]


def _fetch_artist_albums(artist_id):
    # コンピレーション盤("compile")も含め、レコードタイプで絞り込まず全種類を
    # 取得する。マイナー曲・企画コラボ曲の中には、スタジオアルバム/シングルには
    # 収録されず「Best/Compilation」盤にだけ新曲として追加収録されているものが
    # 稀にあり、ここで除外すると全曲モードから取りこぼしてしまう。同じ曲の
    # 再収録による重複は後段の_dedupe_tracksで解決するため、取得段階では
    # 幅広く集めておく方が安全。
    return _paginate(f"/artist/{artist_id}/albums", {}, max_items=300)


def _fetch_album_tracks(album):
    data = _get(f"/album/{album['id']}/tracks", {"limit": 100})
    tracks = (data or {}).get("data", [])
    for t in tracks:
        t["_album_title"] = album.get("title")
        t["_album_cover"] = album.get("cover_medium")
    return tracks


def fetch_all_tracks_raw(artist_id, on_progress=None):
    """on_progress(current, total)は、アルバムの取得が1件終わるたびに呼ばれる
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

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(handle, albums))

    raw = []
    seen_ids = set()
    for tracks in results:
        for t in tracks:
            if t.get("id") in seen_ids:
                continue
            seen_ids.add(t.get("id"))
            raw.append(t)
    return raw


# ---- iTunesからの補完取得(Deezerに無い曲を埋める) ----

_ITUNES_ID_PREFIX = "itunes:"


def _itunes_track_to_raw_shape(t):
    """iTunesの曲データを、Deezer側の各種フィルタ関数(_track_is_usable等)が
    そのまま使える形に変換する。iTunesにはtitle_versionに相当する別フィールド
    が無く、版表記はtrackName本体に埋め込まれている(例:"STAR
    (instrumental)")ため、title_shortにtrackNameをそのまま入れれば既存の
    括弧内キーワード判定がそのまま機能する。rankに相当する人気度指標は
    無いため0固定(Deezer由来の曲より必ず後ろに並ぶ)。"""
    return {
        "id": f"{_ITUNES_ID_PREFIX}{t['trackId']}",
        "title_short": t.get("trackName"),
        "title_version": "",
        "preview": t.get("previewUrl"),
        "readable": True,
        "rank": 0,
        # Deezer由来のtrackと同じ形にしておくことで、YouTube Music側の
        # 個別検索フォールバック(演奏者名を使う)がiTunes補完由来の曲にも
        # そのまま効くようにする。
        "artist": {"name": t.get("artistName")},
        "_album_title": t.get("_album_title"),
        "_album_cover": t.get("_album_cover"),
    }


def _fetch_itunes_catalog(canonical_name, on_progress=None, extra_artist_ids=None):
    """アーティストのiTunes上の全曲を、Deezer側と同じ形に整形・フィルタ・
    重複排除した状態で返す。(1)Deezerに無い曲の補完、(2)Deezer由来の曲の
    再生音源をiTunes側に差し替える、の両方の元データとして使う。iTunes
    側の検索・取得に失敗しても(レート制限、ネットワークエラー等)Deezerの
    結果には一切影響させず、空リストを返す。

    extra_artist_idsは、canonical_name検索では見つからない別アーティスト
    ページ(改名前の旧名義等、_ARTIST_MERGE_GROUPSのitunes_extra_artist_ids
    参照)のIDを追加で束ねて取得したい場合に指定する。"""
    artist_ids = []
    try:
        itunes_artist_id = itunes_service.find_artist_id(canonical_name)
        if itunes_artist_id is not None:
            artist_ids.append(itunes_artist_id)
    except Exception:
        pass
    artist_ids.extend(extra_artist_ids or [])
    if not artist_ids:
        return []

    raw = []
    for artist_id in artist_ids:
        try:
            raw.extend(itunes_service.fetch_all_tracks_raw(artist_id, on_progress=on_progress))
        except Exception:
            continue

    adapted = [_itunes_track_to_raw_shape(t) for t in raw]
    _clean_titles_in_place(adapted)
    usable = [t for t in adapted if _track_is_usable(t)]
    return _dedupe_tracks(usable)


def _build_itunes_audio_lookup(itunes_catalog):
    """曲名(正規化キー)→iTunesのtrackId(接頭辞なし)。Deezer由来の曲の
    再生元をiTunes側の同一曲のプレビューに差し替えるためのルックアップ。"""
    lookup = {}
    for t in itunes_catalog:
        key = normalize_key(t["title_short"])
        if key not in lookup:
            lookup[key] = t["id"][len(_ITUNES_ID_PREFIX):]
    return lookup


def _fallback_search_titles(title):
    """個別検索用のタイトル候補を返す(元のタイトル→"超ver"等の版表記を
    取り除いた通常版タイトルの順)。「初恋サイクリング - 超ver」のような
    歌い直し版は、その版だけ単独では配信・アップロードされておらず通常版
    ("初恋サイクリング")の方だけが存在することがあるため、元のタイトルで
    見つからなければ通常版でも探す。"""
    titles = [title]
    base = _NEW_VER_RE.sub("", strip_trailing_version_suffix(title)).strip()
    if base and base != title:
        titles.append(base)
    return titles


def _search_titles_match(target_title, candidate_title):
    """外部サービスの検索結果1件が、探している曲名と本当に一致するかを
    確認する。

    YouTube Music側では、"曲名/ユニット名"のようなクレジット付き表記が
    (Deezer側で既にこの部分を取り除き済みでも)そのまま残っていることが
    ある(例:「ぁぃぁぃといく日本全国鉄道の旅/廣田あいか」)。比較前に
    両者ともこの形式を取り除く。

    target_titleに括弧書きの版表記(例:"なにがなんでも(エビ中ver.)")が
    含まれる場合はsearch_match_key(括弧の中身を保持)で厳密に比較し、
    含まれない場合はnormalize_keyで比較する。前者を常に使わないのは、
    既存の一致(例:カバー版表記の無い曲名同士)まで壊さないため。"""
    def strip_credit(title):
        split = _split_slash_credit(title)
        return split[0] if split else title

    target_title = strip_credit(target_title)
    candidate_title = strip_credit(candidate_title)
    if re.search(r"[\(\[（【]", target_title or ""):
        return search_match_key(target_title) == search_match_key(candidate_title)
    return normalize_key(target_title) == normalize_key(candidate_title)


def _find_itunes_audio_by_track_artist(title, track_artist_name):
    """曲自体にDeezerが記録している演奏者名(メンバーソロ名義・旧名義等)で
    iTunesを直接検索し、曲名が一致する候補のtrackIdを返す。
    _fetch_itunes_catalog(canonical_name)は1つのiTunesアーティストページの
    範囲しかクロールしないため、メンバーソロ名義や旧名義のように曲ごとに
    演奏者名義が異なる場合はそこで見つからない。その場合に、"遊ぶ側が
    選んだアーティスト名"ではなく"その曲自体の演奏者名"で個別に検索する
    ことで、分裂登録された名義も自動的に追える。見つからなければNone。"""
    if not title or not track_artist_name:
        return None
    # "辻野かなみ (超ときめき♡宣伝部)"のような括弧書きのグループ名クレジット
    # がクエリに混ざると、iTunes側の検索精度がかえって落ち無関係な結果に
    # なることがあるため、括弧部分を取り除いた素の名前(例:"辻野かなみ")で
    # 検索する。
    bare_artist_name = re.sub(r"[\(\[（【].*?[\)\]）】]", "", track_artist_name).strip()
    artist_query = bare_artist_name or track_artist_name
    for search_title in _fallback_search_titles(title):
        candidates = itunes_service.search_songs(f"{search_title} {artist_query}")
        for c in candidates:
            if c.get("previewUrl") and _search_titles_match(search_title, c.get("trackName")):
                return str(c["trackId"])
    return None


def _apply_itunes_audio_override(tracks, itunes_audio_lookup, on_progress=None, should_cancel=None):
    """まだDeezer音源のまま(=YouTube Musicで見つからなかった)曲について、
    可能な限りiTunes側の同一曲プレビューに差し替える(YouTube Musicより
    優先度は低いフォールバック)。曲データ(タイトル・アーティスト・rank等)
    自体は変更しない。

    1. 手動で個別指定した_FORCE_ITUNES_AUDIOを最優先。
    2. なければitunes_audio_lookup(canonical_nameのiTunesカタログクロール
       結果)から曲名一致で探す。
    3. それでも見つからなければ、曲自体に記録されている演奏者名で個別に
       iTunesを検索するフォールバックを試す(_find_itunes_audio_by_track_
       artist参照)。on_progress(current, total)はこのフォールバック検索が
       1件終わるたびに呼ばれる(進捗表示用)。省略可。

    いずれでも見つからなければDeezerの音源のまま(曲自体は失われない)。"""
    unmatched = []
    for t in tracks:
        current_id = str(t["id"])
        if current_id.startswith(_ITUNES_ID_PREFIX) or current_id.startswith(_YTMUSIC_ID_PREFIX):
            continue  # 既にiTunes補完/YouTube Music由来
        forced = _FORCE_ITUNES_AUDIO.get(current_id)
        if forced:
            t["id"] = f"{_ITUNES_ID_PREFIX}{forced}"
            continue
        matched = itunes_audio_lookup.get(normalize_key(t.get("title_short")))
        if matched:
            t["id"] = f"{_ITUNES_ID_PREFIX}{matched}"
        else:
            unmatched.append(t)

    if not unmatched:
        return

    total = len(unmatched)
    completed = 0
    lock = threading.Lock()

    def resolve(t):
        nonlocal completed
        if should_cancel and should_cancel():
            return None
        track_artist_name = (t.get("artist") or {}).get("name")
        result = _find_itunes_audio_by_track_artist(t.get("title_short"), track_artist_name)
        if on_progress:
            with lock:
                completed += 1
                on_progress(completed, total)
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(resolve, unmatched))
    for t, matched in zip(unmatched, results):
        if matched:
            t["id"] = f"{_ITUNES_ID_PREFIX}{matched}"


# ---- YouTube Musicへの再生音源差し替え(試験的) ----
#
# Deezer/iTunesの30秒プレビューは、曲によっては配信元指定のクリップ切り出し
# 位置の都合で曲の途中から始まることがある(両者で同じ曲が同じように途中
# から始まる事例を確認済み)。YouTube Music上のフル尺音源を再生できれば
# 0:00から確実に再生できるため、再生音源としては最優先で使う。ただし
# YouTubeからの音声ストリーム抽出は利用規約上グレーゾーンかつyt-dlpは
# YouTube側の仕様変更で壊れやすい。
_YTMUSIC_ID_PREFIX = "ytmusic:"

# YouTube Music上では、同じ曲でも「Music Video(MUSIC_VIDEO_TYPE_OMV、
# ダンス・寸劇等の演出入りの通常MV)」と「Art Track(MUSIC_VIDEO_TYPE_ATV、
# ジャケット画像固定・音声のみ)」が別videoIdとして存在することがある。
# アルバムのトラックリスト(get_album)はMV版のvideoIdを指していることが
# あり、MVには曲が鳴り出す前に演出シーンが入ることがあるため、イントロ
# クイズの再生としては不適切(このプロジェクトが以前ytmusicapiから離れた
# 理由そのもの)。ATV以外は採用しない。
_YTMUSIC_AUDIO_ONLY_TYPE = "MUSIC_VIDEO_TYPE_ATV"


# 曲名がYouTube Music側にローマ字表記でしか登録されておらず(日本語表記が
# 無い)、かつそのローマ字化が独特で推測できない場合、曲名検索では原理的に
# 見つけられない(例:「宇宙戦争宣戦布告」が"Space Sensou Sensenfukoku"の
# ように直訳+ローマ字混在で登録されている等)。また、曲名自体は普通の表記
# でも検索インデックス側の事情で検索結果に出てこない曲もある(例:いぎなり
# 東北産「線の物語」、ユーザー提供の直接リンクで確認)。そうした曲を個別に
# 手動で対応づけるための、Deezerのtrack_id(文字列)→YouTube MusicのvideoId
# のマッピング。
_FORCE_YTMUSIC_AUDIO = {
    "976114582": "dJmu5YVO49g",  # 宇宙戦争宣戦布告(ローマ字表記"Space Sensou Sensenfukoku"のみで登録)
    "1715443707": "RwxwLnGbxZ0",  # 線の物語(いぎなり東北産、検索結果に出てこないため直接リンクで確認)
}


def _search_ytmusic_audio(search_title, query_suffix):
    """"search_title query_suffix"でYouTube Musicを検索し、曲名が一致する
    ATV(音声のみ版)候補の(videoId, そのYouTube Music上のアルバム名)を返す。
    アルバム名は、演奏者名だけでの検索で見つかった場合でもDeezer/iTunes側
    の版と実際に一致しているかどうかを呼び出し側で確認するために使う
    (_find_ytmusic_audio_for_track参照)。見つからなければNone。"""
    candidates = ytmusic_service.search_songs(f"{search_title} {query_suffix}")
    for c in candidates:
        if c.get("videoType") != _YTMUSIC_AUDIO_ONLY_TYPE:
            continue
        song_title = clean_title(c.get("title") or "")
        if _has_unwanted_keyword(song_title):
            continue
        if _search_titles_match(search_title, song_title):
            return c.get("videoId"), (c.get("album") or {}).get("name")
    return None


def _search_ytmusic_audio_by_album_browse(search_title, album_title, artist_name):
    """曲名検索(_search_ytmusic_audio)で見つからなかった曲向けの最終手段。
    曲名自体は普通の表記でも検索インデックス側の事情でfilter="songs"の
    結果に一切出てこない曲がまれにある(実例:いぎなり東北産「線の物語」
    「ニュートロ」「テキーナ」、ユーザー提供の直接リンクで確認済み)。

    対象アルバム1枚をfilter="albums"で特定し、そのトラックリストの中から
    曲名が一致するものを探す。アーティストの全リリースを対象にした曲の
    長さだけによる突き合わせ(過去に別の曲と誤って一致してしまい廃止した
    方式)とは異なり、「対象アルバム1枚に絞った上でなお曲名一致を要求する」
    ため、別の曲を誤って拾うリスクは通常の曲名検索と同程度に抑えられる。
    ただしアルバムのトラックリストにはMV(videoType=MUSIC_VIDEO_TYPE_OMV)
    のvideoIdしか載っていない曲もあるため、ATV以外は候補として使わない。
    album_titleが無ければNone。"""
    if not album_title:
        return None
    query = f"{artist_name} {album_title}" if artist_name else album_title
    albums = ytmusic_service.search_albums(query, limit=3)
    for album in albums:
        browse_id = album.get("browseId")
        if not browse_id:
            continue
        for c in ytmusic_service.get_album_tracks(browse_id):
            if c.get("videoType") != _YTMUSIC_AUDIO_ONLY_TYPE:
                continue
            song_title = clean_title(c.get("title") or "")
            if _has_unwanted_keyword(song_title):
                continue
            if _search_titles_match(search_title, song_title):
                return c.get("videoId")
    return None


def _query_suffixes_for(album_title, artist_name):
    """検索クエリの補助語(アルバム名→演奏者名の順)を、どちらで見つかったか
    呼び出し側が区別できるよう("album"|"artist", 補助語)のタグ付きで返す。
    アルバム名一致は「Deezer/iTunesのランキングと同じ版」である確度が高いが、
    演奏者名だけでの一致はその演奏者の別のシングル/アルバム収録の同名異版
    (Remix・別ミックス等)を拾ってしまう可能性があり、確度が落ちる。

    "SEXY SEXY/泣いていいよ/Vivid Midnight"のような複数曲まとめの表題を持つ
    シングルは、アルバム名をそのまま検索クエリに使うと無関係な曲名が2つも
    混ざってしまい検索精度が落ちる("泣いていいよ SEXY SEXY/泣いていいよ/
    Vivid Midnight"のような冗長なクエリになる)。"/"区切りのアルバム名は、
    まず各曲名個別を補助語の候補として先に試す(全体をそのまま使う場合より
    優先)。"""
    bare_artist_name = re.sub(r"[\(\[（【].*?[\)\]）】]", "", artist_name or "").strip()
    artist_suffix = bare_artist_name or artist_name
    suffixes = []
    if album_title:
        if "/" in album_title:
            for part in album_title.split("/"):
                part = part.strip()
                if part:
                    suffixes.append(("album", part))
        suffixes.append(("album", album_title))
    if artist_suffix:
        suffixes.append(("artist", artist_suffix))
    return suffixes


def _find_ytmusic_audio_for_track(
    title, album_title, track_artist_name, plain_album_title=None, plain_artist_name=None,
    english_title=None,
):
    """曲名+収録アルバム名(Deezer/iTunes側の_album_title、そのシングル/
    アルバム/EP名)を手がかりにYouTube Musicを直接検索し、一致するATV
    (音声のみ版)のvideoIdを返す。ランキングに載る曲はDeezer/iTunesの
    シングル/アルバム/EPのいずれかに収録されているはずなので、「その版」を
    指定して探すのが最も確実(アルバムのトラックリストをクロールする方式は、
    MV(OMV)のvideoIdしか掲載されていないことがあり信頼できない)。アルバム
    名で見つからなければ、次に演奏者名(メンバーソロ名義・旧名義等)で探す。

    「初恋サイクリング - 超ver」のように版限定タイトルが見つからない場合は
    版表記を取り除いた通常版でも再検索するが、その際はこの曲自身のアルバム
    名ではなく、plain_album_title/plain_artist_name(Deezer/iTunes側で
    prefer_new_verにより実際に除外された、その通常版自体のアルバム名・
    演奏者名。_apply_prefer_new_ver_rule参照)を優先して使う。歌い直し版
    自身のアルバム名で通常版を検索すると、無関係な別収録の同名曲を拾って
    しまう可能性があるため。

    それでも見つからない場合、最後にenglish_title(Deezerのtitle_shortに
    "曲名 - 英題/ローマ字表記"の形で同梱されていた英題/ローマ字表記側。
    extract_english_title参照)でも検索する。YouTube Music側に日本語
    タイトルではなくこちらの表記でしか曲が登録されていないケースがある
    ため(検索は日本語タイトルの完全一致を要求するため、登録表記が違うと
    通常の検索では原理的に見つけられない)。

    戻り値は(videoId, tier)のタプル。tierは"album"(アルバム名一致で発見、
    または演奏者名だけで見つかった候補が実際に申告しているアルバム名が
    Deezer/iTunes側の版と一致した場合=確度が高い)、"artist"(演奏者名
    だけで発見、かつ候補のアルバム名も一致しなかった=別のシングル/アルバム
    収録の可能性がある)、"english"(english_titleで発見)のいずれか。
    見つからなければ(None, None)。

    "first bloom"(つばきファクトリー)のように、アルバム名が英単語として
    ありふれているとアルバム名クエリでは無関係な曲に埋もれて見つからず、
    演奏者名だけのクエリでようやく見つかることがある。この場合でも、
    見つかった候補自身が申告しているアルバム名が探していたアルバム名と
    一致するなら、別版という意味での「確度が低い」には当たらないため、
    "artist"のままにせず"album"へ引き上げる。"""
    if not title:
        return None, None
    default_suffixes = _query_suffixes_for(album_title, track_artist_name)
    plain_suffixes = _query_suffixes_for(plain_album_title, plain_artist_name) or default_suffixes
    for i, search_title in enumerate(_fallback_search_titles(title)):
        query_suffixes = default_suffixes if i == 0 else plain_suffixes
        expected_album = album_title if i == 0 else (plain_album_title or album_title)
        expected_album_key = normalize_key(expected_album) if expected_album else None
        for tier, query_suffix in query_suffixes:
            result = _search_ytmusic_audio(search_title, query_suffix)
            if result:
                video_id, candidate_album = result
                if (
                    tier == "artist"
                    and expected_album_key
                    and candidate_album
                    and normalize_key(candidate_album) == expected_album_key
                ):
                    tier = "album"
                return video_id, tier
    if english_title:
        for _, query_suffix in default_suffixes:
            result = _search_ytmusic_audio(english_title, query_suffix)
            if result:
                matched = result[0]
                return matched, "english"

    # 最終手段: 曲名検索では一切見つからない曲向けに、対象アルバム自体を
    # ブラウズしてトラックリストから探す(_search_ytmusic_audio_by_album_
    # browse参照)。plain_album_titleがあればそちらも試す。
    for candidate_album in (album_title, plain_album_title):
        matched = _search_ytmusic_audio_by_album_browse(title, candidate_album, track_artist_name)
        if matched:
            return matched, "album"
    # ここまでの全手段(曲名+アルバム名/演奏者名、英題フォールバック、
    # アルバムブラウズ)を試しても見つからなかった場合。検索リクエスト
    # 自体は失敗していない(失敗していればytmusic_service._retry側で
    # 別途ログが出る)ため、これは「純粋に一致する候補が無かった」ケース。
    # ローカルでは再現しない失敗がRender上でだけ起きる場合、この行が
    # 出るかどうかで「検索は試みたが見つからなかった」のか「そもそも
    # ここまで処理が到達していない」のかを切り分けられる。
    print(
        f"[deezer_service] no ytmusic match for title={title!r} album={album_title!r} "
        f"artist={track_artist_name!r} plain_album={plain_album_title!r} english={english_title!r}",
        file=sys.stderr,
    )
    return None, None


def _apply_ytmusic_audio_override(tracks, on_progress=None, should_cancel=None):
    """全曲の再生音源を、可能な限りYouTube Music側のフル尺音源(ATV)に
    差し替える(試験的、最優先で適用)。曲ごとに「曲名+収録アルバム名」→
    「曲名+演奏者名」の順でYouTube Musicを直接検索する
    (_find_ytmusic_audio_for_track参照)。iTunes補完由来(Deezerに無かった
    曲)の曲も対象に含める(iTunes音源もDeezer同様「途中から始まる」ことが
    あるため)。曲データ自体は変更しない。on_progress(current, total)は
    1曲の検索が終わるたびに呼ばれる(進捗表示用)。省略可。should_cancel()
    がTrueを返すようになったら、まだ処理していない曲の検索を打ち切る
    (このフェーズが全体の取得時間の大半を占めるため、ユーザーが中断した
    場合にここで早めに切り上げないと無駄なAPI呼び出しが続いてしまう)。"""
    total = len(tracks)
    completed = 0
    lock = threading.Lock()

    def resolve(t):
        nonlocal completed
        if should_cancel and should_cancel():
            return None, None
        forced = _FORCE_YTMUSIC_AUDIO.get(str(t["id"]))
        if forced:
            result, tier = forced, "forced"
        else:
            track_artist_name = (t.get("artist") or {}).get("name")
            plain_artist_name = (t.get("_plain_version_artist") or {}).get("name")
            result, tier = _find_ytmusic_audio_for_track(
                t.get("title_short"), t.get("_album_title"), track_artist_name,
                plain_album_title=t.get("_plain_version_album_title"),
                plain_artist_name=plain_artist_name,
                english_title=t.get("_english_title"),
            )
        if on_progress:
            with lock:
                completed += 1
                on_progress(completed, total)
        return result, tier

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(resolve, tracks))
    for t, (matched, tier) in zip(tracks, results):
        if matched:
            t["id"] = f"{_YTMUSIC_ID_PREFIX}{matched}"
            # "artist"(演奏者名だけでの一致)・"english"(英題/ローマ字表記
            # フォールバック)は、Deezer/iTunesのランキングと同じシングル/
            # アルバム収録の版だと確認できていない(=別版の可能性がある)ため、
            # 確認画面でハイライトできるよう印を付けておく(_to_quiz_track参照)。
            t["_ytmusic_match_tier"] = tier


def get_artist_tracks(artist_name, scope="all", on_progress=None, should_cancel=None):
    """アーティスト名から曲一覧を取得する。見つからない場合はNoneを返す。

    on_progress(stage, current, total)を渡すと、取得の進行状況を通知する
    (進捗表示用、省略可)。stageは"deezer"(Deezerから曲を取得中)、
    "itunes_catalog"(iTunesのカタログを確認中)、"ytmusic_match"
    (YouTube Musicで曲ごとに音源を検索中)、"itunes_match"(iTunesで個別に
    曲を検索中、YouTube Musicでも見つからなかった曲のみ)のいずれか。
    should_cancel()を渡すと、時間のかかる音源検索フェーズ(YouTube Music/
    iTunesの個別検索)の要所でこれを確認し、Trueが返るようになったら
    以降の未処理分をスキップして早めに切り上げる(全体の取得自体は最後
    まで完了し、その時点までに解決できた分だけが反映された結果を返す)。
    scope="all"で全曲、"top25"/"top50"でDeezerの人気度(rank)上位に絞り込む。

    Top25/Top50は、DeezerのチャートAPI(/artist/{id}/top)を使わず、常に
    アーティストの全曲を取得・重複排除した上でrank(人気度)順に並べて
    上位N件を選ぶ。/artist/{id}/topはマイナー/地域アーティストだと集計対象の
    曲数自体が数曲しかないことがあり(カタログ全体には曲が十分あっても)、
    「Top25を選んだのに3曲しか出ない」という結果になっていたため。

    表示するアーティスト名は解決後のDeezer側の表記ではなく、常にユーザーが
    入力/選択した表記(artist_name)をそのまま使う。Deezerの検索結果自体の
    name表記がアーティストによってローマ字化されていることがあり、API側の
    表記を機械的に採用すると意図しない表記に化けることがあるため(ただし
    _ARTIST_MERGE_GROUPSに登録されたアーティストは常に正規表示名を使う)。

    一覧の並び順は、scopeによらず常にrank(人気度、Deezer上で再生数に
    最も近い指標)の高い順。曲データ・ランキングはDeezerのものを使うが、
    Deezer/iTunesのプレビュー音源はどちらも(配信元指定のクリップ切り出し
    位置の都合で)曲の途中から始まることがあるため、再生音源は可能な限り
    YouTube Music側のフル尺音源に差し替える(常に0:00から再生できる)。
    YouTube Musicに無い曲はiTunes、それも無ければDeezerの音源にフォール
    バックする。Deezerに無い曲はiTunesから補うが、iTunes分にはrankが
    無いため常に末尾に回る。"""
    artist_ids, canonical_name, group_config = resolve_artist(artist_name)
    if artist_ids is None:
        return None

    raw = []
    for artist_id in artist_ids:
        raw.extend(fetch_all_tracks_raw(
            artist_id,
            on_progress=(lambda c, t: on_progress("deezer", c, t)) if on_progress else None,
        ))
    if group_config.get("strip_slash_credit"):
        raw = _apply_slash_credit_rule(raw)
    _clean_titles_in_place(raw)
    exclude_exact_titles = set(group_config.get("exclude_exact_titles") or [])
    if exclude_exact_titles:
        raw = [t for t in raw if (t.get("title_short") or "") not in exclude_exact_titles]
    usable = [t for t in raw if _track_is_usable(t)]
    picked = _dedupe_tracks(usable)

    # 再生音源の差し替え対象を探すため、scopeによらず常にiTunesの全曲
    # カタログを取得する(Top25/Top50でDeezer側だけで件数が足りていても、
    # その曲の音源自体をiTunesに差し替える必要があるため省略できない)。
    itunes_catalog = _fetch_itunes_catalog(
        canonical_name,
        on_progress=(lambda c, t: on_progress("itunes_catalog", c, t)) if on_progress else None,
        extra_artist_ids=group_config.get("itunes_extra_artist_ids"),
    )

    existing_keys = {normalize_key(t["title_short"]) for t in picked}
    picked += [t for t in itunes_catalog if normalize_key(t["title_short"]) not in existing_keys]

    # manual_extra_tracksは、Deezer/iTunesどちらにも登録が無く上記の仕組み
    # では出題対象に含められない曲を手動で追加する(_build_manual_extra_
    # tracks参照)。実際の音源はこの後のYouTube Music検索フェーズで探す。
    if group_config.get("manual_extra_tracks"):
        existing_keys = {normalize_key(t["title_short"]) for t in picked}
        picked += _build_manual_extra_tracks(group_config["manual_extra_tracks"], existing_keys)

    # prefer_new_verは、Deezerに無く無印版だけiTunes補完で追加される
    # ケース(例:「GAMUSHARA」)もあるため、Deezer+iTunes統合後の一覧に
    # 対して適用する。
    if group_config.get("prefer_new_ver"):
        picked = _apply_prefer_new_ver_rule(picked)

    picked = _apply_duplicate_titles_rule(picked, group_config.get("duplicate_titles"))

    picked.sort(key=lambda t: t.get("rank") or 0, reverse=True)

    nominal = {"top25": 25, "top50": 50}.get(scope)
    if nominal is not None:
        picked = picked[:nominal]

    # 再生音源はYouTube Musicを最優先で試す(Deezer/iTunesのプレビューは
    # 曲の途中から始まることがあるが、YouTube Musicのフル尺音源なら常に
    # 0:00から確実に再生できるため)。見つからなかった曲だけiTunesにフォール
    # バックし、それでも見つからなければDeezerの音源のまま。
    _apply_ytmusic_audio_override(
        picked,
        on_progress=(lambda c, t: on_progress("ytmusic_match", c, t)) if on_progress else None,
        should_cancel=should_cancel,
    )

    if not (should_cancel and should_cancel()):
        _apply_itunes_audio_override(
            picked,
            _build_itunes_audio_lookup(itunes_catalog),
            on_progress=(lambda c, t: on_progress("itunes_match", c, t)) if on_progress else None,
            should_cancel=should_cancel,
        )

    # manual_extra_tracksで加えた曲のうち、結局YouTube Music・iTunesどちら
    # でも音源が見つからなかったものは、有効な再生元が無いため出題対象から
    # 除外する(Deezer/iTunesどちらにも存在しない曲のためフォールバック先が
    # 無い)。
    picked = [t for t in picked if not str(t["id"]).startswith(_MANUAL_EXTRA_ID_PREFIX)]

    quiz_tracks = [
        _to_quiz_track(t, canonical_name, t.get("_album_title"), t.get("_album_cover"))
        for t in picked
    ]
    return {"artistName": canonical_name, "tracks": quiz_tracks}


# ---- 再生直前の音源URL解決 ----

_PREVIEW_URL_TTL_HINT_SECONDS = 800  # Deezerのプレビュー用署名URLは発行から約14分で失効する


def get_track_audio(track_id):
    """再生直前に呼び出し、その場で有効なプレビュー音源URLを取得する。
    Deezerのpreview URLは発行時刻から短時間(実測で約14分)で失効する署名付き
    URLのため、曲一覧取得時点のURLをそのまま保持して後で再生することはできない
    (曲選択画面で少し迷っている間や、保存済み曲リストを後日読み込んだ場合に
    再生できなくなる)。出題直前(先読み含む)に都度この関数で新しいURLを
    取り直す設計にすることで、この失効の影響を受けないようにしている。"""
    data = _get(f"/track/{track_id}", {})
    if not data or not data.get("preview"):
        return None
    return {
        "previewUrl": data["preview"],
        "durationSeconds": data.get("duration") or 0,
    }
