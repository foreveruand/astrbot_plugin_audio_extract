# Changelog

## v1.1.21

- Updated Telegram `/auex search` to edit its command reply in place for file selection, cancellation, and extraction progress, including single-file searches.
- Fixed `/auex search` commands and text-selection replies being processed by the LLM, and release the selection session before extraction starts.

## v1.1.20

- Fixed `/vclip` command and file-selection replies falling through to the LLM after clip processing completes.

## v1.1.19

- Updated Telegram `/vclip` to reuse its reply message for file selection and clip progress, including single-file clips.
- Fixed Telegram inline selection confirmation and cancellation to stop the pending input waiter immediately, preventing later user messages from being intercepted after completion or failure.

## v1.1.18

- Fixed `/vclip` aborting with `❌ 出错: invalid literal for int() with base 10: 'N/A'` when FFmpeg stream-copy mode emits `out_time_ms=N/A`; non-numeric progress fields are now skipped instead of crashing the clip.
- Added `/vclip` input validation: out-of-range minutes/seconds (e.g. `00:99:99`) are rejected, and end time must be later than the start time.

## v1.1.17

- Fixed `/vclip` to edit each clip progress message into its copyable completion filename instead of sending an additional summary message, with a fallback send when Telegram progress delivery fails.

## v1.1.16

- Fixed `/vclip` progress percentages to use the requested clip duration rather than the source video duration, and removed progress text from the completion message.

## v1.1.15

- Fixed `/vclip` progress updates to use the latest FFmpeg state instead of delayed stderr output, and include the final progress snapshot on completion.

## v1.1.14

- Fixed Telegram progress throttling dropping fast `/vclip` completion messages, including clipped output filenames.

## v1.1.13

- Fixed Telegram `/vclip` text-selection sessions remaining active after clip processing errors and consuming later commands.

## v1.1.12

- Fixed Telegram `/vclip` commands falling through to the LLM after a text-selected clip task finishes.

## v1.1.11

- Fixed Telegram `/vclip` and `/auex` inline keyboard callbacks continuing to the LLM after plugin handling.

## v1.1.10

- Fixed Telegram `/vclip` text selections being processed by the LLM after the clip task completed.

## v1.1.9

- Added Telegram inline menu support for `/auex batch` review confirm, refresh, and cancel actions.
- Added Telegram inline selection and text-reply fallback refresh for `/vclip` multi-file results.
- Telegram review and selection replies are deleted when possible, and menu/progress messages are reused in place.

## v1.1.8

- Added configurable `index_extensions` so directory indexing only includes the configured file extension whitelist.
- Updated index rebuild and search flows to clean up stale entries when the whitelist changes.

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
