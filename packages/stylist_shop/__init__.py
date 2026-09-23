"""Shopping: fill a gap the wardrobe cannot."""

from stylist_shop.gaps import WardrobeGap, find_gaps
from stylist_shop.providers import CatalogueProvider, CsvProvider, Product

__all__ = ["CatalogueProvider", "CsvProvider", "Product", "WardrobeGap", "find_gaps"]
