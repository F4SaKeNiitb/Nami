"""Tables for tenancy, rules, conversations (short-term memory) and long-term memory.

Vault, sessions and task tables from ARCHITECTURE.md §5 are added in their own milestone.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, new_id, utcnow


def _id() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(default=utcnow)


def _updated() -> Mapped[datetime]:
    return mapped_column(default=utcnow, onupdate=utcnow)


# ───────────────────────────── tenancy ─────────────────────────────


class Org(Base):
    """A firm. Every user also gets a personal org, so rules and memory always have an owner."""

    __tablename__ = "orgs"
    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bumped on every firm-rule change; part of every rules/LLM cache key.
    rules_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    google_sub: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    default_org_id: Mapped[str | None] = mapped_column(ForeignKey("orgs.id", ondelete="SET NULL"))
    rules_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id"),
        CheckConstraint("role IN ('owner','admin','member','viewer')", name="role"),
    )
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = _created()


# ───────────────────────────── rules ─────────────────────────────


class Rule(Base):
    """A firm-wide rule (user_id NULL) or a rule for one user inside a firm.

    kind:
      policy      machine-enforced (spend_limit, domain_deny, require_approval, ...)
      preference  key/value defaults the agent should use (diet=veg, address=Home)
      instruction free-text guidance placed in the prompt (not machine-enforced)
    enforcement:
      hard  firm rule users cannot loosen
      soft  default that a user's own rule may override
    site_etld1: optional; rule applies only on that site (e.g. swiggy.com)
    """

    __tablename__ = "rules"
    __table_args__ = (
        CheckConstraint("kind IN ('policy','preference','instruction')", name="kind"),
        CheckConstraint("enforcement IN ('hard','soft')", name="enforcement"),
        CheckConstraint("user_id IS NULL OR enforcement = 'soft'", name="user_rules_soft"),
        Index("ix_rules_lookup", "org_id", "user_id", "enabled"),
    )
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    site_etld1: Mapped[str | None] = mapped_column(String(253))
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[Any] = mapped_column(JSON)
    enforcement: Mapped[str] = mapped_column(String(8), default="soft")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# ───────────────────── conversations (short-term memory) ─────────────────────


class Channel(Base):
    """A surface conversations happen on: the web app, a Slack channel, a WhatsApp number..."""

    __tablename__ = "channels"
    __table_args__ = (
        UniqueConstraint("org_id", "kind", "external_id"),
        CheckConstraint("kind IN ('web','slack','whatsapp','email','api')", name="kind"),
    )
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class ChannelMember(Base):
    """Who belongs to a channel. A thread with visibility "channel" can be read by its channel's members (and its author)."""

    __tablename__ = "channel_members"
    __table_args__ = (UniqueConstraint("channel_id", "user_id"),)
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = _created()


class Thread(Base):
    """One chat. Its id is also the Chainlit thread id and the LangGraph thread id."""

    __tablename__ = "threads"
    id: Mapped[str] = _id()
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    # Rolling summary of everything up to and including summary_upto_seq.
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_upto_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    # private: author only (admins cannot read it) | channel | org
    visibility: Mapped[str] = mapped_column(String(10), default="private", server_default="private")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    messages: Mapped[list["Message"]] = relationship(back_populates="thread", passive_deletes=True)


class Message(Base):
    """Model-facing transcript. Content is redacted of secrets before it is written."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq"),
        CheckConstraint("role IN ('user','assistant','system','tool')", name="role"),
    )
    id: Mapped[str] = _id()
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    author_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()

    thread: Mapped[Thread] = relationship(back_populates="messages")


# ───────────────────────────── long-term memory ─────────────────────────────


class MemoryItem(Base):
    """A durable fact, preference, episode or procedure.

    scope decides who can recall it, always inside one org:
      user     only that user
      org      everyone in the firm
      channel  anyone talking in that channel
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint("scope IN ('user','org','channel')", name="scope"),
        CheckConstraint("kind IN ('fact','preference','episode','procedure')", name="kind"),
        CheckConstraint("scope != 'user' OR user_id IS NOT NULL", name="user_scope_owner"),
        CheckConstraint("scope != 'channel' OR channel_id IS NOT NULL", name="channel_scope_owner"),
        Index("ix_memory_visible", "org_id", "scope"),
    )
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)  # float32, L2-normalised
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding_dim: Mapped[int] = mapped_column(Integer)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    source_thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# ───────────────────────────── vault ─────────────────────────────


