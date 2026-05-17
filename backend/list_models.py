import os

from google import genai

client = genai.Client(
    api_key=os.environ.get("LLM_API_KEY", "AIzaSyA0NZDsmWG6YN7hE0DIE9tJRa_5MLDZ2-c")
)
try:
    models = client.models.list()
    for m in models:
        print(m.name)
except Exception as e:
    print("Error:", e)
