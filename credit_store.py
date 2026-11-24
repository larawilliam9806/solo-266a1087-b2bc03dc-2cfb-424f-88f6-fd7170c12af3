"""署名 CSV 的持久暂存。

设计要点：

- 每个署名表保存为 ``<过期时间戳>-<随机串>.csv`` 一个文件。文件内容先写入
  同目录临时文件，再用 ``os.replace`` 原子落盘；文件存在即表示保存完整。
- 过期时间写在文件名里，服务重启后无需额外状态即可判断链接是否有效。
- 总容量按所有 ``.csv`` 文件的实际字节数（``st_size``）统计；每次保存前先
  清理过期文件，让过期表占用的空间可以被新表复用。
- 线程锁 + ``flock`` 串行化「清理 → 容量检查 → 落盘」，并发核查也不会超限。
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat as stat_mod
import threading
import time
from pathlib import Path

try:  # POSIX（Docker / Linux）
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台仅保留线程锁
    fcntl = None

DEFAULT_TTL_SECONDS = 8 * 60 * 60

# 保存时间戳（十进制秒）+ '-' + 随机串；随机串由 token_urlsafe 生成。
_TOKEN_RE = re.compile(r"^(?P<expires>[0-9]{1,20})-(?P<nonce>[A-Za-z0-9_-]{16,64})$")
_TMP_PREFIX = ".tmp-"


class StoreError(Exception):
    """暂存操作失败（无法保存、读取或链接失效）。"""


class InvalidToken(StoreError):
    """链接格式不正确，或对应的署名表不存在。"""


class ExpiredToken(StoreError):
    """署名表已超过有效期。"""


def _parse_token(token: str):
    """返回 token 中记录的过期时间戳；格式不合法时返回 None。"""
    m = _TOKEN_RE.fullmatch(token)
    if m is None:
        return None
    return int(m.group("expires"))


class CreditStore:
    def __init__(self, root, max_bytes, ttl_seconds=DEFAULT_TTL_SECONDS, clock=None):
        if max_bytes < 0:
            raise ValueError("暂存容量上限不能为负数")
        if ttl_seconds <= 0:
            raise ValueError("有效期必须为正数")
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = int(ttl_seconds)
        self._clock = clock
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------------ 内部

    def _now(self):
        # 不把 time.time 绑定为默认参数，便于测试替换时钟。
        return time.time() if self._clock is None else self._clock()

    def _path(self, token):
        return self.root / f"{token}.csv"

    @contextlib.contextmanager
    def _locked(self):
        """同进程线程锁 + 跨进程文件锁。"""
        with self._thread_lock:
            with open(self.root / ".lock", "a+b") as fh:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _unlink_quiet(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _purge_expired_locked(self, now):
        """清理已过期的署名表和上次崩溃遗留的临时文件（调用方持锁）。"""
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return
        for name in names:
            path = self.root / name
            if name.startswith(_TMP_PREFIX):
                # 持锁期间不可能有本进程正在写的临时文件，残留即可清理。
                self._unlink_quiet(path)
                continue
            if not name.endswith(".csv"):
                continue
            expires_at = _parse_token(name[:-4])
            if expires_at is not None and expires_at <= now:
                self._unlink_quiet(path)

    def _used_locked(self):
        """按 CSV 文件实际字节数统计已用容量（调用方持锁）。"""
        total = 0
        try:
            entries = os.scandir(self.root)
        except FileNotFoundError:
            return 0
        with entries:
            for entry in entries:
                if not entry.name.endswith(".csv"):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat_mod.S_ISREG(st.st_mode):
                    total += st.st_size
        return total

    def _new_token_locked(self, expires_at):
        while True:
            token = f"{expires_at}-{secrets.token_urlsafe(18)}"
            if not self._path(token).exists():
                return token

    def _capacity_message(self, used, needed):
        return (
            f"暂存空间不足：本次署名表 {needed} 字节，"
            f"已用 {used} 字节，上限 {self.max_bytes} 字节"
        )

    # ------------------------------------------------------------------ 对外

    def save(self, data: bytes):
        """保存署名表，返回 ``(token, expires_at)``。

        容量不足、磁盘写入失败等均抛出 ``StoreError``，调用方据此不给出链接。
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data 必须是 bytes")
        data = bytes(data)

        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 目录不可用（权限、只读卷等）：报告照常显示，只是无法给出下载。
            raise StoreError(
                f"暂存目录不可用（{self.root}）：{exc.strerror or exc}"
            ) from exc

        try:
            with self._locked():
                now = self._now()
                # 先腾出过期表的空间，再按实际字节数检查上限。
                self._purge_expired_locked(now)
                used = self._used_locked()
                if used + len(data) > self.max_bytes:
                    raise StoreError(self._capacity_message(used, len(data)))

                expires_at = int(now + self.ttl_seconds)
                token = self._new_token_locked(expires_at)
                path = self._path(token)
                tmp = self.root / f"{_TMP_PREFIX}{token}"
                try:
                    with open(tmp, "xb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, path)
                except OSError as exc:
                    raise StoreError(
                        f"署名表写入失败：{exc.strerror or exc}"
                    ) from exc
                finally:
                    self._unlink_quiet(tmp)
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError(f"署名表保存失败：{exc.strerror or exc}") from exc

        return token, expires_at

    def fetch(self, token: str) -> bytes:
        """读取署名表内容；过期或不存在分别抛出对应异常。"""
        expires_at = _parse_token(token) if isinstance(token, str) else None
        if expires_at is None:
            raise InvalidToken("链接格式不正确")

        try:
            with self._locked():
                now = self._now()
                if now >= expires_at:
                    self._unlink_quiet(self._path(token))
                    raise ExpiredToken("链接已过期")
                try:
                    return self._path(token).read_bytes()
                except FileNotFoundError as exc:
                    raise InvalidToken("署名表不存在") from exc
                except OSError as exc:
                    raise StoreError(
                        f"署名表读取失败：{exc.strerror or exc}"
                    ) from exc
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError(f"署名表读取失败：{exc.strerror or exc}") from exc
