"""
Product catalog for the recommendations feature: products, their sellable variants (SKUs with stock) and
hand-written relations between products (complement, alternative, upgrade).

The first ten products are the ones orders are generated from (generate.CATALOG); their codes are the title
initials that generate.py puts into order_items.sku, so every ordered SKU is a catalog variant (validate.py checks
it). The catalog draws from its own random generator, so adding or changing products never changes the order,
return or refund data.

Stylist data (Everly companion plan P4, for Personal Stylist Mode):
    N1  six new products staged for launch (LAUNCHES): in products with active=false and stock, plus a
        product_launches row with the planned launch time (days after the reference date). A product that is not
        active and has no launch row is discontinued.
    N2  relations of the new products to existing ones (in RELATIONS, the same table).
    N3  2-3 style tags per product from a fixed list (STYLES -> product_styles).
    N4  a colour family for every colour (COLOUR_FAMILY -> colour_families) and how well two families go together
        (HARMONY -> colour_harmony: one row per unordered pair of families, family <= other_family).
New products are appended at the end, so the stock drawn for the existing variants does not change.
"""

import random
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

COLD, WET, WARM, ALL_YEAR, SPRING_FALL = "cold_weather", "wet_weather", "warm_weather", "all_year", "spring_fall"
SHOE_SIZES = ["8", "9", "10", "11"]
TOPS = ["S", "M", "L", "XL"]
WAISTS = ["30", "32", "34", "36"]
ONE_SIZE = [None]

