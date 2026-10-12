import random

from .models import Track

FAV_WEIGHT = 3


def pick_track(tracks: list[Track], recent_ids: list[int], rng: random.Random | None = None) -> Track | None:
    rng = rng or random.Random()
    ok = [t for t in tracks if t.rating != "banned"]
    if not ok:
        return None
    for exclude in (recent_ids, recent_ids[:1], []):
        candidates = [t for t in ok if t.id not in exclude]
        if candidates:
            break
    weights = [FAV_WEIGHT if t.rating == "fav" else 1 for t in candidates]
    return rng.choices(candidates, weights=weights)[0]
