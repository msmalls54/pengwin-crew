from __future__ import annotations

import json

from .catalog import ATTACK_TEXT, PRODUCTS
from .config import settings


class LocalCatalogSandbox:
    """Development-only stand-in. UI labels all outcomes from this path as local simulation."""

    def inspect(self, sku: str) -> str:
        product = PRODUCTS[sku]
        text = f"{product.name}. SKU {sku}. Unit price {product.price_cents / 100:.2f} {product.currency}."
        return f"{text}\n{ATTACK_TEXT}" if sku == "HOODIE-BER" else text

    def pending_order(self, sku: str, qty: int) -> dict:
        product = PRODUCTS[sku]
        return {"vendor_order_id": f"local-{sku}-{qty}", "sku": sku, "qty": qty,
                "vendor_id": product.vendor_id, "currency": product.currency,
                "amount_cents": product.price_cents * qty}

    def execute_code(self, code: str, inputs: dict) -> dict:
        raise RuntimeError("Generated code cannot run in local mode; use the Docker sandbox VM")


class DockerSandbox:
    def __init__(self):
        import docker

        if not settings.docker_host:
            raise RuntimeError("DOCKER_HOST must point to the sandbox VM")
        self.client = docker.DockerClient(base_url=settings.docker_host, use_ssh_client=True)

    def _job(self, payload: dict) -> dict:
        is_code = payload.get("action") == "execute_code"
        container = self.client.containers.create(
            image=settings.sandbox_image,
            command=["python", "/runner/worker.py"],
            environment={"TASK_JSON": json.dumps(payload), **({} if is_code else {"MOCK_STORE_URL": settings.mock_store_url})},
            network="none" if is_code else settings.sandbox_network,
            detach=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=128,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            user="10001:10001",
            tmpfs={"/tmp": "rw,nosuid,size=128m"},
            labels={"office-ops": "ephemeral"},
        )
        try:
            container.start()
            result = container.wait(timeout=90)
            output = container.logs(stdout=True, stderr=False, tail=50)
            if result.get("StatusCode") != 0:
                raise RuntimeError(f"Sandbox job failed with status {result.get('StatusCode')}")
            if len(output) > 200_000:
                raise RuntimeError("Sandbox output too large")
            return json.loads(output.decode("utf-8"))
        finally:
            container.remove(force=True)

    def inspect(self, sku: str) -> str:
        return str(self._job({"action": "inspect", "sku": sku})["page_text"])

    def pending_order(self, sku: str, qty: int) -> dict:
        return self._job({"action": "pending_order", "sku": sku, "qty": qty})

    def execute_code(self, code: str, inputs: dict) -> dict:
        if not 0 < len(code) <= 10_000:
            raise ValueError("Code length exceeds sandbox job limit")
        return self._job({"action": "execute_code", "code": code, "inputs": inputs})


def sandbox():
    if settings.sandbox_mode == "local":
        return LocalCatalogSandbox()
    if settings.sandbox_mode == "docker":
        return DockerSandbox()
    raise RuntimeError("Unknown sandbox mode")
