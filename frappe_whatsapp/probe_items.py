import frappe


def run():
    items = frappe.db.get_all("Item", filters={"disabled": 0}, fields=["name", "item_name", "item_group"], limit=20)
    print("items:", items)
    prices = frappe.db.sql(
        "select parent, price_list, price_list_rate, buying, selling from `tabItem Price` limit 25"
    )
    print("item prices:", prices)
    meta = frappe.get_meta("Item")
    tables = [(df.fieldname, df.options) for df in meta.fields if df.fieldtype == "Table"]
    print("Item child tables:", tables)
    for fn, options in tables:
        child = frappe.get_meta(options)
        print(fn, "->", options, [(f.fieldname, f.fieldtype) for f in child.fields if f.fieldname][:15])
