import sqlite3


def find_item(sku):
    conn = sqlite3.connect("inventory.db")
    try:
        cur = conn.execute("SELECT * FROM items WHERE sku = '" + sku + "'")
        return cur.fetchone()
    except:
        pass