# (code, title, category, colors, sizes, min_price, max_price, material, fit, season, active, description)
PRODUCTS: list[tuple] = [
    # -- ordered today (generate.CATALOG): title, category, colours and sizes must match it
    ("CPJ", "Classic Puffer Jacket", "outerwear", ["blue", "black", "green"], TOPS, 110, 160,
     "recycled polyester shell, synthetic fill", "regular", COLD, True,
     "Warm, light puffer with a water-repellent shell for everyday winter wear."),
    ("RSJ", "Rain Shell Jacket", "outerwear", ["blue", "yellow", "grey"], ["S", "M", "L"], 80, 120,
     "waterproof nylon", "regular", WET, True,
     "Fully waterproof, breathable shell with taped seams and an adjustable hood."),
    ("WO", "Wool Overcoat", "outerwear", ["camel", "navy", "black"], ["S", "M", "L"], 220, 320,
     "wool blend", "tailored", COLD, True,
     "Knee-length tailored overcoat in a soft wool blend."),
    ("MCS", "Merino Crew Sweater", "knitwear", ["grey", "navy", "red"], TOPS, 70, 95,
     "merino wool", "regular", COLD, True,
     "Fine-gauge merino crew neck that layers under jackets and coats."),
    ("SCP", "Slim Chino Pants", "pants", ["khaki", "navy", "olive"], WAISTS, 45, 70,
     "stretch cotton twill", "slim", ALL_YEAR, True,
     "Slim-fit chinos with a little stretch, smart enough for the office."),
    ("TRS", "Trail Running Shoes", "footwear", ["black", "blue", "orange"], SHOE_SIZES, 95, 140,
     "mesh upper, rubber lug sole", "regular", ALL_YEAR, True,
     "Grippy trail shoes with a cushioned midsole for mixed terrain."),
    ("LB", "Leather Belt", "accessories", ["brown", "black"], ["M", "L"], 30, 45,
     "full-grain leather", None, ALL_YEAR, True,
     "Full-grain leather belt with a brushed metal buckle."),
    ("CTB", "Canvas Tote Bag", "accessories", ["natural", "black"], ONE_SIZE, 25, 40,
     "heavy cotton canvas", None, ALL_YEAR, True,
     "Roomy canvas tote with an inside pocket."),
    ("OS", "Oxford Shirt", "shirts", ["white", "blue", "pink"], TOPS, 40, 65,
     "cotton oxford cloth", "regular", ALL_YEAR, True,
     "Button-down oxford shirt that works with chinos or jeans."),
    ("TB", "Thermal Beanie", "accessories", ["grey", "red", "black"], ONE_SIZE, 18, 28,
     "merino blend rib knit", None, COLD, True,
     "Soft rib-knit beanie lined for cold days."),
    # -- outerwear
    ("EDP", "Expedition Down Parka", "outerwear", ["black", "navy"], TOPS, 260, 320,
     "down fill, ripstop shell", "relaxed", COLD, True,
     "Hip-length down parka rated for sub-zero temperatures."),
    ("PRJ", "Packable Rain Jacket", "outerwear", ["blue", "olive"], TOPS, 60, 80,
     "lightweight waterproof nylon", "regular", WET, True,
     "Featherweight rain jacket that packs into its own pocket."),
    ("QV", "Quilted Vest", "outerwear", ["navy", "black", "olive"], TOPS, 65, 85,
     "recycled polyester fill", "regular", SPRING_FALL, True,
     "Light quilted vest for layering in between seasons."),
    ("DTJ", "Denim Trucker Jacket", "outerwear", ["indigo", "black"], TOPS, 85, 110,
     "cotton denim", "regular", SPRING_FALL, True,
     "Classic trucker jacket in rigid cotton denim."),
    # -- knitwear
    ("CCC", "Cashmere Crew Sweater", "knitwear", ["camel", "grey", "navy"], ["S", "M", "L"], 160, 190,
     "pure cashmere", "regular", COLD, True,
     "Pure cashmere crew neck, our softest sweater."),
    ("CKC", "Cable Knit Cardigan", "knitwear", ["cream", "navy"], TOPS, 90, 120,
     "wool and cotton blend", "relaxed", COLD, True,
     "Chunky cable-knit cardigan with horn-effect buttons."),
    ("MHZ", "Merino Half-Zip", "knitwear", ["grey", "black"], TOPS, 85, 105,
     "merino wool", "regular", COLD, True,
     "Merino half-zip pullover with a stand-up collar."),
    # -- pants
    ("SLJ", "Straight Leg Jeans", "pants", ["indigo", "black"], WAISTS, 60, 85,
     "cotton denim", "straight", ALL_YEAR, True,
     "Straight-leg jeans in mid-weight denim."),
    ("WTP", "Wool Trousers", "pants", ["charcoal", "navy"], WAISTS, 95, 130,
     "wool blend", "tailored", COLD, True,
     "Tailored wool trousers that pair with an overcoat."),
    ("RCP", "Relaxed Chino Pants", "pants", ["khaki", "stone"], WAISTS, 45, 70,
     "cotton twill", "relaxed", ALL_YEAR, True,
     "Relaxed-fit chinos with more room through the leg."),
    # -- footwear
    ("LCS", "Leather Chelsea Boots", "footwear", ["brown", "black"], SHOE_SIZES, 150, 190,
     "suede and leather", "regular", ALL_YEAR, True,
     "Pull-on chelsea boots with elastic sides and a rubber sole."),
    ("CCS", "Canvas Court Sneakers", "footwear", ["white", "navy"], SHOE_SIZES, 55, 75,
     "cotton canvas, rubber sole", "regular", WARM, True,
     "Low-top canvas sneakers for everyday wear."),
    ("RRS", "Road Running Shoes", "footwear", ["black", "white"], SHOE_SIZES, 100, 135,
     "engineered mesh", "regular", ALL_YEAR, True,
     "Responsive road running shoes for daily miles."),
    ("WHB", "Waterproof Hiking Boots", "footwear", ["brown", "grey"], SHOE_SIZES, 140, 175,
     "leather with waterproof membrane", "regular", WET, True,
     "Waterproof leather hiking boots with ankle support."),
    # -- accessories
    ("MWS", "Merino Wool Scarf", "accessories", ["grey", "camel", "red"], ONE_SIZE, 35, 50,
     "merino wool", None, COLD, True,
     "Long merino scarf, warm without bulk."),
    ("TG", "Touchscreen Gloves", "accessories", ["black", "grey"], ["S", "M", "L"], 25, 35,
     "knit with conductive fingertips", None, COLD, True,
     "Knit gloves that work with phone screens."),
    ("LW", "Leather Wallet", "accessories", ["brown", "black"], ONE_SIZE, 40, 55,
     "full-grain leather", None, ALL_YEAR, True,
     "Slim bifold wallet in full-grain leather."),
    ("CBC", "Classic Baseball Cap", "accessories", ["navy", "khaki"], ONE_SIZE, 20, 28,
     "cotton twill", None, WARM, True,
     "Six-panel cotton cap with an adjustable strap."),
    ("CB", "Commuter Backpack", "accessories", ["black", "olive"], ONE_SIZE, 70, 90,
     "water-resistant nylon", None, ALL_YEAR, True,
     "Water-resistant backpack with a padded laptop sleeve."),
    # -- shirts
    ("LS", "Linen Shirt", "shirts", ["white", "sky"], TOPS, 50, 70,
     "pure linen", "relaxed", WARM, True,
     "Breathable linen shirt for warm days."),
    ("FS", "Flannel Shirt", "shirts", ["red", "navy"], TOPS, 45, 60,
     "brushed cotton flannel", "regular", COLD, True,
     "Brushed cotton flannel shirt in a classic check."),
    ("PT", "Pique Polo", "shirts", ["white", "navy", "green"], TOPS, 35, 50,
     "cotton pique", "regular", WARM, True,
     "Cotton pique polo with a two-button placket."),
    ("ECT", "Essential Crew Tee", "shirts", ["white", "black", "grey"], TOPS, 18, 25,
     "organic cotton jersey", "regular", ALL_YEAR, True,
     "Everyday crew-neck tee in organic cotton."),
    ("DS", "Denim Shirt", "shirts", ["indigo", "chambray"], TOPS, 50, 65,
     "cotton denim", "regular", ALL_YEAR, True,
     "Western-style denim shirt with snap buttons."),
    # -- activewear
    ("TJ", "Travel Joggers", "activewear", ["black", "charcoal"], TOPS, 55, 70,
     "stretch technical fabric", "tapered", ALL_YEAR, True,
     "Wrinkle-resistant joggers with zip pockets."),
    ("PRS", "Performance Running Socks", "activewear", ["black", "white"], ["M", "L"], 12, 16,
     "cushioned synthetic blend", None, ALL_YEAR, True,
     "Cushioned, blister-resistant running socks."),
    ("LRT", "Lightweight Running Tee", "activewear", ["black", "orange"], TOPS, 28, 38,
     "quick-dry polyester", "regular", ALL_YEAR, True,
     "Quick-dry running tee with reflective details."),
    ("FHZ", "Fleece Half-Zip", "activewear", ["grey", "navy"], TOPS, 60, 75,
     "recycled fleece", "regular", COLD, True,
     "Midweight fleece pullover for cold runs and hikes."),
    # -- discontinued: kept so recommendations can be tested to skip inactive products
    ("CT", "Corduroy Trousers", "pants", ["brown", "olive"], WAISTS, 60, 80,
     "cotton corduroy", "regular", COLD, False,
     "Wide-wale corduroy trousers."),
    ("SRS", "Striped Rugby Shirt", "shirts", ["navy", "red"], TOPS, 55, 70,
     "heavy cotton jersey", "regular", SPRING_FALL, False,
     "Heavyweight striped rugby shirt."),
    # -- staged for launch (N1): not sold yet (active=false), stock ready; see LAUNCHES
    ("CSC", "Cashmere Scarf", "accessories", ["camel", "grey", "navy"], ONE_SIZE, 80, 95,
     "pure cashmere", None, COLD, False,
     "Featherlight pure cashmere scarf, long enough to wrap twice."),
    ("WFJ", "Waxed Field Jacket", "outerwear", ["olive", "navy"], TOPS, 220, 260,
     "waxed cotton, tartan lining", "regular", SPRING_FALL, False,
     "Waxed cotton field jacket with four bellows pockets and a corduroy collar."),
    ("SLF", "Suede Loafers", "footwear", ["tan", "navy"], SHOE_SIZES, 150, 180,
     "suede, leather sole", "regular", ALL_YEAR, False,
     "Unlined suede penny loafers that dress up chinos or wool trousers."),
    ("LOS", "Linen Overshirt", "shirts", ["sand", "sky"], TOPS, 65, 85,
     "linen and cotton blend", "relaxed", SPRING_FALL, False,
     "Linen-cotton overshirt, a light layer over a tee or a shirt for spring and autumn."),
    ("MPL", "Merino Polo", "knitwear", ["navy", "grey", "burgundy"], TOPS, 75, 90,
     "merino wool", "regular", SPRING_FALL, False,
     "Fine merino knitted polo, smart without a tie."),
    ("TFJ", "Trail Fleece Jacket", "activewear", ["forest", "black"], TOPS, 85, 105,
     "recycled grid fleece", "regular", COLD, False,
     "Grid fleece mid-layer for hikes and cold runs."),
]

