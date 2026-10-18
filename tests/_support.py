"""Portable font selection for disposable synthetic OCR fixtures."""
import os
from pathlib import Path


def fixture_font(size):
    from PIL import ImageFont
    choices = [
        os.environ.get("AGENT_TRANSCRIBE_TEST_FONT"),
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for choice in choices:
        if choice and Path(choice).is_file():
            return ImageFont.truetype(choice, size)
    raise RuntimeError("Install a TrueType font or set AGENT_TRANSCRIBE_TEST_FONT for synthetic OCR tests")
