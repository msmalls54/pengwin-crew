from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./crew.db")
    company_name: str = os.getenv("COMPANY_NAME", "Kestrel")
    payment_mode: str = os.getenv("PAYMENT_MODE", "simulated")
    planner_mode: str = os.getenv("PLANNER_MODE", "deterministic")
    sandbox_mode: str = os.getenv("SANDBOX_MODE", "local")
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    freeze: bool = os.getenv("FREEZE", "false").lower() == "true"
    vultr_key: str = os.getenv("VULTR_INFERENCE_KEY", "")
    vultr_model: str = os.getenv("VULTR_MODEL", "")
    vultr_max_calls: int = int(os.getenv("VULTR_MAX_CALLS", "100"))
    airwallex_client_id: str = os.getenv("AIRWALLEX_CLIENT_ID", "")
    airwallex_api_key: str = os.getenv("AIRWALLEX_API_KEY", "")
    airwallex_base: str = os.getenv("AIRWALLEX_SANDBOX_BASE", "https://api.sandbox.airwallex.com")
    printful_token: str = os.getenv("PRINTFUL_TOKEN", "")
    printful_variant_id: str = os.getenv("PRINTFUL_HOODIE_VARIANT_ID", "")
    printful_store_id: str = os.getenv("PRINTFUL_STORE_ID", "")
    docker_host: str = os.getenv("DOCKER_HOST", "")
    sandbox_image: str = os.getenv("SANDBOX_IMAGE", "office-ops-sandbox:latest")
    sandbox_network: str = os.getenv("SANDBOX_NETWORK", "shopnet")
    mock_store_url: str = os.getenv("MOCK_STORE_URL", "http://mock-stores:8080")


settings = Settings()
