import time
import database as db
from web.app import app, reanalyze_all

if __name__ == "__main__":
    with app.app_context():
        reanalyze_all()
    # reanalyze_all() spawns a daemon thread — block here until it finishes
    # so the process doesn't exit and kill the thread mid-run.
    while db.get_pref("reanalyze_status") == "running":
        time.sleep(2)
