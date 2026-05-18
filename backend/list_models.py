import os

from google import genai

client = genai.Client(
    api_key=os.environ.get("LLM_API_KEY")
)
try:
    models = client.models.list()
    for m in models:
        print(m.name)
except Exception as e:
    print("Error:", e)
