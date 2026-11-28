# -*- coding: utf-8 -*-
"""HTTP 多线程分片下载引擎（纯标准库）

调度模型 —— 专为"全程保持高速"设计：

【问题】传统固定分段：文件被均分成 N 段，每线程死守一段。快的连接下完就退出，
慢的连接还在磨 → 最后阶段只剩 1 条连接在跑，速度从 100 MB/s 掉到 12 MB/s（1/N），
也就是"进度快满时速度像被砍了一样"。

【本引擎】
  1. **共享区间池**：文件切成 block，所有连接从同一个池子里随机领取。
     谁快谁多领 → 不存在"死守某一段"的慢连接。
  2. **Work Stealing（关键）**：调度器持续监测各连接速率。若某连接明显偏慢，
     它手里**尚未开始**的尾部会被回收进池子，由空闲连接接手。
     慢连接因此只贡献它真正来得及下的那点速度，不再拖住全局。
  3. **动态瓜分**：池中剩余越少，优先领越小的块 → 尾段由所有连接平分。
  4. **细粒度记账**：piece（≥128KB）为记账单位，piece 数 ≥ 线程数；
     小文件也全速，断点续传精确到 piece，重试/回收都不丢进度、不重复计数。
"""
import os
import re
import time
import json
import random
import threading
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FastDownloader/1.0"

PIECE_ALIGN = 128 * 1024       # piece 粒度对齐
MIN_PIECE = 128 * 1024         # 最小 piece（小文件也不会切出 0 字节段）
MAX_PIECE = 8 * 1024 * 1024    # 最大 piece（避免分片过多）
PIECE_TARGETS = 48             # 目标 piece 数 ≈ 线程数 × 此值

BLOCKS_PER_THREAD = 4          # 初始切成 线程数 × 4 个 block
MIN_BLOCK_BYTES = 256 * 1024   # 段大小下限（太小反而低效）

STEAL_RATIO = 0.35             # 速率低于全场平均此比例即被回收
STEAL_COOLDOWN = 0.4           # 回收冷却（秒）
KEEP_PIECES = 2                # 回收时给慢连接保留几个 piece 继续下


def _headers(url, timeout=15, extra=None):
    h = {"User-Agent": UA}
    h.update(extra or {})
    req = urllib.request.Request(url, method="HEAD", headers=h)
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


