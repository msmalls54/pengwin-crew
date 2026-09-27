"""Runs only inside the secret-free sandbox image on the internal shopnet network."""

import asyncio
import json
import os
import subprocess
import sys

async def inspect(base: str, sku: str) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.goto(f"{base}/products/{sku}", wait_until="domcontentloaded", timeout=15000)
            page_text = await page.locator("body").text_content()
            return {"page_text": (page_text or "")[:20000]}
        finally:
            await browser.close()


def main() -> None:
    task = json.loads(os.environ["TASK_JSON"])
    base = os.environ.get("MOCK_STORE_URL", "").rstrip("/")
    if task["action"] in ("inspect", "pending_order"):
        sku = task["sku"]
        if not isinstance(sku, str) or not sku.replace("-", "").isalnum() or len(sku) > 50:
            raise ValueError("Invalid SKU")
    if task["action"] == "inspect":
        result = asyncio.run(inspect(base, sku))
    elif task["action"] == "pending_order":
        import httpx

        qty = task["qty"]
        if not isinstance(qty, int) or not 1 <= qty <= 1000:
            raise ValueError("Invalid quantity")
        with httpx.Client(timeout=15) as client:
            response = client.post(f"{base}/orders/pending", json={"sku": sku, "qty": qty})
            response.raise_for_status()
            result = response.json()
    elif task["action"] == "execute_code":
        code = task["code"]
        if not isinstance(code, str) or not 0 < len(code) <= 10_000:
            raise ValueError("Invalid code")
        try:
            completed = subprocess.run([sys.executable, "-I", "-c", code],
                                       input=json.dumps(task["inputs"]), capture_output=True,
                                       text=True, timeout=10, cwd="/tmp", env={"PATH": "/usr/local/bin:/usr/bin"})
            result = {"exit_code": completed.returncode,
                      "stdout": completed.stdout[:10_000], "stderr": completed.stderr[:4_000]}
        except subprocess.TimeoutExpired:
            result = {"exit_code": 124, "stdout": "", "stderr": "Execution timed out after 10 seconds"}
    else:
        raise ValueError("Unknown action")
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
