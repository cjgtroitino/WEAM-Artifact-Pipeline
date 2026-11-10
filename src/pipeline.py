"""
WEAM Autonomous Artifact Digitization Pipeline
Automates the extraction, clustering, classification, and cloud integration of historical museum artifacts.
"""
import os
import json
import shutil
from collections import Counter
from PIL import Image
import gspread
from google_auth_oauthlib.flow import InstalledAppFlow
from google import genai
from google.genai import types
from dotenv import load_dotenv

# --- CONFIGURATION SETTINGS ---
load_dotenv()
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Catalogit Archive")
INCOMING_DIR = os.getenv("INCOMING_DIR", "Incoming_Photos")
PROCESSED_DIR = os.getenv("PROCESSED_DIR", "Processed_Photos")
CAMERA_DCIM_DIR = os.getenv("CAMERA_DCIM_DIR", r"D:\DCIM")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

VALID_CATEGORIES = [
    "Aircraft Parts", "Awards", "Books", "Gear", 
    "Magizine Periodicals", "Manuals & Blueprints", "Maps & Charts", 
    "Newspapers", "Paintings", "Patches & Insignia", "Photographs", 
    "Posters Advertisments", "Scales Models", "Training Materials", 
    "Uniforms", "Weapons"
]

# Initialize clients
ai_client = genai.Client(api_key=GEMINI_API_KEY)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def authenticate_google_sheets() -> gspread.Spreadsheet:
    """Authenticates the user via browser popup and connects to the sheet."""
    print("Connecting to Google Sheets...")
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME)

def get_next_item_number() -> int:
    """Scans all directories to find the next available sequential Item number."""
    max_num = 0
    dirs_to_check = [INCOMING_DIR, PROCESSED_DIR]
    
    for directory in dirs_to_check:
        if os.path.exists(directory):
            for root, dirs, files in os.walk(directory):
                for d in dirs + files:
                    if d.startswith("Item_"):
                        try:
                            max_num = max(max_num, int(d.split("_")[1]))
                        except ValueError:
                            pass
    return max_num + 1

