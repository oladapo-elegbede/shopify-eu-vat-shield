"""
FastAPI Server for EU VAT Shield
Handles EU VAT validation via VIES & Creates Tax-Exempt Shopify Draft Orders
"""

import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import our VIES validation function
from vat_validator import validate_eu_vat

load_dotenv()

logger = logging.getLogger(__name__)

# Shopify Environment Credentials from .env
SHOPIFY_SHOP_DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").rstrip("/")
SHOPIFY_ADMIN_ACCESS_TOKEN = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

app = FastAPI(
    title="EU VAT Shield API",
    description="API for validating European VAT numbers and generating 0% tax checkouts.",
    version="1.0.0",
)

# --- CORS Configuration ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows fetch requests from Shopify storefronts
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=86400,
)


# --- Pydantic Schemas ---
class VatCheckRequest(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    vat_number: str = Field(..., min_length=2, max_length=15)


class VatCheckResponse(BaseModel):
    success: bool
    valid: Optional[bool] = None
    country_code: Optional[str] = None
    vat_number: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    request_date: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


class CartLineItem(BaseModel):
    variant_id: int
    quantity: int = Field(..., ge=1)


class ExemptCheckoutRequest(BaseModel):
    line_items: list[CartLineItem] = Field(..., min_length=1)
    vat_number: str
    vat_company: Optional[str] = None
    country_code: str
    customer_email: Optional[str] = None
    revalidate_vat: bool = True


class ExemptCheckoutResponse(BaseModel):
    success: bool
    invoice_url: Optional[str] = None
    draft_order_id: Optional[int] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


# --- Helper: Call Shopify Admin API to create Draft Order ---
async def create_shopify_draft_order(
    line_items: list[dict],
    vat_number: str,
    vat_company: Optional[str] = None,
    customer_email: Optional[str] = None,
) -> dict:
    if not SHOPIFY_SHOP_DOMAIN or not SHOPIFY_ADMIN_ACCESS_TOKEN:
        raise ValueError("Missing SHOPIFY_SHOP_DOMAIN or SHOPIFY_ADMIN_ACCESS_TOKEN in .env file.")

    url = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/draft_orders.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ADMIN_ACCESS_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    normalized_items = [
        {"variant_id": item["variant_id"], "quantity": item["quantity"]}
        for item in line_items
    ]

    company_label = vat_company or "N/A"
    payload = {
        "draft_order": {
            "line_items": normalized_items,
            "tax_exempt": True,  # <-- Removes 100% of tax at checkout
            "note": f"EU B2B Tax-Exempt Order. Validated VAT: {vat_number} ({company_label})",
            "tags": "eu-vat-exempt, b2b",
            "note_attributes": [
                {"name": "VAT_Number", "value": vat_number},
                {"name": "VAT_Company", "value": company_label},
            ],
        }
    }

    if customer_email:
        payload["draft_order"]["email"] = customer_email

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code not in (200, 201):
        logger.error("Shopify Admin API Error: %s", response.text)
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Shopify API Error: {response.text}"
        )

    data = response.json().get("draft_order", {})
    return {
        "draft_order_id": data.get("id"),
        "invoice_url": data.get("invoice_url"),
    }


# --- Endpoints ---
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "vat-shield"}


@app.post("/api/v1/validate-vat", response_model=VatCheckResponse, tags=["VAT"])
async def validate_vat(request: VatCheckRequest):
    """1. Validate EU VAT via VIES API"""
    return validate_eu_vat(
        country_code=request.country_code,
        vat_number=request.vat_number
    )


@app.post("/api/v1/create-exempt-checkout", response_model=ExemptCheckoutResponse, tags=["Checkout"])
async def create_exempt_checkout(request: ExemptCheckoutRequest):
    """2. Intercept Checkout and return 0% Tax Draft Order invoice URL"""

    # Server-Side Re-validation for safety
    if request.revalidate_vat:
        raw_vat = request.vat_number.strip().upper()
        cc = request.country_code.strip().upper()
        vat_only = raw_vat[2:] if raw_vat.startswith(cc) else raw_vat

        vies = validate_eu_vat(cc, vat_only)
        if not vies.get("success") or not vies.get("valid"):
            return ExemptCheckoutResponse(
                success=False,
                error="VAT re-validation failed. Tax-exempt checkout denied.",
                error_code="INVALID_VAT"
            )

    try:
        draft_res = await create_shopify_draft_order(
            line_items=[item.model_dump() for item in request.line_items],
            vat_number=request.vat_number,
            vat_company=request.vat_company,
            customer_email=request.customer_email
        )

        return ExemptCheckoutResponse(
            success=True,
            invoice_url=draft_res["invoice_url"],
            draft_order_id=draft_res["draft_order_id"]
        )

    except Exception as exc:
        logger.error("Draft order creation error: %s", exc)
        return ExemptCheckoutResponse(
            success=False,
            error=str(exc),
            error_code="DRAFT_ORDER_FAILED"
        )