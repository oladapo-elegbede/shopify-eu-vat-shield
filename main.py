"""
FastAPI Server for EU VAT Shield
Production Engine: Handles EU VAT Validation & 0% Tax Draft Checkout Creation
"""

import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from vat_validator import validate_eu_vat

load_dotenv()

logger = logging.getLogger(__name__)

# Shopify Configuration
SHOPIFY_SHOP_DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN", "vat-test-engine.myshopify.com").rstrip("/")
SHOPIFY_ADMIN_ACCESS_TOKEN = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

app = FastAPI(
    title="EU VAT Shield API",
    description="Production API for validating European VAT numbers and generating 0% tax checkouts.",
    version="1.0.0",
)

# --- CORS Configuration ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


# --- Helper: Create Shopify Draft Order ---
async def create_shopify_draft_order(
    line_items: list[dict],
    vat_number: str,
    vat_company: Optional[str] = None,
    customer_email: Optional[str] = None,
) -> dict:
    if not SHOPIFY_SHOP_DOMAIN or not SHOPIFY_ADMIN_ACCESS_TOKEN:
        raise ValueError("Missing SHOPIFY_SHOP_DOMAIN or SHOPIFY_ADMIN_ACCESS_TOKEN in environment variables.")

    url = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/draft_orders.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ADMIN_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }

    normalized_items = [
        {"variant_id": item["variant_id"], "quantity": item["quantity"]}
        for item in line_items
    ]

    company_label = vat_company or "N/A"
    payload = {
        "draft_order": {
            "line_items": normalized_items,
            "tax_exempt": True,  # 0% Tax applied securely
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


# --- Routes ---

# 🔴 MERCHANT EMBEDDED DASHBOARD
@app.get("/", response_class=HTMLResponse)
async def merchant_admin_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>EU VAT Shield Admin</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f6f6f7; padding: 40px; color: #202223; }
            .card { background: #ffffff; border-radius: 8px; border: 1px solid #e1e3e5; padding: 32px; max-width: 550px; margin: 20px auto; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
            .status-badge { display: inline-block; background: #e6f4ea; color: #007f5f; padding: 6px 12px; border-radius: 20px; font-weight: 600; font-size: 13px; margin-bottom: 16px; }
            h1 { font-size: 20px; margin: 0 0 8px 0; }
            p { font-size: 14px; line-height: 1.5; color: #5c5f62; margin-bottom: 20px; }
            .instruction-box { background: #f4f6f8; border-left: 4px solid #008060; padding: 14px; border-radius: 4px; font-size: 13px; margin-top: 20px; }
        </style>
    </head>
    <body>
        <div class="card">
            <span class="status-badge">● EU VIES Validation Engine Active</span>
            <h1>EU VAT Shield is Protected & Running</h1>
            <p>Your store is configured for 0% tax B2B checkout automation. European business customers entering valid VAT IDs will automatically receive tax-exempt orders.</p>
            
            <div class="instruction-box">
                <strong>Theme Setup Instructions:</strong><br>
                Go to <em>Online Store → Themes → Customize → Cart Page</em> and add the <strong>EU VAT Shield</strong> app section/block.
            </div>
        </div>
    </body>
    </html>
    """


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "vat-shield"}


@app.post("/api/v1/validate-vat", response_model=VatCheckResponse)
async def validate_vat(request: VatCheckRequest):
    return validate_eu_vat(
        country_code=request.country_code,
        vat_number=request.vat_number
    )


@app.post("/api/v1/create-exempt-checkout", response_model=ExemptCheckoutResponse)
async def create_exempt_checkout(request: ExemptCheckoutRequest):
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