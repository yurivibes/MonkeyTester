#!/usr/bin/env python3
"""
Visual validation tool for Compose Multiplatform.

Compares screenshots against references using vision model.

Usage:
    python screenshot_diff.py outputs/feature.png references/feature.png
    python screenshot_diff.py references/feature.png  # describe single image
"""
import sys

from vision_analyzer import chat, image_part, text_part


def describe_image(image_path):
    """Describe image using vision model."""
    return chat([image_part(image_path),
                 text_part("Describe this UI screenshot in detail. Note layout structure, components, colors, and typography.")],
                max_tokens=1000, temperature=0.1)


def compare_images(output_path, reference_path):
    """Compare two images using vision model."""
    return chat([image_part(reference_path), image_part(output_path),
                 text_part("Compare these two screenshots. The first is the reference, the second is the current output. List the differences using this format:\n\n## Visual Comparison\n\n**Overall Match:** PASS / FAIL (confidence %)\n\n### Differences Found:\n1. [Component] - [Issue]\n   - Expected: [description]\n   - Actual: [description]\n   - Severity: HIGH / MEDIUM / LOW\n\n### Recommendations:\n- [Fix with code suggestion]")],
                max_tokens=2000, temperature=0.1)


if __name__ == "__main__":
    if len(sys.argv) == 2:
        # Single image description
        print(f"Describing: {sys.argv[1]}")
        print(describe_image(sys.argv[1]))
    elif len(sys.argv) == 3:
        # Compare two images
        output_path = sys.argv[1]
        reference_path = sys.argv[2]
        print(f"Comparing:\n  Reference: {reference_path}\n  Output: {output_path}\n")
        print(compare_images(output_path, reference_path))
    else:
        print("Usage:")
        print("  python screenshot_diff.py <image_path>")
        print("  python screenshot_diff.py <output_path> <reference_path>")