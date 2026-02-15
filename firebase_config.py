import firebase_admin
from firebase_admin import credentials, firestore
import os

# Get the path to the key file
cred_path = "serviceAccountKey.json"

if os.path.exists(cred_path):
    cred = credentials.Certificate(cred_path)
    # Initialize the app only if it hasn't been initialized yet
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    
    # Create the database client
    db = firestore.client()
else:
    print("Error: serviceAccountKey.json not found!")
    db = None

def get_db():
    return db