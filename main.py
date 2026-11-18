# -*- coding: utf-8 -*-
"""
极速下载器 Windows 版 —— 与安卓版互通的局域网互传工具
纯 PyQt5 原生控件 + 现代 QSS（非 WebView、非 win32 原生丑控件）
支持：开房间 / 加入房间、聊天、文件与文件夹上传、下载、内置预览、仅撤回自己、深浅主题
"""
import os
import sys
import json
import time
import uuid
import threading
import webbrowser
import urllib.request

from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QUrl, QPoint, QSize
from PyQt5.QtGui import QFont, QPixmap, QImage, QIcon, QCursor, QPainter, QColor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QLineEdit, QVBoxLayout, QHBoxLayout,
    QFrame, QStackedWidget, QScrollArea, QFileDialog, QMessageBox, QProgressBar,
    QDialog, QSizePolicy, QGraphicsDropShadowEffect, QComboBox, QCheckBox, QMenu
)
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget

import lan_core as L
import theme
from bubble import Bubble

APP_VERSION = L.VERSION
ME = uuid.uuid4().hex[:10]
MY_NAME = "Windows-" + ME[:4]

def res_path(name):
    """兼容 pyinstaller 单文件：资源在 sys._MEIPASS"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

# ---------------- 自绘确认对话框（页面内风格，非系统原生） ----------------

class ConfirmDialog(QDialog):
    def __init__(self, title, message, dark=False, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._dark = dark
        self.result_ok = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        card = QFrame()
        card.setObjectName("card")
        card.setMinimumWidth(330)
        self._paint_card(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        t = QLabel(title); t.setObjectName("h2"); t.setStyleSheet("font-size:16px;font-weight:700;")
        m = QLabel(message); m.setWordWrap(True)
        m.setStyleSheet("color:%s;font-size:13px;" % ("#aeb9cf" if dark else "#55627a"))
        lay.addWidget(t); lay.addWidget(m)
        row = QHBoxLayout(); row.addStretch(1)
        no = QPushButton("取消"); no.setObjectName("gray")
        yes = QPushButton("确认"); yes.setObjectName("primary")
        no.clicked.connect(self._no); yes.clicked.connect(self._yes)
        row.addWidget(no); row.addWidget(yes)
        lay.addLayout(row)
        outer.addWidget(card)

    def _paint_card(self, card):
        bg = "#1e2536" if self._dark else "#ffffff"
        bd = "#2c3547" if self._dark else "#e4e9f2"
        fg = "#e6ebf5" if self._dark else "#1e2536"
        card.setStyleSheet("QFrame{background:%s;border:1px solid %s;border-radius:16px;} QLabel{color:%s;}" % (bg, bd, fg))
        sh = QGraphicsDropShadowEffect(card)
        sh.setBlurRadius(30); sh.setOffset(0, 8); sh.setColor(QColor(0, 0, 0, 120))
        card.setGraphicsEffect(sh)

    def _yes(self):
        self.result_ok = True; self.accept()

    def _no(self):
        self.result_ok = False; self.reject()

    def mousePressEvent(self, e):
        self._drag = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and hasattr(self, "_drag"):
            self.move(e.globalPos() - self._drag)

# ---------------- 内置播放器对话框 ----------------

class PreviewDialog(QDialog):
    def __init__(self, file_path, media, dark=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("预览 · " + os.path.basename(file_path))
        self.resize(880, 620)
        self._dark = dark
        self.player = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        if media == "image":
            pic = QLabel(); pic.setAlignment(Qt.AlignCenter)
            pm = QPixmap(file_path)
            if not pm.isNull():
                pm = pm.scaled(840, 560, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pic.setPixmap(pm)
            else:
                pic.setText("图片加载失败")
            lay.addWidget(pic)
        elif media in ("video", "audio"):
            if media == "video":
                self.vw = QVideoWidget()
                lay.addWidget(self.vw, 1)
            self.player = QMediaPlayer()
            if media == "video":
                self.player.setVideoOutput(self.vw)
            self.player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
            bar = QHBoxLayout()
            play = QPushButton("播放/暂停")
            play.clicked.connect(self._toggle)
            bar.addWidget(play)
            bar.addStretch(1)
            lay.addLayout(bar)
            self.player.play()
        else:
            lab = QLabel("此类型无法内置预览，请下载后打开"); lay.addWidget(lab)

    def _toggle(self):
        if self.player:
            if self.player.state() == QMediaPlayer.PlayingState:
                self.player.pause()
            else:
                self.player.play()

    def closeEvent(self, e):
        if self.player:
            self.player.stop()
        super().closeEvent(e)

# ---------------- 通用工具 ----------------

def pick_save_path(default_name, parent):
    path, _ = QFileDialog.getSaveFileName(parent, "保存到", default_name, "所有文件 (*.*)")
    return path or None

# ---------------- 主窗口 ----------------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("极速下载器 Windows · 局域网互传（与安卓互通）")
        self.resize(1040, 760)
        self.setMinimumSize(920, 640)
        self.dark = False
        self.hub = None            # 本机开的房间（host）
        self.client = None         # 加入安卓房间（guest）
        self.mode = "host"         # host / guest
        self.room_on = False
        self.msgs = []
        self.last_sig = ""
        self.clear_ts = 0
        self.seen_ts = 0
        self.transfer_threads = []
        self._drag_pos = None
        self.setObjectName("root")
        self._build()
        self.apply_theme()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start(2500)

    # ---------- 构建 UI ----------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自绘标题栏
        titlebar = QFrame(); titlebar.setFixedHeight(52)
        titlebar.setStyleSheet("background:transparent;")
        tb = QHBoxLayout(titlebar); tb.setContentsMargins(18, 0, 12, 0)
        brand = QLabel("⚡ 极速下载器")
        brand.setStyleSheet("font-size:16px;font-weight:800;color:#2f6fed;")
        sub = QLabel("Windows 版 · 与安卓互通")
        sub.setObjectName("sub")
        tb.addWidget(brand); tb.addWidget(sub); tb.addStretch(1)
        theme_btn = QPushButton("◐ 主题")
        theme_btn.setObjectName("ghost"); theme_btn.clicked.connect(self.toggle_theme)
        tb.addWidget(theme_btn)
        mn = QPushButton("—"); mn.setFixedWidth(34); mn.clicked.connect(self.showMinimized)
        cls = QPushButton("✕"); cls.setFixedWidth(34); cls.setObjectName("red")
        cls.clicked.connect(self.close)
        tb.addWidget(mn); tb.addWidget(cls)
        root.addWidget(titlebar)
        self.titlebar = titlebar

        body = QHBoxLayout(); body.setContentsMargins(16, 0, 16, 16); body.setSpacing(14)

        # 左侧：模式与房间信息
        left = QFrame(); left.setObjectName("card"); left.setFixedWidth(340)
        lv = QVBoxLayout(left); lv.setContentsMargins(18, 18, 18, 18); lv.setSpacing(12)
        tabrow = QHBoxLayout()
        self.tab_host = QPushButton("开房间"); self.tab_host.setCheckable(True); self.tab_host.setChecked(True)
        self.tab_guest = QPushButton("加入房间"); self.tab_guest.setCheckable(True)
        self.tab_host.setObjectName("tab"); self.tab_guest.setObjectName("tab")
        self.tab_host.clicked.connect(lambda: self.switch_mode("host"))
        self.tab_guest.clicked.connect(lambda: self.switch_mode("guest"))
        tabrow.addWidget(self.tab_host); tabrow.addWidget(self.tab_guest)
        lv.addLayout(tabrow)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_host_panel())
        self.stack.addWidget(self._build_guest_panel())
        lv.addWidget(self.stack)
        lv.addStretch(1)
        body.addWidget(left)

        # 右侧：聊天区
        chatcard = QFrame(); chatcard.setObjectName("card")
        cv = QVBoxLayout(chatcard); cv.setContentsMargins(16, 14, 16, 14); cv.setSpacing(10)
        head = QHBoxLayout()
        h2 = QLabel("聊天 · 文件互传"); h2.setObjectName("h2")
        self.status = QLabel("未连接"); self.status.setObjectName("badge")
        head.addWidget(h2); head.addWidget(self.status); head.addStretch(1)
        clr = QPushButton("清空聊天"); clr.setObjectName("small"); clr.setProperty("cls","gray")
        clr.clicked.connect(self.ask_clear)
        head.addWidget(clr)
        cv.addLayout(head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.chatbox = QWidget(); self.chatlist = QVBoxLayout(self.chatbox)
        self.chatlist.setContentsMargins(4, 4, 4, 4); self.chatlist.setSpacing(2)
        self.chatlist.addStretch(1)
        self.scroll.setWidget(self.chatbox)
        cv.addWidget(self.scroll, 1)

        self.newmsg = QPushButton("↓ 新消息"); self.newmsg.setObjectName("small")
        self.newmsg.hide(); self.newmsg.clicked.connect(self.goto_bottom)

        inp = QHBoxLayout()
        self.input = QLineEdit(); self.input.setPlaceholderText("说点什么，回车发送…")
        self.input.returnPressed.connect(self.send_text)
        send = QPushButton("发送"); send.clicked.connect(self.send_text)
        inp.addWidget(self.input); inp.addWidget(send)
        cv.addLayout(inp)
        tools = QHBoxLayout()
        upf = QPushButton("📎 上传文件"); upf.setObjectName("ghost"); upf.clicked.connect(self.upload_files)
        upd = QPushButton("📁 上传文件夹"); upd.setObjectName("gray"); upd.clicked.connect(self.upload_folder)
        tools.addWidget(upf); tools.addWidget(upd); tools.addStretch(1)
        cv.addLayout(tools)
        self.progress = QProgressBar(); self.progress.hide(); cv.addWidget(self.progress)
        body.addWidget(chatcard)
        root.addLayout(body, 1)

    def _build_host_panel(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        tip = QLabel("开启房间后，同一 Wi-Fi 下的安卓 App 或浏览器输入地址 + 口令即可加入，无需服务器。")
        tip.setObjectName("sub"); tip.setWordWrap(True); v.addWidget(tip)

        self.code_lbl = QLabel("----"); self.code_lbl.setObjectName("codebig")
        self.code_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(QLabel("房间口令", alignment=Qt.AlignCenter))
        v.addWidget(self.code_lbl)

        self.addr_lbl = QLabel("地址：-"); self.addr_lbl.setObjectName("addr")
        self.addr_lbl.setAlignment(Qt.AlignCenter); self.addr_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.addr_lbl)

        self.qr_lbl = QLabel(); self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setMinimumHeight(190); v.addWidget(self.qr_lbl)

        row = QHBoxLayout()
        copy = QPushButton("复制地址"); copy.setObjectName("ghost"); copy.clicked.connect(self.copy_addr)
        openb = QPushButton("浏览器打开"); openb.setObjectName("gray"); openb.clicked.connect(self.open_browser)
        row.addWidget(copy); row.addWidget(openb)
        v.addLayout(row)

        lenrow = QHBoxLayout(); lenrow.addWidget(QLabel("口令位数"))
        self.code_len = QComboBox(); self.code_len.addItems(["4", "5", "6"]); lenrow.addWidget(self.code_len)
        v.addLayout(lenrow)

        self.host_btn = QPushButton("开启房间"); self.host_btn.clicked.connect(self.toggle_host)
        v.addWidget(self.host_btn)
        return w

    def _build_guest_panel(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        tip = QLabel("输入安卓 App（或 Windows）开启的房间地址与口令，即可加入一起互传。")
        tip.setObjectName("sub"); tip.setWordWrap(True); v.addWidget(tip)
        v.addWidget(QLabel("主机地址（IPv4:端口）"))
        self.guest_addr = QLineEdit(); self.guest_addr.setPlaceholderText("例如 192.168.1.8:45321")
        v.addWidget(self.guest_addr)
        v.addWidget(QLabel("数字口令"))
        self.guest_code = QLineEdit(); self.guest_code.setObjectName("code")
        self.guest_code.setPlaceholderText("····"); self.guest_code.setMaxLength(8)
        v.addWidget(self.guest_code)
        v.addWidget(QLabel("我的昵称"))
        self.guest_name = QLineEdit(); self.guest_name.setPlaceholderText(MY_NAME)
        v.addWidget(self.guest_name)
        self.guest_btn = QPushButton("加入房间"); self.guest_btn.clicked.connect(self.toggle_guest)
        v.addWidget(self.guest_btn)
        hint = QLabel("提示：可用安卓端生成的二维码链接，口令会自动带入。")
        hint.setObjectName("sub"); hint.setWordWrap(True); v.addWidget(hint)
        return w

    # ---------- 主题 ----------
    def apply_theme(self):
        QApplication.instance().setStyleSheet(theme.DARK if self.dark else theme.LIGHT)
        if self.hub and self.room_on and self.mode == "host":
            self.render_qr()

    def toggle_theme(self):
        self.dark = not self.dark
        self.apply_theme()
        self.render_chat()

    # ---------- 无边框拖动 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.titlebar.underMouse():
            self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    # ---------- 模式切换 ----------
    def switch_mode(self, mode):
        if self.room_on:
            QMessageBox.information(self, "提示", "请先关闭当前房间/退出后再切换模式。")
            # 恢复按钮状态
            self.tab_host.setChecked(self.mode == "host")
            self.tab_guest.setChecked(self.mode == "guest")
            return
        self.mode = mode
        self.tab_host.setChecked(mode == "host")
        self.tab_guest.setChecked(mode == "guest")
        self.stack.setCurrentIndex(0 if mode == "host" else 1)
        self.set_status("未连接")

    # ---------- 开房间（host） ----------
    def toggle_host(self):
        if self.room_on:
            self.stop_room()
            return
        n = int(self.code_len.currentText())
        page = open(res_path("assets/lan.html"), "rb").read()
        self.hub = L.LanHub(code_len=n, page_bytes=page)
        try:
            self.hub.start()
        except Exception as e:
            self.hub = None
            self.toast("开启失败：" + str(e)[:80])
            return
        self.room_on = True
        self.mode = "host"
        self.code_lbl.setText(self.hub.code)
        self.addr_lbl.setText(self.hub.url())
        self.host_btn.setText("关闭房间"); self.host_btn.setObjectName("red")
        self.host_btn.setStyleSheet("background:#fdeeee;color:#d64545;")
        self.render_qr()
        self.set_status("房间已开启 · %d 人" % self.peer_count())
        # host 自己先发一条系统提示，便于确认可用
        self.msgs = []
        self.last_sig = ""
        self.render_chat()

    def render_qr(self):
        try:
            import qrcode
            img = qrcode.make(self.hub.url() + "/?code=" + self.hub.code, box_size=6, border=2)
            img = img.convert("RGB")
            data = img.tobytes()
            w, h = img.size
            qimg = QImage(data, w, h, QImage.Format_RGB888)
            self.qr_lbl.setPixmap(QPixmap.fromImage(qimg).scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception as e:
            self.qr_lbl.setText("二维码生成失败：" + str(e)[:40])

    def copy_addr(self):
        if not self.hub:
            return
        text = self.hub.url() + "  口令 " + self.hub.code
        QApplication.clipboard().setText(text)
        self.toast("已复制地址与口令")

    def open_browser(self):
        if self.hub:
            webbrowser.open(self.hub.url() + "/?code=" + self.hub.code)

    # ---------- 加入房间（guest，连安卓） ----------
    def toggle_guest(self):
        if self.room_on:
            self.stop_room()
            return
        addr = self.guest_addr.text().strip()
        code = self.guest_code.text().strip()
        name = self.guest_name.text().strip() or MY_NAME
        if not addr or not code:
            self.toast("请填写地址与口令")
            return
        self.guest_btn.setText("连接中…"); self.guest_btn.setEnabled(False)
        self.client = L.LanClient(addr, code, ME, name)
        def work():
            r = self.client.join()
            self.guest_btn.setEnabled(True)
            if r.ok:
                self.room_on = True
                self.guest_btn.setText("退出房间"); self.guest_btn.setObjectName("red")
                self.guest_btn.setStyleSheet("background:#fdeeee;color:#d64545;")
                self.set_status("已加入 " + code)
                self.msgs = []; self.last_sig = ""; self.clear_ts = 0; self.seen_ts = 0
            else:
                self.guest_btn.setText("加入房间")
                self.toast("加入失败：" + r.msg)
        threading.Thread(target=work, daemon=True).start()

    # ---------- 统一关闭 ----------
    def stop_room(self):
        if self.mode == "host" and self.hub:
            self.hub.stop(); self.hub = None
            self.code_lbl.setText("----"); self.addr_lbl.setText("地址：-"); self.qr_lbl.clear()
            self.host_btn.setText("开启房间")
            self.host_btn.setStyleSheet("")
        else:
            self.client = None
            self.guest_btn.setText("加入房间"); self.guest_btn.setStyleSheet("")
        self.room_on = False
        self.msgs = []; self.last_sig = ""; self.clear_ts = 0; self.seen_ts = 0
        self.set_status("未连接")
        self.render_chat()

    # ---------- 轮询 ----------
    def poll(self):
        if not self.room_on:
            return
        if self.mode == "host":
            msgs = self.hub.chat_json() if self.hub else []
            self.set_status("房间已开启 · %d 人" % self.peer_count())
        else:
            try:
                r = self.client.chat()
                if not r.ok:
                    self.set_status("断开：" + r.msg[:16]); return
                msgs = r.msgs
            except Exception:
                return
        sig = "|".join(m.get("id", "") + m.get("kind", "") for m in msgs)
        if sig != self.last_sig:
            self.last_sig = sig
            self.msgs = msgs
            self.render_chat()

    def peer_count(self):
        if not self.hub:
            return 1
        with self.hub.lock:
            now = time.time()
            for k in list(self.hub.peers):
                if now - self.hub.peers[k] > 90:
                    self.hub.peers.pop(k, None)
            return len(self.hub.peers) + 1

    # ---------- 渲染聊天 ----------
    def render_chat(self):
        while self.chatlist.count() > 1:
            item = self.chatlist.takeAt(self.chatlist.count() - 1)
            w = item.widget()
            if w:
                w.setParent(None); w.deleteLater()
        shown = [m for m in self.msgs if m.get("time", 0) > self.clear_ts]
        if not shown:
            empty = QLabel("还没有消息，发条消息或上传文件试试～")
            empty.setObjectName("sub"); empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(120)
            self.chatlist.insertWidget(0, empty)
            return
        near_bottom = (self.scroll.verticalScrollBar().value() >=
                       self.scroll.verticalScrollBar().maximum() - 60)
        max_t = max(m.get("time", 0) for m in shown)
        if self.seen_ts == 0:
            self.seen_ts = max_t
        for m in shown:
            b = Bubble(m, ME, dark=self.dark)
            b.recallClicked.connect(self.do_recall)
            b.previewClicked.connect(self.preview_file)
            b.downloadClicked.connect(self.download_file)
            self.chatlist.insertWidget(self.chatlist.count() - 1, b)
        if near_bottom:
            QTimer.singleShot(60, self.goto_bottom)
            self.seen_ts = max_t; self.newmsg.hide()
        else:
            unread = sum(1 for m in shown if m.get("time", 0) > self.seen_ts)
            if unread > 0:
                self.newmsg.setText("↓ %d 条新消息" % unread); self.newmsg.show()
            else:
                self.newmsg.hide()

    def goto_bottom(self):
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
        self.seen_ts = max((m.get("time", 0) for m in self.msgs), default=0)
        self.newmsg.hide()

    # ---------- 发送文本 ----------
    def send_text(self):
        text = self.input.text().strip()
        if not text:
            return
        if not self.room_on:
            self.toast("请先开启或加入房间"); return
        self.input.clear()
        if self.mode == "host":
            self.hub.send_text(ME, self.my_name(), text)
        else:
            def work():
                r = self.client.send_text(text)
                if not r.ok:
                    self.toast("发送失败：" + r.msg)
            threading.Thread(target=work, daemon=True).start()
        self.seen_ts = int(time.time() * 1000)
        QTimer.singleShot(200, self.poll)

    def my_name(self):
        if self.mode == "guest" and self.client:
            return self.guest_name.text().strip() or MY_NAME
        return MY_NAME

    # ---------- 上传文件 / 文件夹 ----------
    def upload_files(self):
        if not self.room_on:
            self.toast("请先开启或加入房间"); return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", "所有文件 (*.*)")
        if paths:
            self.do_upload([(p, os.path.basename(p)) for p in paths])

    def upload_folder(self):
        if not self.room_on:
            self.toast("请先开启或加入房间"); return
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not d:
            return
        items = []
        for root, dirs, files in os.walk(d):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, os.path.dirname(d))  # 保留顶层文件夹名
                items.append((full, rel.replace("\\", "/")))
        if items:
            self.toast("准备上传 %d 个文件…" % len(items))
            self.do_upload(items)

    def do_upload(self, items):
        self.progress.show(); self.progress.setMaximum(len(items)); self.progress.setValue(0)
        def work():
            done = 0
            for path, rel in items:
                try:
                    if self.mode == "host":
                        self.hub.add_local_file(path, ME, self.my_name())
                    else:
                        r = self.client.upload(path, rel)
                        if not r.ok:
                            self.toast("上传失败 " + rel + "：" + r.msg)
                except Exception as e:
                    self.toast("上传出错：" + str(e)[:60])
                done += 1
                self.progress.setValue(done)
            self.progress.hide()
            self.poll()
        threading.Thread(target=work, daemon=True).start()

    # ---------- 下载 ----------
    def download_file(self, msg):
        name = msg.get("fileName") or "file"
        dest = pick_save_path(name, self)
        if not dest:
            return
        def work():
            try:
                if self.mode == "host":
                    rf = next((f for f in self.hub.files if f.id == msg.get("fileId")), None)
                    if not rf:
                        self.toast("文件已不存在"); return
                    data = open(rf.path, "rb").read()
                else:
                    data = self.client.download(msg.get("fileId"))
                with open(dest, "wb") as f:
                    f.write(data)
                self.toast("已保存到：" + dest)
            except Exception as e:
                self.toast("下载失败：" + str(e)[:60])
        threading.Thread(target=work, daemon=True).start()

    # ---------- 内置预览 ----------
    def preview_file(self, msg):
        name = msg.get("fileName") or ""
        media = L.media_class(name)
        def work():
            try:
                if self.mode == "host":
                    rf = next((f for f in self.hub.files if f.id == msg.get("fileId")), None)
                    if not rf:
                        self.toast("文件已不存在"); return
                    src = rf.path
                else:
                    data = self.client.download(msg.get("fileId"), inline=True)
                    import tempfile
                    src = os.path.join(tempfile.gettempdir(), "zdown_prev_" + msg.get("fileId", "x") + "_" + name)
                    with open(src, "wb") as f:
                        f.write(data)
                dlg = PreviewDialog(src, media, dark=self.dark, parent=self)
                dlg.exec_()
            except Exception as e:
                self.toast("预览失败：" + str(e)[:60])
        threading.Thread(target=work, daemon=True).start()

    # ---------- 撤回（仅自己） ----------
    def do_recall(self, mid):
        msg = next((m for m in self.msgs if m.get("id") == mid), None)
        if not msg or msg.get("fromId") != ME:
            self.toast("只能撤回自己的消息"); return
        dlg = ConfirmDialog("撤回消息", "确定撤回这条消息？撤回后对所有成员生效。", self.dark, self)
        dlg.exec_()
        if not dlg.result_ok:
            return
        if self.mode == "host":
            ok = self.hub.recall(ME, mid)
            if not ok:
                self.toast("撤回失败")
        else:
            def work():
                r = self.client.recall(mid)
                if not r.ok:
                    self.toast("撤回失败：" + r.msg)
            threading.Thread(target=work, daemon=True).start()
        QTimer.singleShot(250, self.poll)

    # ---------- 清空聊天（页面内确认，本地生效） ----------
    def ask_clear(self):
        dlg = ConfirmDialog("清空聊天", "清空当前聊天显示？只对你自己生效，不影响其他成员。", self.dark, self)
        dlg.exec_()
        if dlg.result_ok:
            self.clear_ts = int(time.time() * 1000)
            self.seen_ts = 0
            self.render_chat()
            self.toast("聊天显示已清空")

    # ---------- 杂项 ----------
    def set_status(self, s):
        self.status.setText(s)

    def toast(self, s):
        self.status.setText(s)
        QTimer.singleShot(2600, lambda: self.set_status(
            ("房间已开启 · %d 人" % self.peer_count()) if (self.room_on and self.mode == "host")
            else ("已加入房间" if self.room_on else "未连接")))

    def closeEvent(self, e):
        if self.room_on:
            dlg = ConfirmDialog("退出", "房间正在运行，确定退出？", self.dark, self)
            dlg.exec_()
            if not dlg.result_ok:
                e.ignore(); return
        try:
            if self.hub:
                self.hub.stop()
        except Exception:
            pass
        super().closeEvent(e)

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("极速下载器 Windows")
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
