---
name: Safe JSON export cleanup
description: Preserve valid JSON while removing links from large exported documents.
---

For large JSON exports, do not apply URL regexes to raw JSON bytes: escaped quotes and backslashes can make a valid document invalid. Sanitize the complete document's string values before splitting it into byte-sized parts; those parts may end inside a JSON string and are not standalone documents.

**Why:** a raw-byte link-removal pass corrupted a previously valid JSON file, while string-aware processing preserved its structure.

**How to apply:** use an escape-aware JSON-string transformation, validate the reassembled document, and skip or reassemble generated chunks before processing them again.
