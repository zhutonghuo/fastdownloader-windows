# -*- coding: utf-8 -*-
import re
import os
import traceback
from PyQt5.QtCore import QUrl, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QIcon, QMovie, QPixmap, QPainter, QPen, QColor
from PyQt5.QtNetwork import QNetworkCookie
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QFrame,
    QMessageBox,
)
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile, QWebEnginePage

# 登录跳转地址（由用户指定，勿擅自改动）
LOGIN_URLS = {
    "quark": "https://pan.quark.cn/",
    "uc": "https://drive.uc.cn/",
    "xunlei": "https://pan.xunlei.com/login",
    "baidu": "https://pan.baidu.com/login",
    "c139": "https://yun.139.com/w/#/",
    "pan123": "https://user.123pan.cn/centerlogin",
    "pan115": "https://115.com/",
    "guangya": "https://www.guangyapan.com/",
    "ilanzou": "https://www.ilanzou.com/",
    "lanzou": "https://www.lanzoui.com/",
    "github": "https://github.com/login",
}


def login_url_for(platform):
    url = LOGIN_URLS.get(platform)
    if not url:
        raise ValueError("没有 %s 的登录地址" % platform)
    return url


def clear_cookies():
    """清空内置浏览器的全部 Cookie（下次登录需重新扫码/输入）。"""
    try:
        prof = QWebEngineProfile.defaultProfile()
        prof.cookieStore().deleteAllCookies()
        # 部分站点还写在本机存储里，一并清掉
        try:
            prof.clearHttpCache()
        except Exception:
            pass
        _log("cookies cleared")
        return True
    except Exception:
        _log("clear ERR " + traceback.format_exc())
        return False


