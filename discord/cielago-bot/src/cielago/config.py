from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_bot_token: str = Field(alias="DISCORD_BOT_TOKEN")
    last_sietch_guild_id: int = Field(alias="LAST_SIETCH_GUILD_ID")
    cielago_admin_ids: str = Field(alias="CIELAGO_ADMIN_IDS", default="")
    cielago_audit_channel_id: int | None = Field(alias="CIELAGO_AUDIT_CHANNEL_ID", default=None)
    cielago_audit_log_path: str = Field(
        alias="CIELAGO_AUDIT_LOG_PATH", default="logs/cielago-audit.jsonl"
    )
    cielago_giveaway_data_path: str = Field(
        alias="CIELAGO_GIVEAWAY_DATA_PATH", default="data/giveaways.json"
    )
    cielago_jtc_trigger_channel_id: int | None = Field(
        alias="CIELAGO_JTC_TRIGGER_CHANNEL_ID", default=None
    )
    cielago_log_level: str = Field(alias="CIELAGO_LOG_LEVEL", default="INFO")

    # Dune server-status digest (daily/weekly) — posts to the merged #server-info
    # (the former #service-alerts channel id, merged 2026-08-27).
    cielago_dune_status_channel_id: int | None = Field(
        alias="CIELAGO_DUNE_STATUS_CHANNEL_ID", default=None
    )
    # lastsietch-relay (<web-host> localhost) supplies the stats blob from lastsietch-dune.
    cielago_relay_base_url: str = Field(
        alias="CIELAGO_RELAY_BASE_URL", default="http://127.0.0.1:8077"
    )
    cielago_relay_api_key: str = Field(alias="CIELAGO_RELAY_API_KEY", default="")

    # New editions use the portal archive and a separate scoped publisher key.
    cielago_reports_enabled: bool = Field(alias="CIELAGO_REPORTS_ENABLED", default=False)
    cielago_report_publish_key: str = Field(alias="CIELAGO_REPORT_PUBLISH_KEY", default="")
    cielago_reports_public_origin: str = Field(
        alias="CIELAGO_REPORTS_PUBLIC_ORIGIN", default="https://portal.lastsietch.com"
    )

    # Market price-alert DMs. The admin backend (same <web-host> box) exposes
    # /_internal/market-alerts/{pending,ack}; this cog polls it and DMs players.
    # Disabled (no DMs) when the base URL or poll key is unset.
    cielago_admin_base_url: str = Field(
        alias="CIELAGO_ADMIN_BASE_URL", default="http://127.0.0.1:8078"
    )
    cielago_alert_poll_key: str = Field(alias="CIELAGO_ALERT_POLL_KEY", default="")
    cielago_market_alert_interval: int = Field(
        alias="CIELAGO_MARKET_ALERT_INTERVAL", default=120
    )
    # Portal events -> Discord scheduled events. Same base URL and key as the
    # alerts; off by default (the backend has its own flag too).
    cielago_events_sync_enabled: bool = Field(alias="CIELAGO_EVENTS_SYNC_ENABLED", default=False)
    cielago_events_sync_interval: int = Field(alias="CIELAGO_EVENTS_SYNC_INTERVAL", default=60)

    # Bug/feature tracker (RETIRED — superseded by the assistant). The feedback
    # channel id is still used: it's the default channel the assistant watches,
    # and the tracker JSON is migrated into support.sqlite on first assistant load.
    cielago_feedback_channel_id: int | None = Field(
        alias="CIELAGO_FEEDBACK_CHANNEL_ID", default=0
    )
    cielago_tracker_data_path: str = Field(
        alias="CIELAGO_TRACKER_DATA_PATH", default="data/tracker.json"
    )

    # Cielago Assistant (Phase 0/1): mod-facing support triage with local-embedding
    # dedup, a ticket lifecycle, and feature-request upvotes. No external LLM.
    cielago_assistant_enabled: bool = Field(alias="CIELAGO_ASSISTANT_ENABLED", default=True)
    # Emoji reactions the bot places on PLAYER messages (👁 lead-noticed, 📝 filed,
    # 🔗 merged). Disabled 2026-08-26 at the owner's request: the watcher's lead
    # detection is broad, so 👁 landed on most posts and read as the bot reacting
    # to everything. Off by default so it stays off if .env is ever rebuilt; a v2
    # of the assistant is planned and will revisit the player-facing surface.
    # Does NOT affect the 👍 vote reaction, which goes on the bot's OWN embed in
    # the mod channel, not on anyone's post.
    cielago_assistant_react_to_players: bool = Field(
        alias="CIELAGO_ASSISTANT_REACT_TO_PLAYERS", default=False
    )
    cielago_assistant_db_path: str = Field(
        alias="CIELAGO_ASSISTANT_DB_PATH", default="data/support.sqlite"
    )
    # Where ticket / feature-request embeds are posted — the 🛡️｜mod-ops channel
    # in the ADMIN category. Unset = surfacing disabled.
    cielago_assistant_mod_channel_id: int | None = Field(
        alias="CIELAGO_ASSISTANT_MOD_CHANNEL_ID", default=0
    )
    # CSV of channel ids to watch. Blank falls back to the feedback channel.
    cielago_assistant_watch_channel_ids: str = Field(
        alias="CIELAGO_ASSISTANT_WATCH_CHANNEL_IDS", default=""
    )
    # CSV subset of the watch list where the reporter ack (reaction + quote-reply)
    # is allowed. Filing is silent everywhere else: general-chat channels are still
    # monitored and still open tickets, they just don't get bot replies threaded
    # through the conversation. Blank falls back to the whole watch list.
    cielago_assistant_ack_channel_ids: str = Field(
        alias="CIELAGO_ASSISTANT_ACK_CHANNEL_IDS", default=""
    )
    # WATCHER MODE. False (default) = the passive channel watcher records a LEAD
    # and reacts, but does NOT open a ticket and does NOT reply in channel. A mod
    # promotes the ones worth acting on. True = the old behaviour, where every
    # detection became a ticket in mod-ops.
    #
    # Why the default flipped (2026-08-04): the watcher is a keyword classifier,
    # so its tickets carry no in-game name, no surface, no server and a guessed
    # severity, and its quote-reply interrupted conversations in channels meant
    # for players talking to each other. Structured reports now arrive via
    # /report. Detection is still valuable, it just should not mint tickets.
    cielago_assistant_watcher_files_tickets: bool = Field(
        alias="CIELAGO_ASSISTANT_WATCHER_FILES_TICKETS", default=False
    )
    # Pinged on Escalate-to-owner (the owner's user id); falls back to admin ids.
    cielago_assistant_owner_id: int | None = Field(
        alias="CIELAGO_ASSISTANT_OWNER_ID", default=None
    )
    # Role pinged on urgent tickets; falls back to admin ids.
    cielago_assistant_mod_role_id: int | None = Field(
        alias="CIELAGO_ASSISTANT_MOD_ROLE_ID", default=None
    )
    # bge-small ONNX dir (tokenizer.json + model.onnx). Absent = hashing fallback.
    cielago_assistant_embed_model_dir: str | None = Field(
        alias="CIELAGO_ASSISTANT_EMBED_MODEL_DIR", default=None
    )
    # Cosine dup cutoff override; unset uses the embedder backend's default.
    cielago_assistant_dup_threshold: float | None = Field(
        alias="CIELAGO_ASSISTANT_DUP_THRESHOLD", default=None
    )
    # How far back the nightly sweep is allowed to reach, in hours. The sweep
    # re-reads channel history to catch anything on_message missed, but with no
    # age bound it re-litigates weeks of conversation every night: on 2026-07-26
    # it fired across messages 4 to 26 days old. Two nights of cover is plenty
    # to catch a downtime or a classifier fix without dredging history nobody
    # is thinking about any more. 0 or less disables the bound.
    cielago_assistant_sweep_max_age_hours: int = Field(
        alias="CIELAGO_ASSISTANT_SWEEP_MAX_AGE_HOURS", default=48
    )

    # Maintenance/status posts target (#service-alerts). The env var has been
    # in the live .env since the service-alerts rollout but was never declared
    # here; pydantic-settings forbids extras, so it crash-looped the bot.
    cielago_service_alerts_channel_id: int | None = Field(
        alias="CIELAGO_SERVICE_ALERTS_CHANNEL_ID", default=0
    )

    # Broad server-news channel (#announcements). Maintenance windows and major
    # updates cross-post here alongside #service-alerts.
    cielago_announcements_channel_id: int | None = Field(
        alias="CIELAGO_ANNOUNCEMENTS_CHANNEL_ID", default=0
    )

    # Staff-only infra/monitoring channel (⚙️｜server-logs). Cielago posts the
    # Dune server-build watch (Steam buildid changes) here, and the <game-host>
    # pod-watcher reposts pod restart/disappearance alerts here as Cielago. This
    # replaces the Dune half of the old personal-Last Sietch "server monitor" bot; Conan
    # + Enshrouded build watches stay on the Last Sietch box.
    cielago_server_logs_channel_id: int | None = Field(
        alias="CIELAGO_SERVER_LOGS_CHANNEL_ID", default=0
    )
    # Dune Steam build watcher (self-host server appid 4754530 + live client
    # 1172710). Disabled (loop never starts) when the server-logs channel is unset.
    cielago_buildwatch_enabled: bool = Field(
        alias="CIELAGO_BUILDWATCH_ENABLED", default=True
    )
    cielago_buildwatch_interval: int = Field(
        alias="CIELAGO_BUILDWATCH_INTERVAL", default=900
    )
    cielago_buildwatch_state_path: str = Field(
        alias="CIELAGO_BUILDWATCH_STATE_PATH", default="data/dune-buildwatch.json"
    )

    # Social publisher approvals (ops/social-publisher). Off by default. The
    # approver is cielago_assistant_owner_id (the owner); with that unset the cog
    # never starts. The approvals dir must be writable by the cielago user ONLY.
    cielago_social_approvals_enabled: bool = Field(
        alias="CIELAGO_SOCIAL_APPROVALS_ENABLED", default=False
    )
    cielago_social_outbox_dir: str = Field(
        alias="CIELAGO_SOCIAL_OUTBOX_DIR", default="/var/lib/social-pub/outbox"
    )
    cielago_social_approvals_dir: str = Field(
        alias="CIELAGO_SOCIAL_APPROVALS_DIR", default="/var/lib/social-pub/approvals"
    )
    cielago_social_sent_path: str = Field(
        alias="CIELAGO_SOCIAL_SENT_PATH", default="data/social-approvals-sent.json"
    )

    # Event contests (cogs/contests.py, definitions in data/contests.json). Off by
    # default. The channel is 🏆｜server-events; staff reports go to the assistant
    # mod channel. The award key is X-Sietch-Award-Key, the same value as
    # LASTSIETCH_SIETCH_AWARD_KEY on lastsietch-admin; blank = no payouts and no link hints.
    cielago_contests_enabled: bool = Field(alias="CIELAGO_CONTESTS_ENABLED", default=False)
    cielago_contest_channel_id: int | None = Field(
        alias="CIELAGO_CONTEST_CHANNEL_ID", default=None
    )
    cielago_contest_data_path: str = Field(
        alias="CIELAGO_CONTEST_DATA_PATH", default="data/contests-state.json"
    )
    cielago_contest_award_key: str = Field(alias="CIELAGO_CONTEST_AWARD_KEY", default="")

    # Join welcome card (cogs/onboarding.py). Blank channel = the channel whose name
    # ends in "intro", which is where the old one-line greeting always posted.
    cielago_welcome_channel_id: int | None = Field(
        alias="CIELAGO_WELCOME_CHANNEL_ID", default=None
    )
    # Short public "welcome to <sietch>" line on a member's FIRST sietch pick. No
    # pings. Blank channel = the channel whose name ends in "dune-general".
    cielago_sietch_public_welcome: bool = Field(
        alias="CIELAGO_SIETCH_PUBLIC_WELCOME", default=True
    )
    cielago_sietch_welcome_channel_id: int | None = Field(
        alias="CIELAGO_SIETCH_WELCOME_CHANNEL_ID", default=None
    )
    cielago_sietch_welcomed_path: str = Field(
        alias="CIELAGO_SIETCH_WELCOMED_PATH", default="data/sietch-welcomed.json"
    )

    @field_validator(
        "cielago_audit_channel_id",
        "cielago_welcome_channel_id",
        "cielago_sietch_welcome_channel_id",
        "cielago_contest_channel_id",
        "cielago_jtc_trigger_channel_id",
        "cielago_dune_status_channel_id",
        "cielago_feedback_channel_id",
        "cielago_service_alerts_channel_id",
        "cielago_announcements_channel_id",
        "cielago_server_logs_channel_id",
        "cielago_assistant_mod_channel_id",
        "cielago_assistant_owner_id",
        "cielago_assistant_mod_role_id",
        "cielago_assistant_dup_threshold",
        "cielago_assistant_embed_model_dir",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v):
        # An empty env var (e.g. CIELAGO_AUDIT_CHANNEL_ID=) arrives as "" and would fail
        # int parsing; treat blank/whitespace as unset.
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return v

    @property
    def admin_ids(self) -> set[int]:
        if not self.cielago_admin_ids:
            return set()
        return {int(x) for x in self.cielago_admin_ids.split(",") if x.strip()}

    @property
    def assistant_watch_channel_ids(self) -> set[int]:
        """Channels the assistant watches. Blank env falls back to the feedback channel."""
        raw = self.cielago_assistant_watch_channel_ids
        ids = {int(x) for x in raw.split(",") if x.strip()} if raw else set()
        if not ids and self.cielago_feedback_channel_id:
            ids = {self.cielago_feedback_channel_id}
        return ids

    @property
    def assistant_ack_channel_ids(self) -> set[int]:
        """Channels where the reporter ack may be posted.

        Intersected with the watch list, so an id here can never widen where the
        assistant listens -- it only narrows where it speaks. Blank falls back to
        the whole watch list (the pre-gate behaviour).
        """
        raw = self.cielago_assistant_ack_channel_ids
        watched = self.assistant_watch_channel_ids
        if not raw:
            return watched
        return {int(x) for x in raw.split(",") if x.strip()} & watched


settings = Settings()