# N1: product code -> (days after the reference date, headline). Launch time is 09:00 UTC that day.
LAUNCHES: dict[str, tuple[int, str]] = {
    "CSC": (2, "New in: pure cashmere scarves"),
    "WFJ": (5, "New in: the waxed field jacket"),
    "SLF": (7, "New in: suede loafers"),
    "LOS": (10, "New in: the linen overshirt"),
    "MPL": (14, "New in: merino polos"),
    "TFJ": (21, "New in: trail fleece"),
}

# N3: the fixed style vocabulary (also an enum in schema/tables.yaml) and 1-3 tags per product.
STYLE_TAGS = ["smart", "casual", "classic", "minimal", "outdoor", "sport", "statement", "cosy"]
STYLES: dict[str, list[str]] = {
    "CPJ": ["casual", "outdoor"], "RSJ": ["outdoor", "casual"], "WO": ["smart", "classic"],
    "MCS": ["classic", "smart", "minimal"], "SCP": ["smart", "classic"], "TRS": ["sport", "outdoor"],
    "LB": ["classic", "smart"], "CTB": ["casual", "minimal"], "OS": ["smart", "classic"], "TB": ["casual", "cosy"],
    "EDP": ["outdoor", "statement"], "PRJ": ["outdoor", "casual", "minimal"], "QV": ["casual", "outdoor"],
    "DTJ": ["casual", "classic"], "CCC": ["smart", "cosy", "minimal"], "CKC": ["cosy", "classic"],
    "MHZ": ["smart", "minimal"], "SLJ": ["casual", "classic"], "WTP": ["smart", "classic"], "RCP": ["casual"],
    "LCS": ["smart", "classic"], "CCS": ["casual", "minimal"], "RRS": ["sport"], "WHB": ["outdoor"],
    "MWS": ["cosy", "classic"], "TG": ["casual"], "LW": ["classic", "minimal"], "CBC": ["casual", "sport"],
    "CB": ["casual", "minimal"], "LS": ["casual", "minimal"], "FS": ["casual", "cosy"], "PT": ["casual", "classic"],
    "ECT": ["casual", "minimal"], "DS": ["casual", "statement"], "TJ": ["sport", "casual"], "PRS": ["sport"],
    "LRT": ["sport"], "FHZ": ["sport", "outdoor", "cosy"], "CT": ["casual", "classic"],
    "SRS": ["casual", "statement"], "CSC": ["smart", "cosy", "classic"], "WFJ": ["outdoor", "classic", "statement"],
    "SLF": ["smart", "classic"], "LOS": ["casual", "minimal"], "MPL": ["smart", "minimal"],
    "TFJ": ["outdoor", "sport"],
}

