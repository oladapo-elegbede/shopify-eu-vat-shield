"""
Resilient EU VAT Validation Module with Regex Pre-Check, Retries, and Graceful Fallback
"""

import logging
import re
import time
from typing import Optional
import requests

logger = logging.getLogger(__name__)

VIES_BASE_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country_code}/vat/{vat_number}"

REQUEST_TIMEOUT = 8  # seconds per attempt
MAX_RETRIES = 2     # Total 3 attempts

# Official EU VAT Format Regex Rules
COUNTRY_REGEX = {
    "AT": r"^U[0-9]{8}$",
    "BE": r"^0[0-9]{9}$",
    "BG": r"^[0-9]{9,10}$",
    "CY": r"^[0-9]{8}[A-Z]$",
    "CZ": r"^[0-9]{8,10}$",
    "DE": r"^[0-9]{9}$",
    "DK": r"^[0-9]{8}$",
    "EE": r"^[0-9]{9}$",
    "EL": r"^[0-9]{9}$",
    "ES": r"^[A-Z0-9][0-9]{7}[A-Z0-9]$",
    "FI": r"^[0-9]{8}$",
    "FR": r"^[A-Z0-9]{2}[0-9]{9}$",
    "HR": r"^[0-9]{11}$",
    "HU": r"^[0-9]{8}$",
    "IE": r"^[0-9]{7}[A-W][A-I]?$",
    "IT": r"^[0-9]{11}$",
    "LT": r"^([0-9]{9}|[0-9]{12})$",
    "LU": r"^[0-9]{8}$",
    "LV": r"^[0-9]{11}$",
    "MT": r"^[0-9]{8}$",
    "NL": r"^[0-9]{9}B[0-9]{2}$",
    "PL": r"^[0-9]{10}$",
    "PT": r"^[0-9]{9}$",
    "RO": r"^[0-9]{2,10}$",
    "SE": r"^[0-9]{12}$",
    "SI": r"^[0-9]{8}$",
    "SK": r"^[0-9]{10}$",
    "XI": r"^[0-9]{9,12}$",
}


def _normalize_inputs(country_code: str, vat_number: str) -> tuple[str, str]:
    country_code = country_code.strip().upper()
    vat_number = re.sub(r"[\s\-\.]", "", vat_number).upper()
    
    # Strip country prefix if included in VAT field (e.g. DE123456789 -> 123456789)
    if vat_number.startswith(country_code):
        vat_number = vat_number[len(country_code):]
        
    return country_code, vat_number


def validate_eu_vat(country_code: str, vat_number: str) -> dict:
    country_code, vat_number = _normalize_inputs(country_code, vat_number)

    if country_code not in COUNTRY_REGEX:
        return {
            "success": False,
            "valid": False,
            "country_code": country_code,
            "vat_number": vat_number,
            "error": f"Unsupported EU country code: {country_code}",
            "error_code": "INVALID_COUNTRY",
        }

    # 1. INSTANT REGEX SYNTAX CHECK (0ms)
    pattern = COUNTRY_REGEX[country_code]
    is_valid_format = bool(re.match(pattern, vat_number))

    if not is_valid_format:
        return {
            "success": True,
            "valid": False,
            "country_code": country_code,
            "vat_number": vat_number,
            "error": f"Invalid VAT format for {country_code}",
            "error_code": "INVALID_FORMAT",
        }

    # 2. VIES QUERY WITH SILENT RETRIES
    url = VIES_BASE_URL.format(country_code=country_code, vat_number=vat_number)
    headers = {"Accept": "application/json", "User-Agent": "ShopifyVATShield/1.0"}

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                data = response.json()
                user_error = data.get("userError", "VALID")

                if user_error == "VALID":
                    return {
                        "success": True,
                        "valid": bool(data.get("isValid", False) or data.get("valid", False)),
                        "country_code": data.get("countryCode", country_code),
                        "vat_number": data.get("vatNumber", vat_number),
                        "name": data.get("name") if data.get("name") not in ("---", "") else None,
                        "address": data.get("address") if data.get("address") not in ("---", "") else None,
                        "provisional": False,
                    }
                elif user_error in ("INVALID", "INVALID_INPUT"):
                    return {
                        "success": True,
                        "valid": False,
                        "country_code": country_code,
                        "vat_number": vat_number,
                        "error": "VAT number rejected by EU VIES",
                        "error_code": "INVALID_VAT",
                        "provisional": False,
                    }

        except Exception as exc:
            logger.warning("VIES attempt %d failed for %s%s: %s", attempt + 1, country_code, vat_number, exc)
            if attempt < MAX_RETRIES:
                time.sleep(1)  # brief pause before retry

    # 3. GRACEFUL FALLBACK (VIES Offline / Timeout)
    # Syntax format was verified as correct above. Do not block the sale!
    logger.info("VIES unavailable for %s%s. Falling back to provisional format approval.", country_code, vat_number)
    return {
        "success": True,
        "valid": True,
        "country_code": country_code,
        "vat_number": vat_number,
        "name": "Provisionally Approved (Format Validated)",
        "provisional": True,
        "error": None,
    }