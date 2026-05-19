# Skill: send-files

You CAN send files to the user. The proxy intercepts a special tag in your
response and replaces it with a download link. You do not send bytes — you
reference a file that already exists inside your workspace, and the proxy
materializes a link.

## When to use

Use this skill when the user asks to:
- share, send, export, or download a file
- get a copy of something you wrote or generated (report, image, data export)
- keep a file permanently in their account

Also use it proactively when you have just produced a file the user will
obviously want (e.g. you wrote a PDF report they asked for — send it).

## Workflow

Follow these steps in order. **Do not skip step 1.**

### 1. Verify the file exists

Before emitting any tag, confirm the file is actually on disk. Pick ONE:

- `shell`: `ls -la /zeroclaw-data/workspace/<subdir>/` — list the directory
- `file_read`: read a byte or two to confirm access

If the file does not exist, **do not guess or invent a path.** Either:
- locate the correct file (list `workspace/temp/` or wherever it should be), or
- tell the user you cannot find it and ask what they want to send

Do not fabricate filenames. A tag pointing at a non-existent file produces
no link and leaves the literal `[DOWNLOAD:...]` text in your reply, which
is visible and ugly.

### 2. Pick the tag type

- **`DOWNLOAD`** — temporary link, expires in 1 hour. Use for most cases:
  anything the user asked you to send right now, one-off exports, previews.
- **`SAVE`** — permanent link, stored in the user's account. Use when the
  user explicitly asks to save, keep, or archive a file, or when the file is
  a long-lived artifact (e.g. a plan, a reference document).

When in doubt, use `DOWNLOAD`.

### 3. Emit the tag

Use the exact absolute container path. No spaces inside the brackets.

    [DOWNLOAD:/zeroclaw-data/workspace/<path>/<filename>]
    [SAVE:/zeroclaw-data/workspace/<path>/<filename>]

Place the tag inline in your natural reply — the proxy replaces it with a
markdown link `[<filename>](<signed-url>)` before the user sees it.

## Examples

User uploaded a photo and asks you to send it back:

    Here's your photo: [DOWNLOAD:/zeroclaw-data/workspace/temp/1776149011968_cat.jpg]

You generated a report and want to share it:

    I wrote the weekly summary: [DOWNLOAD:/zeroclaw-data/workspace/temp/summary.pdf]

User asks you to save a plan permanently:

    Saved your project plan: [SAVE:/zeroclaw-data/workspace/output/plan.md]

Multiple files in one reply:

    Attaching both:
    - original: [DOWNLOAD:/zeroclaw-data/workspace/temp/1776149011968_cat.jpg]
    - edited: [DOWNLOAD:/zeroclaw-data/workspace/temp/cat_edited.jpg]

## Common mistakes

These will break silently — the tag stays as literal text in the reply:

- Spaces inside brackets: `[DOWNLOAD: /path]` — broken
- Lowercase tag: `[download:/path]` — ignored
- Relative paths: `[DOWNLOAD:temp/file.txt]` — ignored
- Ordinary Markdown links are not export tags; emit only the DOWNLOAD or SAVE
  tag forms above
- Pointing at a file that doesn't exist (this is the most common failure —
  see step 1)
- Path outside `/zeroclaw-data/workspace/` — rejected

## Notes

- The container volume is mounted at `/zeroclaw-data/`. The workspace root
  is `/zeroclaw-data/workspace/`. User-uploaded files live under
  `/zeroclaw-data/workspace/temp/` with a timestamp prefix in the name.
- `DOWNLOAD` links expire after 1 hour. `SAVE` links persist.
- You do not need to read or copy the file — the proxy handles I/O. Just
  confirm it exists and emit the tag.
