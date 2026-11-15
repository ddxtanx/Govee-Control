#!/usr/bin/env python3
"""Tiny stand-in for govee.com SSR pages, for tests/test_lookup.py.

Routes:
  /search?q=FLOODX   -> results page linking /products/flood-light
  /search?q=NOISY    -> results page linking /products/noisy-strip
  /search?q=anything else -> 404 (like the real site for no hits)
  /products/flood-light  -> title states 1600 Lumens, page mentions FLOODX
  /products/noisy-strip  -> no title lumens; body disagrees (1200 vs 150)
"""
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PAGES = {
    "/products/flood-light": (
        "<html><head><title>Govee Flood Light 1600 Lumens | Govee US</title>"
        '<meta name="description" content="Bright 1600 lumens flood, model '
        'FLOODX."/></head><body>'
        '<script type="application/ld+json">'
        '[{"@context":"https://schema.org","@type":"ProductGroup",'
        '"name":"Govee Flood Light 1600lm","description":"Model: FLOODX",'
        '"hasVariant":[{"@type":"Product","name":"Govee Flood Light 1600lm",'
        '"sku":"FLOODX1"}]}]</script>'
        "<div>irrelevant copy</div></body></html>"),
    "/products/noisy-strip": (
        "<html><head><title>Govee Noisy Strip Light | Govee US</title>"
        "</head><body>Model NOISY. <div>1200 lumens</div>"
        "<div>150 lm per meter</div></body></html>"),
}
SEARCH = {
    "FLOODX": ('{"url":"http://127.0.0.1:9093/products/refurbished-flood-light"}'
               '{"image":"https://cdn.shopify.com/s/files/products/H5179.jpg",'
               '"url":"http://127.0.0.1:9093/products/flood-light"}'),
    "NOISY": '{"url":"http://127.0.0.1:9093/products/noisy-strip"}',
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        body, code = None, 200
        if u.path == "/search":
            q = parse_qs(u.query).get("q", [""])[0]
            hit = next((v for k, v in SEARCH.items() if k in q.upper()), None)
            if hit is None:
                code, body = 404, b"not found"      # real site behaviour
            else:
                body = f"<html><body>search {hit}</body></html>".encode()
        elif u.path in PAGES:
            body = PAGES[u.path].encode()
        else:
            code, body = 404, b"not found"
        print(f"{self.address_string()} {u.path} -> {code}",
              file=sys.stderr, flush=True)
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 9093), H).serve_forever()
