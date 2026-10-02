"""GST state codes and place of supply (PRIV-4).

The first two digits of a GSTIN are the buyer's state code. A sale to a buyer in the same state as the seller is taxed
as CGST + SGST; to a buyer in another state it is IGST. ERPNext (India Compliance) names the place of supply as
"27-Maharashtra".
"""

from typing import Optional

GST_STATE_NAMES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}


def state_code_from_gstin(gstin: Optional[str]) -> Optional[str]:
    """The two-digit state code of a GSTIN, or None when it is missing or not a known state."""
    code = str(gstin or "").strip()[:2]
    return code if code in GST_STATE_NAMES else None


def place_of_supply(gstin: Optional[str]) -> Optional[str]:
    """'27-Maharashtra' style place of supply for the buyer's GSTIN, or None."""
    code = state_code_from_gstin(gstin)
    return f"{code}-{GST_STATE_NAMES[code]}" if code else None


def is_inter_state(seller_state_code: str, buyer_gstin: Optional[str]) -> bool:
    """True when the buyer is in a different state from the seller (so IGST applies). Unknown buyer state = False
    (sold as in-state, the safe default for a business-to-consumer receipt)."""
    buyer = state_code_from_gstin(buyer_gstin)
    seller = (seller_state_code or "").strip()
    return bool(buyer and seller and buyer != seller)
