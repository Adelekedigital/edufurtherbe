"""Every piece of configuration enters the process here.

No module outside this one reads ``os.environ``. Secrets are ``SecretStr`` so an
accidental log line or repr renders ``**********`` instead of the value.

**Only names generic enough to collide carry the ``EDUFURTHER_`` prefix.**
``ENVIRONMENT`` and ``DEBUG`` are words a host, a base image or another tool may
already be using, so those two keep it. ``SUPABASE_URL`` and ``BUBBLE_API_TOKEN``
name their own vendor and cannot plausibly mean anything else, so they do not —
and ``DATABASE_URL`` unprefixed is a small bonus, since most managed platforms
inject exactly that name.

**What this cost.** The old blanket prefix let one validator catch *any*
misspelling: anything starting with ``EDUFURTHER_`` that was not a field was a
typo, provably. That is now only true for the two prefixed keys. A misspelled
``SUPABSE_URL`` is indistinguishable from an unrelated host variable and passes
silently, leaving the default in place — the exact failure mode the old guard
existed to prevent, now reachable for six of the eight settings.

The guard below does the one thing it still can: it catches every *old* key,
because those are known strings. It cannot do more. Said plainly here rather
than left for someone to discover, and a real reason to keep secrets out of
optional fields whose absence is silent.
"""

import json
import os
import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "EDUFURTHER_"

#: Which dotenv file to read. ``ENV_FILE=.env.staging`` selects a whole
#: environment rather than five variables edited by hand — and editing four of
#: five is the mistake the validator at the bottom of this class refuses.
#:
#: Read here, at import, which is the one place in this project permitted to
#: consult the environment directly. That pairs with ``get_settings`` being
#: cached: one process reads one file, and nothing switches mid-run.
ENV_FILE = os.environ.get("ENV_FILE", ".env")

#: A Supabase project ref as it appears in each value that carries one: the
#: pooler puts it in the username (``postgres.<ref>``), the direct connection in
#: the host (``db.<ref>.supabase.co``), and both URLs in the subdomain.
#:
#: Anything else yields ``None`` and takes no part in the comparison — a
#: ``localhost`` DSN names no project, so it cannot contradict one.
_PROJECT_REF_PATTERNS = (
    re.compile(r"://postgres\.([a-z0-9]{16,})[:@]"),
    re.compile(r"@db\.([a-z0-9]{16,})\.supabase\.co"),
    re.compile(r"https://([a-z0-9]{16,})\.supabase\.co"),
)


def supabase_project_ref(value: str) -> str | None:
    """The Supabase project a value names, or ``None`` if it names none."""
    for pattern in _PROJECT_REF_PATTERNS:
        if match := pattern.search(value):
            return match.group(1)
    return None


#: The only fields whose environment key keeps the prefix. Both are single
#: generic words; everything else names its own subject.
PREFIXED_FIELDS = frozenset({"environment", "debug"})

Environment = Literal["local", "ci", "staging", "production"]


def env_key(field: str) -> str:
    """The environment variable a field is read from.

    One function, so the prefix rule has a single representation — the guard and
    the field declarations below both ask it rather than each spelling the rule
    out.
    """
    prefix = ENV_PREFIX if field in PREFIXED_FIELDS else ""
    return f"{prefix}{field}".upper()


