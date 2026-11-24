"""Flask 路由测试：python3 test_app.py"""
import io
import json
import os
import tempfile

import app as app_module

SAMPLE = {
    "board": {"width": 100, "height": 100},
    "photos": [
        {"id": "P1", "credit": "甲", "permit": {"x": 0, "y": 0, "w": 50, "h": 50}},
    ],
    "pieces": [
        {"photo": "P1", "crop": {"x": 0, "y": 0, "w": 10, "h": 10}, "at": {"x": 0, "y": 0}},
    ],
}

DIRTY = {
    "board": {"width": 100, "height": 100},
    "photos": [
        {"id": "P1", "credit": "甲", "permit": {"x": 0, "y": 0, "w": 5, "h": 5}},
    ],
    "pieces": [
        {"photo": "P1", "crop": {"x": 0, "y": 0, "w": 10, "h": 10}, "at": {"x": 0, "y": 0}},
    ],
}


def make_client(tmp, max_bytes=10_000):
    application = app_module.create_app({
        "TESTING": True,
        "CREDIT_STORE_DIR": tmp,
        "CREDIT_STORE_MAX_BYTES": max_bytes,
        "CREDIT_STORE_TTL_SECONDS": 800,
    })
    return application, application.test_client()


def post(client, payload=None):
    data = payload or SAMPLE
    return client.post(
        "/analyze",
        data={"file": (io.BytesIO(json.dumps(data).encode()), "input.json")},
        content_type="multipart/form-data",
    )


def download_path(html):
    start = html.index('href="/download/') + len('href="')
    end = html.index('"', start)
    return html[start:end]


def test_clean_flow_and_repeat_download():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp)
        r = post(client)
        assert r.status_code == 200
        assert "下载署名 CSV" in r.get_data(as_text=True)
        url = download_path(r.get_data(as_text=True))
        first = client.get(url)
        assert first.status_code == 200
        assert "text/csv" in first.headers["Content-Type"]
        body = first.get_data()
        assert body.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
        assert "P1" in body.decode("utf-8-sig")
        # 从磁盘重新读，模拟重启后新进程
        application2, client2 = make_client(tmp)
        assert client2.get(url).get_data() == body


def test_later_analyses_do_not_invalidate_earlier_link():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp)
        url1 = download_path(post(client).get_data(as_text=True))
        for _ in range(5):
            post(client)
        assert client.get(url1).status_code == 200


def test_dirty_report_has_no_link():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp)
        r = post(client, DIRTY)
        html = r.get_data(as_text=True)
        assert "无法下载署名 CSV" in html
        assert "/download/" not in html


def test_capacity_exceeded_still_shows_report():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp, max_bytes=5)
        r = post(client)
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "缺许可面积" in html
        assert "空间不足" in html
        assert "/download/" not in html


def test_expired_link_shows_recheck_page():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp)
        r = post(client)
        url = download_path(r.get_data(as_text=True))
        # 把 token 改成过期时间戳（格式合法、已过期）
        token = url.rsplit("/", 1)[1]
        expired_token = "1-" + token.split("-", 1)[1]
        resp = client.get(f"/download/{expired_token}")
        assert resp.status_code == 410
        assert "重新核查" in resp.get_data(as_text=True)


def test_unknown_and_malformed_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp)
        assert client.get("/download/deadbeef").status_code == 404
        assert client.get("/download/..%2f..%2fetc%2fpasswd").status_code == 404


def test_bad_json_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        application, client = make_client(tmp)
        r = client.post(
            "/analyze",
            data={"file": (io.BytesIO(b"{not json"), "x.json")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 400


def test_env_config_validation():
    old = os.environ.get("CREDIT_STORE_MAX_BYTES")
    try:
        os.environ["CREDIT_STORE_MAX_BYTES"] = "abc"
        try:
            app_module._env_int("CREDIT_STORE_MAX_BYTES", 1)
        except SystemExit:
            pass
        else:
            raise AssertionError("非整数应报错")
    finally:
        if old is None:
            os.environ.pop("CREDIT_STORE_MAX_BYTES", None)
        else:
            os.environ["CREDIT_STORE_MAX_BYTES"] = old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("全部测试通过")
