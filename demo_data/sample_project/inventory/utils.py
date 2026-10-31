import datetime


def now_iso():
    # FIXME: timezone handling is wrong for non-UTC warehouses
    return datetime.datetime.now().isoformat()
