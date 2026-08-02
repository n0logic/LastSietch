# 13. Safe database writes

If you build tooling that writes to the game database, read this first. These
rules were learned by breaking things on a live server with players on it.

The short version: **giving is safe, taking is not.**

## The client holds state you are about to contradict

A logged-in player's client caches records it has open, including their
identifiers. The server writes that cache back on certain actions without
re-checking that what it is writing still matches the database.

So a row you change out from under a loaded session is not simply changed. The
client can write its stale copy back over your change, or resurrect something
you removed, depending on what the player does next.

This is not a live exploit anyone can trigger from the game: it requires
out-of-band database access that players do not have. It is a hazard in
**operator tooling**, which is exactly what this repository is about.

## The rules

1. **Never DELETE or UPDATE a player's inventory rows while they have a loaded
   session.** Gate those writes on the player being fully offline, and check it
   inside the same transaction rather than beforehand.
2. **Inserting for an online player is generally safe**, but do not assume it
   renders immediately. In our experience a new item appears at a zone
   transition rather than the moment a UI is opened. Tell the player that, or
   they will report it as broken.
3. **Direct currency balance writes are safe online.** A balance row carries no
   client-held identifier, so there is nothing stale to write back.
4. **Never remove your own balance pre-checks.** Do not assume the database
   functions clamp or validate. We found a currency function whose
   negative-balance branch references an undefined variable, so it raises
   instead of clamping, and its anti-cheat logging can never fire. Your checks
   may be the only ones that run.
5. **Never call a vendor `get_*` function from a read-only path.** Several
   create a row when they miss. A "read" that writes will surprise you inside a
   transaction you thought was safe.

## Progression is RAM-backed

Character progression, skills, and inventory live in memory while a player is
online and are flushed periodically. Two consequences:

- Writing them for an online player is pointless at best: the flush overwrites
  you.
- A restart under load can lose recent progress. This is the reason for the
  never-restart-with-players-online rule in [12-operations.md](12-operations.md).

Some fields revert faster than you would expect. We measured one that reverted
within ten seconds of shutdown being signalled, before the container had even
restarted, because it is written from memory during shutdown. If you need to
change something like that, the write has to land inside the shutdown window,
after the save and before the reload.

## Test the apply path, not just the dry run

A dry-run test suite is not evidence that the apply path works. We shipped a
writer whose apply mode had never once executed: every test, preflight, and
smoke check used dry run, which returned before reaching the code that failed.
It failed on the first real call.

Get one real apply run against a real target before trusting anything, and test
every guard against a deliberately broken variant to confirm the guard is what
is stopping it.

## Write an audit trail, and never discard it

Log every write your tooling performs to your own table, including failures.
Two specifics that cost us:

- **Never send an audit insert to `/dev/null`.** If it fails you will never
  know, and the failure will be silent for weeks.
- **Do not infer direction from a magnitude.** If you log deltas, confirm
  whether the source is signed. We had two functions logging the same concept
  differently, one signed and one as an unsigned magnitude, which made a naive
  query report the opposite of what happened.

Also worth knowing: vendor event logs may retain only days. If you want to be
able to answer a question about last month, snapshot into your own table now.
