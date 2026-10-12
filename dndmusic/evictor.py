from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    id: int
    pool_id: int
    rating: str
    bytes: int
    last_played: float | None


@dataclass(frozen=True)
class Plan:
    delete_ids: list[int]
    over_cap: bool   # the protected set alone still exceeds the cap


def plan(items: list[Item], cap_bytes: int, protected: set[int]) -> Plan:
    """Which tracks to delete to get under the cap: banned first (oldest id first), then played tracks by
    least recently played, then never-played by id (a fresh track is the one a scene needs).
    Favourites and protected ids are never chosen."""
    used = sum(i.bytes for i in items)
    if used <= cap_bytes:
        return Plan([], False)
    eligible = [i for i in items if i.rating != "fav" and i.id not in protected]
    order = ([i for i in sorted(eligible, key=lambda i: i.id) if i.rating == "banned"]
             + sorted((i for i in eligible if i.rating != "banned" and i.last_played is not None),
                      key=lambda i: (i.last_played, i.id))
             + sorted((i for i in eligible if i.rating != "banned" and i.last_played is None), key=lambda i: i.id))
    delete: list[int] = []
    for i in order:
        if used <= cap_bytes:
            break
        delete.append(i.id)
        used -= i.bytes
    return Plan(delete, used > cap_bytes)
