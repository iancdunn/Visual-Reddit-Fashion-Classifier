from apify_client import ApifyClient
import base64
import json
from dotenv import load_dotenv
import os
import re
import requests
from databricks import sql
import uuid

load_dotenv()

DB_TOKEN = os.getenv("DB_TOKEN")
DB_HOST = os.getenv("DB_HOST")
DB_HTTP_PATH = os.getenv("DB_HTTP_PATH")
APIFY_KEY = os.getenv("APIFY_KEY")
MODEL_NAME = "qwen2.5vl"
OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"
POST_COUNT = 5

client = ApifyClient(APIFY_KEY)

def extract_posts():
    run_input = {
        "debugMode": False,
        "ignoreStartUrls": False,
        "includeNSFW": False,
        "maxComments": 0,
        "maxCommunitiesCount": 1,
        "maxItems": POST_COUNT,
        "maxPostCount": POST_COUNT,
        "proxy": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"]
        },
        "scrollTimeout": 60,
        "searchComments": False,
        "searchCommunities": False,
        "searchMedia": True,
        "searchPosts": True,
        "searchUsers": False,
        "skipComments": True,
        "skipCommunity": True,
        "sort": "new",
        "startUrls": [
            {
                "url": "https://www.reddit.com/r/fashion"
            }
        ]
    }
    run = client.actor("trudax/reddit-scraper-lite").call(run_input=run_input)
    candidate_posts = []
    dataset_id = run.default_dataset_id

    # Regex the first <img> tag to recover clean image URLs
    for item in client.dataset(dataset_id).iterate_items():
        html_content = item.get("html", "")
        img_match = re.search(r'<img[^>]+src="([^">]+)"', html_content)

        if item.get("dataType") == "post" and img_match:
            img_url = img_match.group(1).replace("&amp;", "&")

            candidate_posts.append((item, img_url))

    if not candidate_posts:
        print("No posts with image metadata found.")
        return

    try:
        conn = sql.connect(server_hostname=DB_HOST, http_path=DB_HTTP_PATH, access_token=DB_TOKEN)
        cursor = conn.cursor()

        # Fetches existing IDs to prevent data duplication and redundant VLM inference
        existing_ids = set()
        check_query = "SELECT id FROM default.reddit_fashion_posts"
        cursor.execute(check_query)

        existing_ids = {row[0] for row in cursor.fetchall()}

        extracted_posts = []
        extracted_items = []

        for item, img_url in candidate_posts:
            post_id = item.get("id")

            if post_id in existing_ids:
                continue

            extracted_posts.append((
                post_id,
                item.get("title"),
                img_url,
                item.get("body"),
                item.get("createdAt")
            ))

            raw_items = analyze_image(img_url)
            
            for itm in raw_items:
                secondary_colors = itm.get("secondary_colors", [])
                secondary_color_json = json.dumps(secondary_colors)

                extracted_items.append((
                    str(uuid.uuid4()),
                    post_id,
                    itm.get("clothing_category"),
                    itm.get("specific_item"),
                    itm.get("primary_color"),
                    secondary_color_json,
                    itm.get("overall_style"),
                    itm.get("brand")
                ))

        insert_query = """
            INSERT INTO default.reddit_fashion_posts
            (id, title, url, body, createdAt)
            VALUES (?, ?, ?, ?, ?)
        """

        for post in extracted_posts:
            cursor.execute(insert_query, post)

        insert_query = """
            INSERT INTO default.reddit_fashion_items
            (id, postId, clothingCategory, specificItem, primaryColor, secondaryColors, overallStyle, brand)
            VALUES (?, ?, ?, ?, ?, from_json(?, 'ARRAY<STRING>'), ?, ?)
        """

        for item in extracted_items:
            cursor.execute(insert_query, item)

        conn.commit()
    except Exception as e:
        print(f"Error connecting to Databricks: {e}")
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()

def analyze_image(img_url):
    # Avoids browser blockage when the VLM is viewing the Reddit media assets
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        img_response = requests.get(img_url, headers=headers, timeout=15)
        img_response.raise_for_status()

        b64_img = base64.b64encode(img_response.content).decode("utf-8")
    except Exception as e:
        print(f"Failed to fetch image from {img_url}: {e}")

        return []


    prompt = """
        You are a professional fashion analyst. Analyze the provided outfit image.

        Extract the distinct clothing items and accessories visible.
        Return a JSON with this schema for each item:
        {
            "items": [
                {
                    "clothing_category": (top, bottom, footwear, accessory),
                    "specific_item",
                    "primary_color",
                    "secondary_colors",
                    "overall_style" (e.g. athleisure, bohemian, casual, formal, gothic, minimalist, preppy, streetwear, vintage),
                    "brand": (If a logo or brand is explicitly visible, state it. If there is no clear brand, this value MUST be null).
                }
            ]
        }
    """

    # Enforces JSON output from Ollama for structured schema responses
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "images": [b64_img],
        "format": "json",
        "stream": False
    }

    try:
        response = requests.post(OLLAMA_ENDPOINT, json=payload, timeout=60)
        response.raise_for_status()

        raw_text = response.json().get("response").strip()
        parsed_data = json.loads(raw_text)

        if isinstance(parsed_data, dict) and "items" in parsed_data:
            return parsed_data["items"]

        return []

    except requests.exceptions.ConnectionError:
        print("Could not connect to Ollama.")
        return []
    except Exception as e:
        print(f"VLM inference error: {e}")
        return []

if __name__ == "__main__":
    extract_posts()