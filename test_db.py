import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT * FROM visitors LIMIT 1")
rows = cur.fetchall()

print("Columns in DB:")
for desc in cur.description:
    print(desc.name)

print(f"Number of columns: {len(cur.description)}")
conn.close()
