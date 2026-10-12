"""The DM password: three D&D words in camelCase. A curtain against players peeking at the queue,
not a security boundary."""
import hashlib
import hmac
import secrets
from pathlib import Path

WORDS = """
Acolyte Alchemist Amulet Anvil Arcane Archer Armor Axe Banshee Barbarian Bard Basilisk Beholder Bugbear
Cantrip Cavern Centaur Cleric Cloak Cobalt Crypt Cultist Dagger Delver Demon Dire Dragon Druid Dungeon
Dwarf Elixir Elf Ember Fable Falchion Familiar Fey Flagon Gargoyle Gauntlet Ghoul Giant Gnoll Gnome Goblin
Golem Gorgon Griffon Grimoire Guild Halfling Harpy Hearth Helm Hobgoblin Hydra Illusion Imp Iron Jester
Kobold Kraken Lantern Lich Longbow Mace Mage Manticore Medusa Mimic Minotaur Mithril Monk Moat Mummy
Necromancer Nymph Ogre Oracle Orc Owlbear Paladin Pegasus Phoenix Pixie Portal Potion Quest Quiver Ranger
Relic Rogue Rune Sage Satyr Scroll Shadow Shield Siren Skeleton Sorcerer Specter Sphinx Spire Sprite Staff
Stirge Sword Talisman Tavern Thief Tome Torch Treant Troll Troglodyte Unicorn Vampire Vault Warlock Warden
Wand Werewolf Wight Wizard Wraith Wyvern Yeti Zombie
""".split()

FILE = "dm-password"
COOKIE = "dm_session"


def make_password(rng=secrets) -> str:
    words: list[str] = []
    while len(words) < 3:
        w = rng.choice(WORDS)
        if w not in words:
            words.append(w)
    return "".join(words)


def load_password(data_dir: Path) -> str:
    """The saved password; created on first use so it survives restarts and updates."""
    p = Path(data_dir) / FILE
    try:
        text = p.read_text(encoding="utf-8").strip()
        if text:
            return text
    except FileNotFoundError:
        pass
    pw = make_password()
    p.write_text(pw + "\n", encoding="utf-8")
    p.chmod(0o600)
    return pw


def session_token(password: str) -> str:
    return hmac.new(password.encode(), b"dnd-music-dm-session", hashlib.sha256).hexdigest()


def password_ok(given: str, password: str) -> bool:
    return hmac.compare_digest(given.strip().encode(), password.encode())


OPEN_PREFIXES = ("/static/", "/api/players/", "/api/login")
OPEN_PATHS = {"/login", "/logout", "/players", "/stream.mp3", "/favicon.ico"}


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


class DmGate:
    """ASGI middleware: everything outside the open paths needs the session cookie."""

    def __init__(self, app, get_token):
        self.app = app
        self.get_token = get_token   # () -> session token, or None while the password is off

    def _has_cookie(self, scope, token: str) -> bool:
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    name, _, val = part.strip().partition("=")
                    if name == COOKIE and hmac.compare_digest(val, token):
                        return True
        return False

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket") or is_open(scope["path"]):
            return await self.app(scope, receive, send)
        token = self.get_token()
        if token is None or self._has_cookie(scope, token):
            return await self.app(scope, receive, send)
        if kind == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        api = scope["path"].startswith("/api/")
        status, headers, body = (401, [(b"content-type", b"application/json")], b'{"detail":"password required"}') \
            if api else (302, [(b"location", b"/login")], b"")
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
