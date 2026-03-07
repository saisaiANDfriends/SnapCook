from firebase_config import get_db
from datetime import datetime
from google.cloud.firestore_v1.base_query import FieldFilter
import google.generativeai as genai
import os
import json
import hashlib
from dotenv import load_dotenv
import time
import asyncio  # <--- NEW: Imported asyncio to prevent server blocking
import requests

load_dotenv(override=True)

# --- CONFIGURATION ---
GENAI_API_KEY = os.getenv("GENAI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)  

db = get_db()

# Collection names
USERS_COLLECTION = "users"
RECIPES_COLLECTION = "recipes"
CACHE_COLLECTION = "search_cache" 

def get_dish_image(dish_name):
    """
    Using Serper.dev as Google deprecated Custom Search API for new projects.
    Acts as a universal filter to clean recipe names before searching.
    """
    api_key = os.getenv("SERPER_API_KEY")
    
    # --- UNIVERSAL CLEANING LOGIC ---
    # 1. Chop off parentheses
    clean_name = dish_name.split('(')[0].strip()
    # 2. Remove AI fluff words
    clean_name = clean_name.replace("Classic", "").replace("Speedy", "").strip()
    
    print(f"--- [DEBUG] Serper searching for Cleaned Name: '{clean_name}' ---")

    if not api_key:
        print("--- [WARNING] Serper API key missing. Using placeholder. ---")
        return "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?q=80&w=500&auto=format&fit=crop"

    url = "https://google.serper.dev/images"
    
    # Use clean_name here!
    payload = json.dumps({
      "q": f"{clean_name} food plated Filipino" 
    })
    
    headers = {
      'X-API-KEY': api_key,
      'Content-Type': 'application/json'
    }

    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            images = data.get('images', [])
            if images:
                return images[0]['imageUrl']
            else:
                print(f"--- [WARNING] Serper found NO images for '{clean_name}' ---")
        else:
            print(f"--- [SERPER API ERROR] Code: {response.status_code} | Msg: {response.text} ---")
                
    except Exception as e:
        print(f"--- [Image Search Network Error] {e} ---")
    
    return "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?q=80&w=500&auto=format&fit=crop"
# --- 1. FIRESTORE: USER MANAGEMENT ---
def create_user(user_id: str, device_uuid: str):
    if db is None: return {"error": "Database not connected"}
    
    user_ref = db.collection(USERS_COLLECTION).document(user_id)
    user_data = {
        "user_id": user_id,
        "device_uuid": device_uuid,
        "last_active": datetime.utcnow().isoformat()
    }
    user_ref.set(user_data, merge=True)
    return user_data

# --- 2. FIRESTORE: SAVE SCANS ---
def save_scan(user_id: str, ingredients: list):
    if db is None: return {"error": "Database not connected"}

    scan_data = {
        "detected_ingredients": ingredients,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    db.collection(USERS_COLLECTION).document(user_id).collection("scans").add(scan_data)
    return scan_data

# --- 3. FIRESTORE: FIND RECIPES (DATABASE SEARCH) ---
def find_recipes_by_ingredients(ingredients: list):
    if db is None: return []

    recipes_ref = db.collection(RECIPES_COLLECTION)
    
    # Firestore limit: array_contains_any max 10 items
    search_list = ingredients[:10]
    
    query = recipes_ref.where(filter=FieldFilter("ingredients", "array_contains_any", search_list))
    
    results = []
    for doc in query.stream():
        data = doc.to_dict()
        data['id'] = doc.id
        results.append(data)
        
    return results

# --- 4. FIRESTORE: FAVORITES ---
def add_favorite(favorite_data: dict):
    if db is None: return {"error": "Database not connected"}

    user_id = favorite_data.get("user_id")
    recipe_id = favorite_data.get("recipe_id")

    # Add a timestamp to the data
    favorite_data["added_at"] = datetime.utcnow().isoformat()
    
    # Save the FULL data dictionary to Firestore
    # This includes ingredients, instructions, servings, etc.
    db.collection(USERS_COLLECTION).document(user_id).collection("favorites").document(recipe_id).set(favorite_data)
    
    return favorite_data

def remove_favorite(user_id: str, recipe_id: str):
    if db is None: return {"error": "Database not connected"}
    db.collection(USERS_COLLECTION).document(user_id).collection("favorites").document(recipe_id).delete()
    return {"status": "success", "message": f"Recipe {recipe_id} removed"}

def get_favorites(user_id: str):
    if db is None: return []
    docs = db.collection(USERS_COLLECTION).document(user_id).collection("favorites").stream()
    return [doc.to_dict() for doc in docs]

# --- 5. AI: IMAGE SCAN (STRICT QUANTITIES) ---
def analyze_image_with_gemini(image_bytes):
    print("--- [DEBUG] Sending Image to Gemini with STRICT Constraints... ---")
    model = genai.GenerativeModel('gemini-3.1-flash-lite-preview')

    # --- THE NEW HYPER-STRICT PROMPT ---
    prompt = """
    ROLE: You are an expert Head Chef, a highly precise visual estimator and visual  classifier.
    TASK: Analyze the image of the ingredients. Suggest 3 recipes that can be made USING ONLY the ingredients visible in the picture.

    CRITICAL REJECTION RULE (PRIORITY 1):
    Before suggesting recipes, analyze if the image contains actual, edible food ingredients.
    - If the image contains NO food (e.g., a hand, a banknote/money, a wall, electronics, animals, or a person), you MUST return: {"suggestions": [{"recipe_name": "no food"}]}.
    - If you see a cooking pot or container but cannot clearly see the food inside it, you MUST return: {"suggestions": [{"recipe_name": "no food"}]}.
    - Do NOT try to be funny. Do NOT suggest "Currency Exchange" or "Empty Pot" as a recipe.
    
    STRICT RULE 1 - NO MISSING INGREDIENTS:
    The recipes MUST NOT require any ingredients that are not clearly visible in the image. You may ONLY assume the user has water, salt, pepper, and basic cooking oil. Do not add sauces, spices, or garnishes that you cannot see. 

    STRICT RULE 2 - EXACT VISUAL SERVING ESTIMATION:
    Carefully analyze the volume, count, and size of the physical ingredients in the image. Calculate the exact number of servings these specific items will yield. Do not use generic default servings.
    Example: If you see exactly 2 chicken breasts and 3 potatoes, the yield is exactly 2 servings. 
    
    STRICT RULE 3 - SHORT RECIPE NAMES:
    Keep the "recipe_name" strictly between 1 to 3 words. Do NOT use descriptive fluff like "Classic", "Style", "Delicious", or "Authentic". 
    - GOOD: "Chicken Adobo", "Beef Pares", "Pork Sinigang"
    - BAD: "Classic Savory Filipino Chicken Adobo with Soy Sauce"
    
    CRITICAL INSTRUCTION ON QUANTITIES:
    You MUST provide specific measurements for EVERY detected ingredient based on your visual estimation.
    - GOOD: "2 whole Chicken Breasts (approx 400g)", "3 medium Tomatoes"

    RETURN ONLY RAW JSON (Do NOT include a missing_ingredients field):
    {
      "suggestions": [
        {
          "recipe_name": "Name",
          "detected_ingredients": ["Quantity + Ingredient", "Quantity + Ingredient"],
          "estimated_servings": 2,
          "serving_reasoning": "I counted exactly 2 chicken breasts and 3 medium potatoes visible, which yields exactly 2 standard portions.",
          "instructions": ["Step 1 details...", "Step 2 details..."]
        }
      ]
    }
    """

    try:
        response = model.generate_content([
            {"mime_type": "image/jpeg", "data": image_bytes},
            prompt
        ])
        
        text = response.text.replace('```json', '').replace('```', '').strip()
        data = json.loads(text)
        
        if "suggestions" in data:
            # Check if the AI triggered the 'no food' safety valve
            if any(s.get("recipe_name", "").lower() == "no food" for s in data["suggestions"]):
                print("--- [DEBUG] AI Safety Triggered: No Food Detected. ---")
                return {
                    "suggestions": [{
                        "recipe_name": "no food",
                        "detected_ingredients": [],
                        "missing_ingredients": [],
                        "estimated_servings": 0,
                        "serving_reasoning": "No edible food ingredients were detected in the image.",
                        "instructions": ["Please try taking a clearer photo of your ingredients."]
                    }]
                }

            valid_suggestions = []
            for recipe in data["suggestions"]:
                name = recipe.get("recipe_name", "Food")
                
                # Double-check naming to prevent "Unknown" results from passing
                if "no food" in name.lower() or "unknown" in name.lower():
                    continue

                recipe["estimated_servings"] = max(1, recipe.get("estimated_servings", 1))
                recipe["image_url"] = get_dish_image(name)
                
                valid_suggestions.append(recipe)
            
            data["suggestions"] = valid_suggestions
        return data
        
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"suggestions": []}

# --- 6. AI: TEXT SEARCH (STRICT QUANTITIES + HISTORY) ---
async def search_recipes_by_text(ingredients_list: list):
    ingredients_str = ", ".join(ingredients_list)
    print(f"--- [DEBUG] Searching Text: {ingredients_str} ---")
    
    # 1. CHECK CACHE (Simple version)
    ingredients_list.sort()
    combo_string = "_".join(ingredients_list).lower().strip()
    cache_id = hashlib.md5(combo_string.encode()).hexdigest()

    if db:
        doc = db.collection(CACHE_COLLECTION).document(cache_id).get()
        if doc.exists:
            print("--- [CACHE HIT] Returning saved results ---")
            return doc.to_dict()

    # 2. ASK AI
    model = genai.GenerativeModel('gemini-3.1-flash-lite-preview') 

    prompt = f"""
    ROLE: You are an expert Chef.
    TASK: Suggest 3 distinct recipes using: {ingredients_str}.
    
    REQUIREMENTS:
    1. Keep the "recipe_name" strictly between 1 to 3 words. Do NOT use descriptive fluff like "Classic", "Style", "Delicious", or "Authentic". 
    - GOOD: "Chicken Adobo", "Beef Pares", "Pork Sinigang"
    - BAD: "Classic Savory Filipino Chicken Adobo"
    2. INGREDIENTS MUST HAVE QUANTITIES (e.g., '1 cup', '500g').
    3. Instructions must be descriptive and include cooking times.
    4. Prioritize Filipino dishes if applicable but if not then search another dishes.


    
    
    RETURN JSON ONLY:
    {{
      "suggestions": [
        {{
          "recipe_name": "Name",
          "detected_ingredients": ["Quantity + Ingredient", "Quantity + Ingredient"], 
          "missing_ingredients": ["Quantity + Ingredient"],
          "estimated_servings": 2,
          "serving_reasoning": "Standard serving.",
          "instructions": ["Step 1 details...", "Step 2 details..."]
        }}
      ]
    }}
    """
    
    try:
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)

        if "suggestions" in data:
            print("--- [DEBUG] Fetching images... ---")
            for recipe in data["suggestions"]:
                name = recipe.get("recipe_name", "Food")
                servings = recipe.get("estimated_servings", 1)
                if servings < 1: recipe["estimated_servings"] = 1
                
                print(f"--- [DEBUG] Waiting 1s before searching image for: {name} ---")
                
                # --- NEW: Async sleep prevents your whole server from locking up! ---
                await asyncio.sleep(1) 
                
                recipe["image_url"] = get_dish_image(name)

        # 3. SAVE TO CACHE
        if db and "suggestions" in data:
            db.collection(CACHE_COLLECTION).document(cache_id).set(data)

        return data

    except Exception as e:
        print(f"AI Error: {e}")
        return {"suggestions": []}
