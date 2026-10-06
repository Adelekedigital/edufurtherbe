"""Registering interest in something the product has not shipped yet.

**The vocabulary is open on purpose** (#365). A closed enum would turn a client
typo into a `422`, and would put a backend release in front of every new
coming-soon button — which is the thing this exists to remove. Two buttons had
already been cut for want of somewhere to record a press, so the cost was
chosen deliberately: a typo stores a key nobody notifies against, found by
reporting distinct keys rather than prevented.

**Recording is feature-agnostic; notifying is not.** Sending anything for a new
key needs a template id and a `Notification` member, so a button built on this
may promise "we'll let you know" and never a date. Nothing here sends.
"""

from __future__ import annotations

import re

#: What a feature key may look like: lowercase, starting with a letter, 2 to 40
#: characters. Long enough for `new_mentors`, short enough to stay a key rather
#: than becoming a sentence.
#:
#: **The `CHECK` in the `feature_interest` migration is a second copy**, pinned
#: to this by a test. A migration may not import application code — the chain is
#: frozen, and an edit here would otherwise change what an old migration meant.
FEATURE_PATTERN = r"^[a-z][a-z0-9_]{1,39}$"

_FEATURE = re.compile(FEATURE_PATTERN)

#: How many distinct features one account may register interest in.
#:
#: **A bound on rows, not a product rule.** `UNIQUE (user_id, feature)` means a
#: repeated press adds nothing, so this is the only thing standing between an
#: open vocabulary and an account looping fresh keys. Set far above any honest
#: use: the product has two keys today and would have to grow twenty-five times
#: over before a real person met this.
#:
#: It is why there is no hourly rate limit as well. A limit would be a second
#: mechanism for the one fact this already settles — the most rows an account
#: can ever create — and the intake upload limiter it would have copied is
#: specific to that table's window and `Retry-After` maths.
MAX_FEATURES_PER_ACCOUNT = 50


def is_feature_key(value: str) -> bool:
    """Whether `value` is a well-formed feature key.

    Shape only. **Nothing here knows which features exist**, because that is the
    point: a key this accepts may name something nobody has built, and that is a
    button shipping before its backend rather than an error.
    """
    return _FEATURE.fullmatch(value) is not None
