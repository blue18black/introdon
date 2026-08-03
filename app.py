import os
import socket
import sys
import time

from flask import Flask, jsonify, request

import ytmusic_service

# Renderなどのホスティング環境では$PORTで割り当てポートが渡され、0.0.0.0で
# リッスンする必要がある。ローカル実行時はPORT未設定なら従来通り5000番。
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__, static_folder="static", static_url_path="/static")

# static/app.js等の修正を配信しても、ブラウザ側のキャッシュが古いバージョンを
# 使い続けてしまい、修正が反映されているか確認できない/実は直っているのに
# 直っていないように見える、という混乱が何度も起きていた。サーバー起動時刻を
# クエリ文字列として静的ファイルのURLに付与し、デプロイ(再起動)のたびに
# ブラウザが確実に最新のファイルを取りに行くようにする。
_ASSET_VERSION = str(int(time.time()))
with open("index.html", encoding="utf-8") as _f:
    _INDEX_HTML = _f.read()
for _asset in ("style.css", "api.js", "youtube-player.js", "quiz.js", "app.js"):
    _INDEX_HTML = _INDEX_HTML.replace(
        f'/static/{_asset}"', f'/static/{_asset}?v={_ASSET_VERSION}"'
    )


@app.route("/")
def index():
    return _INDEX_HTML


@app.after_request
def _no_store_api_responses(response):
    # /api/配下はブラウザ・中間プロキシのHTTPキャッシュに一切乗せない。
    # 「修正したはずなのに古い結果のまま」という混乱が繰り返し起きていたため、
    # 静的ファイルのキャッシュバスティングとは別に、動的なAPI応答側にも
    # 明示的にno-storeを付けて疑いの余地を無くす。
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/artist_suggest")
def api_artist_suggest():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    try:
        names = ytmusic_service.get_artist_suggestions(query)
    except Exception:
        names = []
    return jsonify(names)


@app.route("/api/artist_tracks")
def api_artist_tracks():
    artist_name = request.args.get("name", "").strip()
    scope = request.args.get("scope", "all")
    if scope not in ("all", "top25", "top50"):
        scope = "all"
    if not artist_name:
        return jsonify({"error": "アーティスト名を指定してください。"}), 400
    try:
        result = ytmusic_service.get_artist_tracks(artist_name, scope=scope)
        if result is None:
            return jsonify({"error": "アーティストが見つかりませんでした。"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/playlist_tracks")
def api_playlist_tracks():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "プレイリストのURLを指定してください。"}), 400
    try:
        result = ytmusic_service.get_playlist_tracks(url)
        if result is None:
            return jsonify({"error": "プレイリストが見つかりませんでした。URLをご確認ください。"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _ensure_port_free(host, port):
    """
    Windowsはデフォルトのソケット挙動が緩く、既に別プロセスがlistenして
    いるポートに気づかず2重起動できてしまうことがある(片方が実際には
    応答できず、ブラウザ側で "Failed to fetch" になる原因になる)。
    SO_EXCLUSIVEADDRUSEで排他バインドを試みて、既に使われていれば
    起動前にはっきりエラーを出す。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind((host, port))
    except OSError:
        print(
            f"エラー: ポート{port}は既に使用中です。"
            f"既に起動しているイントロドンのサーバー(python app.py)を終了してから、"
            f"もう一度実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        s.close()


if __name__ == "__main__":
    _ensure_port_free(HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
