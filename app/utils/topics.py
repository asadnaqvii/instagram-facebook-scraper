"""Parse and manage topics from topics.txt file."""
import re
from pathlib import Path
from typing import List, Dict, Optional


def parse_topics_file(topics_file: str = "topics.txt") -> List[Dict]:
    """
    Parse topics from a topics.txt file.

    Format:
        [Topic Name]
        keywords  = #hashtag1, #hashtag2
        handles   = @username1, @username2
        platforms = instagram, facebook
    """
    script_dir = Path(__file__).parent.parent.parent
    topics_path = script_dir / topics_file

    if not topics_path.exists():
        print(f"Topics file not found: {topics_path}")
        return []

    topics = []
    current = None

    with open(topics_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            header = re.match(r"^\[(.+)\]$", line)
            if header:
                if current:
                    topics.append(current)
                current = {
                    "name": header.group(1).strip(),
                    "keywords": [],
                    "handles": [],
                    "platforms": ["instagram", "facebook"],
                    "enabled": True,
                }
                continue

            if current is None:
                continue

            if line.lower().startswith("keywords"):
                parts = line.split("=", 1)
                if len(parts) > 1:
                    current["keywords"] = [k.strip() for k in parts[1].split(",") if k.strip()]
            elif line.lower().startswith("handles"):
                parts = line.split("=", 1)
                if len(parts) > 1:
                    handles = [h.strip() for h in parts[1].split(",") if h.strip()]
                    current["handles"] = [h if h.startswith("@") else f"@{h}" for h in handles]
            elif line.lower().startswith("platforms"):
                parts = line.split("=", 1)
                if len(parts) > 1:
                    current["platforms"] = [p.strip().lower() for p in parts[1].split(",") if p.strip()]

    if current:
        topics.append(current)

    return topics


def save_topics_file(topics: List[Dict], topics_file: str = "topics.txt") -> bool:
    """Save topics back to topics.txt."""
    try:
        script_dir = Path(__file__).parent.parent.parent
        path = script_dir / topics_file
        with open(path, "w", encoding="utf-8") as f:
            for i, t in enumerate(topics):
                f.write(f"[{t['name']}]\n")
                kw = ", ".join(t.get("keywords", []))
                f.write(f"keywords  = {kw}\n")
                handles = ", ".join(t.get("handles", []))
                f.write(f"handles   = {handles}\n")
                platforms = ", ".join(t.get("platforms", ["instagram", "facebook"]))
                f.write(f"platforms = {platforms}\n")
                if i < len(topics) - 1:
                    f.write("\n")
        return True
    except Exception as e:
        print(f"Error saving topics: {e}")
        return False
