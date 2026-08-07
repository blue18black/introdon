import os
import socket
import sys
import threading
import time
import uuid

from flask import Flask, jsonify, request

import deezer_service
import itunes_service
import ytmusic_service

# Renderなどのホスティング環境では$PORTで割り当てポートが渡され、0.0.0.0で
# リッスンする必要がある。ローカル実行時はPORT未設定なら従来通り5000番。
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__, static_folder="static", static_url_path="/static")

# static/app.js等の修正を配信しても、ブラウザ側のキャッシュが古いバージョンを
# 使い続けてしまい、修正が反映されているか確認できない/実は直っているのに
# 直っていないように見える、という混乱が何度も起きていた。当初はサーバー
# 起動時刻をクエリ文字列として付与していたが、use_reloader=Trueの監視対象は
# .pyファイルのみで.jsファイルの保存では再起動されないため、.jsだけを編集
# した場合はサーバーを手動再起動しない限り常に同じバージョン文字列のまま
# =ブラウザキャッシュが効いたままになり、実質的に直っていなかった
# (index.html自体は毎回読み直す対策が既に入っていたのに、こちらのバージョン
# 文字列側だけ対策が漏れていた)。ファイルの更新日時(mtime)を都度読んで
# バージョン文字列にすることで、サーバー再起動無しに保存直後から反映される
# ようにする。
def _asset_version(asset):
    try:
        return str(int(os.path.getmtime(os.path.join("static", asset))))
    except OSError:
        return str(int(time.time()))


@app.route("/")
def index():
    # index.html自体は起動時に一度だけ読み込んでメモリにキャッシュしていたため、
    # 保存してもサーバーを再起動するまでCtrl+F5をしても反映されなかった。
    # アクセスのたびに読み直すことで、保存後すぐに反映されるようにする
    # (ローカルの個人利用規模なので毎回読み直すコストは無視できる)。
    with open("index.html", encoding="utf-8") as f:
        html = f.read()
    for asset in ("style.css", "api.js", "audio-player.js", "youtube-player.js", "quiz.js", "app.js"):
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={_asset_version(asset)}"')
    return html


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
        names = itunes_service.search_artist_suggestions(query)
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
        result = deezer_service.get_artist_tracks(artist_name, scope=scope)
        if result is None:
            return jsonify({"error": "アーティストが見つかりませんでした。"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- 曲取得の進捗表示用(ポーリング) ----
#
# iTunesとの突き合わせを含む全曲取得は数十秒かかることがあり、単発の
# リクエスト/レスポンスでは進行状況を返せない。そのため、取得処理を
# バックグラウンドスレッドで開始してjobIdを即座に返し(/start)、
# フロントエンドはそのjobIdで進捗を都度取得する(/progress)。
_jobs = {}
_jobs_lock = threading.Lock()
_JOB_MAX_AGE_SECONDS = 600  # 取りに来られないまま放置されたジョブの掃除用


def _prune_stale_jobs():
    cutoff = time.time() - _JOB_MAX_AGE_SECONDS
    stale_ids = [jid for jid, job in _jobs.items() if job["created_at"] < cutoff]
    for jid in stale_ids:
        del _jobs[jid]


@app.route("/api/artist_tracks/start")
def api_artist_tracks_start():
    artist_name = request.args.get("name", "").strip()
    scope = request.args.get("scope", "all")
    if scope not in ("all", "top25", "top50"):
        scope = "all"
    if not artist_name:
        return jsonify({"error": "アーティスト名を指定してください。"}), 400

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _prune_stale_jobs()
        _jobs[job_id] = {
            "status": "running",
            "stage": None,
            "current": 0,
            "total": 0,
            "result": None,
            "error": None,
            "cancel_requested": False,
            "created_at": time.time(),
        }

    def is_cancelled():
        with _jobs_lock:
            job = _jobs.get(job_id)
            return job is None or job["cancel_requested"]

    def run():
        def on_progress(stage, current, total):
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    job["stage"] = stage
                    job["current"] = current
                    job["total"] = total

        try:
            result = deezer_service.get_artist_tracks(
                artist_name, scope=scope, on_progress=on_progress, should_cancel=is_cancelled
            )
        except Exception as e:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(e)
            return
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or job["cancel_requested"]:
                return  # 中断済み: クライアントはもう見に来ないので結果は捨てる
            if result is None:
                job["status"] = "error"
                job["error"] = "アーティストが見つかりませんでした。"
            else:
                job["status"] = "done"
                job["result"] = result

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": job_id})


@app.route("/api/artist_tracks/cancel", methods=["POST"])
def api_artist_tracks_cancel():
    # バックグラウンドスレッド自体を強制終了はできないため、中断要求フラグを
    # 立てて、deezer_service側の要所(should_cancel)で自主的に打ち切って
    # もらう形にする(完全に即座には止まらないが、無駄なAPI呼び出しの継続を
    # 早めに切り上げられる)。jobId不明でもエラーにはしない(既に完了・
    # 期限切れの可能性があり、呼び出し側は気にしなくてよいため)。
    job_id = request.args.get("jobId", "").strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job["cancel_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/artist_tracks/progress")
def api_artist_tracks_progress():
    job_id = request.args.get("jobId", "").strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "不明なjobIdです(サーバー再起動、または期限切れの可能性があります)。"}), 404
        response = {
            "status": job["status"],
            "stage": job["stage"],
            "current": job["current"],
            "total": job["total"],
        }
        if job["status"] in ("done", "error"):
            if job["status"] == "done":
                response["result"] = job["result"]
            else:
                response["error"] = job["error"]
            del _jobs[job_id]
    return jsonify(response)


@app.route("/api/track_audio")
def api_track_audio():
    # Deezer/iTunes/YouTube Musicいずれのプレビュー・ストリームURLも発行から
    # 短時間で失効しうるため、曲一覧取得時点でまとめて渡すのではなく、実際に
    # 再生する直前にこのエンドポイントで都度取り直す。曲IDの接頭辞で振り分ける
    # (deezer_service._ITUNES_ID_PREFIX/_YTMUSIC_ID_PREFIX参照)。
    track_id = request.args.get("id", "").strip()
    if not track_id:
        return jsonify({"error": "曲IDを指定してください。"}), 400
    try:
        if track_id.startswith("itunes:"):
            audio = itunes_service.get_track_audio(track_id[len("itunes:"):])
        elif track_id.startswith("ytmusic:"):
            audio = ytmusic_service.get_track_audio(track_id[len("ytmusic:"):])
        else:
            audio = deezer_service.get_track_audio(track_id)
        if audio is None:
            return jsonify({"error": "曲の音源を取得できませんでした。"}), 404
        return jsonify(audio)
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
    # use_reloader=Trueにすることで、.pyファイルを保存すると自動でサーバーが
    # 再起動される(手動でstart_server.batを閉じ直す必要がなくなる)。
    # 実運用(Procfile経由のgunicorn)はこの__main__ブロックを通らないため、
    # ここを変更しても本番環境には影響しない。
    #
    # use_reloader=Trueだと、監視用の親プロセスと実際にサーブする子プロセス
    # (環境変数WERKZEUG_RUN_MAIN=trueで区別される)の両方でこの__main__
    # ブロックが実行される。_ensure_port_freeを両方で呼ぶと、Windowsでは
    # 親側が閉じた直後のソケットへ子側が再bindしようとして一時的に失敗する
    # ことがあるため、実際にサーブする側でのみチェックする。
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _ensure_port_free(HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=True, threaded=True)
