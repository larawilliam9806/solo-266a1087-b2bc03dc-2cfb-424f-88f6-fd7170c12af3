import json
import os
from datetime import datetime

from flask import Flask, Response, abort, render_template, request

from credit_store import (
    DEFAULT_TTL_SECONDS,
    CreditStore,
    ExpiredToken,
    InvalidToken,
    StoreError,
)
from geometry import analyze

DEFAULT_MAX_BYTES = 16 * 1024 * 1024  # 署名表暂存总上限 16MB


def _env_int(name, default, minimum=0):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"环境变量 {name} 必须是整数，得到：{raw!r}")
    if value < minimum:
        raise SystemExit(f"环境变量 {name} 不能小于 {minimum}，得到：{value}")
    return value


def _expires_text(expires_at):
    if expires_at is None:
        return None
    try:
        return datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def create_app(config=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传 JSON 上限 16MB
    app.config["CREDIT_STORE_DIR"] = os.environ.get("CREDIT_STORE_DIR", "data")
    app.config["CREDIT_STORE_MAX_BYTES"] = _env_int(
        "CREDIT_STORE_MAX_BYTES", DEFAULT_MAX_BYTES
    )
    app.config["CREDIT_STORE_TTL_SECONDS"] = _env_int(
        "CREDIT_STORE_TTL_SECONDS", DEFAULT_TTL_SECONDS, minimum=1
    )
    if config:
        app.config.update(config)

    store = CreditStore(
        app.config["CREDIT_STORE_DIR"],
        app.config["CREDIT_STORE_MAX_BYTES"],
        app.config["CREDIT_STORE_TTL_SECONDS"],
    )
    app.extensions["credit_store"] = store

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/analyze", methods=["POST"])
    def analyze_route():
        f = request.files.get("file")
        if f is None or f.filename == "":
            return render_template("index.html", error="请选择一个 JSON 文件"), 400
        try:
            data = json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return render_template("index.html", error=f"JSON 解析失败：{e}"), 400
        try:
            report = analyze(data)
        except ValueError as e:
            return render_template("index.html", error=f"输入不合法：{e}"), 400

        token = None
        expires_at = None
        download_error = None
        if report["clean"]:
            try:
                token, expires_at = store.save(report["csv"].encode("utf-8-sig"))
            except StoreError as e:
                download_error = str(e)
        return render_template(
            "result.html",
            report=report,
            token=token,
            expires_text=_expires_text(expires_at),
            ttl_hours=app.config["CREDIT_STORE_TTL_SECONDS"] / 3600,
            download_error=download_error,
        )

    @app.route("/download/<token>")
    def download(token):
        try:
            payload = store.fetch(token)
        except ExpiredToken:
            return render_template("expired.html"), 410
        except InvalidToken:
            abort(404)
        except StoreError as e:
            return render_template("expired.html", error=str(e)), 503
        return Response(
            payload,
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=credits.csv",
                "Cache-Control": "private, max-age=0",
            },
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
