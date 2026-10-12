"""Player votes on the playing track: in memory, one vote per device token, dropped when the track changes."""
import re

MAX_PLAYERS = 20
MAX_TOKENS = 200
TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CHOICES = ("like", "dislike", "none")


class VoteError(ValueError):
    pass


class Votes:
    def __init__(self, players: int = 0):
        self.players = players
        self.track_id: int | None = None
        self._votes: dict[str, str] = {}
        self.skipped_id: int | None = None   # a skip was issued for this track; the crossfade may still show it

    def set_players(self, n: int) -> None:
        if not 0 <= n <= MAX_PLAYERS:
            raise ValueError(f"players must be 0-{MAX_PLAYERS}")
        self.players = n

    def sync(self, track_id: int | None) -> None:
        """Call with the playing track; a different track starts with no votes."""
        if track_id != self.track_id:
            self.track_id = track_id
            self._votes = {}

    def cast(self, track_id: int, token: str, vote: str) -> None:
        if self.players == 0:
            raise VoteError("voting is off")
        if not TOKEN.match(token or ""):
            raise VoteError("bad token")
        if vote not in CHOICES:
            raise VoteError("bad vote")
        if track_id != self.track_id:
            raise VoteError("stale track")
        if vote == "none":
            self._votes.pop(token, None)
        elif token in self._votes or len(self._votes) < MAX_TOKENS:
            self._votes[token] = vote

    def mine(self, token: str) -> str | None:
        return self._votes.get(token)

    def tally(self, track_id: int | None) -> dict:
        n = list(self._votes.values()) if track_id == self.track_id else []
        return {"likes": n.count("like"), "dislikes": n.count("dislike"),
                "needed": self.players // 2 + 1, "players": self.players}

    def skip_due(self, track_id: int | None) -> bool:
        return (self.players > 0 and track_id == self.track_id and track_id != self.skipped_id and self.tally(track_id)["dislikes"] * 2 > self.players)
