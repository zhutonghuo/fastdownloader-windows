# -*- coding: utf-8 -*-
"""聊天气泡 widget：文本 / 文件，自己靠右、他人靠左，含时间与操作按钮"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont, QCursor
from PyQt5.QtWidgets import (QFrame, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
                             QSizePolicy)

import lan_core as L

class Bubble(QFrame):
    recallClicked = pyqtSignal(str)   # msg id
    previewClicked = pyqtSignal(dict) # msg dict
    downloadClicked = pyqtSignal(dict)

    def __init__(self, msg, my_id, dark=False, parent=None):
        super().__init__(parent)
        self.msg = msg
        self.mid = msg.get("id", "")
        self.own = msg.get("fromId") == my_id
        self.dark = dark
        self.kind = msg.get("kind", "text")
        self.setObjectName("bubble")
        self._build()

    def _colors(self):
        if self.dark:
            return (dict(bg="#2f6fed", fg="#ffffff", meta="#9fb0cd"),
                    dict(bg="#252d3f", fg="#e6ebf5", meta="#8894ab"))
        return (dict(bg="#2f6fed", fg="#ffffff", meta="#dbe7ff"),
                dict(bg="#ffffff", fg="#1e2536", meta="#7b869c"))

    def _build(self):
        own_c, other_c = self._colors()
        c = own_c if self.own else other_c
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setAlignment(Qt.AlignRight if self.own else Qt.AlignLeft)

        who = QLabel(self.msg.get("fromName") or "匿名")
        who.setFont(QFont("Microsoft YaHei", 8))
        who.setStyleSheet("color:%s;" % c["meta"])
        who.setAlignment(Qt.AlignRight if self.own else Qt.AlignLeft)

        card = QFrame()
        border = "#3a4459" if (self.dark and not self.own) else "#e4e9f2"
        card.setStyleSheet("QFrame{background:%s;border-radius:12px;border:1px solid %s;}" % (c["bg"], border))
        cb = QVBoxLayout(card)
        cb.setContentsMargins(11, 8, 11, 8)
        cb.setSpacing(4)

        if self.kind == "text":
            txt = QLabel(self.msg.get("text", ""))
            txt.setWordWrap(True)
            txt.setMaximumWidth(360)
            txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
            txt.setStyleSheet("color:%s;font-size:13px;" % c["fg"])
            cb.addWidget(txt)
        else:
            fn = QLabel("📎 " + (self.msg.get("fileName") or "文件"))
            fn.setWordWrap(True)
            fn.setMaximumWidth(340)
            fn.setStyleSheet("color:%s;font-size:13px;font-weight:600;" % c["fg"])
            cb.addWidget(fn)
            sz = QLabel(L.fmt_size(self.msg.get("fileSize", 0)))
            sz.setStyleSheet("color:%s;font-size:11px;" % c["meta"])
            cb.addWidget(sz)
            acts = QHBoxLayout()
            acts.setSpacing(5)
            media = L.media_class(self.msg.get("fileName") or "")
            if media in ("image", "video", "audio"):
                pv = QPushButton("预览"); pv.setProperty("cls", "small")
                pv.setStyleSheet("padding:5px 11px;font-size:11px;border-radius:7px;background:#edf2fd;color:#2f6fed;")
                pv.setCursor(QCursor(Qt.PointingHandCursor))
                pv.clicked.connect(lambda: self.previewClicked.emit(self.msg))
                acts.addWidget(pv)
            dl = QPushButton("下载"); dl.setProperty("cls", "small")
            dl.setStyleSheet("padding:5px 11px;font-size:11px;border-radius:7px;background:#edf2fd;color:#2f6fed;")
            dl.setCursor(QCursor(Qt.PointingHandCursor))
            dl.clicked.connect(lambda: self.downloadClicked.emit(self.msg))
            acts.addWidget(dl)
            if self.own:
                rc = QPushButton("撤回"); rc.setProperty("cls", "small")
                rc.setStyleSheet("padding:5px 11px;font-size:11px;border-radius:7px;background:#fdeeee;color:#d64545;")
                rc.setCursor(QCursor(Qt.PointingHandCursor))
                rc.clicked.connect(lambda: self.recallClicked.emit(self.mid))
                acts.addWidget(rc)
            acts.addStretch(1)
            cb.addLayout(acts)

        # 时间 + 自己的撤回（文本消息）
        foot = QHBoxLayout()
        foot.setSpacing(6)
        t = QLabel(self._time())
        t.setStyleSheet("color:%s;font-size:10px;" % c["meta"])
        if self.own:
            foot.addStretch(1)
        foot.addWidget(t)
        if self.own and self.kind == "text":
            rc = QPushButton("撤回")
            rc.setStyleSheet("padding:3px 8px;font-size:10px;border-radius:6px;background:rgba(255,255,255,.25);color:#fff;")
            rc.setCursor(QCursor(Qt.PointingHandCursor))
            rc.clicked.connect(lambda: self.recallClicked.emit(self.mid))
            foot.addWidget(rc)
        cb.addLayout(foot)

        outer.addWidget(who)
        outer.addWidget(card)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

    def _time(self):
        try:
            import datetime
            dt = datetime.datetime.fromtimestamp(self.msg.get("time", 0) / 1000.0)
            return dt.strftime("%m-%d %H:%M")
        except Exception:
            return ""
