---
name: release-notes
description: Generate release notes, changelogs, and version bumps from Conventional Commits using SemVer and Keep a Changelog. Use when cutting a release, tagging a version, writing or updating CHANGELOG.md, preparing release notes for a PR or deploy, deciding whether a change is major/minor/patch, or when asked what changed between two versions.
---

# Release Notes

Release notes are **generated from commit history**, never hand-written. If the
notes are wrong, the commit messages were wrong — fix the process, not the file.

## The chain

```
Conventional Commits  →  SemVer bump  →  CHANGELOG.md  →  git tag  →  GitHub Release
```

Each step is mechanical. A human decides only two things: whether a change is
breaking, and how to phrase the migration note.

## Conventional Commits

```
<type>(<optional scope>)<optional !>: <description>

<optional body>

<optional footers>
```

| Type | Meaning | SemVer effect |
|---|---|---|
| `feat` | New user-facing capability | **MINOR** |
| `fix` | Bug fix | **PATCH** |
| `perf` | Performance improvement | **PATCH** |
| `refactor` | Restructuring, no behaviour change | none |
| `docs` | Documentation only | none |
| `test` | Tests only | none |
| `build` | Build system, dependencies | none |
| `ci` | Pipeline config | none |
| `chore` | Everything else | none |
| any + `!` | Breaking change | **MAJOR** |

Rules that matter in practice:

- **Description**: imperative mood, lower case, no trailing period.
  `fix: reject cancellation of shipped orders` — not `Fixed a bug where...`
- **Scope** is the affected area, ideally a module: `feat(orders):`, `fix(auth):`.
- **One logical change per commit.** If the description needs "and", split it.
- The description is read by users in a changelog. Write it for them, not for
  your future self reading `git log`. "fix: null check" tells a user nothing.

### Breaking changes

Both markers, always — the `!` for scanning, the footer for the migration path:

```
feat(api)!: return ISO-8601 timestamps in order responses

Timestamps were Unix epoch integers, which forced every client to
know the unit and the timezone.

BREAKING CHANGE: `created_at` and `updated_at` in all /v1/orders
responses change from integer epoch seconds to ISO-8601 UTC strings.
Clients parsing these as integers must update.

Migration: pass `?timestamp_format=epoch` for one release cycle to
retain the previous shape. The parameter is removed in v3.0.0.
```

A `BREAKING CHANGE:` footer without a migration path is an incomplete commit.

## SemVer

`MAJOR.MINOR.PATCH`

- **MAJOR** — a consumer must change code. Removed/renamed fields, changed types,
  stricter validation, changed defaults, removed endpoints, changed error codes.
- **MINOR** — new capability, backward compatible. New optional field, new
  endpoint, new optional parameter.
- **PATCH** — bug fix or performance work, no contract change.

Things people get wrong:

- **Adding a required request field is MAJOR**, not minor. Existing callers break.
- **Tightening validation is MAJOR.** Requests that used to succeed now 422.
- **Changing a default value is MAJOR** if behaviour changes for callers who did
  not pass it.
- **Adding a response field is MINOR** — but only if your clients tolerate unknown
  fields. Document that expectation, or it is MAJOR in practice.
- Pre-1.0 (`0.x`), the leading non-zero segment absorbs breakage; still tag `!`
  so the record is accurate when you reach 1.0.

## CHANGELOG.md — Keep a Changelog

```markdown
# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] - 2026-07-30

### Added
- Bulk order cancellation endpoint `POST /v1/orders/bulk-cancel` (#412)

### Fixed
- Cancelling a shipped order returned 500 instead of 409 (#418)
- Correlation ID was dropped on outbound payment-provider calls (#421)

### Security
- Order lookup now scopes by customer, preventing cross-tenant reads (#425)

## [2.0.0] - 2026-06-14

### Changed
- **BREAKING** `created_at` / `updated_at` are ISO-8601 strings, previously
  epoch integers. Pass `?timestamp_format=epoch` to retain the old shape
  through v2.x. (#390)

[Unreleased]: https://github.com/org/repo/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/org/repo/compare/v2.0.0...v2.1.0
```

Section order is fixed: **Added, Changed, Deprecated, Removed, Fixed, Security.**
Omit empty sections. Always keep an `[Unreleased]` section at the top.

## Writing for the reader

The changelog audience is someone deciding whether to upgrade and what will break.

| ❌ Commit-speak | ✅ User-facing |
|---|---|
| `fix: null check in svc` | Order lookup no longer fails when the customer has no billing address |
| `feat: add endpoint` | New `POST /v1/orders/bulk-cancel` cancels up to 100 orders in one request |
| `perf: optimize query` | Order list responses are ~4× faster for accounts with >10k orders |
| `chore: bump deps` | *(omit — no user impact)* |

Filter out `chore`, `ci`, `test`, and `refactor` from published notes unless they
carry a user-visible consequence. Volume is not value; a changelog nobody reads
because it is 80% dependency bumps has failed.

## Generating a release

```bash
# 1. Inspect what has landed since the last tag
git log $(git describe --tags --abbrev=0)..HEAD --pretty=format:'%s%n%b%n---'

# 2. Determine the bump
#    any '!' or 'BREAKING CHANGE:'  -> MAJOR
#    else any 'feat:'               -> MINOR
#    else any 'fix:'/'perf:'        -> PATCH
#    else                           -> no release

# 3. Update CHANGELOG.md: move [Unreleased] into the new version + date,
#    rewrite entries for a user audience, update the link refs.

# 4. Tag and push
git commit -am "chore(release): v2.1.0"
git tag -a v2.1.0 -m "v2.1.0"
git push origin main --follow-tags
```

The `release.yml` workflow takes over from the tag: builds the artifact,
extracts that version's changelog section, and publishes the GitHub Release.

## Release note structure for a significant version

```markdown
## v2.1.0

### Highlights
One or two sentences on why anyone should care about this release.

### Upgrading
Steps required, in order. State "no action required" explicitly when true —
readers need to know the absence of steps is intentional, not an omission.

### Added / Fixed / Security
...

### Deprecated
What is going away, in which version, and what replaces it.
```

## Checklist before tagging

- [ ] Every commit since the last tag follows Conventional Commits
- [ ] Bump matches the highest-impact change present
- [ ] Breaking changes each have a migration path in the notes
- [ ] Entries rewritten for users, not copied verbatim from commit subjects
- [ ] `chore`/`ci`/`test` noise filtered out
- [ ] Version updated in `pyproject.toml` and matches the tag
- [ ] Link references at the bottom of CHANGELOG.md updated
- [ ] Tag is annotated (`-a`), not lightweight
