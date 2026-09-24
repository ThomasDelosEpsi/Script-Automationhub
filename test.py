import os, requests, psycopg2, json
from dotenv import load_dotenv
load_dotenv()

DB_URL = os.getenv("DATABASE_URL").replace("postgresql+psycopg2","postgresql")
API = "https://api.mistral.ai/v1/embeddings"
KEY = os.getenv("MISTRAL_API_KEY")
PROXY = os.getenv("PROXY_URL")  # ex: http://proxy.lyreco.com:8080
proxies = {"http": PROXY, "https": PROXY} if PROXY else None

q = "Monthly SAP statistics extraction, cross-check with spreadsheets, send per customer to account managers."
resp = requests.post(
    API,
    headers={"Authorization": f"Bearer {KEY}", "Content-Type":"application/json"},
    json={"input": q, "model": "mistral-embed"},
    timeout=(15, 60),
    proxies=proxies,              # ← IMPORTANT
)
resp.raise_for_status()
emb = resp.json()["data"][0]["embedding"]

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute("""
SELECT 
  "Subject Name Automation hub" AS title,
  "Departments" AS dept,
  "Country" AS country,
  LEFT("Purpose", 160) AS purpose_snip,
  "Link Automation Hub" AS link,
  (embedding <-> %s::vector) AS l2_dist
FROM automationhub
ORDER BY embedding <-> %s::vector
LIMIT 5;
""", (emb, emb))
for r in cur.fetchall():
    print(r)