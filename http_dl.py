# -*- coding: utf-8 -*-
"""HTTP 多线程分片下载引擎（纯标准库）
支持：Range 分片、多线程并发、断点续传(.part)、暂停/继续/取消、实时速度与进度回调。
与安卓版下载器理念一致：多线程分片 + 断点续传。
"""
import os
import re
import time
import threading
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FastDownloader/1.0"

def _headers(url, timeout=15):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return dict(r.headers), r.geturl()

def _filename(url, headers, final_url=None):
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    if m:
        from urllib.parse import unquote
        return os.path.basename(unquote(m.group(1)))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return os.path.basename(m.group(1))
    from urllib.parse import urlparse, unquote
    p = urlparse(final_url or url).path
    name = os.path.basename(unquote(p)) or "download"
    return name

class Task:
    """一个下载任务"""
    def __init__(self, url, save_path, threads=8, on_progress=None, on_done=None):
        self.url = url
        self.save_path = save_path
        self.threads = max(1, min(32, int(threads)))
        self.on_progress = on_progress   # cb(done_bytes, total, speed_bps, state)
        self.on_done = on_done           # cb(ok, message)

        self.total = 0
        self.done = 0
        self.speed = 0
        self.state = "准备中"            # 准备中/下载中/暂停/完成/失败/取消
        self.ranges = []                 # [(start,end,cur), ...]
        self.part = save_path + ".part"
        self.meta = save_path + ".partmeta"
        self._lock = threading.Lock()
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._threads = []
        self._workers_done = 0
        self._support_range = False

    def pause(self):
        if self.state in ("下载中", "准备中"):
            self._pause.set()
            self.state = "暂停"
            self._report()

    def resume(self):
        if self.state == "暂停":
            was_not_started = not self._threads   # 准备阶段暂停：worker 从未启动
            self._pause.clear()
            if was_not_started:
                self.state = "准备中"
                self._report()
                self.start()   # 重新走 _run（会读断点 meta 继续）
                return
            self.state = "下载中"
            self._report()

    def cancel(self):
        self._cancel.set()
        self._pause.clear()
        self.state = "取消"
        self._report()

    def _enter_downloading(self):
        """从准备阶段进入下载：尊重暂停/取消状态，不强行覆盖"""
        if self._cancel.is_set():
            self.state = "取消"
            return False
        if self._pause.is_set():
            self.state = "暂停"
            return False
        self.state = "下载中"
        return True

    def _report(self):
        if self.on_progress:
            try:
                self.on_progress(self.done, self.total, self.speed, self.state)
            except Exception:
                pass

    def _wait(self):
        """受暂停/取消控制：暂停时在此阻塞等待恢复，只有取消才返回 False"""
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.05)
        return not self._cancel.is_set()

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="dl-main")
        t.start()
        return self

    def _run(self):
        try:
            if not self._prepare():
                return
            if self._cancel.is_set():
                return
            if not self._enter_downloading():
                # 准备期间被暂停/取消：不启动 worker，等待由外部 resume 重新 start
                self._save_meta()
                if self._cancel.is_set() and self.on_done:
                    self.on_done(False, "已取消（进度已保留）")
                return
            self._report()
            # 已有断点进度
            self.done = sum(r[2] - r[0] for r in self.ranges)
            self._report()
            self._last_bytes = self.done
            self._last_t = time.time()

            self._workers_done = 0
            for i, rg in enumerate(self.ranges):
                if self._cancel.is_set():
                    break
                w = threading.Thread(target=self._worker, args=(i,), daemon=True, name="dl-%d" % i)
                self._threads.append(w)
                w.start()
            for w in self._threads:
                w.join()

            if self._cancel.is_set():
                self._save_meta()
                self.state = "取消"
                self._report()
                if self.on_done:
                    self.on_done(False, "已取消（进度已保留）")
                return

            if self._workers_done < len(self.ranges):
                self.state = "失败"
                self._report()
                if self.on_done:
                    self.on_done(False, "部分分片下载失败，可重试（进度已保留）")
                return

            # 合并分片 -> 目标文件
            ok = self._assemble()
            if ok:
                for f in (self.part, self.meta):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                self.done = self.total
                self.state = "完成"
                self._report()
                if self.on_done:
                    self.on_done(True, self.save_path)
            else:
                self.state = "失败"
                self._report()
                if self.on_done:
                    self.on_done(False, "合并文件失败")
        except Exception as e:
            self.state = "失败"
            self.speed = 0
            self._report()
            if self.on_done:
                self.on_done(False, "下载失败：" + str(e)[:100])

    def _prepare(self):
        # 断点续传：有 meta 且与 url 匹配则复用
        if os.path.exists(self.meta) and os.path.exists(self.part):
            try:
                import json
                m = json.load(open(self.meta, encoding="utf-8"))
                if m.get("url") == self.url and m.get("total", 0) > 0:
                    self.total = m["total"]
                    self.ranges = [list(x) for x in m["ranges"]]  # 必须是可变 list：worker 会回写 cur
                    self._support_range = m.get("range", False)
                    self.threads = len(self.ranges)
                    return True
            except Exception:
                pass
        hdr, final = _headers(self.url)
        total = int(hdr.get("Content-Length", 0) or 0)
        ar = hdr.get("Accept-Ranges", "").lower()
        self._support_range = ("bytes" in ar) and total > 0
        if not total:
            self.state = "失败"
            self._report()
            if self.on_done:
                self.on_done(False, "服务器未提供文件大小，无法分片下载")
            return False
        self.total = total
        n = self.threads if self._support_range else 1
        size = total // n
        self.ranges = []
        for i in range(n):
            s = i * size
            e = total - 1 if i == n - 1 else (s + size - 1)
            self.ranges.append([s, e, s])  # cur = 本片已完成到的位置
        # 预分配 .part 文件
        with open(self.part, "wb") as f:
            f.truncate(total)
        self._save_meta()
        return True

    def _save_meta(self):
        try:
            import json
            json.dump({"url": self.url, "total": self.total, "range": self._support_range,
                       "ranges": [list(r) for r in self.ranges]},
                      open(self.meta, "w", encoding="utf-8"))
        except Exception:
            pass

    def _worker(self, idx):
        try:
            while True:
                if not self._wait():      # 取消 -> 退出（保留断点）
                    return
                s, e, cur = self.ranges[idx]
                if cur > e:               # 本片完成
                    break
                end = e
                req = urllib.request.Request(self.url, headers={
                    "User-Agent": UA,
                    "Range": "bytes=%d-%d" % (cur, end),
                })
                try:
                    r = urllib.request.urlopen(req, timeout=30)
                except urllib.error.HTTPError as he:
                    if he.code == 416:    # 服务端认为已完成
                        cur = e + 1
                        with self._lock:
                            self.ranges[idx][2] = cur
                        break
                    raise
                with r, open(self.part, "r+b") as f:
                    f.seek(cur)
                    while True:
                        if not self._wait():  # 取消 -> 立即停（断点已由循环末保存）
                            self._save_meta()
                            return
                        buf = r.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        cur += len(buf)
                        with self._lock:
                            self.ranges[idx][2] = cur
                            self.done += len(buf)
                        self._tick_speed()
                        self._save_meta_throttled()
                with self._lock:
                    self.ranges[idx][2] = cur
                self._save_meta()
            self._workers_done += 1
        except Exception:
            pass

    _last_meta = 0.0
    def _save_meta_throttled(self):
        now = time.time()
        if now - Task._last_meta > 2.0:
            Task._last_meta = now
            self._save_meta()

    def _tick_speed(self):
        now = time.time()
        dt = now - self._last_t
        if dt >= 0.5:
            self.speed = int((self.done - self._last_bytes) / dt)
            self._last_bytes = self.done
            self._last_t = now
            self._report()

    def _assemble(self):
        # 单线程下载或不支持 Range 时，.part 已是完整文件
        try:
            os.replace(self.part, self.save_path)
            return os.path.exists(self.save_path) and os.path.getsize(self.save_path) == self.total
        except Exception:
            return False

def probe(url, timeout=15):
    """返回 (文件名, 总大小, 是否支持 Range, 最终URL)；失败抛异常"""
    hdr, final = _headers(url, timeout)
    total = int(hdr.get("Content-Length", 0) or 0)
    rng = "bytes" in hdr.get("Accept-Ranges", "").lower()
    return _filename(url, hdr, final), total, rng, final
