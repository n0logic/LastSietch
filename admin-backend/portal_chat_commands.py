"""Slash commands for portal chat (Fremkit wave 11.1, owner ruling 2026-09-05).

One pure function. `parse(body)` takes an ALREADY CLEANED body and answers
`(kind, body, reason)`: the kind the row is stored under, the body that replaces
what the player typed, and a refusal token instead of both when the command does
not exist or its argument is out of range.

Three things about where this sits:

  * IT RUNS AFTER `clean_body`, NEVER BEFORE. The 500-character cap, the link
    allowlist and the mention strip apply to what the player typed, so '/me' can
    neither carry a longer message than a plain one nor smuggle a live link past
    the filter by wearing a command in front of it.
  * ONLY THE SERVER ROLLS. `/roll` resolves here, out of `secrets`, and the
    result is stored as text. A client-side roll is a number the client chose.
  * THE OTHER THREE COMMANDS NEVER ARRIVE. `/w`, `/price` and `/help` are
    answered by the composer against the mailbox and market namespaces and are
    never posted to the room, so reaching this module means a client that does
    not know them: they refuse as unknown, and the refusal lists all five so the
    player is told what exists rather than what happens to be implemented here.

Nothing here logs a body.
"""
import re
import secrets

# What the unknown_command refusal hands back, in the order the composer's
# /help lists them.
COMMANDS = ("/me", "/roll", "/w", "/price", "/help")

# The kinds a stored row can carry. 'system' is the page's own ephemeral rows
# and is never stored, so it is deliberately not here: a kind that reaches the
# table is a kind some moderator has to be able to read back.
KIND_SAY = "say"
KIND_EMOTE = "emote"
KIND_ROLL = "roll"
STORED_KINDS = (KIND_SAY, KIND_EMOTE, KIND_ROLL)

# Bounds the owner set. The ceiling is not about arithmetic cost, it is about a
# roll being readable: "rolled 200d1000" is a wall, not a dice roll.
ROLL_DEFAULT_DICE = 1
ROLL_DEFAULT_SIDES = 20
ROLL_MAX_DICE = 10
ROLL_MIN_SIDES = 2
ROLL_MAX_SIDES = 1000

# The command word is letters only, so '/roll' is a command and '/2' or '//x' is
# not one and refuses as unknown rather than being silently posted as text. The
# argument may span the newlines clean_body kept.
_COMMAND_RE = re.compile(r"/([A-Za-z]+)(?:\s+(.*))?", re.DOTALL)
# Digit counts are bounded HERE as well as by the range check below: an integer
# is parsed out of this, and a bound on the value is not a bound on the parse.
_ROLL_RE = re.compile(r"(\d{1,3})?d(\d{1,5})", re.IGNORECASE)


def roll_dice(count: int, sides: int) -> int:
    """`secrets`, not `random`. A player who can predict the next roll from the
    ones before it is a player who wins every bet in the room, and `random` is
    seeded well enough to make that a real afternoon's work rather than a
    theoretical one."""
    return sum(1 + secrets.randbelow(int(sides)) for _ in range(int(count)))


def parse(body):
    """(kind, body, reason) for one cleaned body.

    On success `reason` is None and the caller stores `body` under `kind`. On a
    refusal `kind` is None and `reason` is the token the route hands back at
    HTTP 200: 'unknown_command' for a word that is not a command, 'bad_request'
    for a command whose argument is missing or out of range.

    A body that does not begin with '/' is not a command and comes back
    unchanged as 'say'. There is no escape for a message that legitimately
    starts with a slash: every leading-slash word is a command attempt, which is
    what a player typing one means every time."""
    if not isinstance(body, str):
        return None, "", "bad_request"
    if not body.startswith("/"):
        return KIND_SAY, body, None

    match = _COMMAND_RE.fullmatch(body)
    if match is None:
        return None, "", "unknown_command"
    name = match.group(1).lower()
    rest = (match.group(2) or "").strip()

    if name == "me":
        # '/me' alone is not an empty emote, it is a player who meant to type
        # something. Refusing is the only answer that does not post their name
        # on its own line.
        if not rest:
            return None, "", "bad_request"
        return KIND_EMOTE, rest, None
    if name == "roll":
        return _parse_roll(rest)
    return None, "", "unknown_command"


def _parse_roll(spec: str):
    """'' -> 1d20, else NdM with N optional. An unparseable or out-of-range spec
    is 'bad_request' and not 'unknown_command': /roll exists, the argument is
    what is wrong, and telling the player otherwise sends them looking for a
    command they already found."""
    count, sides = ROLL_DEFAULT_DICE, ROLL_DEFAULT_SIDES
    if spec:
        match = _ROLL_RE.fullmatch(spec)
        if match is None:
            return None, "", "bad_request"
        count = int(match.group(1)) if match.group(1) else 1
        sides = int(match.group(2))
        if not 1 <= count <= ROLL_MAX_DICE:
            return None, "", "bad_request"
        if not ROLL_MIN_SIDES <= sides <= ROLL_MAX_SIDES:
            return None, "", "bad_request"
    return KIND_ROLL, "rolled %dd%d: %d" % (count, sides, roll_dice(count, sides)), None
