# -*- coding: utf-8 -*-
"""协议自检：服务端 + 客户端 + 原始 HTTP 全链路（离线，不依赖外网）"""
import os, sys, io, time, json, urllib.request, urllib.parse
import lan_core as L

def raw(method, path, params=None, body=None, ctype=None):
    base = "http://127.0.0.1:%d" % hub.port
    url = base + path
    if params:
        url += "?" + "&".join(L.enc(k) + "=" + L.enc(v) for k, v in params.items())
    data = body
    if isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

page = b"<html>test</html>"
hub = L.LanHub(code_len=4, page_bytes=page, storage_dir=os.path.join(os.environ.get("TEMP", "/tmp"), "zdown_test_room"))
hub.start()
time.sleep(0.4)
C = hub.code
ok = 0; fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("[OK]  " + name)
    else:
        fail += 1; print("[FAIL] " + name + " " + extra)

# 1 首页
s, b, h = raw("GET", "/")
check("首页返回页面", s == 200 and b == page and "text/html" in h.get("Content-Type", ""))
# 2 ping 免口令
s, b, h = raw("GET", "/api/ping")
check("ping 免口令", s == 200 and json.loads(b)["ok"])
# 3 错误口令 403
s, b, h = raw("GET", "/api/join", {"code": "0000" if C != "0000" else "1111", "name": "x"})
check("错误口令 403", s == 403 and not json.loads(b)["ok"])
# 4 join
s, b, h = raw("GET", "/api/join", {"code": C, "name": "安卓手机"})
check("join 成功", s == 200 and json.loads(b)["ok"])
# 5 发文本
s, b, h = raw("POST", "/api/chat", {"code": C, "me": "a1", "name": "安卓手机"},
              "text=" + L.enc("你好 Windows") + "&me=a1&name=" + L.enc("安卓手机"), "application/x-www-form-urlencoded")
check("发文本", s == 200 and json.loads(b)["ok"])
# 6 拉聊天
s, b, h = raw("GET", "/api/chat", {"code": C, "name": "PC", "me": "pc"})
msgs = json.loads(b)["msgs"]
check("拉聊天含文本", any(m["kind"] == "text" and m["text"] == "你好 Windows" for m in msgs))
mid_text = next(m["id"] for m in msgs if m["kind"] == "text")
# 7 上传（Content-Length，中文名）
payload = "你好世界".encode("utf-8")
s, b, h = raw("POST", "/api/upload", {"code": C, "me": "a1", "name": "安卓手机", "filename": "测试文件.txt"},
              payload, "application/octet-stream")
up = json.loads(b)
check("上传中文名文件", s == 200 and up["ok"] and up["name"] == "测试文件.txt", str(up))
file_id = up.get("id", "")
# 8 下载字节一致
s, b, h = raw("GET", "/api/download", {"code": C, "id": file_id})
check("下载字节一致", s == 200 and b == payload)
check("下载 Content-Disposition attachment", "attachment" in h.get("Content-Disposition", ""))
# 9 inline 预览头
s, b, h = raw("GET", "/api/download", {"code": C, "id": file_id, "inline": "1"})
check("inline 预览头", "inline" in h.get("Content-Disposition", ""))
# 10 列表
s, b, h = raw("GET", "/api/list", {"code": C, "name": "PC"})
lst = json.loads(b)
check("list 含文件", any(f["id"] == file_id for f in lst["files"]))
# 11 撤回越权（别人撤我的）应失败
s, b, h = raw("POST", "/api/recall", {"code": C}, "me=other&id=" + mid_text, "application/x-www-form-urlencoded")
check("越权撤回被拒", s == 400 and not json.loads(b)["ok"])
# 12 本人撤回文本成功
s, b, h = raw("POST", "/api/recall", {"code": C}, "me=a1&id=" + mid_text, "application/x-www-form-urlencoded")
check("本人撤回文本", s == 200 and json.loads(b)["ok"])
# 13 撤回文件后文件消失
s, b, h = raw("GET", "/api/chat", {"code": C, "me": "pc"})
msgs2 = json.loads(b)["msgs"]
fid_msg = next((m for m in msgs2 if m["kind"] == "file"), None)
if fid_msg:
    s, b, h = raw("POST", "/api/recall", {"code": C}, "me=a1&id=" + fid_msg["id"], "application/x-www-form-urlencoded")
    s2, b2, h2 = raw("GET", "/api/download", {"code": C, "id": file_id})
    check("撤回文件后不可下载", s == 200 and s2 == 404)
else:
    check("撤回文件后不可下载", False, "无文件消息")
# 14 二维码 PNG
s, b, h = raw("GET", "/api/qr", {"code": C})
check("二维码 PNG", s == 200 and b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) > 200)
# 15 LanClient 连本地 hub
cli = L.LanClient("http://127.0.0.1:%d" % hub.port, C, "win1", "Windows版")
r = cli.join()
check("LanClient.join", r.ok)
r = cli.send_text("Windows 你好安卓")
check("LanClient.sendText", r.ok)
r = cli.chat()
check("LanClient.chat 拉到 Windows 消息", any(m["text"] == "Windows 你好安卓" for m in r.msgs))
import tempfile
tf = os.path.join(tempfile.gettempdir(), "zdown_win_up.bin")
with open(tf, "wb") as f:
    f.write(b"WINDOWS-FILE-DATA")
r = cli.upload(tf)
check("LanClient.upload", r.ok)
fid_win = next((f["fileId"] for f in cli.chat().msgs if f["kind"] == "file"), "")
data = cli.download(fid_win)
check("LanClient.download 字节一致", data == b"WINDOWS-FILE-DATA")
# 16 口令 != 端口、首位非0、非全同
check("口令规则", hub.code != str(hub.port) and hub.code[0] != "0" and len(set(hub.code)) > 1)

hub.stop()
print("\n=== 结果: %d 通过 / %d 失败 ===" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
