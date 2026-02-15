from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel
from typing import List
import crud

app = FastAPI(title="SnapCook API")

# --- DATA MODELS ---
class UserSchema(BaseModel):
    user_id: str
    device_uuid: str

class ScanSchema(BaseModel):
    user_id: str
    ingredients: List[str]

class FavoriteSchema(BaseModel):
    user_id: str
    recipe_id: str
    recipe_name: str
    image_url: str = None
    ingredients: List[str] = []
    missing_ingredients: List[str] = []
    instructions: List[str] = []
    estimated_servings: int = 1
    serving_reasoning: str = ""

# --- NEW: Schema for removing favorites ---
class RemoveFavoriteSchema(BaseModel):
    user_id: str
    recipe_id: str 

class RecipeSuggestion(BaseModel):
    recipe_name: str
    detected_ingredients: List[str]
    missing_ingredients: List[str]
    estimated_servings: int
    serving_reasoning: str
    instructions: List[str]

class AIAnalysisResponse(BaseModel):
    suggestions: List[RecipeSuggestion]

# --- ENDPOINTS ---

@app.get("/")
def home():
    return {"message": "SnapCook Backend is Running!"}

@app.post("/login")
def login(user: UserSchema):
    return crud.create_user(user.user_id, user.device_uuid)

@app.post("/scan")
def save_scan(scan: ScanSchema):
    return crud.save_scan(scan.user_id, scan.ingredients)

# --- 1. FAVORITES ---
@app.post("/favorites")
def add_favorite(fav: FavoriteSchema):
    # Convert the Pydantic model to a dictionary (this keeps all instructions/ingredients)
    fav_dict = fav.dict() 
    
    # Pass the full dictionary to the crud function
    return crud.add_favorite(fav_dict)

# --- NEW: Delete Endpoint ---
@app.post("/favorites/remove")
def remove_favorite(data: RemoveFavoriteSchema):
    return crud.remove_favorite(data.user_id, data.recipe_id)

@app.get("/favorites/{user_id}")
def get_user_favorites(user_id: str):
    return crud.get_favorites(user_id)

# --- 2. AI IMAGE SCAN ---
@app.post("/scan/ai/online", response_model=AIAnalysisResponse)
async def analyze_online(file: UploadFile = File(...)):
    image_bytes = await file.read()
    result = crud.analyze_image_with_gemini(image_bytes)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result

# --- 3. AI TEXT SEARCH ---
@app.post("/recipes/search")
async def search_recipes(data: dict):
    ingredients = data.get("ingredients", [])
    if not ingredients:
        return {"suggestions": []}
    result = await crud.search_recipes_by_text(ingredients)
    return result
# --- ADD THIS TO YOUR main.py (Inside Endpoints) ---

# --- ADD THIS TO main.py (Inside Endpoints section) ---

@app.get("/recipes/suggestions")
def get_suggestions():
    # Returns a curated list of recipes for the Home Screen
    return {
        "suggestions": [
            {
                "recipe_name": "Chicken Adobo", 
                "image_url": "https://upload.wikimedia.org/wikipedia/commons/c/c9/Adobo_DSC_1677.jpg"
            },
            {
                "recipe_name": "Sinigang na Baboy", 
                "image_url": "https://upload.wikimedia.org/wikipedia/commons/f/f3/Sinigang_na_Baboy_%28Pork_Sinigang%29.jpg"
            },
            {
                "recipe_name": "Bicol Express", 
                "image_url": "https://upload.wikimedia.org/wikipedia/commons/e/ec/Fried_chicken_2.jpg" 
            },
            {
                "recipe_name": "Kare-Kare", 
                "image_url": "https://upload.wikimedia.org/wikipedia/commons/a/a2/Kare-kare_01.jpg"
            }
        ]
    }