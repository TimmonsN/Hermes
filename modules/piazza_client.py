import logging
from config import Config

logger = logging.getLogger("hermes.piazza")

_network = None

def login():
    global _network
    if not Config.PIAZZA_EMAIL or not Config.PIAZZA_PASSWORD or not Config.PIAZZA_NETWORK_ID:
        logger.debug("Piazza credentials not configured, skipping.")
        return False

    try:
        import piazza_api
        p = piazza_api.Piazza()
        p.user_login(email=Config.PIAZZA_EMAIL, password=Config.PIAZZA_PASSWORD)
        _network = p.network(Config.PIAZZA_NETWORK_ID)
        logger.info("Piazza login successful.")
        return True
    except ImportError:
        logger.warning("piazza-api not installed. Run: pip install piazza-api")
        return False
    except Exception as e:
        logger.error(f"Piazza login failed: {e}")
        return False

def get_announcements(limit=10):
    """Return recent instructor announcements from Piazza."""
    if _network is None:
        if not login():
            return []

    try:
        feed = _network.get_feed(limit=limit, offset=0)
        announcements = []
        for item in feed.get("feed", []):
            # Instructors post with type "instructor-note" or "followup"
            if item.get("type") in ("note", "question") and item.get("folders"):
                if "instructor-note" in item.get("folders", []) or item.get("bucket_name") == "Pinned":
                    post = _network.get_post(item["nr"])
                    subject = item.get("subject", "")
                    content = ""
                    if post.get("history"):
                        content = post["history"][0].get("content", "")
                    announcements.append({
                        "id": item["id"],
                        "subject": subject,
                        "content": content,
                        "created": item.get("created", "")
                    })
        return announcements
    except Exception as e:
        logger.error(f"Failed to fetch Piazza announcements: {e}")
        return []

def get_recent_posts(limit=20):
    """Return recent posts that might contain assignment info."""
    if _network is None:
        if not login():
            return []

    try:
        feed = _network.get_feed(limit=limit, offset=0)
        posts = []
        for item in feed.get("feed", []):
            posts.append({
                "id": item.get("id"),
                "subject": item.get("subject", ""),
                "created": item.get("created", ""),
                "type": item.get("type", ""),
                "folders": item.get("folders", [])
            })
        return posts
    except Exception as e:
        logger.error(f"Failed to fetch Piazza posts: {e}")
        return []
