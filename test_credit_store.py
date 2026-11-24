"""credit_store 的单元测试：python3 test_credit_store.py"""
import os
import tempfile
import threading
import time
from pathlib import Path

import credit_store
from credit_store import (
    CreditStore,
    ExpiredToken,
    InvalidToken,
    StoreError,
)


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def new_store(tmp, max_bytes=1024, ttl=800, clock=None):
    return CreditStore(tmp, max_bytes, ttl, clock=clock)


def test_save_and_fetch():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = new_store(tmp, clock=clock)
        token, expires = s.save(b"abc")
        assert expires == 1_000_000 + 800
        assert s.fetch(token) == b"abc"
        # 同一链接可反复下载
        assert s.fetch(token) == b"abc"
        assert os.path.getsize(os.path.join(tmp, token + ".csv")) == 3


def test_persists_across_instances():
    """模拟服务重启：新实例（无内存状态）仍能读取。"""
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        token, _ = new_store(tmp, clock=clock).save(b"payload")
        again = new_store(tmp, clock=FakeClock())  # 时钟前进一点，未过期
        assert again.fetch(token) == b"payload"


def test_expiry_and_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = new_store(tmp, clock=clock)
        token, expires = s.save(b"x" * 10)
        clock.now = expires - 1
        assert s.fetch(token) == b"x" * 10
        clock.now = expires
        try:
            s.fetch(token)
        except ExpiredToken:
            pass
        else:
            raise AssertionError("过期应抛 ExpiredToken")
        assert not os.path.exists(os.path.join(tmp, token + ".csv"))


def test_expired_space_reused():
    """过期表占用的空间可被新表使用。"""
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = new_store(tmp, max_bytes=10, ttl=100, clock=clock)
        token, expires = s.save(b"0123456789")
        # 未过期时再存 1 字节应失败
        try:
            s.save(b"a")
        except StoreError:
            pass
        else:
            raise AssertionError("容量满时应失败")
        clock.now = expires
        token2, _ = s.save(b"new")
        assert s.fetch(token2) == b"new"
        # 旧链接已过期
        assert not os.path.exists(os.path.join(tmp, token + ".csv"))


def test_capacity_uses_actual_bytes():
    """按 CSV 文件实际字节数计入：恰好占满可以，超 1 字节即拒绝。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp, max_bytes=20)
        token, _ = s.save(b"a" * 20)
        assert s.fetch(token) == b"a" * 20
        try:
            s.save(b"b")
        except StoreError as e:
            assert "空间不足" in str(e)
        else:
            raise AssertionError("超出上限应失败")


def test_failed_save_leaves_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp, max_bytes=5)
        try:
            s.save(b"too large")
        except StoreError:
            pass
        else:
            raise AssertionError("应失败")
        assert [n for n in os.listdir(tmp) if n.endswith(".csv")] == []


def test_tmp_leftovers_purged():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".tmp-abc").write_bytes(b"junk")
        s = new_store(tmp, max_bytes=100)
        s.save(b"ok")
        assert not os.path.exists(os.path.join(tmp, ".tmp-abc"))


def test_invalid_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        for bad in ["", "nope", "../../etc/passwd", "123", "999999999999999999999-a" * 1]:
            try:
                s.fetch(bad)
            except InvalidToken:
                pass
            else:
                raise AssertionError(f"应拒绝 token: {bad!r}")
        # 格式合法但不存在
        try:
            s.fetch(f"{int(time.time()) + 100}-{'a' * 24}")
        except InvalidToken:
            pass
        else:
            raise AssertionError("不存在应抛 InvalidToken")


def test_concurrent_saves_respect_cap():
    """并发保存不会超过总上限。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp, max_bytes=100)
        results = []
        lock = threading.Lock()

        def worker():
            try:
                token, _ = s.save(b"x" * 10)
                ok = True
            except StoreError:
                ok = False
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = sum(
            os.path.getsize(os.path.join(tmp, n))
            for n in os.listdir(tmp)
            if n.endswith(".csv")
        )
        assert total <= 100, total
        assert sum(results) == 10, results


def test_unusable_store_dir_is_reported():
    """暂存目录不可用时抛 StoreError，而不是让请求崩溃。"""
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp, "not-a-dir")
        blocker.write_bytes(b"x")
        s = CreditStore(blocker / "data", 100, 800)
        try:
            s.save(b"data")
        except StoreError as e:
            assert "暂存目录不可用" in str(e)
        else:
            raise AssertionError("应失败")


def test_bad_init():
    with tempfile.TemporaryDirectory() as tmp:
        for kwargs in [{"max_bytes": -1}, {"max_bytes": 1, "ttl_seconds": 0}]:
            try:
                CreditStore(tmp, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f"应拒绝参数：{kwargs}")


def test_multiprocess_saves_respect_cap():
    """多进程（多个 worker）并发保存也遵守总上限，跨进程靠 flock。"""
    if not hasattr(os, "fork"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        n_proc, per_proc = 4, 10
        # 每个子进程独立创建 store（模拟独立 worker），先同步起跑
        pipe_r, pipe_w = os.pipe()
        pids = []
        for _ in range(n_proc):
            pid = os.fork()
            if pid == 0:
                os.close(pipe_w)
                try:
                    os.read(pipe_r, 1)  # 等待起跑信号
                    s = new_store(tmp, max_bytes=100)
                    ok = 0
                    for _ in range(per_proc):
                        try:
                            s.save(b"x" * 10)
                            ok += 1
                        except StoreError:
                            pass
                    os._exit(ok)
                except BaseException:
                    os._exit(200)
            pids.append(pid)
        os.close(pipe_r)
        os.write(pipe_w, b"!" * n_proc)
        os.close(pipe_w)
        total_ok = 0
        for pid in pids:
            _, status = os.waitpid(pid, 0)
            total_ok += os.waitstatus_to_exitcode(status)
        assert total_ok == 10, total_ok  # 100 字节 / 每份 10 字节
        total = sum(
            os.path.getsize(os.path.join(tmp, n))
            for n in os.listdir(tmp)
            if n.endswith(".csv")
        )
        assert total <= 100, total


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("全部测试通过")