def ingest_photos_from_camera():
    """Executes a global visual clustering pass to intelligently sort scrambled camera photos."""
    if not os.path.exists(CAMERA_DCIM_DIR):
        print(f"ℹ️ Camera directory ({CAMERA_DCIM_DIR}) not detected. Skipping.")
        return

    image_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.tiff')
    camera_photos = [os.path.join(r, f) for r, d, files in os.walk(CAMERA_DCIM_DIR) 
                     for f in files if f.lower().endswith(image_extensions)]
                
    if not camera_photos: return

    ai_contents = [
        "You are an expert museum data organizer. Look at this entire collection of photos from an out-of-order camera roll. "
        "Your task is to analyze them collectively and group their exact filenames together into matching artifact batches based entirely on visual continuity. "
        "CRITICAL CRITERIA: Completely ignore/exclude any images that show the round white 'War Eagles Air Museum' logo card. "
        "You MUST respond ONLY with a valid JSON object matching this exact structure: "
        '{"groups": [["image_nameA.jpg", "image_nameB.jpg"]]}'
    ]
    
    path_map = {}
    for photo_path in camera_photos:
        fname = os.path.basename(photo_path)
        path_map[fname] = photo_path
        try:
            with Image.open(photo_path) as img:
                img.thumbnail((400, 400)) 
                ai_contents.extend([f"Filename: {fname}", img.copy()])
        except Exception: continue

    try:
        response = ai_client.models.generate_content(
            model="gemini-3.5-flash",
            contents=ai_contents,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        clustering_data = json.loads(response.text)
    except Exception as e:
        print(f"❌ Clustering Failed: {e}")
        return

    successfully_copied_files = set()
    for group in clustering_data.get("groups", []):
        valid_paths = [path_map[fname] for fname in group if fname in path_map and os.path.exists(path_map[fname])]
        if not valid_paths: continue

        item_folder = f"Item_{get_next_item_number():04d}"
        target_path = os.path.join(INCOMING_DIR, item_folder)
        os.makedirs(target_path, exist_ok=True)

        for src_path in valid_paths:
            shutil.copy(src_path, os.path.join(target_path, os.path.basename(src_path)))
            successfully_copied_files.add(src_path)

    # Safe camera wipe
    for path in successfully_copied_files:
        try: os.remove(path)
        except Exception: pass

def analyze_item_with_ai(item_folder_path: str, folder_name: str) -> tuple:
    """Deep thinking loop that executes 3-pass consensus voting and self-correction."""
    image_extensions = ('.jpg', '.jpeg', '.png', '.webp', '.tiff')
    pil_images, detected_filenames = [], []
    
    for filename in os.listdir(item_folder_path):
        if filename.lower().endswith(image_extensions):
            with Image.open(os.path.join(item_folder_path, filename)) as img:
                img.thumbnail((768, 768)) 
                pil_images.append(img.copy())
            detected_filenames.append(filename)
            
    if not pil_images: return None, ""
    image_filenames_string = "; ".join(detected_filenames)

    prompt = f"""
    You are a head museum archivist. Generate highly detailed catalog entries.
    TASK 1: Choose the best category from: {VALID_CATEGORIES}.
    TASK 2: Generate the fields. 
    "description": Start with Time Period Used, Manufacturer/Model, Dimensions/Materials, Markings. Followed by a detailed physical narrative.
    You MUST respond ONLY with a JSON object containing keys: category, title, description, use, context.
    """

    category_votes, initial_data = [], None
    for i in range(3): # 3-Pass Consensus Loop
        try:
            res = ai_client.models.generate_content(
                model="gemini-3.1-pro-preview",
                contents=[prompt, *pil_images],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level="high")
                )
            )
            data = json.loads(res.text)
            data = data[0] if isinstance(data, list) else data
            category_votes.append(data.get("category"))
            if not initial_data: initial_data = data
        except Exception: continue

    if not category_votes: return None, ""
    
    winning_category = Counter(category_votes).most_common(1)[0][0]
    initial_data["category"] = winning_category

    # Self-Critique Pass
    critique_prompt = f"Review and polish this museum entry. Ensure metadata labels lead the description. JSON Data: {json.dumps(initial_data)}"
    try:
        final_res = ai_client.models.generate_content(
            model="gemini-3.1-pro-preview", contents=critique_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", thinking_config=types.ThinkingConfig(thinking_level="high"))
        )
        final_data = json.loads(final_res.text)
        return final_data if isinstance(final_data, dict) else initial_data, image_filenames_string
    except Exception:
        return initial_data, image_filenames_string

def main():
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    os.makedirs(INCOMING_DIR, exist_ok=True)
    
    ingest_photos_from_camera()
    
    print("\n🛑 OPERATOR CHECKPOINT: CAMERA INGESTION COMPLETE!")
    input("👉 Please verify the Incoming_Photos directory, then press [ENTER] to continue...")
    
    try: spreadsheet = authenticate_google_sheets()
    except Exception as e: return print(f"Auth Failed: {e}")

    item_folders = [f for f in os.listdir(INCOMING_DIR) if os.path.isdir(os.path.join(INCOMING_DIR, f))]
    for folder_name in item_folders:
        item_path = os.path.join(INCOMING_DIR, folder_name)
        ai_data, filenames_str = analyze_item_with_ai(item_path, folder_name)
        if not ai_data: continue
            
        category = ai_data.get("category", "Aircraft Parts")
        if category not in VALID_CATEGORIES: category = "Aircraft Parts"
            
        try:
            worksheet = spreadsheet.worksheet(category)
            worksheet.append_row([
                ai_data.get("title", ""), folder_name, ai_data.get("description", ""),
                ai_data.get("use", ""), ai_data.get("context", ""), filenames_str
            ])
            target_dir = os.path.join(PROCESSED_DIR, category)
            os.makedirs(target_dir, exist_ok=True)
            shutil.move(item_path, os.path.join(target_dir, folder_name))
        except Exception as e: print(f"❌ Export Failed: {e}")

if __name__ == "__main__":
    main()
