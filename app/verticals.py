import json
from pathlib import Path

VERTICALS_FILE = Path(__file__).parent.parent / "verticals.json"

DEFAULT = ["StrategyGames", "RewardsApps", "CryptoCasino", "SolitaireRefs"]

def load():
    if VERTICALS_FILE.exists():
        return json.loads(VERTICALS_FILE.read_text())
    return DEFAULT.copy()

def save(items):
    VERTICALS_FILE.write_text(json.dumps(sorted(set(items))))

def add(name):
    items = load()
    if name and name not in items:
        items.append(name)
        save(items)
    return load()

def remove(name):
    items = [v for v in load() if v != name]
    save(items)
    return load()