# N4: every colour in the catalog belongs to one family (an enum in schema/tables.yaml).
COLOUR_FAMILY: dict[str, str] = {
    "black": "dark_neutral", "charcoal": "dark_neutral", "grey": "dark_neutral",
    "white": "light_neutral", "cream": "light_neutral", "natural": "light_neutral", "stone": "light_neutral",
    "camel": "earth", "khaki": "earth", "brown": "earth", "tan": "earth", "sand": "earth", "olive": "earth",
    "navy": "blue", "blue": "blue", "indigo": "blue", "sky": "blue", "chambray": "blue",
    "green": "green", "forest": "green",
    "red": "warm", "orange": "warm", "yellow": "warm", "pink": "warm", "burgundy": "warm",
}
# How well two families go together, 0-1 (stylist rule of thumb: neutrals go with everything; navy and camel are a
# classic pair; bright warm colours are hardest to combine). One row per unordered pair, family <= other_family.
HARMONY: dict[tuple[str, str], float] = {
    ("blue", "blue"): 0.8, ("blue", "dark_neutral"): 1.0, ("blue", "earth"): 0.9, ("blue", "green"): 0.5,
    ("blue", "light_neutral"): 1.0, ("blue", "warm"): 0.6,
    ("dark_neutral", "dark_neutral"): 0.9, ("dark_neutral", "earth"): 0.8, ("dark_neutral", "green"): 0.8,
    ("dark_neutral", "light_neutral"): 1.0, ("dark_neutral", "warm"): 0.8,
    ("earth", "earth"): 1.0, ("earth", "green"): 0.8, ("earth", "light_neutral"): 1.0, ("earth", "warm"): 0.6,
    ("green", "green"): 0.6, ("green", "light_neutral"): 0.9, ("green", "warm"): 0.3,
    ("light_neutral", "light_neutral"): 0.9, ("light_neutral", "warm"): 0.9,
    ("warm", "warm"): 0.4,
}

