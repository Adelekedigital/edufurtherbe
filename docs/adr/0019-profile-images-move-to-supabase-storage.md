# 19. Profile images move to Supabase Storage, keyed on their own content

Date: 2026-08-06

## Status

Accepted

## Context

The migration package lists file migration off Bubble as **cross-cutting, start
day one** — "network-bound, slow, and schema-independent". It is the one cutover
task whose duration is not ours to set, and nothing had been built for it.

`user_profiles.avatar_url` and `banner_url` already exist and already hold Bubble
URLs. The profile model carries a warning worth repeating, because it is what
makes this harder than it looks:

> Both MUST be re-hosted off Bubble before cutover. Note the check for that
> cannot be `LIKE '%bubble%'`: the extract shows avatars on three hosts, and
> eleven of them sit behind the custom domain app.edufurther.org, which resolves
> to Bubble and would pass such a check.

Four things were measured against the real 43-user export before any of this was
designed. Three of them contradicted an assumption that would otherwise have
shipped.

| | |
|---|---|
| **Hosts** | 20 users have an image: **11** on `app.edufurther.org`, **5** on `cdn.bubble.io`, **4** on `lh3.googleusercontent.com` |
| **`HEAD` is refused** | `app.edufurther.org` answers `HEAD` with **403** and the identical `GET` with **200** |
| **Extensions lie** | a `.gif` and an `.avif` both return JFIF (JPEG) bytes |
| **Size** | 5.7 MiB across 21 assets, averaging 270 KiB → roughly **154 MiB** for 1,200 users |

That last number is the one that changes the plan: this is minutes, not hours.
"Start day one" was written without a measurement, and the measurement says the
urgency is lower than the package assumed. It still runs early, because it is
free to re-run and there is no reason to save it for the freeze.

## Decision

**1. Re-hosted images live in a public Supabase Storage bucket**, `profile-images`
by default and configurable.

Public rather than signed. These images are already served with no authentication
from Bubble's CDN, they appear on public mentor profiles, and signed URLs expire —
which breaks caching, breaks any static render, and means every profile view mints
a URL. Unguessability comes from the object name instead (point 3).

**2. The bucket is created by an operator, once, and never by the migration.** The
adapter checks it exists and refuses to start otherwise.

A script that can create a *public* bucket is a script that can publish a private
one by typo, and the check runs once per run rather than per object — 1,200
objects failing individually on a missing bucket is 1,200 identical lines and no
headline.

**3. The object path is `users/{our_user_id}/{kind}-{content_hash}.{ext}`.**

Our uuid, never Bubble's: the identifier rule this project keeps is to translate
at the boundary and never let a vendor's identifier become something other
systems depend on. The hash does three jobs at once — a re-run is idempotent
without asking storage what it holds, the object is unguessable despite the
bucket being public, and a changed image gets a new URL instead of fighting a CDN
cache.

**4. The extension and `Content-Type` come from the bytes, never from the URL.**
Measured, not hypothetical. Publishing a JPEG as `image/gif` is the kind of defect
a CDN then caches, and bytes that match no known signature are served as
`application/octet-stream` — mislabelling an unknown blob as `image/*` is how a
non-image ends up rendered on a profile page.

**5. "Is this already ours?" is an allowlist, and that is the whole point.** The
question asked is whether a URL's host is the storage host we are configured for.
Anything else needs re-hosting, whatever it is called.

The obvious alternative — "does this mention bubble" — reports **eleven of the
sixteen** Bubble-hosted dev assets as already migrated, because they are served
from a custom domain. An allowlist has no such gap, and the storage host is
*derived* from configuration rather than written down a second time.

**6. The column keeps a full URL, not a storage path.** `avatar_url` stays honest
about its own name. Storing a path would mean renaming two columns — expand and
contract across two releases — to avoid a future one-statement backfill if the
CDN ever moves. Moving CDN later is a scripted `UPDATE`, not a design flaw.

**7. Google profile pictures are copied too.** They are not Bubble's, so they are
not strictly in scope. They are copied anyway: a `lh3.googleusercontent.com` URL
is tied to the OAuth grant and can rotate or 403 when someone changes their Google
avatar or revokes access — and after migration they may never re-authenticate with
Google. One host afterwards means one code path.

**8. An asset the source will not serve is reported, not fatal.** One dev asset
answers 401 with a zero-length body. That user keeps a null image; it is not a
reason to stop the other 1,199.

### Rejected alternatives

**Signed URLs.** Genuinely stronger access control, and the right answer if any
profile image were private. Rejected because none is: they are public today, on a
public CDN, and expiry would trade a caching story and a static-render story for
protection this data does not need. Reversible only at the cost of re-issuing
every published URL, which is why it is named here rather than left implicit.

