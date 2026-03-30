"""
Piazza integration — uses the unofficial piazza-api library.

Fetches instructor posts, announcements, and assignment details for
injection into Hermes's analysis context and announcements feed.
"""

import logging
import re

from config import Config

logger = logging.getLogger("hermes.piazza")

_piazza = None
_networks = {}  # network_id -> network object


def _login():
    global _piazza
    if _piazza is not None:
        return True
    if not Config.PIAZZA_EMAIL or not Config.PIAZZA_PASSWORD:
        return False
    try:
        from piazza_api import Piazza
        p = Piazza()
        p.user_login(email=Config.PIAZZA_EMAIL, password=Config.PIAZZA_PASSWORD)
        _piazza = p
        logger.info("Piazza login successful")
        return True
    except ImportError:
        logger.warning("piazza-api not installed")
        return False
    except Exception as e:
        logger.error(f"Piazza login failed: {e}")
        return False


def get_network(network_id: str):
    if not _login():
        return None
    if network_id not in _networks:
        _networks[network_id] = _piazza.network(network_id)
    return _networks[network_id]


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r'<[^>]+>', ' ', html)
    for ent, ch in [('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                    ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")]:
        text = text.replace(ent, ch)
    return re.sub(r'\s+', ' ', text).strip()


def get_posts(network_id: str, limit: int = 100) -> list:
    """Fetch up to `limit` posts from a Piazza network.

    Returns list of dicts:
      id, subject, content, post_type, created, tags, instructor_answer
    """
    network = get_network(network_id)
    if not network:
        return []
    results = []
    try:
        post_iter = network.iter_all_posts(limit=limit)
    except Exception as e:
        logger.error(f"Failed to start Piazza post iterator for {network_id}: {e}")
        return []

    while True:
        try:
            post = next(post_iter)
        except StopIteration:
            break
        except Exception as e:
            # Individual post fetch failed (deleted post, permission issue) — skip it
            logger.debug(f"Skipping Piazza post (fetch error): {e}")
            continue

        try:
            history = post.get("history") or []
            if not history:
                continue
            subject = history[0].get("subject", "")
            content = strip_html(history[0].get("content", ""))
            created = history[0].get("created", "")
            post_type = post.get("type", "")
            tags = post.get("tags") or []
            uid = str(post.get("nr", ""))

            instructor_answer = ""
            for child in (post.get("children") or []):
                if child.get("type") in ("i_answer", "instructor"):
                    child_hist = child.get("history") or [{}]
                    instructor_answer = strip_html(child_hist[0].get("content", ""))
                    break

            results.append({
                "id": uid,
                "subject": subject,
                "content": content,
                "post_type": post_type,
                "created": created,
                "tags": tags,
                "instructor_answer": instructor_answer,
            })
        except Exception as e:
            logger.debug(f"Skipping malformed Piazza post: {e}")
            continue

    logger.info(f"Piazza: fetched {len(results)} posts from network {network_id}")
    return results


def is_configured() -> bool:
    return bool(Config.PIAZZA_EMAIL and Config.PIAZZA_PASSWORD and Config.PIAZZA_NETWORK_ID)