# (product, related product, relation, strength 0-1): complement = goes well with; alternative = instead of;
# upgrade = a better version of the same need.
RELATIONS: list[tuple[str, str, str, float]] = [
    ("SCP", "LB", "complement", 0.9), ("SCP", "OS", "complement", 0.8), ("SCP", "LCS", "complement", 0.5),
    ("SCP", "RCP", "alternative", 0.8), ("SCP", "CT", "alternative", 0.6),
    ("OS", "SCP", "complement", 0.8), ("OS", "MCS", "complement", 0.6),
    ("LB", "SCP", "complement", 0.7), ("LB", "SLJ", "complement", 0.7), ("LB", "LW", "complement", 0.5),
    ("CPJ", "TB", "complement", 0.9), ("CPJ", "MWS", "complement", 0.7), ("CPJ", "TG", "complement", 0.6),
    ("CPJ", "QV", "alternative", 0.5), ("CPJ", "EDP", "upgrade", 0.7),
    ("WO", "MWS", "complement", 0.9), ("WO", "WTP", "complement", 0.8), ("WO", "TG", "complement", 0.6),
    ("WO", "LCS", "complement", 0.6),
    ("MCS", "OS", "complement", 0.7), ("MCS", "SCP", "complement", 0.6), ("MCS", "MHZ", "alternative", 0.6),
    ("MCS", "CCC", "upgrade", 0.7),
    ("TRS", "PRS", "complement", 0.9), ("TRS", "LRT", "complement", 0.8), ("TRS", "RRS", "alternative", 0.7),
    ("RRS", "PRS", "complement", 0.9), ("RRS", "LRT", "complement", 0.7), ("RRS", "TRS", "alternative", 0.7),
    ("RSJ", "WHB", "complement", 0.6), ("RSJ", "CB", "complement", 0.5), ("RSJ", "PRJ", "alternative", 0.8),
    ("PRJ", "RSJ", "alternative", 0.8),
    ("TB", "MWS", "complement", 0.6), ("TB", "TG", "complement", 0.6),
    ("CTB", "LW", "complement", 0.4), ("CTB", "CB", "alternative", 0.5),
    ("SLJ", "LB", "complement", 0.8), ("SLJ", "ECT", "complement", 0.6), ("SLJ", "DS", "complement", 0.4),
    ("DTJ", "ECT", "complement", 0.7), ("DTJ", "SLJ", "complement", 0.4),
    ("WTP", "OS", "complement", 0.7), ("WTP", "WO", "complement", 0.6),
    ("LS", "RCP", "complement", 0.6), ("LS", "CCS", "complement", 0.5),
    ("WHB", "PRS", "complement", 0.5), ("WHB", "FHZ", "complement", 0.5),
    ("EDP", "TB", "complement", 0.8), ("EDP", "TG", "complement", 0.7),
    # -- N2: the staged products (inactive until launch, so recommendations skip them until then)
    ("CSC", "WO", "complement", 0.95), ("CSC", "CPJ", "complement", 0.7), ("CSC", "EDP", "complement", 0.7),
    ("CSC", "CCC", "complement", 0.6), ("CSC", "MCS", "complement", 0.5),
    ("WO", "CSC", "complement", 0.9), ("CCC", "CSC", "complement", 0.6),
    ("WFJ", "SCP", "complement", 0.7), ("WFJ", "MCS", "complement", 0.7), ("WFJ", "WHB", "complement", 0.6),
    ("WFJ", "FS", "complement", 0.6), ("WFJ", "SLJ", "complement", 0.5), ("QV", "WFJ", "upgrade", 0.6),
    ("SLF", "SCP", "complement", 0.9), ("SLF", "WTP", "complement", 0.8), ("SLF", "RCP", "complement", 0.7),
    ("SLF", "OS", "complement", 0.6), ("SCP", "SLF", "complement", 0.7), ("WTP", "SLF", "complement", 0.7),
    ("LOS", "ECT", "complement", 0.8), ("LOS", "RCP", "complement", 0.7), ("LOS", "CCS", "complement", 0.6),
    ("LOS", "SCP", "complement", 0.85), ("LS", "LOS", "alternative", 0.6),
    ("MPL", "SCP", "complement", 0.8), ("MPL", "WTP", "complement", 0.7), ("MPL", "WO", "complement", 0.5),
    ("MPL", "LCS", "complement", 0.5), ("MCS", "MPL", "alternative", 0.5),
    ("TFJ", "TJ", "complement", 0.8), ("TFJ", "TRS", "complement", 0.9), ("TFJ", "WHB", "complement", 0.7),
    ("TFJ", "LRT", "complement", 0.6), ("FHZ", "TFJ", "upgrade", 0.6),
]