**Leaving Google-hosted pictures where they are.** No work at all, and the URLs
resolve today. Rejected on point 7's reasoning — the URLs are the *provider's*, not
the user's, and we would be depending on an OAuth grant we no longer exercise.

**Creating the bucket from the script.** One less setup step, and tempting for a
one-shot migration. Rejected on point 2: the failure mode is silent and public.

**Storing the storage path and composing the URL in the API layer.** Cleaner in
principle, and it would make a CDN move free. Rejected because it requires renaming
two columns *now*, across two releases, to save a scripted `UPDATE` later.

## Consequences

**The cutover step is short and repeatable.** ~154 MiB and a few hundred requests,
bounded to four concurrent transfers so that a slow run is not also a rude one. It
can be run today, again next week, and again during the freeze; each run does only
what the last did not.

**The database stops referencing Bubble for images.** Asserted by a query in the
test suite — and deliberately not with `LIKE '%bubble%'`, which would pass while
eleven assets still pointed at the custom domain.

**`user_profiles.updated_at` survives.** Moving a file is not a modification of
the user's data, so the write holds `trg_set_updated_at` off, the same discipline
the ETL and provisioning already use.

**Nothing else changes.** No schema migration, no API contract change. `GET
/api/v1/me` returns whatever the column holds, which is now a URL we serve.

### Confirmation

- **Mechanical:** `test_the_custom_domain_is_not_mistaken_for_ours` and
  `test_every_foreign_host_in_the_export_is_copied` cover all three real hosts.
  Replacing the allowlist with a Bubble-denylist turns ten tests red.
- **Mechanical:** `test_the_source_is_never_probed_with_head` fails if anything
  reintroduces `HEAD`, which the real host answers 403.
- **Mechanical:** `test_the_stored_content_type_comes_from_the_bytes` uses a file
  named `.gif` holding JPEG bytes — the case the export actually contains.
- **Mechanical:** `test_no_bubble_url_survives` asserts the acceptance criterion
  by query.
- **Mechanical:** `test_a_second_run_costs_nothing` asserts a re-run fetches
  nothing and uploads nothing.
- **Mechanical:** `test_profile_timestamps_are_not_rewritten` asserts a known past
  timestamp survives.
- **Not mechanical:** nothing enforces that the bucket is actually *public*. The
  adapter checks it exists; a private bucket would pass that check and produce
  URLs that 400 for everyone. Worth a one-line check if this is ever re-run
  against a fresh project.
- **Not mechanical:** nothing verifies the uploaded object is byte-identical to
  the source afterwards. The content hash makes it self-consistent, not verified.

### Open questions

- **Whether the source images should be deleted from Bubble afterwards.** Not
  while Bubble is still the live system. Belongs to decommissioning, not here.
- ~~**Whether a rendition pipeline is wanted** — thumbnails, WebP conversion,
  stripping EXIF.~~ **Closed by the upload endpoint.** See below.
- ~~**What happens to an image a user replaces after this runs.**~~ **Closed by
  the upload endpoint.** See below.

### Closed by the upload endpoint

Both open questions above were answered together, because one construction
answers both: **every image entering storage is decoded and re-encoded**, from
the upload endpoint and from `scripts/migrate_assets.py` alike.

- **EXIF, including GPS, is gone — by construction rather than by measurement.**
  Re-encoding writes only what it is given, and nothing gives it the metadata.
  There is no strip step to forget and no population of stripped and unstripped
  images to reason about later. The original plan here was to *measure* how many
  migrated images carried coordinates; that measurement was dropped as useless —
  nothing had been migrated yet, so it would have scanned an empty set, and the
  answer would not have changed what was built.
- **Renditions: one, not a pipeline.** A single stored size per kind — 512px for
  an avatar, 1500px for a banner, longest edge, never enlarged. Larger images are
  resized rather than refused, which is what every comparable product does;
  refusing a photo for having too many pixels is a limit no user should meet.
- **A replaced image is deleted**, after the profile points at the new one and
  never fatally. Object paths are keyed on the user *and* the content, so no two
  profiles can share an object and deleting one cannot affect another. Uploading
  the same image twice lands on the same path and deletes nothing.

**Why the API and not a presigned URL.** A presigned PUT puts the client in
direct contact with the bucket, which means nothing can strip metadata, nothing
can resize, and the object name cannot be derived from the content — the three
things this endpoint exists to do. The cost is that the bytes pass through the
API; at 5 MB with the work on a worker thread, that is the right trade.
