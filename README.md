# AstrBot Audio Extract Plugin

A media processing plugin for AstrBot that supports audio extraction from video files and video clipping using FFmpeg.

## Features

- 🎵 **Audio Extraction**: Extract audio from video files as MP3 format
- ✂️ **Video Clipping**: Clip video segments by time range
- 📝 **Subtitle Sync**: Automatic subtitle synchronization (VTT to LRC conversion)
- 🗂️ **File Index**: SQLite-based file index for fast searching
- ⏰ **Scheduled Tasks**: Automatic index refresh and subtitle sync

## Prerequisites

- FFmpeg must be installed and available in PATH
- Python 3.10+

## Installation

1. Place the plugin folder in `data/plugins/astrbot_plugin_audio_extract/`
2. Configure the plugin through AstrBot admin panel
3. Restart AstrBot

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `work_dir` | string | `resources/audio_extract` | Working directory for temp files |
| `out_dir` | string | `/tmp/audio_extract` | Output directory for MP3 and subtitles |
| `scan_dirs` | list | `[]` | List of directories to scan for file index |

## Commands

### Audio Extraction

```
/auex <keyword>
```

Search for video files matching the keyword and extract audio as MP3.

**Example:**
```
/auex movie_name
```

### Video Clipping

```
/vclip <keyword> <start_time> <end_time>
```

Clip video segment by time range. Time format: `HH:MM:SS` or `MM:SS`.

**Example:**
```
/vclip movie 00:05:30 00:10:45
/vclip video 5:30 10:45
```

### Index Rebuild (Admin)

```
/aurebuild
```

Rebuild the file index for all configured directories.

## Scheduled Tasks

The plugin automatically runs the following scheduled tasks:

| Task | Schedule | Description |
|------|----------|-------------|
| Subtitle Sync | Every 2 minutes | Scan and sync subtitle files |
| Index Refresh | Every 6 hours | Incremental file index refresh |
| Full Rebuild | Daily at 3 AM | Complete file index rebuild |

## Supported Video Formats

- MP4, MKV, MOV, WMV, FLV, WebM, TS
- FLAC (with LRC conversion)

## Changelog

### v1.1.2

- 修复 Telegram 平台下进度消息未正确启用 MarkdownV2 的问题
- 调整进度更新策略：支持编辑的平台使用覆盖式更新，不支持的平台仅发送关键过程与结果消息
- 优化多平台回退逻辑，减少刷屏并保持插件兼容性

### v1.1.0

- **vclip 命令优化**: 进度更新改为编辑消息而非发送新消息（Telegram 平台）
- 添加消息编辑节流机制（0.5秒间隔），避免频繁 API 调用
- 文本变化检测，仅在实际内容变化时才编辑消息

## Notes

1. Ensure FFmpeg is properly installed
2. Configure `scan_dirs` with directories containing your media files
3. The file index is stored in `file_index.db` in the plugin directory
4. Output files are saved to the configured `out_dir`
5. **This plugin includes a `callback_query` handler for Telegram inline keyboards (prefix: `auex:`). **

## Migration from NoneBot

This plugin was migrated from `nonebot_plugin_audio_extract`. Main changes:

1. Uses AstrBot's `star.Star` API for plugin registration
2. Uses AstrBot's `CronJobManager` for scheduled tasks
3. Simplified file selector (no Telegram-specific inline keyboards)
4. Standard logging instead of NoneBot's logger

## License

MIT License