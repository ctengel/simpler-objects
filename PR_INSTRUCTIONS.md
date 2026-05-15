# Pull Request Instructions

This branch is ready to be merged. Use these details to create the PR:

## PR Details

### Title
```
Phase 1: Add standard HTTP headers to object API (Issue #14)
```

### Description
```markdown
## Overview
Implements Phase 1 of API standardization for Issue #14 (Make the volume API simple/standard).

Adds RFC 9162 `Content-Digest` and `Content-Type` headers to GET/PUT responses, aligning with HTTP standards and enabling future phases.

## What's Included
- ✅ OpenAPI 3.0 specification documenting current and proposed API
- ✅ Comprehensive API standardization analysis with 4-phase roadmap
- ✅ Phase 1 implementation: standard HTTP headers
- ✅ Complete test suite for validation
- ✅ Full backward compatibility (non-breaking changes)

## Changes
### object_server.py
- Import `mimetypes` for MIME type detection
- Added `get_content_type()` helper function
- GET/HEAD now return `Content-Type` header (detected from filename)
- GET/HEAD now return both `Repr-Digest` (RFC 8949) and `Content-Digest` (RFC 9162)
- PUT now returns both `Repr-Digest` and `Content-Digest` headers
- Updated docstrings to document new headers

### New Files
- **openapi.yaml** - Complete OpenAPI 3.0 specification
- **API_STANDARDIZATION.md** - Detailed analysis and 4-phase migration plan
- **PHASE_1_SUMMARY.md** - Phase 1 overview and next steps
- **test_phase1.py** - Comprehensive test suite

## Testing
Run the test script to verify implementation:
```bash
# Terminal 1: Start server
OBJECT_DIRECTORY=/tmp/test-objects fastapi dev simpler_objects/object_server.py --port 29171

# Terminal 2: Run tests
python test_phase1.py
```

Tests verify:
- ✅ PUT returns both digest headers (201)
- ✅ GET returns both digest headers + Content-Type (200)
- ✅ HEAD returns headers without body
- ✅ MIME type detection for various file types

## Impact
- **Risk Level**: Low (non-breaking, additive changes only)
- **Backward Compatibility**: 100% (existing clients unaffected)
- **Standards Alignment**: RFC 9162 (Content-Digest), HTTP spec (Content-Type)
- **Future Impact**: Unblocks Phase 2 (Range requests) and Phase 3 (health status enum)

## Related
- Closes: Issue #14 (Phase 1)
- Enables: Issue #26 (double PUT, Phase 2)
- Enables: Issue #18 (PUT locking, Phase 3)

## Roadmap
- **Phase 1** (this PR): Standard headers ✅
- **Phase 2** (future): RFC 7233 Range request support
- **Phase 3** (future): Health status enum, PUT locking
- **Phase 4** (future): Pagination, timestamps

See API_STANDARDIZATION.md for complete roadmap.
```

### Base Branch
```
main
```

### Head Branch
```
issue-14-openapi-spec
```

---

## Quick Copy-Paste Instructions

1. Go to: https://github.com/ctengel/simpler-objects/compare/main...issue-14-openapi-spec
2. Click "Create pull request"
3. Use the title and description above
4. Submit

Or use GitHub CLI:
```bash
gh pr create \
  --title "Phase 1: Add standard HTTP headers to object API (Issue #14)" \
  --body "Implements Phase 1 of API standardization for Issue #14..." \
  --base main \
  --head issue-14-openapi-spec
```

Or create manually at:
https://github.com/ctengel/simpler-objects/pulls/new
