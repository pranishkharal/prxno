from pathlib import Path
import hashlib
import json
import time

HISTORY_FILE = Path("clip_history.json")


def _load():
    if not HISTORY_FILE.exists():
        return []

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(data):
    HISTORY_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def file_hash(path):
    path = Path(path)

    h = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def is_duplicate(path):
    current_hash = file_hash(path)

    for item in _load():
        if item.get("sha256") == current_hash:
            return True, item

    return False, None


def record_clip(
    path,
    streamer=None,
    source_url=None,
    start_time=None,
    end_time=None,
    transcript=None,
    score=None
):
    path = Path(path)

    if not path.exists():
        return None

    data = _load()

    sha256 = file_hash(path)

    entry = {
        "sha256": sha256,
        "filename": path.name,
        "streamer": streamer,
        "source_url": source_url,
        "start_time": start_time,
        "end_time": end_time,
        "transcript": transcript,
        "score": score,
        "created_at": time.time()
    }

    data.append(entry)

    # Keep the history manageable.
    if len(data) > 5000:
        data = data[-5000:]

    _save(data)

    return entry


def search_similar_transcript(text, limit=10):
    if not text:
        return []

    query = " ".join(str(text).lower().split())

    if not query:
        return []

    words = set(query.split())

    results = []

    for item in _load():
        old = " ".join(
            str(item.get("transcript") or "").lower().split()
        )

        if not old:
            continue

        old_words = set(old.split())

        if not old_words:
            continue

        overlap = len(words & old_words) / max(1, len(words | old_words))

        if overlap >= 0.35:
            results.append(
                {
                    "similarity": round(overlap, 3),
                    "filename": item.get("filename"),
                    "streamer": item.get("streamer"),
                    "source_url": item.get("source_url"),
                    "score": item.get("score")
                }
            )

    results.sort(
        key=lambda x: x["similarity"],
        reverse=True
    )

    return results[:limit]
