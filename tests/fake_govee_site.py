#!/usr/bin/env python3
"""Tiny stand-in for Govee storefront SSR pages, for tests/test_lookup.py.

Run one per storefront in the chain:
  PORT=9093 SITE=primary python3 fake_govee_site.py   # GOVEE_SITE
  PORT=9094 SITE=region  python3 fake_govee_site.py   # GOVEE_REGIONS

Primary store:
  /search?q=FLOODX    -> links refurb decoy + /products/flood-light
                         (+ a cdn.shopify.com image URL decoy)
  /search?q=NOISY     -> /products/noisy-strip (disagreeing copy)
  /search?q=REFURBX   -> ONLY /products/refurbished-refurbx (archive)
  /search?q=DECOYX    -> /products/decoy (page names another SKU;
                         the SKU appears in page text but NOT in the
                         ld+json block -> must be rejected)
  /search?q=DELISTX   -> 404 (delisted in this region)
Region store:
  /search?q=DELISTX   -> /products/delisted-bulb (found here)
  everything else     -> 404
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "9093"))
SITE = os.environ.get("SITE", "primary")
BASE = f"http://127.0.0.1:{PORT}"

LD = ('<script type="application/ld+json">{}</script>')


def ld(name: str, model: str) -> str:
    import json
    return LD.format(json.dumps([{
        "@context": "https://schema.org", "@type": "ProductGroup",
        "name": name, "description": f"Model: {model}",
        "hasVariant": [{"@type": "Product", "name": name,
                        "sku": model + "1"}]}]))


PAGES = {
    "/products/flood-light": (
        "<html><head><title>Govee Flood Light 1600 Lumens | Govee US</title>"
        '<meta name="description" content="Bright 1600 lumens flood, model '
        'FLOODX."/></head><body>'
        + ld("Govee Flood Light 1600lm", "FLOODX")
        + "<div>irrelevant copy</div></body></html>"),
    "/products/noisy-strip": (
        "<html><head><title>Govee Noisy Strip Light | Govee US</title>"
        "</head><body>Model NOISY. <div>1200 lumens</div>"
        "<div>150 lm per meter</div></body></html>"),
    "/products/refurbished-refurbx": (
        "<html><head><title>Refurbished Govee Strip | Govee US</title>"
        "</head><body>"
        + ld("Refurb Govee Strip Light 1500lm", "REFURBX")
        + "<div>menu mentions many SKUs</div></body></html>"),
    "/products/decoy": (
        "<html><head><title>Govee Other Light | Govee US</title>"
        "</head><body>"
        + ld("Govee Other Light 999lm", "OTHERSKU")
        + "<div>the nav text mentions DECOYX and 999 lumens</div>"
        "</body></html>"),
    "/products/delisted-bulb": (
        "<html><head><title>Govee Smart LED Bulb | Govee EU</title></head>"
        "<body>" + ld("Govee Smart LED Bulb 1300lm", "DELISTX")
        + "</body></html>"),
}

SEARCH_PRIMARY = {
    "FLOODX": (f'{{"url":"{BASE}/products/refurbished-flood-light"}}'
               f'{{"image":"https://cdn.shopify.com/s/files/products/'
               f'H5179.jpg","url":"{BASE}/products/flood-light"}}'),
    "NOISY": f'{{"url":"{BASE}/products/noisy-strip"}}',
    "REFURBX": f'{{"url":"{BASE}/products/refurbished-refurbx"}}',
    "DECOYX": f'{{"url":"{BASE}/products/decoy"}}',
}
SEARCH_REGION = {
    "DELISTX": f'{{"url":"{BASE}/products/delisted-bulb"}}',
}
SEARCH = SEARCH_PRIMARY if SITE == "primary" else SEARCH_REGION


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        body, code = None, 200
        if u.path == "/search":
            q = parse_qs(u.query).get("q", [None])[0]
            hit = SEARCH.get(q.upper()) if q else None
            if hit is None:
                code, body = 404, b"not found"      # real site behaviour
            else:
                body = f"<html><body>search {hit}</body></html>".encode()
        elif u.path in PAGES:
            body = PAGES[u.path].encode()
        else:
            code, body = 404, b"not found"
        print(f"{PORT} {u.path} -> {code}", file=sys.stderr, flush=True)
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
