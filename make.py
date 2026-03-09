from PIL import Image
import os

# Ensure the folder exists
os.makedirs(r"C:\Users\user\Desktop\Capstone APP\snapcook_app\assets\apps", exist_ok=True)

# Create a 200x200 fully transparent image
img = Image.new('RGBA', (200, 200), (0, 0, 0, 0))

# Save it
img.save(r"C:\Users\user\Desktop\Capstone APP\snapcook_app\assets\apps\transparent.png", "PNG")

print("Successfully created a perfect 200x200 transparent.png!")