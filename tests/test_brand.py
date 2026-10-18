"""Brand images served by Home Assistant (≥ 2026.3) from custom_components/stockpot/brand/,
also accepted by the HACS brands check (https://github.com/home-assistant/brands#image-specification)."""

from __future__ import annotations

from pathlib import Path
import struct

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_custom_components

from custom_components.stockpot.const import DOMAIN

BRAND = Path(__file__).parents[1] / "custom_components" / DOMAIN / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png(path: Path) -> tuple[int, int, int]:
    """(width, height, colour type) from the IHDR chunk."""
    header = path.read_bytes()[:26]
    assert header[:8] == PNG_SIGNATURE, path.name
    assert header[12:16] == b"IHDR", path.name
    width, height, _depth, colour = struct.unpack(">IIBB", header[16:26])
    return width, height, colour


@pytest.mark.parametrize("prefix", ["", "dark_"])
def test_brand_images(prefix: str) -> None:
    """Icons 256 / 512 square, logos with a shorter side of 128 / 256, all with alpha."""
    for name, size in ((f"{prefix}icon.png", 256), (f"{prefix}icon@2x.png", 512)):
        assert _png(BRAND / name) == (size, size, 6), name
    for name, short in ((f"{prefix}logo.png", 128), (f"{prefix}logo@2x.png", 256)):
        width, height, colour = _png(BRAND / name)
        assert min(width, height) == short, name
        assert width > height, name  # landscape
        assert colour == 6, name  # RGBA: transparent background


def test_only_known_brand_files() -> None:
    """Only the names Home Assistant serves (homeassistant/components/brands/const.py)."""
    allowed = {
        f"{dark}{kind}{hidpi}.png" for dark in ("", "dark_") for kind in ("icon", "logo") for hidpi in ("", "@2x")
    }
    assert {path.name for path in BRAND.iterdir()} == allowed


async def test_home_assistant_sees_the_brand(hass: HomeAssistant) -> None:
    """The loader flags the integration as branded, so /api/brands serves the local files."""
    integration = (await async_get_custom_components(hass))[DOMAIN]
    assert integration.has_branding
