#!/usr/bin/env python3
"""
Generate placeholder reference screenshots for visual validation.

Creates mock screenshots based on the actual UI structure.
Replace these with real app screenshots when you have access to an emulator/device.

Usage:
    python generate_placeholders.py
"""
import os

def create_placeholder(output_dir, name, width=1080, height=1920):
    """Create a simple placeholder PNG."""
    from PIL import Image, ImageDraw, ImageFont
    
    img = Image.new('RGB', (width, height), color='#1A1A2E')
    draw = ImageDraw.Draw(img)
    
    # Text
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
    except:
        font = ImageFont.load_default()
        small_font = font
    
    text = name.replace('_', '\n').replace(' ', '\n')
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) // 2
    y = (height - text_h) // 2
    
    draw.text((x, y), text, fill='white', font=font)
    draw.text((width // 2 - 150, height - 200), "PLACEHOLDER - Replace with real screenshot", fill='gray', font=small_font)
    
    output_path = os.path.join(output_dir, f"{name}.png")
    img.save(output_path)
    print(f"Created: {output_path}")
    return output_path

if __name__ == "__main__":
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "references")
    os.makedirs(output_dir, exist_ok=True)
    
    # Based on actual app structure - customize these
    screens = [
        "WelcomeScreen",
        "CodeValidationScreen",
        "LoginScreen",
        "RegistrationScreen",
        "HomeScreen",
        "DashboardScreen",
        "ContractDetailScreen",
        "SettingsScreen",
    ]
    
    for screen in screens:
        create_placeholder(output_dir, screen)
    
    print(f"\nDone! Placeholders saved to: {output_dir}")
    print("\nTo replace with real screenshots:")
    print("1. Run Android app on emulator/device")
    print("2. Take screenshots")
    print("3. Place them in references/ with matching names")