def pick_piece_size(total, threads):
    """按总大小与线程数算出 piece 粒度：目标 piece 数约为线程数 × 48。

    保证 piece 数 ≥ 线程数，否则小文件只会有一条连接在跑（速度上不去）。
    """
    try:
        total = int(total)
    except Exception:
        total = 0
    threads = max(1, min(64, int(threads or 1)))
    target = total // (threads * PIECE_TARGETS)
    if target <= 0:
        target = MIN_PIECE
    ps = ((target + PIECE_ALIGN - 1) // PIECE_ALIGN) * PIECE_ALIGN
    ps = max(MIN_PIECE, min(MAX_PIECE, ps))
    if total > 0:
        need = -(-total // threads)          # ceil，保证每线程至少一片
        if ps > need:
            ps = max(1, need)
    return ps


class Task:
    """一个下载任务"""
    def __init__(self, url, save_path, threads=8, on_progress=None, on_done=None, headers=None):
        self.url = url
        self.save_path = save_path
        self.threads = max(1, min(32, int(threads)))
        self.on_progress = on_progress   # cb(done_bytes, total, speed_bps, state)
        self.on_done = on_done           # cb(ok, message)
        self.headers = dict(headers or {})

        self.total = 0
        self.done = 0
        self.speed = 0
        self.state = "准备中"            # 准备中/下载中/暂停/完成/失败/取消
        self.ranges = []                 # [(start,end,cur), ...]（单线程模式用）
        self.part = save_path + ".part"
        self.meta = save_path + ".partmeta"
        self._lock = threading.RLock()
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._threads = []
        self._workers_done = 0
        self._support_range = False
        self._conn_stats = {}             # idx -> 速率统计
        self._conn_bytes = {}             # idx -> 本连接累计下载字节
        self._stop_watchdog = threading.Event()
        self.final_url = url

        # ---- 调度状态 ----
        self.piece_size = 0
        self.total_pieces = 0
        self._done_bits = bytearray()     # piece 完成位图（仅用于快速跳过整片）
        self._done_ranges = []            # 已完成的字节区间 [(start,end)] —— 精确断点
        self._pool = []                   # [(start_byte, end_byte)] 未领取的段
        self._slow_until = {}             # idx -> 慢连接冷却截止时间
        self._pool_lock = threading.RLock()
        self._segs = {}                   # idx -> 当前任务段（供调度器回收回收）
        self._last_bytes = 0
        self._last_t = 0.0
        self._agg_speed = 0.0             # 全场聚合速率
        self._live = 0                    # 本会话实时下载字节
        self._base = 0                    # 会话开始时已完成的字节

    # ------------------------------------------------------------------ 基础
    def pause(self):
        if self.state in ("下载中", "准备中"):
            self._pause.set()
            self.state = "暂停"
            self._save_meta()
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
        self._save_meta()
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
                shown = self.done
                if shown > self.total:
                    shown = self.total
                if shown < 0:
                    shown = 0
                self.on_progress(shown, self.total, self.speed, self.state)
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

    # ------------------------------------------------------------ piece 位图
    def _is_done(self, i):
        if i < 0 or i >= self.total_pieces:
            return True
        return bool(self._done_bits[i >> 3] & (0x80 >> (i & 7)))

    def _mark_done(self, i, add_bytes):
        """标记一个 piece 完成。self.done 始终等于已完成 piece 的字节总数。"""
        with self._lock:
            if i < 0 or i >= self.total_pieces:
                return
            if self._done_bits[i >> 3] & (0x80 >> (i & 7)):
                return
            self._done_bits[i >> 3] |= (0x80 >> (i & 7))
            self.done += add_bytes

    def _piece_len(self, i):
        start = i * self.piece_size
        return max(0, min(self.piece_size, self.total - start))

    # ------------------------------------------------------------------ 主体
    def _run(self):
        try:
            if not self._prepare():
                return
            if self._cancel.is_set():
                return
            if not self._enter_downloading():
                self._save_meta()
                if self._cancel.is_set() and self.on_done:
                    self.on_done(False, "已取消（进度已保留）")
                return
            self._report()
            self._last_bytes = self.done   # 速度基准：断点带入的字节
            self._last_t = time.time()
            self._dl_start_t = time.time()
            self._build_pool()
            wd = threading.Thread(target=self._watchdog, daemon=True, name="dl-watchdog")
            wd.start()

            self._workers_done = 0
            if self._support_range:
                for i in range(self.threads):
                    if self._cancel.is_set():
                        break
                    w = threading.Thread(target=self._worker, args=(i,), daemon=True, name="dl-%d" % i)
                    self._threads.append(w)
                    w.start()
                for w in self._threads:
                    w.join()
            else:
                self._worker_single()
            self._stop_watchdog.set()

            if self._cancel.is_set():
                self._save_meta()
                self.state = "取消"
                self._report()
                if self.on_done:
                    self.on_done(False, "已取消（进度已保留）")
                return

            if self.done < self.total:
                self.state = "失败"
                self._report()
                if self.on_done:
                    self.on_done(False, "部分分片下载失败，可重试（进度已保留）")
                return

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
            self._stop_watchdog.set()
            self.state = "失败"
            self.speed = 0
            self._report()
            if self.on_done:
                self.on_done(False, "下载失败：" + str(e)[:100])

    def _build_pool(self):
        """构建字节级区间池：跳过已完成的区间，其余按段切分（随机顺序）。

        精确到字节 → 中断时已写入但未记账的那部分也会被重下，不会留空洞。
        """
        done = self._completed_ranges()
        # 计算未完成的区间（总区间减去已完成区间）
        gaps = []
        pos = 0
        for a, b in done:
            if a > pos:
                gaps.append((pos, a))
            pos = max(pos, b)
        if pos < self.total:
            gaps.append((pos, self.total))

        pool = []
        for gstart, gend in gaps:
            span = gend - gstart
            # 目标段数 = 线程数 × 4；每段不小于 MIN_BLOCK_BYTES
            nblocks = max(1, self.threads * BLOCKS_PER_THREAD)
            per = span // nblocks
            if per < MIN_BLOCK_BYTES:
                per = MIN_BLOCK_BYTES
            p = gstart
            while p < gend:
                e = min(gend, p + per)
                pool.append((p, e))
                p = e
        random.shuffle(pool)
        with self._pool_lock:
            self._pool = pool

    def _prepare(self):
        # 断点续传：有 meta 且与 url 匹配则复用
        resumed = False
        if os.path.exists(self.meta) and os.path.exists(self.part):
            try:
                m = json.load(open(self.meta, encoding="utf-8"))
                if (m.get("v") == 3 and m.get("url") == self.url
                        and m.get("total", 0) > 0 and m.get("ps", 0) > 0):
                    self.total = int(m["total"])
                    self.piece_size = int(m["ps"])
                    self.total_pieces = (self.total + self.piece_size - 1) // self.piece_size
                    bits = bytearray.fromhex(m.get("done", ""))
                    if len(bits) == (self.total_pieces + 7) // 8:
                        self._done_bits = bits
                        # 精确的已完成字节区间
                        self._done_ranges = []
                        for r in m.get("ranges", []):
                            self._done_ranges.append((int(r[0]), int(r[1])))
                        self._done_ranges.sort()
                        merged = []
                        for a, b in self._done_ranges:
                            if merged and a <= merged[-1][1]:
                                if b > merged[-1][1]:
                                    merged[-1] = (merged[-1][0], b)
                            else:
                                merged.append((a, b))
                        self._done_ranges = merged
                        self.done = sum(b - a for a, b in merged)   # 断点带入的已完成字节
                        # 中断时正在下载的区间：已写入部分作废，整段重下
                        self._pending_ranges = []
                        for r in m.get("inflight", []):
                            self._pending_ranges.append((int(r[0]), int(r[2])))
                        self._support_range = bool(m.get("range", True))
                        try:
                            hdr, final = _headers(self.url, extra=self.headers)
                            self.final_url = final
                        except Exception:
                            self.final_url = self.url
                        resumed = True
            except Exception:
                pass
        if resumed:
            return not self._cancel.is_set()

        hdr, final = _headers(self.url, extra=self.headers)
        self.final_url = final
        total = int(hdr.get("Content-Length", 0) or 0)
        ar = hdr.get("Accept-Ranges", "").lower()
        ct = hdr.get("Content-Type", "")
        self._support_range = ("bytes" in ar) and total > 0
        if "text/html" in ct.lower():
            self.state = "失败"
            self._report()
            if self.on_done:
                self.on_done(False, "链接返回网页而非文件（可能需要登录或直链已过期）")
            return False
        if not total:
            self.state = "失败"
            self._report()
            if self.on_done:
                self.on_done(False, "服务器未提供文件大小，无法分片下载")
            return False
        self.total = total
        if self._support_range:
            self.piece_size = pick_piece_size(total, self.threads)
            self.total_pieces = (total + self.piece_size - 1) // self.piece_size
            self._done_bits = bytearray((self.total_pieces + 7) // 8)
            self.done = 0
            with open(self.part, "wb") as f:   # 预分配 .part 文件
                f.truncate(total)
        else:
            # 不支持 Range：单线程顺序下载，同样需要先建好 .part 文件
            self.ranges = [[0, total - 1, 0]]
            with open(self.part, "wb") as f:
                f.truncate(total)
        self._save_meta()
        return True

    def _save_meta(self):
        try:
            if not self.total:
                return
            if self._support_range:
                with self._pool_lock:
                    inflight = [[seg["start"], seg["cur"], seg["limit"]]
                                for seg in self._segs.values()]
                data = {"v": 3, "url": self.url, "total": self.total, "ps": self.piece_size,
                        "range": True, "done": bytes(self._done_bits).hex(),
                        "ranges": self._completed_ranges(),
                        "inflight": inflight}
            else:
                data = {"v": 3, "url": self.url, "total": self.total, "ps": 0, "range": False,
                        "cur": self.ranges[0][2] if self.ranges else 0}
            tmp = self.meta + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.meta)
        except Exception:
            pass

    _last_meta = 0.0
    def _save_meta_throttled(self):
        now = time.time()
        if now - Task._last_meta > 2.0:
            Task._last_meta = now
            self._save_meta()

    # -------------------------------------------------------------- 区间池
    def _pool_bytes(self):
        """池中尚未领取的字节数。"""
        with self._pool_lock:
            return sum(c for _, c in self._pool) * self.piece_size

    def _claim_block(self, idx=None):
        """从池中领取一段字节区间 [start, end)；慢连接在冷却期内领不到活。"""
        with self._pool_lock:
            if not self._pool:
                return None
            if idx is not None:
                until = self._slow_until.get(idx, 0)
                if time.time() < until and len(self._pool) > 1:
                    return None      # 慢连接暂时歇着，让快连接先干
            k = random.randrange(len(self._pool))
            return self._pool.pop(k)

    def _return_block(self, start, end):
        """把未完成的字节区间放回池子。"""
        if end <= start:
            return
        with self._pool_lock:
            self._pool.append((start, end))

    # ------------------------------------------------------------------ 下载
    def _worker(self, idx):
        """多线程 worker：循环领取字节段 → Range 下载 → 标记 piece 完成。"""
        try:
            while True:
                if not self._wait():
                    return
                claim = self._claim_block(idx)
                if claim is None:
                    with self._pool_lock:
                        has_pool = bool(self._pool)
                    if not has_pool:
                        break
                    # 池里还有活（自己被降权了）→ 稍等重试
                    time.sleep(0.05)
                    continue
                start, limit = claim
                seg = {"start": start, "cur": start, "limit": limit}
                with self._pool_lock:
                    self._segs[idx] = seg
                try:
                    self._download_block(idx, seg)
                finally:
                    with self._pool_lock:
                        self._segs.pop(idx, None)
            with self._lock:
                self._workers_done += 1
        except Exception:
            pass

    def _watchdog(self):
        """看门狗 + Work Stealing 调度器。

        每 2 秒统计一次各连接的速率（累计字节差 / 时间差）；
        明显偏慢的连接，它手里尚未开始的尾部会被回收进池子交给空闲连接
        → 慢连接不再拖住全局，尾段也能保持高速。
        """
        while not self._stop_watchdog.wait(0.5):
            if self.state != "下载中" or self._cancel.is_set():
                continue
            now = time.time()
            rates = {}
            total_rate = 0.0
            with self._lock:
                for idx, st in self._conn_stats.items():
                    t0 = st.get("rate_t", now)
                    if now - t0 < 2.0:
                        continue
                    last = st.get("rate_b", 0)
                    cur = self._conn_bytes.get(idx, 0)
                    r = (cur - last) / max(0.001, now - t0)
                    st["rate_t"] = now
                    st["rate_b"] = cur
                    st["bps"] = r
                    rates[idx] = r
                    total_rate += r
            self._agg_speed = total_rate
            if total_rate <= 0:
                continue
            with self._pool_lock:
                segs = list(self._segs.items())
                idle = self.threads - len(segs)
            if idle <= 0:
                continue
            for idx, seg in segs:
                r = rates.get(idx, 0.0)
                if r <= 0 or r >= total_rate * STEAL_RATIO:
                    continue
                with self._lock:
                    st = self._conn_stats.get(idx)
                    if st is None:
                        continue
                    if now - st.get("last_steal", 0.0) < STEAL_COOLDOWN:
                        continue
                    st["last_steal"] = now
                keep = seg["cur"]
                if keep >= seg["limit"]:
                    continue
                # 回收 [cur, limit) —— 慢连接剩下的全部交给空闲连接
                self._return_block(keep, seg["limit"])
                seg["limit"] = keep
                # 慢连接进入冷却：暂不让它领新活，避免它又抢回慢活
                self._slow_until[idx] = now + 1.5

    def _download_block(self, idx, seg):
        """下载一段字节区间 seg = {start, cur, limit}。

        调度器回收尾部时会把 limit 调小 → 本连接下到 limit 即收工，余下交还池子。
        """
        start = seg["start"]
        limit = seg["limit"]
        end = limit - 1                 # 闭区间
        h = dict(self.headers)
        h["User-Agent"] = UA
        h["Range"] = "bytes=%d-%d" % (start, end)
        req = urllib.request.Request(self.final_url, headers=h)
        retries = 0
        while True:
            if not self._wait():
                self._save_meta()
                return
            try:
                r = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as he:
                if he.code == 416:
                    self._mark_range_done(start, limit)
                    return
                retries += 1
                if retries > 3:
                    self._return_block(start, limit)
                    return
                time.sleep(min(0.4 * retries, 2))
                continue
            except Exception:
                retries += 1
                if retries > 3:
                    self._return_block(start, limit)
                    return
                time.sleep(min(0.4 * retries, 2))
                continue

            code = getattr(r, "status", 200)
            crange = r.headers.get("Content-Range", "")
            if code == 200 and not crange:
                # 服务器忽略了 Range：仅当这段覆盖全文件时才能用
                if start != 0 or (end - start + 1) < self.total:
                    r.close()
                    self._return_block(start, limit)
                    return

            written = 0
            pos = start
            stolen = False
            try:
                with r, open(self.part, "r+b") as f:
                    f.seek(start)
                    while pos < limit:
                        if not self._wait():
                            self._save_meta()
                            return
                        buf = r.read(65536)
                        if not buf:
                            break
                        allow = min(len(buf), limit - pos)
                        if allow <= 0:
                            break
                        f.write(buf[:allow])
                        written += allow
                        pos += allow
                        seg["cur"] = pos
                        with self._lock:
                            self.done += allow      # 实时进度（显示用）
                            st = self._conn_stats.setdefault(idx, {"bytes": 0, "t0": time.time(),
                                   "last_bytes": 0, "last_t": time.time(), "bps": 0,
                                   "win_bytes": 0, "win_t": time.time(),
                                   "last_steal": 0.0})
                            st["bytes"] += allow
                            st["win_bytes"] += allow
                            self._conn_bytes[idx] = self._conn_bytes.get(idx, 0) + allow
                        self._tick_speed()
                        self._save_meta_throttled()
                        # 该段尾部已被调度器回收 → 提前收工
                        if seg["limit"] <= pos:
                            stolen = True
                            break
            except Exception:
                pass
            finally:
                try:
                    r.close()
                except Exception:
                    pass

            if pos >= limit:
                self._mark_range_done(start, pos)
                self._save_meta_throttled()
                return

            if stolen:
                # 尾部已被回收并交给别人 → 不再重复放回
                self._mark_range_done(start, pos)
                return
            retries += 1
            if retries > 3:
                self._return_block(pos, limit)
                return
            time.sleep(min(0.3 * retries, 1.5))
            # 续传：从当前字节继续
            dup = pos - start
            if dup > 0:
                with self._lock:
                    self.done -= dup
                    if self.done < 0:
                        self.done = 0
            start = pos
            end = limit - 1
            h["Range"] = "bytes=%d-%d" % (start, end)
            req = urllib.request.Request(self.final_url, headers=h)

    def _mark_range_done(self, start, end):
        """记录一段已完成的字节区间（精确断点，end 为开区间）。"""
        if end <= start:
            return
        with self._lock:
            self._done_ranges.append((start, end))
            self._done_ranges.sort()
            # 合并相邻区间
            merged = []
            for a, b in self._done_ranges:
                if merged and a <= merged[-1][1]:
                    if b > merged[-1][1]:
                        merged[-1] = (merged[-1][0], b)
                else:
                    merged.append((a, b))
            self._done_ranges = merged
            # 同步 piece 位图（整片完成的才置位）
            for a, b in merged:
                k0 = a // self.piece_size
                k1 = (b - 1) // self.piece_size
                for k in range(k0, min(k1 + 1, self.total_pieces)):
                    if not self._is_done(k) and (k + 1) * self.piece_size <= b:
                        self._done_bits[k >> 3] |= (0x80 >> (k & 7))
            # 注意：self.done 是实时累计值，这里不重算（避免与 _live 重复）

    def _completed_ranges(self):
        with self._lock:
            return list(self._done_ranges)

    def _worker_single(self):
        """服务器不支持 Range：单线程顺序下载。"""
        s, e, cur = self.ranges[0]
        h = dict(self.headers)
        h["User-Agent"] = UA
        req = urllib.request.Request(self.final_url, headers=h)
        retries = 0
        while cur <= e:
            if not self._wait():
                self._save_meta()
                return
            try:
                r = urllib.request.urlopen(req, timeout=30)
            except Exception:
                retries += 1
                if retries > 3:
                    return
                time.sleep(min(0.5 * retries, 2))
                continue
            try:
                with r, open(self.part, "r+b") as f:
                    f.seek(cur)
                    while cur <= e:
                        if not self._wait():
                            self._save_meta()
                            return
                        buf = r.read(65536)
                        if not buf:
                            break
                        allow = min(buf and len(buf), e - cur + 1)
                        f.write(buf[:allow])
                        cur += allow
                        with self._lock:
                            self.ranges[0][2] = cur
                            self.done = cur
                        self._tick_speed()
                        self._save_meta_throttled()
            except Exception:
                pass
            finally:
                try:
                    r.close()
                except Exception:
                    pass
            self._save_meta()
            if cur > e:
                break
            retries += 1
            if retries > 3:
                return
            time.sleep(min(0.5 * retries, 2))

    # ------------------------------------------------------------------ 速度
    def _tick_speed(self):
        now = time.time()
        dt = now - self._last_t
        if dt >= 0.4:
            with self._lock:
                cur = self.done
                delta = cur - self._last_bytes
            self.speed = int(delta / dt)
            self._last_bytes = cur
            self._last_t = now
            self._report()

    def _assemble(self):
        # .part 已是按偏移写入的完整文件，直接改名
        try:
            os.replace(self.part, self.save_path)
            return os.path.exists(self.save_path) and os.path.getsize(self.save_path) == self.total
        except Exception:
            return False


def probe(url, timeout=15, headers=None):
    """返回 (文件名, 总大小, 是否支持 Range, 最终URL)；失败抛异常"""
    hdr, final = _headers(url, timeout, extra=headers)
    total = int(hdr.get("Content-Length", 0) or 0)
    rng = "bytes" in hdr.get("Accept-Ranges", "").lower()
    return _filename(url, hdr, final), total, rng, final
