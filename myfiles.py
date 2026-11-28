# -*- coding: utf-8 -*-
"""账号文件浏览器：浏览登录账号的网盘文件/文件夹并加入下载队列。"""
import os
import threading

import pan
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QMessageBox,
)


class MyFilesDialog(QDialog):
    def __init__(self, platform, cookies, parent=None, dark=False):
        super().__init__(parent)
        self.platform = platform
        self.cookies = cookies
        self.stack = [("0", "全部文件")]
        self._alive = True          # 对话框是否仍可安全访问 UI
        self._result = []
        self.setWindowTitle("我的文件 · " + pan.PLATFORMS.get(platform, platform))
        self.resize(880, 620)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)

        top = QHBoxLayout()
        self.path_lbl = QLabel("全部文件"); top.addWidget(self.path_lbl, 1)
        back = QPushButton("← 上级"); back.clicked.connect(self.go_up); top.addWidget(back)
        refresh = QPushButton("刷新"); refresh.clicked.connect(self.load); top.addWidget(refresh)
        v.addLayout(top)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.itemDoubleClicked.connect(self.on_double)
        v.addWidget(self.list, 1)

        bottom = QHBoxLayout()
        self.info = QLabel("双击文件夹进入，选中文件后点下载")
        bottom.addWidget(self.info, 1)
        dl = QPushButton("下载选中"); dl.setObjectName("primary")
        dl.clicked.connect(self.download); bottom.addWidget(dl)
        v.addLayout(bottom)

        QTimer.singleShot(0, self.load)

    # ---------- 生命周期保护 ----------
    def closeEvent(self, e):
        self._alive = False
        super().closeEvent(e)

    def _post(self, fn, *args):
        """把 UI 更新切回主线程执行；若对话框已销毁则丢弃。"""
        def runner():
            if not self._alive:
                return
            try:
                fn(*args)
            except RuntimeError:
                self._alive = False
            except Exception:
                pass
        QTimer.singleShot(0, runner)

    # ---------- 数据加载 ----------
    def load(self):
        if not self._alive:
            return
        fid, _ = self.stack[-1]
        self.path_lbl.setText(" / ".join(n for _, n in self.stack))
        self.list.clear()
        self.info.setText("加载中…")

        def work():
            try:
                files = pan.list_my_files(self.platform, self.cookies, fid)
                self._post(self.show_files, files)
            except Exception as e:
                self._post(self._fail, "加载失败：" + str(e)[:120])

        threading.Thread(target=work, daemon=True).start()

    def _fail(self, msg):
        self.info.setText(msg)

    def show_files(self, files):
        self.list.clear()
        for f in files:
            f = f or {}
            icon = "📁" if f.get("is_dir") else "📄"
            size = "" if f.get("is_dir") else ("  %.1f KB" % (float(f.get("size") or 0) / 1024.0))
            it = QListWidgetItem("%s  %s%s" % (icon, f.get("name", "文件"), size))
            it.setData(Qt.UserRole, f)
            self.list.addItem(it)
        self.info.setText("共 %d 项 · 双击文件夹进入，选中文件后点下载" % len(files))

    def on_double(self, item):
        f = item.data(Qt.UserRole) or {}
        if f.get("is_dir"):
            self.stack.append((f.get("fid", "0"), f.get("name", "文件夹")))
            self.load()

    def go_up(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self.load()

    def download(self):
        items = self.list.selectedItems()
        files = []
        for it in items:
            f = it.data(Qt.UserRole) or {}
            if not f.get("is_dir"):
                files.append(f)
        if not files:
            QMessageBox.information(self, "提示", "请先选中要下载的文件（文件夹请双击进入）")
            return
        self.info.setText("正在取直链…")

        def work():
            try:
                direct = pan.myfile_direct(files, self.cookies)
                self._post(self.accept_files, direct)
            except Exception as e:
                self._post(self._dl_fail, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _dl_fail(self, msg):
        self.info.setText("取直链失败：" + msg[:120])
        QMessageBox.warning(self, "取直链失败", msg[:200])

    def accept_files(self, direct):
        self._result = direct
        self.accept()

    def result_files(self):
        return getattr(self, "_result", []) or []