def sku(code: str, color: str, size: str | None) -> str:
    """The SKU format generate.py writes into order_items (code = the title's initials for ordered products)."""
    return f"SKU-{code}-{color[:3].upper()}-{size or 'OS'}"


def build_catalog(settings: dict[str, Any], reference: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Rows for the catalog tables (incl. the stylist data). Deterministic for a given seed and reference date."""
    rng = random.Random(f"{settings['seed']}-catalog")
    reference = reference or datetime.fromisoformat(settings["reference_date"]).replace(tzinfo=timezone.utc)
    products, variants = [], []
    for code, title, category, colors, sizes, lo, hi, material, fit, season, active, description in PRODUCTS:
        product_id = f"PRD-{code}"
        products.append({
            "product_id": product_id, "title": title, "category": category, "description": description,
            "material": material, "fit": fit, "season": season,
            "list_price": round((lo + hi) / 2) - 0.01, "currency": settings["currency"], "active": active,
        })
        for color in colors:
            for size in sizes:
                # About one variant in seven is sold out; discontinued products have no stock left, staged ones do.
                gone = not active and code not in LAUNCHES
                stock = 0 if gone or rng.random() < 0.15 else rng.randint(1, 40)
                variants.append({"sku": sku(code, color, size), "product_id": product_id, "color": color,
                                 "size": size, "stock": stock})
    relations = [
        {"relation_id": f"REL-{a}-{b}", "product_id": f"PRD-{a}", "related_product_id": f"PRD-{b}",
         "relation": relation, "strength": strength}
        for a, b, relation, strength in RELATIONS
    ]
    day = reference.replace(hour=9, minute=0, second=0, microsecond=0)
    launches = [{"launch_id": f"LNC-{code}", "product_id": f"PRD-{code}", "planned_at": day + timedelta(days=days),
                 "headline": headline} for code, (days, headline) in LAUNCHES.items()]
    styles = [{"style_id": f"STY-{code}-{style}", "product_id": f"PRD-{code}", "style": style}
              for code, tags in STYLES.items() for style in tags]
    families = [{"color": color, "family": family} for color, family in COLOUR_FAMILY.items()]
    harmony = [{"harmony_id": f"{a}+{b}", "family": a, "other_family": b, "strength": s}
               for (a, b), s in HARMONY.items()]
    return {"products": products, "product_variants": variants, "product_relations": relations,
            "product_launches": launches, "product_styles": styles, "colour_families": families,
            "colour_harmony": harmony}