class UserKey(Base):
    """One data-encryption key per user, stored only in wrapped form.

    Destroying the key (wrapped_dek emptied, destroyed_at set) makes every secret of the user
    permanently undecryptable: crypto-shredding.
    """

    __tablename__ = "user_keys"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    kek_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()
    destroyed_at: Mapped[datetime | None] = mapped_column()


class Credential(Base):
    """A secret the user gave Nami. Only `meta` and `label` are readable without the user's key.

    `meta` holds display facts (last4, username, algorithm). The vault service builds it,
    callers cannot pass free text into it.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint("kind IN ('api_key','password','oauth','card','totp')", name="kind"),
        Index("ix_credentials_user_active", "user_id", "revoked_at"),
    )
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(120))
    origin_allowlist: Mapped[list[Any]] = mapped_column(JSON, default=list)  # registrable domains
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    dek_version: Mapped[int] = mapped_column(Integer, default=1)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    linked_credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()


class SecretAccessLog(Base):
    """Append-only, user-visible record of every attempt to use a secret, allowed or not.

    Never contains secret values. credential_id has no foreign key so the history outlives revocation.
    """

    __tablename__ = "secret_access_log"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    credential_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200))       # agent id / "user" / "system"
    origin: Mapped[str | None] = mapped_column(String(300))
    action: Mapped[str] = mapped_column(String(16))       # create|inject|totp|request|revoke|shred
    result: Mapped[str] = mapped_column(String(16))       # allowed|denied (request rows: ok|error)
    reason: Mapped[str | None] = mapped_column(String(200))
    ts: Mapped[datetime] = _created()


# ───────────────────── sessions, tasks, events ─────────────────────


class BrowserSnapshot(Base):
    """Encrypted per-(user, site) cookies + localStorage. A session cookie is a password: encrypted
    with the user's DEK, AAD-bound to user and site."""

    __tablename__ = "browser_snapshots"
    __table_args__ = (UniqueConstraint("user_id", "site", name="user_site"),)
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    site: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    captured_at: Mapped[datetime] = _created()
    last_verified_at: Mapped[datetime | None] = mapped_column()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('planning','awaiting_access','running','awaiting_user','awaiting_approval',"
                        "'blocked','verifying','done','unverified','failed','cancelled')", name="status"),
    )
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    request_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="planning")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class TaskEvent(Base):
    """Append-only. Everything a model saw or a user was shown is recorded here (never secrets)."""

    __tablename__ = "task_events"
    id: Mapped[str] = _id()
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = _created()


class AuthzLog(Base):
    """Append-only record of every authorisation decision, allow or deny."""

    __tablename__ = "authz_log"
    id: Mapped[str] = _id()
    org_id: Mapped[str | None] = mapped_column(String(32), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(40))
    resource: Mapped[str] = mapped_column(String(120))
    allowed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(200))
    ts: Mapped[datetime] = _created()


# ───────────────────────────── skills (learned workflows) ─────────────────────────────

class Skill(Base):
    """A learned workflow or snippet. Derived from a trajectory that read web pages, so it stays untrusted until
    scanned, hash-pinned, signed and (for code or org scope) approved. Every rewrite is a new version."""

    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("status IN ('candidate','approved','revoked','rejected')", name="status"),
        CheckConstraint("scope IN ('user','org')", name="scope"),
        CheckConstraint("kind IN ('workflow','snippet')", name="kind"),
    )
    id: Mapped[str] = _id()
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(8), default="user")
    kind: Mapped[str] = mapped_column(String(10), default="workflow")
    site: Mapped[str] = mapped_column(String(100), index=True)
    intent: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="candidate", index=True)
    approved_by: Mapped[str | None] = mapped_column(String(32))
    approved_at: Mapped[datetime | None] = mapped_column()
    scan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    signature: Mapped[bytes | None] = mapped_column(LargeBinary)
    success_count: Mapped[int] = mapped_column(default=0)
    last_used_at: Mapped[datetime | None] = mapped_column()
    last_verified_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = _created()


class SkillEvent(Base):
    """Append-only transparency log for skills: created, scanned, approved, loaded, blocked, revoked, drifted."""

    __tablename__ = "skill_events"
    id: Mapped[str] = _id()
    skill_id: Mapped[str] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = _created()


class SlackLink(Base):
    """A Slack account (workspace + user) tied to a Nami user, by that user's own confirmation on the web app."""

    __tablename__ = "slack_links"
    __table_args__ = (UniqueConstraint("team_id", "slack_user_id"),)
    id: Mapped[str] = _id()
    team_id: Mapped[str] = mapped_column(String(32))
    slack_user_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = _created()
