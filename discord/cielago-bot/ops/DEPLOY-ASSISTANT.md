# Deploying the Cielago Assistant (Phase 0/1)

The Assistant is a cog inside the existing Cielago bot on **the web host**
(`mail.example.com`). Deploying it = updating the Cielago code + env, applying
the resource-cap drop-in, and (optionally) staging the embedding model. No game
server, mail server, or game DB is touched.

> Cielago is safe to restart anytime — it is not a Dune game pod.

## 0. The site-packages trap (read first)

The service runs `python -m cielago.bot` from the **venv site-packages**, NOT
from `/opt/cielago/src`. A code-only `git pull` will NOT take effect until the
package is re-synced into the venv. Every deploy must reinstall the package:

`/opt/cielago` is the deploy dir on the web host and is **NOT a git repo** — the
assistant code is local/unpushed, so deploy by **rsync from the dev box**, then
re-sync into the venv (the live venv is `venv`, **not** `.venv`):

```bash
# from the dev box (~/Source/Gaming/LastSietch/discord/cielago-bot):
rsync -ai --no-owner --no-group \
  --exclude='/.env' --exclude='/.venv/' --exclude='/venv/' --exclude='/logs/' \
  --exclude='/data/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.sqlite' --exclude='.pytest_cache/' \
  -e ssh ~/Source/Gaming/LastSietch/discord/cielago-bot/ the web host:/opt/cielago/

# on the web host:
cd /opt/cielago
sudo -u cielago venv/bin/pip install -e .   # editable re-sync (venv, NOT .venv)
```

> **Deploy model (corrected 2026-06-14):** the install is now **editable**
> (`_editable_impl_cielago.pth` -> `/opt/cielago/src`); the old non-editable
> `site-packages/cielago/` COPY was retired (moved aside to
> `cielago.legacycopy-*`). If a real `site-packages/cielago/` dir ever reappears
> it will SHADOW the editable pointer — move it aside, don't trust `pip` to
> remove a manually-copied dir. After an editable install, code changes take
> effect on restart with no copy step.

Confirm the new module is importable from the venv the unit uses (run from
`/opt/cielago` so pydantic finds `.env`):

```bash
cd /opt/cielago && sudo -u cielago venv/bin/python -c "import cielago.cogs.assistant; print('ok')"
```

## 1. Env vars (declare every one — pydantic forbids extras)

Any env var present in `.env` MUST have a matching `Settings` field or the bot
crash-loops on boot (`extra_forbidden`). The assistant fields are already
declared in `config.py`. Add to `/opt/cielago/.env`:

```ini
CIELAGO_ASSISTANT_ENABLED=true
CIELAGO_ASSISTANT_DB_PATH=data/support.sqlite
CIELAGO_ASSISTANT_MOD_CHANNEL_ID=                          # default 🛡️｜mod-ops (baked in)
CIELAGO_ASSISTANT_WATCH_CHANNEL_IDS=                       # blank = help-and-feedback
CIELAGO_ASSISTANT_ACK_CHANNEL_IDS=                         # blank = ack in every watched channel
CIELAGO_ASSISTANT_OWNER_ID=<the owner's user id>             # Escalate-to-owner ping
CIELAGO_ASSISTANT_MOD_ROLE_ID=<mod role id>               # urgent ping (optional)
CIELAGO_ASSISTANT_EMBED_MODEL_DIR=                         # blank = hashing fallback
CIELAGO_ASSISTANT_DUP_THRESHOLD=                           # blank = backend default
```

No env is strictly required: the mod channel defaults to **🛡️｜mod-ops**
(`<discord-id>`) and watching defaults to **💡｜help-and-feedback**
(`<discord-id>`), both baked into `config.py`. Set `OWNER_ID` so
Escalate-to-owner pings you by name (else it pings the admin ids). The Discord
app already has the Message Content intent (used by the chat herald), so no
portal change is needed.

**Watching and acking are separate lists.** `WATCH_CHANNEL_IDS` is where the
assistant reads and files; `ACK_CHANNEL_IDS` is the subset where it may answer the
reporter (reaction + quote-reply). The ack list is intersected with the watch list,
so it can only ever narrow where the bot speaks, never widen where it listens.
Live as of 2026-07-25: five channels watched, acks only in 💡｜help-and-feedback
and ❓｜dune-help. 🏜️｜dune-general, 💬｜general and 🪱｜deep-desert are monitored
silently — tickets still open and still surface in mod-ops, but the bot does not
reply in rooms designed for players talking to each other.

## 2. Resource caps (Phase 0 non-interference contract)

```bash
sudo install -D -m644 ops/cielago.service.d/10-resource-caps.conf \
    /etc/systemd/system/cielago.service.d/10-resource-caps.conf
sudo systemctl daemon-reload
```

## 3. (Optional) stage the bge-small ONNX model

Without a model the assistant uses a deterministic hashing embedder — fully
functional dedup, lighter footprint. To enable real semantic similarity:

```bash
cd /opt/cielago
sudo -u cielago venv/bin/pip install -e '.[embeddings]'   # onnxruntime + tokenizers + numpy
mkdir -p models/bge-small-en-v1.5
# Fetch the ONNX export + tokenizer (BAAI/bge-small-en-v1.5, onnx/model.onnx):
#   tokenizer.json  ->  models/bge-small-en-v1.5/tokenizer.json
#   model.onnx      ->  models/bge-small-en-v1.5/model.onnx
# (huggingface-cli download BAAI/bge-small-en-v1.5 onnx/model.onnx tokenizer.json ...)
```

Then set `CIELAGO_ASSISTANT_EMBED_MODEL_DIR=models/bge-small-en-v1.5`. Do NOT
commit the ~130 MB model. On load the bot logs `assistant.embedder
backend=bge-small-onnx`; on any failure it logs `embedder_onnx_unavailable` and
silently falls back to hashing (the bot never crash-loops over the model).

> Switching backends changes the embedding scale, so previously stored vectors
> are skipped by dedup until re-embedded. At community volume this self-heals as
> new reports arrive; a one-off backfill is a Phase 2 nicety, not required.

## 4. Restart + verify

```bash
sudo systemctl restart cielago
journalctl -u cielago -n 50 --no-pager | grep -E "assistant|store_ready|migrated"
systemctl show cielago -p MemoryMax -p CPUQuotaPerSecUSec -p Nice
```

Expected log lines: `assistant.store_ready`, `assistant.embedder backend=...`,
and `assistant.migrated tickets=N feature_requests=M` (the one-time tracker
import). In Discord, `/assistant status` should report enabled + queue counts.

## 5. Kill switch

`/assistant disable` stops all auto-triage and persists a `support.sqlite.disabled`
marker (survives restarts); `/assistant enable` clears it. Existing tickets are
untouched either way.

## Rollback

Re-point `cielago.cogs.assistant` back to `cielago.cogs.tracker` in `bot.py` and
re-sync, OR just `/assistant disable`. `support.sqlite` and the original
`data/tracker.json` are both retained, so nothing is lost.