class Settings(BaseSettings):
    """Process configuration, read from the environment and an optional ``.env``."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # The prefix stays the *default*, and the fields that do not need it opt
        # out with an explicit alias below.
        #
        # Inverted deliberately. Dropping `env_prefix` entirely and aliasing the
        # two that keep it looked tidier and was wrong: `populate_by_name` then
        # also matches the bare field name, so a host-set `DEBUG=true` was read
        # straight into the setting — defeating the only reason those two are
        # prefixed. Caught by `test_the_two_collision_prone_keys_keep_their_prefix`.
        env_prefix=ENV_PREFIX,
        # So `Settings(environment="ci")` still works for the fields that carry an
        # alias. Safe here: the name-based lookup a settings source then performs
        # is the *prefixed* one, which is a stale key, and the guard rejects it.
        populate_by_name=True,
        extra="ignore",
    )

    environment: Environment = "local"
    debug: bool = False
    # ``NoDecode`` is load-bearing, not decoration. Without it pydantic-settings
    # JSON-decodes a complex type *inside the settings source*, before any
    # validator runs, and a bare origin typed into a cloud console fails as
    # ``SettingsError: error parsing value for field "cors_origins"`` — a message
    # naming neither the value nor the expected shape, from a field that at the
    # time was wired to nothing. That took a deploy to diagnose.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, validation_alias=env_key("cors_origins")
    )

    #: Which template stands for which message, as ``{notification: id}``.
    #:
    #: **``NoDecode`` for the same reason ``cors_origins`` has it**, and the
    #: reason is a deploy that took an afternoon to diagnose: without it
    #: pydantic-settings JSON-decodes the value inside the settings source,
    #: before any validator runs, and a malformed entry fails as
    #: ``SettingsError`` naming neither the value nor the expected shape.
    #:
    #: Both spellings are accepted, for the same reason as there — JSON is what
    #: ``.env.example`` documents, and ``a=1,b=2`` is what a person types into a
    #: cloud console form field that gives no hint a map is wanted. Supporting
    #: one and not the other breaks somebody, and the breakage is a service that
    #: will not start.
    #:
    #: **Typed ``dict[str, str]``, not keyed on the message vocabulary**, and
    #: that is the layer boundary rather than a preference: ``core`` may import
    #: nothing, so it cannot name a ``domain`` enum. The adapter resolves a
    #: `Notification` against this map and raises ``ConfigurationError`` when
    #: there is no entry — which is where that error's own docstring says it
    #: belongs, *"raised where the setting is consumed rather than at startup"*.
    #:
    #: Not a ``SecretStr``: a template id names a message, and hiding it would
    #: only make an operator's misconfiguration harder to read.
    email_templates: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict, validation_alias=env_key("email_templates")
    )

    #: Same shape, separate map. **Not one map with two columns**: a message
    #: exists on one channel long before the other, since email reaches
    #: everybody today and WhatsApp reaches nobody until phone numbers are
    #: collected — and a single map would make the absent half look like a
    #: mistake rather than a phase.
    #:
    #: Every WhatsApp send is business-initiated and therefore outside the
    #: 24-hour customer-service window, so each of these must be a template Meta
    #: has approved. That approval has a lead time nobody here controls, which
    #: is the strongest argument for the ids living in configuration rather than
    #: in code.
    whatsapp_templates: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict, validation_alias=env_key("whatsapp_templates")
    )

    @field_validator("email_templates", "whatsapp_templates", mode="before")
    @classmethod
    def accept_json_or_key_value_pairs(cls, value: object) -> object:
        """Take either spelling, because both have a legitimate author.

        Anything that is not a string — a real mapping from code, as the tests
        pass — is handed straight on to normal field validation. Disabling the
        decoder must not also disable the type check.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            # Let a malformed object raise here: inside a validator it becomes a
            # ``ValidationError`` naming the field, rather than the opaque
            # ``SettingsError`` the annotation above exists to avoid.
            return json.loads(text)
        return dict(
            part.split("=", 1) for part in (p.strip() for p in text.split(",")) if "=" in part
        )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def accept_json_or_a_comma_separated_list(cls, value: object) -> object:
        """Take either spelling, because both have a legitimate author.

        JSON is what ``.env.example`` documents and what any existing
        configuration already uses; comma-separated is what a person types into a
        form field that gives no hint a list is wanted. Supporting one and not the
        other breaks somebody, and the breakage is a service that will not start.

        Anything that is not a string — a real list from code, as the tests and
        ``create_app`` pass — is handed straight on to normal field validation.
        Disabling the decoder must not also disable the type check.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            # Let a malformed array raise here: inside a validator it becomes a
            # ``ValidationError`` naming the field, rather than the opaque
            # ``SettingsError`` this whole annotation exists to avoid.
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]

    # Optional, and deliberately so. ``main.py`` builds the application at import
    # time (``app = create_app()``), so a required field would make
    # ``import app.main`` fail on any machine without a database configured —
    # including every unit test and the pre-commit hook that runs them. The
    # engine factory raises ``ConfigurationError`` at the point of use instead,
    # which is where a missing DSN is actually actionable.
    #
    # ``SecretStr`` because a DSN carries a password: ``repr`` and any log line
    # render it as ``**********``. Read the value with ``.get_secret_value()``,
    # and only inside ``infra/``.
    database_url: SecretStr | None = Field(default=None, validation_alias=env_key("database_url"))

    # The Bubble Data API, used to extract the legacy snapshot. Both optional for
    # the same reason as ``database_url``: the application imports on machines
    # that will never run an extract, and the script raises at the point of use.
    #
    # The URL carries the environment in its path — ``/version-test/`` is the dev
    # app and its absence is production — so it is configuration rather than a
    # constant. Trailing slashes are stripped by the adapter.
    bubble_api_url: str | None = Field(default=None, validation_alias=env_key("bubble_api_url"))
    bubble_api_token: SecretStr | None = Field(
        default=None, validation_alias=env_key("bubble_api_token")
    )

    # Supabase. Optional for the same reason as the others: importing the
    # application must not require credentials that only some code paths need,
    # and the point of use raises `ConfigurationError` with something actionable.
    #
    # Projects differ on signing: newer ones sign asymmetrically and publish a
    # JWKS, older ones use a shared HS256 secret. Set whichever the dashboard
    # shows; the verifier prefers the JWKS when both are present.
    supabase_url: str | None = Field(default=None, validation_alias=env_key("supabase_url"))
    supabase_jwks_url: str | None = Field(
        default=None, validation_alias=env_key("supabase_jwks_url")
    )
    supabase_jwt_secret: SecretStr | None = Field(
        default=None, validation_alias=env_key("supabase_jwt_secret")
    )
    # Bypasses RLS and can create or delete users. Never the anon key, never sent
    # to a browser, and used only by the provisioning CLI.
    supabase_service_role_key: SecretStr | None = Field(
        default=None, validation_alias=env_key("supabase_service_role_key")
    )

    #: Loops' transactional key (ADR 0025). Carries every message this service
    #: sends; the Supabase auth code goes through Emailit's SMTP, configured in
    #: the Supabase console rather than here.
    #:
    #: Unset means `NullNotifier`, so the outbox still fills and drains and
    #: every message is recorded as **skipped** rather than lost — a missing key
    #: is then visible in a table rather than in nobody's inbox.
    loops_api_key: SecretStr | None = Field(default=None, validation_alias=env_key("loops_api_key"))

    #: QStash's publish token, and the keys it signs callbacks with.
    #:
    #: Unset means `NullScheduler`: bookings still work, deadlines still pass,
    #: and the expiry sweep still frees the slot — the mentor is simply never
    #: nudged. Nothing else degrades.
    qstash_token: SecretStr | None = Field(default=None, validation_alias=env_key("qstash_token"))

    #: **Two keys, because QStash rotates them.** Both are tried, so a rotation
    #: does not drop callbacks in the window where the old and the new are each
    #: live. Configuring only the current one is legal, and is what a deployment
    #: that has never rotated looks like.
    qstash_current_signing_key: SecretStr | None = Field(
        default=None, validation_alias=env_key("qstash_current_signing_key")
    )
    qstash_next_signing_key: SecretStr | None = Field(
        default=None, validation_alias=env_key("qstash_next_signing_key")
    )

    #: Where QStash calls back to. **Ours to state rather than derive**: the
    #: service cannot see the URL a proxy presented to the caller, and the
    #: signature names its destination — so a derived value that is wrong
    #: rejects every callback with a message about signatures rather than about
    #: configuration.
    public_base_url: str | None = Field(default=None, validation_alias=env_key("public_base_url"))

    # Where re-hosted profile images live. A plain name, not a secret: it appears
    # in every public image URL. The bucket is created once by an operator in the
    # dashboard, not by the migration script — see `infra/storage/supabase.py`.
    supabase_storage_bucket: str = Field(
        default="profile-images", validation_alias=env_key("supabase_storage_bucket")
    )

    @model_validator(mode="after")
    def reject_stale_and_unknown_prefixed_variables(self) -> Settings:
        """Fail startup on an ``EDUFURTHER_`` variable that no longer means anything.

        Two distinct failures, and the messages are different because the fixes
        are different.

        **A stale key.** ``EDUFURTHER_SUPABASE_URL`` was correct until the prefix
        was dropped. pydantic-settings silently ignores a variable matching no
        field, so a deployed environment still holding one would start *healthy
        with nothing configured* — a service that runs, passes its health check,
        and cannot reach Supabase. This is the failure this guard exists for, and
        the message names the replacement so the operator does not have to read
        the source to find it.

        **An unknown key.** Anything else beginning with ``EDUFURTHER_`` is a
        misspelling of one of the two prefixed fields. Narrower than before, and
        the module docstring says why.
        """
        fields = type(self).model_fields
        stale = {
            f"{ENV_PREFIX}{name}".upper(): env_key(name)
            for name in fields
            if name not in PREFIXED_FIELDS
        }
        live = {env_key(name) for name in fields}

        present = {name.upper() for name in os.environ}

        if outdated := sorted(present & stale.keys()):
            renames = ", ".join(f"{old} -> {stale[old]}" for old in outdated)
            raise ValueError(
                f"These environment variables use the old prefixed names: {renames}. "
                "The prefix now applies only to "
                f"{', '.join(sorted(env_key(f) for f in PREFIXED_FIELDS))}. "
                "Rename them; leaving them set would start this process with the "
                "affected settings unconfigured."
            )

        if unknown := sorted(
            name for name in present if name.startswith(ENV_PREFIX) and name not in live
        ):
            raise ValueError(
                f"Unrecognised {ENV_PREFIX} variable(s): {', '.join(unknown)}. "
                f"Only {', '.join(sorted(env_key(f) for f in PREFIXED_FIELDS))} "
                "carry the prefix; everything else is unprefixed."
            )
        return self

    @model_validator(mode="after")
    def refuse_a_mixed_set_of_supabase_credentials(self) -> Settings:
        """Every Supabase value must name the same project.

        **The failure this prevents has no undo.** These are separate strings
        describing two halves of one environment — which database rows go to, and
        which project's auth accounts get created. Switching environment means
        editing all of them, and editing all but one is not an error anything
        reports: ``provision_auth.py --link-migrated`` reads users out of one
        project's database and creates real accounts in another's, then prints
        ``created 43 … failed 0``, because from the loader's view nothing failed.

        **Deliberately narrow.** A value takes part only if a project ref can be
        read out of it, so anything not Supabase-shaped is ignored rather than
        guessed at. A ``localhost`` DSN beside a real ``SUPABASE_URL`` is the
        ordinary development state here — Docker for the database, the shared
        project for auth — and refusing it would fail this repository's own suite,
        which is the pressure that turns a guard into a formality.

        **Not complete cover, and not to be read as it.** A variable that is
        absent cannot disagree with anything: leave one exported from a previous
        environment and omit it from the next file, and the stale value wins
        silently. Replacing an env file whole is what prevents that; this catches
        the case where both are present and disagree.
        """
        found: dict[str, str] = {}
        for name, raw in (
            ("DATABASE_URL", self.database_url),
            ("SUPABASE_URL", self.supabase_url),
            ("SUPABASE_JWKS_URL", self.supabase_jwks_url),
        ):
            value = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
            if value and (ref := supabase_project_ref(value)):
                found[name] = ref

        if len(set(found.values())) > 1:
            named = ", ".join(f"{name} names {ref}" for name, ref in sorted(found.items()))
            raise ValueError(
                f"These settings name different Supabase projects: {named}. "
                "They are one environment and must be set together — replace the "
                "whole env file rather than a variable at a time."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once."""
    return Settings()
