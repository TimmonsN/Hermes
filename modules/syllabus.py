import hashlib
import logging
import io
from bs4 import BeautifulSoup

logger = logging.getLogger("hermes.syllabus")

def hash_content(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def parse_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts)
    except Exception as e:
        logger.error(f"PDF parse error: {e}")
        return ""

def parse_html(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator="\n", strip=True)
    except Exception as e:
        logger.error(f"HTML parse error: {e}")
        return html

def truncate_for_llm(text: str, max_chars=12000) -> str:
    """Keep text under token budget for Claude."""
    if len(text) <= max_chars:
        return text
    # Try to keep the beginning (course info, policies) and the end (schedule)
    half = max_chars // 2
    return text[:half] + "\n\n[... content truncated ...]\n\n" + text[-half:]
