from firebase_config import get_db
from datetime import datetime
from google.cloud.firestore_v1.base_query import FieldFilter
import google.generativeai as genai
import os
import json
import hashlib
from duckduckgo_search import DDGS 
from dotenv import load_dotenv
import time

load_dotenv()

# --- CONFIGURATION ---
GENAI_API_KEY = os.getenv("GENAI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)  

db = get_db()

# Collection names
USERS_COLLECTION = "users"
RECIPES_COLLECTION = "recipes"
CACHE_COLLECTION = "search_cache" 

# --- HELPER: GET REAL IMAGE ---
def get_dish_image(dish_name):
    """
    Searches DuckDuckGo for a real image of the cooked dish.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(
                keywords=f"{dish_name} food cooked plating", 
                max_results=1
            ))
            if results and len(results) > 0:
                return results[0]['image']
    except Exception as e:
        print(f"Image Search Error: {e}")
    
    # Fallback Placeholder
    return "https://via.placeholder.com/400x300?text=No+Image"

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
    print("--- [DEBUG] Sending Image to Gemini... ---")
    model = genai.GenerativeModel('gemini-2.5-flash')

    prompt = """
    ROLE: You are an expert Head Chef.
    TASK: Analyze the image and suggest 3 recipes.
    
    CRITICAL INSTRUCTION ON QUANTITIES:
    You MUST provide specific measurements for EVERY ingredient based on the estimated servings.
    - BAD: "Chicken", "Soy Sauce", "Garlic"
    - GOOD: "500g Chicken Thighs", "1/2 cup Soy Sauce", "4 cloves Garlic (minced)"

    CRITICAL INSTRUCTION ON INSTRUCTIONS:
    Instructions must be detailed, step-by-step, and include cooking times.
    - BAD: "Cook the chicken."
    - GOOD: "Sear the chicken in the pan for 5-7 minutes until golden brown."

    RETURN ONLY RAW JSON:
    {
      "suggestions": [
        {
          "recipe_name": "Name",
          "detected_ingredients": ["Quantity + Ingredient", "Quantity + Ingredient"],
          "missing_ingredients": ["Quantity + Ingredient"],
          "estimated_servings": 2,
          "serving_reasoning": "Based on volume visible",
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
            valid_suggestions = []
            for recipe in data["suggestions"]:
                name = recipe.get("recipe_name", "Food")
                
                if "no food" in name.lower() or "unknown" in name.lower() or "ingredient" in name.lower():
                    continue

                servings = recipe.get("estimated_servings", 1)
                if servings < 1: recipe["estimated_servings"] = 1

                print(f"--- [DEBUG] Waiting 2s before searching image for: {name} ---")
                time.sleep(2) 
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
    model = genai.GenerativeModel('gemini-2.5-flash') 

    prompt = f"""
    ROLE: You are an expert Chef.
    TASK: Suggest 3 distinct recipes using: {ingredients_str}.
    
    REQUIREMENTS:
    1. INGREDIENTS MUST HAVE QUANTITIES (e.g., '1 cup', '2 tbsp', '500g').
    2. Instructions must be descriptive and include cooking times.
    3. Prioritize Filipino dishes if applicable.
    
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
                
                print(f"--- [DEBUG] Waiting 2s before searching image for: {name} ---")
                time.sleep(2) 
                recipe["image_url"] = get_dish_image(name)

        # 3. SAVE TO CACHE
        if db and "suggestions" in data:
            db.collection(CACHE_COLLECTION).document(cache_id).set(data)

        return data

    except Exception as e:
        print(f"AI Error: {e}")
        return {"suggestions": []}