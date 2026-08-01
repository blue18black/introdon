import os
import socket
import sys

from flask import Flask, jsonify, request, send_from_directory

import ytmusic_service

# Renderなどのホスティング環境では$PORTで割り当てポートが渡され、0.0.0.0で
# リッスンする必要がある。ローカル実行時はPORT未設定なら従来通り5000番。
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


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
    if scope not in ("all", "top25", "top50", "mix"):
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


@app.route("/api/_debug_artist")
def api_debug_artist():
    # 一時的な調査用エンドポイント。私立恵比寿中学が全スコープで0曲になる
    # 不具合の原因をRender環境上で特定するために追加した。原因判明後に削除する。
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "name required"}), 400
    out = {}
    try:
        target = ytmusic_service.find_target_artist(artist_name)
        out["target"] = target
    except Exception as e:
        out["target_error"] = f"{type(e).__name__}: {e}"
        return jsonify(out)
    if not target:
        return jsonify(out)
    _, artist_id = target
    try:
        artist = ytmusic_service._yt.get_artist(artist_id)
        out["artist_keys"] = list(artist.keys())
        albums_section = artist.get("albums") or {}
        singles_section = artist.get("singles") or {}
        out["albums_results_count"] = len(albums_section.get("results", []))
        out["albums_params"] = bool(albums_section.get("params"))
        out["singles_results_count"] = len(singles_section.get("results", []))
        out["singles_params"] = bool(singles_section.get("params"))
    except Exception as e:
        out["get_artist_error"] = f"{type(e).__name__}: {e}"
    try:
        raw = ytmusic_service.fetch_all_tracks(artist_id)
        out["fetch_all_tracks_count"] = len(raw)
    except Exception as e:
        out["fetch_all_tracks_error"] = f"{type(e).__name__}: {e}"
    return jsonify(out)


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
