"""Omit purchase entry points from rendering and input without unlocking content."""
from pathlib import PurePosixPath

BUTTONS = frozenset(('store', 'storeon', 'storeicon', 'storeiconon', 'toys', 'toyson',
                     'removeads', 'removeadson', 'buy', 'buyon', 'restore', 'restoreon', 'share', 'shareon', 'sharepopup',
                     'whatsnew', 'whatsnewtext', 'newspanel'))


def excluded(name):
    path = PurePosixPath(name)
    if 'store' in path.parts or ('popups' in path.parts and path.stem.startswith('game-over-')):
        return True
    stem = path.stem
    return stem.split('_')[0].lower() in BUTTONS


def contains(rectangles, x, y):
    return any(left <= x <= right and top <= y <= bottom for left, top, right, bottom in rectangles)


def blocked(draws, x, y):
    for rectangle, hidden in reversed(draws):
        if contains([rectangle], x, y):
            return hidden
    return False
