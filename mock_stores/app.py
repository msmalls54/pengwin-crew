from __future__ import annotations

from html import escape
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from crew.catalog import ATTACK_TEXT, PRODUCTS

app = FastAPI(title="Contained fictional stores")
orders: dict[str, dict] = {}


class PendingOrderInput(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=1000)


@app.get("/products/{sku}", response_class=__import__("fastapi").responses.HTMLResponse)
def product_page(sku: str):
    product = PRODUCTS.get(sku)
    if product is None:
        raise HTTPException(404)
    attack = f'<span style="display:none">{escape(ATTACK_TEXT)}</span>' if sku == "HOODIE-BER" else ""
    return f"<html><body><h1>{escape(product.name)}</h1><p>SKU: {escape(sku)}</p><p>Price: {product.price_cents / 100:.2f} {escape(product.currency)}</p>{attack}</body></html>"


@app.post("/orders/pending")
def create_pending(body: PendingOrderInput):
    product = PRODUCTS.get(body.sku)
    if product is None:
        raise HTTPException(404)
    order = {"vendor_order_id": str(uuid4()), "sku": body.sku, "qty": body.qty,
             "vendor_id": product.vendor_id, "currency": product.currency,
             "amount_cents": product.price_cents * body.qty, "status": "PENDING"}
    orders[order["vendor_order_id"]] = order
    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in orders:
        raise HTTPException(404)
    return orders[order_id]
