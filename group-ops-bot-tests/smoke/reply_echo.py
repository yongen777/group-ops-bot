# -*- coding: utf-8 -*-
"""本地冒烟：回推接收器（扮演平台 reply_url，验证出站链路）。

把每个 POST 的 JSON 体追加写入日志文件（一行一个）。
⛔ 只监听 127.0.0.1，⛔ 不暴露局域网。
用法：python reply_echo.py <port> <log_path>
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def main(port: int, log_path: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(body.decode("utf-8", "replace") + "\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, fmt, *args):
            sys.stderr.write("[reply-echo] " + (fmt % args) + "\n")

    server = HTTPServer(("127.0.0.1", port), Handler)
    print("reply-echo listening on 127.0.0.1:%d" % port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
