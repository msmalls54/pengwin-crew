"""Read-only Printful catalog research boundaries."""

import json


def _listing():
    return {"code": 200, "result": [
        {"id": 382, "title": "Stainless Steel Water Bottle", "brand": "Printful",
         "type_name": "Water Bottle", "model": "", "variant_count": 4},
        {"id": 717, "title": "All-Over Print Recycled Unisex Zip Hoodie",
         "brand": "Printful", "type_name": "Hoodie", "model": "", "variant_count": 6},
        {"id": 146, "title": "Unisex Heavy Blend Hoodie | Gildan 18500",
         "brand": "Gildan", "type_name": "Hoodie", "model": "18500", "variant_count": 192},
        {"id": 294, "title": "Unisex Pullover Hoodie | Bella + Canvas 3719",
         "brand": "Bella + Canvas", "type_name": "Hoodie", "model": "3719",
         "variant_count": 55},
    ]}


def _details(product_id):
    return {"code": 200, "result": {
        "product": {"id": product_id, "title": {
            382: "Stainless Steel Water Bottle",
            717: "All-Over Print Recycled Unisex Zip Hoodie",
            146: "Unisex Heavy Blend Hoodie | Gildan 18500",
            294: "Unisex Pullover Hoodie | Bella + Canvas 3719",
        }[product_id], "brand": "Gildan" if product_id == 146 else "Printful",
            "model": "18500" if product_id == 146 else "", "currency": "USD"},
        "variants": [
            {"product_id": product_id, "color": "Black", "size": "M", "price": "22.63"},
            {"product_id": product_id, "color": "Navy", "size": "L", "price": "30.63"},
        ],
    }}


def test_natural_product_requests_use_official_catalog_without_sending_query(monkeypatch):
    from crew import research

    calls = []

    def fake_fetch(url, *, max_bytes):
        calls.append((url, max_bytes))
        if url.endswith("/products"):
            return _listing()
        return _details(int(url.rsplit("/", 1)[-1]))

    monkeypatch.setattr(research, "_printful_json", fake_fetch)
    colors = research.lookup_printful_catalog("look up the hoodie colors", max_products=1)
    assert colors.status == "ok"
    assert colors.products[0].id == 146  # Most variant choices, not first catalog row.
    assert colors.products[0].colors == ["Black", "Navy"]
    assert colors.products[0].sizes == ["L", "M"]
    assert (colors.products[0].min_price, colors.products[0].max_price) == (22.63, 30.63)
    assert colors.products[0].checked_at is not None
    assert colors.products[0].source_url == "https://api.printful.com/products/146"
    assert calls == [
        ("https://api.printful.com/products", 2_500_000),
        ("https://api.printful.com/products/146", 800_000),
    ]

    calls.clear()
    alternative = research.lookup_printful_catalog(
        "what else can we buy outside of water bottles? what about hoodies", max_products=1)
    assert alternative.status == "ok" and alternative.products[0].id == 146
    assert all("water bottles" not in url for url, _ in calls)


def test_private_text_and_underspecified_request_never_call_catalog(monkeypatch):
    from crew import research

    monkeypatch.setattr(research, "_printful_json", lambda *_, **__: (_ for _ in ()).throw(
        AssertionError("Catalog network must not be called")))
    assert research.lookup_printful_catalog("Look up a hoodie for jane@example.com").query == "[withheld]"
    assert research.lookup_printful_catalog("What colors are available?").status == "unavailable"


def test_catalog_failure_does_not_invent_product_options(monkeypatch):
    from crew import research

    monkeypatch.setattr(research, "_printful_json", lambda *_, **__: {"code": 200, "result": "bad"})
    result = research.lookup_printful_catalog("hoodies")
    assert result.status == "error" and result.products == []


def test_catalog_url_is_fixed_to_printful(monkeypatch):
    from crew import research

    monkeypatch.setattr(research.httpx, "stream", lambda *_, **__: (_ for _ in ()).throw(
        AssertionError("No network should be called")))
    try:
        research._printful_json("http://localhost/products/146", max_bytes=100)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid host accepted")


def test_live_shape_gildan_18500_sanitizes_variant_labels():
    from crew import research

    # Shape and ordinary values observed from the public /products/146 response.
    payload = {"code": 200, "result": {
        "product": {"id": 146, "title": "Unisex Heavy Blend Hoodie | Gildan 18500",
                    "brand": "Gildan", "model": "18500", "currency": "USD",
                    "is_discontinued": False, "variant_count": 192},
        "variants": [
            {"id": 20556, "product_id": 146,
             "name": "Gildan 18500 Unisex Heavy Blend Hooded Sweatshirt (Ash / 2XL)",
             "size": "2XL", "color": "Ash", "color_code": "#dedede",
             "price": "24.63", "in_stock": True,
             "availability_regions": {"US": "United States"},
             "availability_status": [{"region": "US", "status": "in_stock"}]},
            {"id": 20557, "product_id": 146, "size": "M", "color": "Black",
             "price": "22.63", "in_stock": False},
            {"id": 20558, "product_id": 146, "size": "<@here>",
             "color": "@here<script>alert(1)</script>", "price": "30.63"},
            {"id": 999, "product_id": 999, "size": "XL", "color": "Red",
             "price": "0.01"},
        ],
    }}
    product = research._catalog_product(payload, 146)
    assert product.title == "Unisex Heavy Blend Hoodie | Gildan 18500"
    assert product.min_price == 22.63 and product.max_price == 30.63
    assert "Red" not in product.colors
    assert all("@" not in label and "<" not in label for label in product.colors + product.sizes)
    assert product.source_url == "https://api.printful.com/products/146"


def test_catalog_network_never_receives_raw_request(monkeypatch):
    from crew import research

    sent = []

    class Response:
        def __init__(self, body):
            self.body = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield self.body

    def fake_stream(method, url, **kwargs):
        sent.append((method, url, kwargs))
        return Response(_listing() if url.endswith("/products") else _details(146))

    monkeypatch.setattr(research.httpx, "stream", fake_stream)
    request = "Look up the Gildan 18500 hoodie colors for our private fall launch"
    result = research.lookup_printful_catalog(request, max_products=1)
    assert result.status == "ok"
    assert [url for _, url, _ in sent] == [
        "https://api.printful.com/products", "https://api.printful.com/products/146"]
    assert all(method == "GET" and "params" not in kwargs and not kwargs["follow_redirects"]
               for method, _, kwargs in sent)
    assert all(request not in str(call) and "private fall launch" not in str(call)
               for call in sent)
