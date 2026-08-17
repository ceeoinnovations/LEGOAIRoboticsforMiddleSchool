#!/usr/bin/env python3
"""Local dev server for the Magic Wand app.

Plain `python3 -m http.server` sends a Last-Modified header but no
Cache-Control, so browsers are free to heuristically cache main.py /
wand_ml.py / index.html across reloads — meaning an edit here can silently
keep running whatever was fetched on an earlier page load instead of your
latest change. This adds no-cache headers to every response so a normal
reload always picks up the latest edits.
"""
import http.server


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        super().end_headers()


if __name__ == '__main__':
    http.server.test(HandlerClass=NoCacheHandler, port=8000)
