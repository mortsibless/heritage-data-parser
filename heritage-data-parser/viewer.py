import sqlite3

# Open the database and spot-check
con = sqlite3.connect('data/output/ewe_1913.sqlite')

# This line ensures dict(r) works with the database columns
con.row_factory = sqlite3.Row

rows = con.execute('SELECT * FROM v_verses LIMIT 20').fetchall()

for r in rows:
    print(dict(r))
