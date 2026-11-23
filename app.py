import json
import secrets
import threading

from flask import Flask, Response, abort, render_template, request

from geometry import analyze

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传 JSON 上限 16MB

# 署名 CSV 暂存（仅当整页无缺口时生成），容量有限，与画布像素数无关
_store = {}
_store_lock = threading.Lock()
_STORE_CAP = 32


def _stash(csv_text):
    token = secrets.token_urlsafe(12)
    with _store_lock:
        while len(_store) >= _STORE_CAP:
            _store.pop(next(iter(_store)))
        _store[token] = csv_text
    return token


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
    token = _stash(report["csv"]) if report["clean"] else None
    return render_template("result.html", report=report, token=token)


@app.route("/download/<token>")
def download(token):
    with _store_lock:
        csv_text = _store.get(token)
    if csv_text is None:
        abort(404)
    return Response(
        csv_text.encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=credits.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