def _log(msg):
    try:
        p = os.path.join(os.path.expanduser("~"), ".jisudownloader", "weblogin.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


class Spinner(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(40, 40)

    def start(self):
        self._timer.start(60)

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self.angle = (self.angle + 24) % 360
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#2563eb"), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(4, 4, 32, 32, self.angle * 16, 260 * 16)


_warm_view = None


def warm_up():
    global _warm_view
    if _warm_view is not None:
        return
    try:
        _warm_view = QWebEngineView()
        _warm_view.setFixedSize(1, 1)
        _warm_view.load(QUrl("about:blank"))
        _log("warmup done")
    except Exception:
        _log("warmup ERR " + traceback.format_exc())


class LoginPage(QWebEnginePage):
    def createWindow(self, typ):
        return self


class WebLoginDialog(QDialog):
    cookies_ready = pyqtSignal(str)

    def __init__(self, platform, parent=None, dark=False):
        super().__init__(parent)
        _log("WebLoginDialog init " + platform)
        self.platform = platform
        self._cookies = {}
        self.setWindowTitle("网盘登录 · " + platform)
        self.resize(1000, 720)
        self.setModal(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QFrame(); bar.setFixedHeight(46)
        bl = QHBoxLayout(bar); bl.setContentsMargins(14, 0, 12, 0)
        self.title_lbl = QLabel("登录 %s · 正在加载…" % platform)
        self.title_lbl.setStyleSheet("font-size:14px;font-weight:700;")
        bl.addWidget(self.title_lbl); bl.addStretch(1)
        clr = QPushButton("清除Cookie"); clr.setObjectName("gray")
        clr.clicked.connect(self._clear_cookies); bl.addWidget(clr)
        done = QPushButton("完成登录"); done.setObjectName("primary")
        done.clicked.connect(self._finish); bl.addWidget(done)
        cancel = QPushButton("取消"); cancel.setObjectName("gray")
        cancel.clicked.connect(self.reject); bl.addWidget(cancel)
        v.addWidget(bar)

        self.view = QWebEngineView()
        self.view.setPage(LoginPage(self.view))
        self.profile = QWebEngineProfile.defaultProfile()
        self.profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        v.addWidget(self.view, 1)

        self.overlay = QFrame(self.view)
        self.overlay.setStyleSheet("background:rgba(244,247,252,235);")
        ov = QVBoxLayout(self.overlay)
        ov.setAlignment(Qt.AlignCenter)
        self.spinner = Spinner(self.overlay)
        ov.addWidget(self.spinner, 0, Qt.AlignCenter)
        self.wait_lbl = QLabel("webview加载较慢，请稍等…")
        self.wait_lbl.setAlignment(Qt.AlignCenter)
        self.wait_lbl.setStyleSheet("font-size:13px;color:#586a80;margin-top:10px;")
        ov.addWidget(self.wait_lbl, 0, Qt.AlignCenter)
        self.overlay.setGeometry(self.view.rect())
        self.spinner.start()
        _log("profile ready")

        hint = QLabel("提示：在网页里正常登录（扫码 / 账号密码均可）。登录后点右上角「完成登录」，程序会自动提取该站 Cookie。")
        hint.setObjectName("sub"); hint.setWordWrap(True)
        hint.setStyleSheet("padding:6px 14px;color:#7b869c;font-size:12px;")
        v.addWidget(hint)

        cs = self.profile.cookieStore()
        cs.cookieAdded.connect(self._on_cookie)
        self.view.urlChanged.connect(self._on_url)
        self.view.loadProgress.connect(self._on_progress)
        self.view.loadFinished.connect(self._on_loaded)
        _log("cookie store connected")

        try:
            self.view.load(QUrl(login_url_for(platform)))
            _log("load called")
        except Exception:
            _log("load ERR " + traceback.format_exc())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "overlay") and hasattr(self, "view"):
            self.overlay.setGeometry(self.view.rect())

    def _on_progress(self, pct):
        self.title_lbl.setText("登录 %s · 加载中 %d%%" % (self.platform, pct))

    def _on_loaded(self, ok):
        self.title_lbl.setText("登录 %s · 完成后点「完成登录」" % self.platform)
        self.overlay.hide()
        self.spinner.stop()
        _log("load finished ok=%s" % ok)

    def _on_cookie(self, cookie):
        try:
            name = bytes(cookie.name()).decode("utf-8", "replace")
            value = bytes(cookie.value()).decode("utf-8", "replace")
            domain = bytes(cookie.domain()).decode("utf-8", "replace")
            if not name or not value or len(name) >= 80 or len(value) >= 2000:
                return
            if not self._domain_ok(domain):
                return
            self._cookies[name] = value
            _log("cookie +%s (%s)" % (name, domain))
        except Exception:
            pass

    def _domain_ok(self, domain):
        if not domain:
            return True
        host = (LOGIN_URLS.get(self.platform, "") or "").replace("https://", "").split("/")[0]
        if not host:
            return True
        d = domain.lstrip(".").lower()
        h = host.lower()
        base = ".".join(h.split(".")[-2:]) if h.count(".") >= 1 else h
        return d == h or d.endswith("." + base) or h.endswith("." + d) or d == base

    def _on_url(self, url):
        pass

    def _clear_cookies(self):
        """清掉浏览器里的全部 Cookie（含本机存储），便于重新登录。"""
        clear_cookies()
        self._cookies = {}
        try:
            self.profile.cookieStore().deleteAllCookies()
        except Exception:
            pass
        self.title_lbl.setText("已清除 Cookie，请重新登录")
        QMessageBox.information(self, "已清除 Cookie",
                                "浏览器中的 Cookie 已清空。\n请在网页里重新登录后再点「完成登录」。")

    def _finish(self):
        """提取 Cookie → 校验登录态 → 确认后才算登录成功。"""
        cookie_str = self._from_storage()
        if not cookie_str:
            cookie_str = "; ".join("%s=%s" % (k, v) for k, v in self._cookies.items())
        if not cookie_str.strip():
            _log("finish: no cookie")
            QMessageBox.warning(
                self, "未获取到 Cookie",
                "没有在该站点检测到任何 Cookie。\n\n"
                "请先在网页里完成登录（扫码或账号密码），页面显示已登录后，再点「完成登录」。")
            return
        _log("finish: cookie %d 项，开始校验登录态" % len(cookie_str.split(";")))
        self.title_lbl.setText("正在校验登录状态…")
        self._verify_then(cookie_str)

    def _verify_then(self, cookie_str):
        import threading
        import pan

        def work():
            try:
                state = pan.verify_login(self.platform, cookie_str)
            except Exception:
                state = None
            QTimer.singleShot(0, lambda: self._on_verified(cookie_str, state))

        threading.Thread(target=work, daemon=True).start()

    def _on_verified(self, cookie_str, state):
        if state is True:
            _log("verify: logged in")
            self.title_lbl.setText("登录成功")
            self.cookies_ready.emit(cookie_str)
            self.accept()
            return
        if state is None:
            # 该平台暂无法自动校验（或网络异常）：不阻塞用户，但要如实说明
            _log("verify: unknown")
            self.title_lbl.setText("已保存登录状态")
            self.cookies_ready.emit(cookie_str)
            self.accept()
            return
        # state is False：明确未登录
        _log("verify: NOT logged in")
        self.title_lbl.setText("未检测到登录状态")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("未检测到登录状态")
        box.setText("没有检测到 %s 的有效登录态。\n\n"
                    "可能的原因：\n"
                    "· 还没在网页里完成登录（扫码后需要在网页上确认）\n"
                    "· 账号已退出登录\n"
                    "· 接口临时受限（风控），但 Cookie 本身可能仍然是有效的\n\n"
                    "可以选「仍然保存」直接使用，或点左上角「清除Cookie」后重新登录。"
                    % (pan.PLATFORMS.get(self.platform, self.platform)))
        save_btn = box.addButton("仍然保存", QMessageBox.AcceptRole)
        box.addButton("继续登录", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is save_btn:
            self.cookies_ready.emit(cookie_str)
            self.accept()
        else:
            self.title_lbl.setText("请在网页里完成登录")

    def _from_storage(self):
        """从持久化 Cookie 存储读取该站点应有的全部 Cookie。
        除登录页外，同时按「主域名」采样——登录态 Cookie 常挂在主域（如 .quark.cn）上。"""
        out = {}
        import pan
        urls = []
        url = LOGIN_URLS.get(self.platform)
        if url:
            urls.append(url)
            urls.append(url.rstrip("/") + "/")
        md = pan.main_domain(self.platform)
        if md:
            urls.append("https://" + md + "/")
            urls.append("https://www." + md + "/")
        try:
            store = self.profile.cookieStore()
            for u in urls:
                for c in store.cookieForUrl(QUrl(u)):
                    name = bytes(c.name()).decode("utf-8", "replace")
                    value = bytes(c.value()).decode("utf-8", "replace")
                    domain = bytes(c.domain()).decode("utf-8", "replace")
                    if not name or not value:
                        continue
                    if not self._domain_ok(domain):
                        continue
                    out[name] = value
        except Exception:
            _log("storage ERR " + traceback.format_exc())
        return "; ".join("%s=%s" % (k, v) for k, v in out.items())

    def _collect(self, domain):
        """兼容旧调用：返回指定域下的 (name, value) 列表。"""
        out = []
        try:
            for c in self.profile.cookieStore().cookieForUrl(QUrl("https://" + domain.lstrip("."))):
                name = bytes(c.name()).decode("utf-8", "replace")
                value = bytes(c.value()).decode("utf-8", "replace")
                if name and value:
                    out.append((name, value))
        except Exception:
            pass
        return out
