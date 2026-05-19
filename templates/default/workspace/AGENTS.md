# AGENTS.md — Personal Assistant

## Every Session (required)

Before doing anything else:

1. Read SOUL.md and IDENTITY.md — this is who you are
2. Read USER.md — this is who you're helping
3. Use memory_recall for recent context

## Scheduled Messages & Reminders

You are connected to the user via the **LifeAtlas** channel. When creating
cron jobs that should deliver output to the user (reminders, scheduled checks,
notifications), you **must** include a delivery config:

```json
{
  "mode": "announce",
  "channel": "lifeatlas",
  "to": "user"
}
```

Example — setting a reminder:
```json
{
  "schedule": {"kind": "at", "at": "2026-04-06T10:00:00Z"},
  "job_type": "agent",
  "prompt": "Send the user a reminder: Time to stretch!",
  "delivery": {"mode": "announce", "channel": "lifeatlas", "to": "user"}
}
```

Without the `delivery` field, cron output is silently discarded.

## Sending Files to the User

You can send files to the user by emitting a `[DOWNLOAD:]` or `[SAVE:]` tag
that the proxy replaces with a signed URL. Use the `send-files` skill — it
has the full workflow, including verifying the file exists before you
reference it (the most common failure mode is inventing a path).

---
*Add your own conventions, style, and rules.*
