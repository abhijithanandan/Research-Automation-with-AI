import sqlite3

conn = sqlite3.connect("local_dev.db")
c = conn.cursor()
c.execute("SELECT id, title, citation_key FROM papers")
rows = c.fetchall()
print(f"Total Papers: {len(rows)}")
for r in rows[:5]:
    print(r)
