# Image Search Index Redesign

## Context

The image search in specimen_app uses O(n) linear scan with regex matching and requires 3+ segments in the query. Searching "QD-C" returns no results. For workspaces with thousands of images, search is slow and inflexible.

## Goal

Build a token-based inverted index for Everything-like instant search, support partial prefix matching with any number of query segments, and run smoothly on 4GB RAM.

## Design

### New class: `ImageSearchIndex`

Stored in `specimen_app/image_search.py`.

```
ImageSearchIndex
├── _entries: list[ImageIndexEntry]        # all entries
├── _by_stem: list[tuple[str, int]]        # (stem_lower, entry_idx) sorted by stem
├── _token_index: dict[str, set[int]]      # token → matching entry indices
```

### Index building

For each file stem (e.g. `QD-CK-SC008-1-20250923`):
1. Split on `[-_]` → tokens `["QD", "CK", "SC008", "1", "20250923"]`
2. For each token, index all prefixes: `"q"`, `"qd"`, `"c"`, `"ck"`, `"s"`, `"sc"`, `"sc0"`, `"sc00"`, `"sc008"`, `"1"`, `"2"`, `"20"`, `"202"`, ...
3. Each prefix maps to the set of entry indices containing that token prefix

### Search algorithm (two-phase)

1. **Token phase**: split query into tokens, look up each in `_token_index`, intersect result sets
2. **Verification phase**: check that matched tokens appear in consecutive positions in the stem

### Query behavior

| Input | Matches |
|-------|---------|
| `QD-C` | stems with consecutive tokens starting with "QD", "C" |
| `CK` | stems containing token "CK" anywhere |
| `QD-CK-SC008` | stems with first three tokens matching exactly |
| `26` | stems containing a date-like token starting with "26" |

### Disk cache

Bump `IMAGE_INDEX_DISK_VERSION` to 3. Store token index alongside entries in the JSON cache file. Old v2 caches auto-rebuild.

### UI changes (minimal)

- Remove `core_identifier` empty-value guard in `ImageSearchDialog.refresh_results`
- Update placeholder text
- Add match type indicator to results

### Memory

~3MB for 10K files, ~15MB for 50K files. Well within 4GB budget.
