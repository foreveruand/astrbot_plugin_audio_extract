# Changelog

## v1.1.7

- Converted `auex` into a command group.
- Moved the existing keyword extraction flow to `/auex search <keyword>`.
- Added admin-only `/auex batch <directory>` for recursive no-subtitle/no-lyrics media scans, interactive list review, batch audio extraction, and plugin-compatible job file creation.

## v1.1.6

- Added multi-keyword search for `auex` using Chinese or English comma separators.
- Merged `auex` search results are deduplicated and limited to the first 20 files for selection.

## v1.1.5

- Changed `auex` extraction progress to use a single stream update flow, so platforms supporting message editing now present the final completion list by editing the same progress message.
- Removed per-file success completion messages during processing; only the final summary completion message is retained.

## v1.1.4

- Fixed `auex` Telegram multi-file confirm message being incorrectly shown as "start processing" after extraction finished.
- Improved `auex` completion output: now reports a full per-file completion list using filename stems (without suffixes).

## v1.1.3

- Added support for compact time interval format in `vclip`: `HMMSS-HMMSS` and `HHMMSS-HHMMSS`.
- Example: `/vclip movie 10101-20356` is parsed as `01:01:01 -> 02:03:56`.
