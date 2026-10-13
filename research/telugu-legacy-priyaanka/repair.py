"""Pure Unicode candidates. Only explicitly supplied, validation-frozen rules run.
One ordered pass, no dictionary, glyph identities, or word-specific exceptions.
"""

# ruff: noqa: N802 -- Preserve the source research rule names R1-R5.
import re
import unicodedata

SIGNS = "ాిీుూృౄెేైొోౌ"
C = "[క-హౘౙౚ]"
S = "[" + SIGNS + "]"
CHAIN = "(?:్" + C + ")+"
CLUSTER = C + "(?:్" + C + ")*"
COMBINE = {"ెా": "ొ", "ేా": "ో", "ై": "ై"}
DUPLICATES = {"ెై": "ై", "ెూ": "ూ", "ాీ": "ీ"}


def R1(text, variant="whole"):
    if variant == "inverse":
        return re.sub("(" + CHAIN + ")(" + S + ")", r"\2\1", text)
    tail = "్" + C if variant == "one" else CHAIN
    return re.sub("(" + S + ")(" + tail + ")", r"\2\1", text)


def R2(text, variant="whole"):
    if variant == "inverse":
        return re.sub("(?<![క-హౘౙౚ్])(" + CLUSTER + ")(" + S + ")", r"\2\1", text)
    cluster = C if variant == "one" else CLUSTER
    # Adjacent duplicates belong to R3/R4; signs after virama are not prebase.
    return re.sub("(?<![క-హౘౙౚ్" + SIGNS + "])(" + S + ")(" + cluster + ")", r"\2\1", text)


def R3(text, variant="last_combine"):
    def collapse(m):
        value = m[0][0]
        for c in m[0][1:]:
            pair = value + c
            value = (
                COMBINE.get(pair, value if variant.startswith("first") else c)
                if variant.endswith("combine")
                else (value if variant == "first" else c)
            )
        return value

    return re.sub("[" + SIGNS + "ౖ]{2,}", collapse, text)


def R4(text, variant="forward"):
    for source, target in DUPLICATES.items():
        text = (
            text.replace(target, source) if variant == "inverse" else text.replace(source, target)
        )
    return text


def R5(text, variant="whole"):
    if variant == "inverse":
        return re.sub("(" + CLUSTER + ")(" + S + ")", r"\2\1", text)
    cluster = C if variant == "one" else CLUSTER
    return re.sub("(" + S + ")(" + cluster + ")", r"\2\1", text)


RULES = {f.__name__: f for f in (R1, R2, R3, R4, R5)}
CANDIDATES = {
    "R1": ["whole", "one", "inverse"],
    "R2": ["whole", "one", "inverse"],
    "R3": ["last_combine", "last", "first_combine", "first"],
    "R4": ["forward", "inverse"],
    "R5": ["whole", "one", "inverse"],
}


def repair(text, rules):
    for rule in rules:
        text = RULES[rule["id"]](text, rule["variant"])
    return unicodedata.normalize("NFC", text)
