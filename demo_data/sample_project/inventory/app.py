"""Inventory service entrypoint (RESOLVE demo project with planted, realistic defects)."""
import auth
import db
import utils


def handle_request(token, sku):
    if not auth.check(token):
        return {"error": "unauthorized"}
    # TODO: add rate limiting before release
    return db.find_item(sku)


def health():
    return utils.now_iso()
