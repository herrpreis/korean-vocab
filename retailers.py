"""The shops the planner can price and order from.

A week is ordered from one shop. Add a shop here and it appears in the
shop picker, the shopping list and the week headings. The key is what is
stored in the database (products.retailer, weeks.retailer).
"""

RETAILERS = {
    "ocado": "Ocado",
    "morrisons": "Morrisons",
    "tesco": "Tesco",
    "sainsburys": "Sainsbury's",
}


def name(key):
    """Display name, e.g. 'sainsburys' -> "Sainsbury's"."""
    return RETAILERS.get(key) or (key or "").title()
