<!-- README.md -->
# Youtube Automation Backend

A FastAPI backend for managing many YouTube channels from one place. A user signs up, connects any number of YouTube channels (different Google accounts), and publishes one video to as many channels as they choose in a single action. The video goes from the browser straight to temporary cloud storage, then from the cloud to every channel in parallel, with live status per channel, upload that resumes where it stopped, automatic retries and a permanent history.

## Table of contents

- [Features](#features)
- [Project status](#project-status)
- [How it works](#how-it-works)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [External setup](#external-setup)
- [API reference](#api-reference)
- [Publishing flow for the frontend](#publishing-flow-for-the-frontend)
- [Upload engine](#upload-engine)
- [Limits and constraints](#limits-and-constraints)
- [Security](#security)
- [Testing](#testing)
- [Configuration reference](#configuration-reference)
- [Deployment](#deployment)
- [Roadmap](#roadmap)
- [Code conventions](#code-conventions)

## Features

- **Accounts:** sign up, log in, refresh tokens, logout, forgot and reset password. Authentication is built in-house; Supabase Auth is not used.
- **Multiple YouTube channels:** connect, rename, list and disconnect channels. Tokens are encrypted at rest and refreshed automatically. A channel whose permission was revoked is flagged "needs reconnect".
- **Publish once, reach every channel:** choose a video on your computer, fill in the title, description and tags, pick up to 50 channels and press Publish. Nothing is uploaded before that.
- **Direct upload:** the browser sends the video straight to Cloudflare R2 in parts through presigned URLs. The API server never carries the video. A broken connection or a page refresh continues from the last stored part.
- **Parallel delivery from the cloud:** every channel is processed independently, up to 10 at the same time, each with its own status, progress and error. A failure on one channel never stops the others.
- **Resumable YouTube uploads:** the video is sent to YouTube in chunks. After a failure the upload continues from the byte YouTube already has, also after a server restart.
- **Automatic and manual retry:** temporary errors are retried automatically (6 attempts). Failed channels can be retried with one call.
- **Temporary storage:** the video is deleted from the cloud as soon as every channel has it, and in any case 24 hours after the upload.
- **Upload history:** a permanent record of what was published, with which title, description and tags, to which channels, and whether each one succeeded or failed.
- **Quota awareness:** a daily upload counter keeps each user inside the YouTube API limit. Credits are given back when an attempt never reached YouTube.
- **Image library:** thumbnails are kept in a private library, tagged and searchable. Videos are not stored in the library.
- **Channel analytics dashboard:** every channel's videos with views, likes and comments, channel and single-video analytics (watch time, retention, traffic sources, search terms, audience), and computed insights: best videos, best time to upload, best tags, title and duration patterns, upload cadence. An all-channels overview compares every connected channel.
- **AI insights (Gemini):** strategy, content, publishing, audience and keyword reports, comment analysis, a 30-day content plan, single-video reviews and a multi-channel portfolio review. Numbers are computed by the backend; Gemini only interprets them.

## Project status

| Area | Status |
|---|---|
| Auth: signup, login, refresh, logout, forgot and reset password | Done and tested |
| YouTube accounts: connect, rename, list, disconnect, token refresh | Done |
| Direct browser upload to R2 with resume (multipart, presigned URLs) | Done. Tested against a local S3 server that enforces the real multipart rules |
| Resumable YouTube upload engine, leases, retries, quota credits | Done. Tested against a fake YouTube server that injects failures. Run one end-to-end check against your own Google project before production |
| Temporary file lifecycle: delete on success, 24 hour limit, abandoned uploads | Done and tested |
| Image library (images only) | Done and tested |
| Channel analytics: sync, dashboards, insights | Implemented. Check the reports once against a real channel, because the YouTube Analytics API cannot be called from the test suite |
| AI insights with Gemini | Implemented. Needs `GEMINI_API_KEY` |
| Frontend part upload (parallel parts, retry, resume) | Not part of this repository. See [Publishing flow for the frontend](#publishing-flow-for-the-frontend) |
| Deployment | Not started |

## How it works

```mermaid
flowchart LR
    A[Browser] -->|1. Publish: file info, details, channels| B[API]
    B --> C[(Postgres: jobs, targets, files)]
    B -->|presigned part URLs| A
    A -->|2. parts, straight to storage| D[(Cloudflare R2 staging)]
    A -->|3. complete| B
    E[Upload worker] -->|4. claim channel| C
    E -->|5. read chunks| D
    E -->|6. resumable upload| F[YouTube Data API]
    E -->|7. progress, result, cleanup| C
    A -->|8. poll job| B
```

1. The user selects a video on their computer and fills in the details. Nothing leaves the browser yet.
2. On Publish the browser calls `POST /yt-channels/uploads` with the file name, size and type, the details and the channels. The API checks everything (limits, channels, daily quota), creates the job and an upload session in R2, and returns a presigned URL for every part.
3. The browser sends the parts to R2 directly, a few in parallel. If something fails it asks `GET .../media` which parts are stored and continues with the rest.
4. The browser calls `POST .../media/complete`. The server checks every part itself, joins them into one file, checks that it is a video, and queues every channel.
5. The worker claims channels one by one (up to 10 at once), reads the video from R2 in chunks and sends it to YouTube through a resumable upload session.
6. The client polls the job for live status and progress.
7. When every channel has the video, the file is deleted from R2. If some channels failed it stays until the 24 hour limit so that a retry is possible.

## Tech stack

- Python 3.12, FastAPI, Uvicorn
- SQLAlchemy 2 (async) with asyncpg, Supabase Postgres (database only)
- Cloudflare R2 (S3 compatible, private bucket) accessed with boto3
- Google OAuth 2.0 and YouTube Data API v3 (resumable upload protocol over `requests`; `google-api-python-client` for thumbnails and analytics)
- Argon2 password hashing, JWT access tokens, rotating refresh tokens
- Fernet encryption for Google tokens and saved upload sessions
- SMTP (Gmail) for password reset codes
- Pydantic Settings for configuration
- Pytest, pytest-asyncio, httpx, moto for tests

## Project structure

```
main.py                  application entrypoint (uvicorn main:app)
app/
  config.py              all settings: values from .env plus fixed values
  core/                  database, security, exceptions, handlers, logging, dependencies
  models/                SQLAlchemy tables
  schemas/               request and response models
  crud/                  database queries
  service/               business logic
  routes/                API endpoints
  integrations/          external systems (SMTP, YouTube APIs, Gemini)
  workers/               background jobs (YouTube upload worker)
  utils/                 helpers: validators, retry and error rules, R2 client, tokens
migrations/              SQL to run by hand on an existing database
tests/                   automated tests
```

Each feature has one file per layer:

| Feature | File name in every layer |
|---|---|
| Authentication | `auth.py` |
| YouTube account connection | `yt_management.py` |
| Google OAuth client per user | `google_credentials.py` |
| Image library | `storage_bucket.py` |
| Multi-channel upload | `yt_channel_management.py` |
| Temporary video file and its lifecycle | `yt_media_upload.py` |
| Channel analytics | `yt_analytics.py` |
| AI insights | `yt_ai_insights.py` |

## Getting started

### Prerequisites

- Python 3.12
- A Supabase project (database)
- A Cloudflare account with one private R2 bucket (file storage)
- A Google Cloud project with YouTube Data API v3 enabled
- On Windows, the `tzdata` package (the daily quota follows Pacific Time)

### Run locally

1. Create and activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt` (add `requirements-dev.txt` to run the tests). Make sure `tzdata` and `boto3` 1.36 or newer are included.
3. Create `.env` from `.env.example` and fill in the values.
4. If you already have a database from an earlier version, run `migrations/001_direct_upload.sql` first (see [Deployment](#deployment)).
5. Start the server: `uvicorn main:app --reload`
6. Open `http://localhost:8000/docs`.

New tables are created automatically on startup. The upload worker starts with the app.

To run the worker as a separate process, set `yt_upload_worker_enabled` to `False` in `app/config.py` for the API process and run `python -m app.workers.yt_channel_management` in another terminal.

### Environment variables

Only these live in `.env`. Everything else is fixed in `app/config.py`.

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Supabase Postgres connection string. Use the Session pooler string from Connect in the Supabase dashboard. |
| `JWT_SECRET_KEY` | Yes | Random secret, at least 32 characters. Also used to hash reset codes. |
| `APP_ENV` | No | `development` (default) or `production`. In production the interactive docs are off. |
| `TOKEN_ENCRYPTION_KEY` | Yes | Fernet key used to encrypt Google tokens and saved upload sessions. |
| `R2_ACCOUNT_ID` | Uploads and library | Cloudflare account ID. The endpoint is built from it. |
| `R2_BUCKET_NAME` | Uploads and library | Name of the private R2 bucket. |
| `R2_ACCESS_KEY_ID` | Uploads and library | Access key ID of an R2 API token limited to that bucket. |
| `R2_SECRET_ACCESS_KEY` | Uploads and library | Secret of that token. Backend only. |
| `R2_ENDPOINT_URL` | No | Only when the endpoint is not `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`. |
| `SMTP_USERNAME` | For reset emails | Gmail address that sends the codes. Also used as the From address. |
| `SMTP_PASSWORD` | For reset emails | Gmail app password. |
| `GEMINI_API_KEY` | AI insights | API key from Google AI Studio. Without it every AI endpoint answers `ai_not_configured` and the rest of the app works normally. |
| `GEMINI_MODEL_FAST` | No | Model for quick reports. |
| `GEMINI_MODEL_DEEP` | No | Model for strategy, content, plan and portfolio reports. |

Generate a JWT secret: `python -c "import secrets; print(secrets.token_urlsafe(64))"`

Generate a token encryption key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

The old `B2_*`, `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` variables are no longer used and can be removed from `.env`.

## External setup

### Supabase

1. Create a project at supabase.com and keep the database password safe.
2. Click Connect in the project, choose Session pooler and copy the string into `DATABASE_URL`. Replace the password placeholder and URL-encode special characters in the password.
3. Start the app and check `GET /health/ready`. It should report the database as up.
4. Enable row level security on every table so the public Supabase REST API cannot read them. The backend connects as the database owner and is not affected:

```sql
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.password_reset_otps ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.google_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.youtube_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.storage_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.storage_file_tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_media_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_upload_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_upload_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_upload_quota_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_analytics_syncs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_channel_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_videos ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_analytics_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.yt_ai_reports ENABLE ROW LEVEL SECURITY;
```

Repeat this for every new table.

### Cloudflare R2

R2 holds two kinds of files: the temporary videos (prefix `staging/`) and the thumbnail images of the library. The free plan includes 10 GB of storage per month, 1 million write operations, 10 million read operations and free downloads. Check the current limits on the Cloudflare pricing page.

1. In the Cloudflare dashboard open **R2 Object Storage** and enable it. Cloudflare may ask for a payment method even for the free plan.
2. Create a bucket. Keep public access off (do not enable the `r2.dev` URL and do not attach a custom domain: presigned URLs only work on the S3 API address).
3. Note the **Account ID** from the R2 overview page.
4. Open **Manage R2 API Tokens** and create a token with **Object Read & Write**, limited to that one bucket. The access key ID and secret are shown only once. Put them in `.env`.
5. Bucket **Settings, CORS Policy**. The browser sends parts straight to R2, so this is required:

```json
[
  {
    "AllowedOrigins": ["http://localhost:5173", "https://your-frontend.example.com"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

Use the exact origin of your frontend, with the port and without a trailing slash. `ETag` must be exposed. This is the dashboard format; the Wrangler CLI uses a different one.

6. Bucket **Settings, Object lifecycle rules**. Add a rule that deletes objects with prefix `staging/` after 1 day and aborts incomplete multipart uploads after 1 day. Do not add a delete rule for the rest of the bucket: it also holds the image library. The application deletes files itself; the rule is a safety net.
7. Check it once: call `POST /yt-channels/uploads` from `/docs`, send one part to its URL with `curl -X PUT --data-binary @file URL` and look for an `ETag` header, then cancel the upload with `POST /yt-channels/uploads/{id}/cancel` and confirm the bucket is empty.

### Google Cloud

1. Create a project in Google Cloud Console and enable **YouTube Data API v3** and **YouTube Analytics API**.
2. Configure the OAuth consent screen. While it is in Testing mode, add every Gmail you will connect as a test user and note that refresh tokens expire after 7 days. Publish and verify the app for real users.
3. Create an OAuth client of type **Web application** with the redirect URI `http://localhost:8000/api/v1/youtube/callback` (use your deployed callback URL in production).
4. Every user saves their own OAuth client ID and secret in the app (`PUT /youtube/credentials`). Uploads, quota and analytics then run in that user's own Google project.

The app requests the scopes `openid`, `userinfo.email`, `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly` and `youtube.force-ssl`. They cover uploading videos, setting thumbnails, reading channel and video details, reading comments and reading analytics. Adding a scope later means every channel has to be connected again. A channel connected before a scope was added keeps working for everything else, and the affected endpoints answer with a clear message until it is reconnected.

Videos uploaded through the API by a Google project that has not passed Google's API compliance audit can be restricted to private. Check the current YouTube policy before relying on `public` or `unlisted`.

## API reference

Base prefix: `/api/v1`. Interactive documentation is at `/docs` outside production. Protected endpoints need `Authorization: Bearer <access_token>`.

### Auth

| Method and path | Auth | Description |
|---|---|---|
| `POST /auth/signup` | No | Create an account and log in immediately. |
| `POST /auth/login` | No | Log in. |
| `POST /auth/refresh` | No | Exchange a refresh token for a new token pair. |
| `POST /auth/logout` | No | Revoke a refresh token. |
| `POST /auth/forgot-password` | No | Email a one-time code. The response is the same whether or not the email exists. |
| `POST /auth/reset-password` | No | Verify the code and set a new password. |
| `GET /auth/me` | Yes | Return the logged in user. |

Signup and login return `{ "user": {...}, "tokens": { "access_token", "refresh_token", "token_type", "expires_in" } }`.

### YouTube accounts

| Method and path | Description |
|---|---|
| `GET /youtube/credentials` | Whether the user has saved a Google OAuth client. |
| `PUT /youtube/credentials` | Save the Google OAuth client ID and secret. |
| `DELETE /youtube/credentials` | Remove them. |
| `POST /youtube/connect` | Body: `{ "nickname": "..." }`. Returns the Google consent URL; redirect the user to it. |
| `GET /youtube/callback` | Used by Google only. Redirects to `{frontend_url}/youtube/connect?status=success` or `status=error`. |
| `GET /youtube/accounts` | List connected channels. A channel that needs to be connected again has `needs_reconnect: true`. |
| `PATCH /youtube/accounts/{id}` | Rename a channel. |
| `DELETE /youtube/accounts/{id}` | Revoke the Google token and disconnect the channel. |

A channel can be connected once per user. Connecting it again refreshes its tokens.

### Library (images only)

| Method and path | Description |
|---|---|
| `POST /storage/files` | Add one image as multipart form data. Optional `tags` field, comma separated. |
| `GET /storage/files` | List images with temporary signed URLs. Filters: `tags`, `search`, `limit`, `offset`. |
| `PATCH /storage/files/{id}` | Replace the tags of an image. |
| `DELETE /storage/files/{id}` | Delete an image. |

JPG, PNG, WEBP or GIF, up to 10 MB. The content of the file is checked, not only its name or declared type. Videos are rejected here: they are uploaded when they are published. Signed URLs are valid for 1 hour.

### Publishing (multi-channel upload)

| Method and path | Description |
|---|---|
| `POST /yt-channels/uploads` | Start publishing. Creates the job and returns presigned URLs for the video parts. |
| `GET /yt-channels/uploads/{id}/media` | State of the browser upload: stored parts and fresh URLs for the missing ones. Use it to resume. |
| `POST /yt-channels/uploads/{id}/media/complete` | Finish the browser upload. Publishing to the channels starts. Safe to call twice. |
| `GET /yt-channels/uploads` | Upload history, newest first. Filters: `status`, `account_id`, `search` (title), `date_from`, `date_to`, `limit`, `offset`. |
| `GET /yt-channels/uploads/{id}` | One job with the status, progress, YouTube link and error of every channel. Poll this for live progress. |
| `GET /yt-channels/uploads/quota` | Uploads left today: daily limit, used, waiting, remaining and the reset time. |
| `POST /yt-channels/uploads/{id}/retry` | Run the failed channels again. They continue from the saved upload session when it is still valid. |
| `POST /yt-channels/uploads/{id}/cancel` | Cancel every channel that has not finished. Also stops a browser upload. |

Start request:

```json
{
  "file": { "name": "holiday.mp4", "size_bytes": 734003200, "content_type": "video/mp4" },
  "account_ids": ["a1...", "b2..."],
  "title": "My video",
  "description": "About the video",
  "tags": ["travel", "vlog"],
  "category_id": "22",
  "privacy_status": "private",
  "publish_at": null,
  "made_for_kids": false,
  "default_language": "en",
  "notify_subscribers": true,
  "thumbnail_file_id": null,
  "client_request_id": "3f6c2c1e-9a52-4a57-8f3a-0a1c5d1f7e11"
}
```

| Field | Rules |
|---|---|
| `file` | Required. Name, size in bytes and type of the video on the user's computer. MP4, MOV, AVI, MKV, WEBM or MPEG, at most 1.5 GB. The type may be empty; it is then taken from the extension. |
| `account_ids` | Required. 1 to 50 connected channels that are active and not waiting for a reconnect. Duplicates are ignored. |
| `title` | Required. 1 to 100 characters, no `<` or `>`. |
| `description` | Up to 5000 bytes, no `<` or `>`. |
| `tags` | Up to 500 characters in total, each tag up to 30 characters. Duplicates are ignored. |
| `category_id` | YouTube category ID. Default `22`. |
| `privacy_status` | `public`, `unlisted` or `private`. Default `private`. |
| `publish_at` | Optional schedule time with a timezone, in the future. Forces `private` until that time. |
| `made_for_kids` | Default `false`. |
| `notify_subscribers` | Default `true`. |
| `thumbnail_file_id` | Optional JPG or PNG from the image library. |
| `client_request_id` | Optional, 8 to 64 letters, digits, `-` or `_`. Send the same value when repeating a request (double click, retry after a network error): the same upload is returned with `reused: true` instead of a second one. |

Start response (shortened):

```json
{
  "job": { "id": "9c7e...", "status": "uploading_media", "total_channels": 2, "targets": [ ... ] },
  "upload": {
    "asset_id": "5b1f...",
    "size_bytes": 734003200,
    "part_size_bytes": 16777216,
    "total_parts": 44,
    "max_parallel_parts": 4,
    "upload_deadline": "2026-10-29T08:00:00Z",
    "uploaded_part_numbers": [],
    "parts": [
      { "part_number": 1, "size_bytes": 16777216, "url": "https://....r2.cloudflarestorage.com/..." }
    ]
  },
  "reused": false
}
```

Example job response while publishing (shortened):

```json
{
  "id": "9c7e...",
  "status": "running",
  "media_status": "ready",
  "media_expires_at": "2026-10-29T08:00:00Z",
  "title": "My video",
  "total_channels": 2,
  "done_count": 1,
  "failed_count": 0,
  "active_count": 1,
  "targets": [
    {
      "account_nickname": "Gaming",
      "channel_title": "My Gaming Channel",
      "status": "done",
      "progress_percent": 100,
      "attempts": 1,
      "youtube_url": "https://www.youtube.com/watch?v=...",
      "thumbnail_status": "done"
    },
    {
      "account_nickname": "Vlogs",
      "channel_title": "My Vlogs",
      "status": "uploading",
      "progress_percent": 42,
      "bytes_confirmed": 308281344,
      "attempts": 1
    }
  ]
}
```

`media_status` is `uploading`, `ready`, `deleted` or `aborted`. `media_expires_at` is when the file will be removed from the cloud; failed channels can be retried until then.

### Channel analytics

All paths start with `/yt-analytics`. Dates are `YYYY-MM-DD`. Every range endpoint takes `days` (1 to 730, default 28) or `start_date` with `end_date`. YouTube analytics lags about 2 days, so the range ends 2 days ago.

| Method and path | Description |
|---|---|
| `GET /overview` | All connected channels side by side: subscribers, views, upload activity, growth, sync state. Uses stored data only. |
| `POST /accounts/{id}/sync` | Load the channel and all of its videos in the background. Returns 202 with the sync state. If a sync is already running, its state is returned. |
| `GET /accounts/{id}/sync` | Sync state: `idle`, `running`, `completed` or `failed`, with `is_stale` when the data is older than 6 hours. |
| `GET /accounts/{id}/channel` | Dashboard header: profile, subscribers, growth, video counts, totals, `analytics_enabled`, `needs_reconnect`, `has_data`. |
| `GET /accounts/{id}/performance` | Views, watch time, average view duration and percentage, likes, comments, shares and subscribers for the period, the period before it, the change in percent and a daily series. |
| `GET /accounts/{id}/audience` | Age and gender, countries, devices, subscribed versus not subscribed. |
| `GET /accounts/{id}/traffic` | Traffic sources, YouTube search terms, Shorts versus long-form versus live. |
| `GET /accounts/{id}/top-videos` | Best videos of the period. `limit` up to 200. |
| `GET /accounts/{id}/videos` | Every video. Filters: `search`, `content_type` (`all`, `short`, `long`), `privacy_status`, `date_from`, `date_to`. Sort: `published_at`, `views`, `likes`, `comments`, `engagement`, `performance`, `duration`. Paging: `limit`, `offset`. |
| `GET /accounts/{id}/videos/{video_id}` | One video with its rank and comparison against the channel median. |
| `GET /accounts/{id}/videos/{video_id}/analytics` | Daily series, audience retention curve, traffic sources, search terms and demographics. Without a range the whole life of the video is used. |
| `GET /accounts/{id}/videos/{video_id}/comments` | Top comments (up to 100). `comments_disabled` is true when the video has comments turned off. |
| `GET /accounts/{id}/insights/publishing` | Best weekday, hour and day part to upload, heatmap and upload cadence. Optional `timezone` (default `Asia/Kolkata`) and `content_type`. |
| `GET /accounts/{id}/insights/content` | Best and weakest videos, tag performance, title patterns, duration groups. |

How the insights are computed:

- Videos are stored on sync. A video gets a `performance_index`: its views divided by the median views of videos of the same format (Shorts or long-form) published within 90 days of it. 1.0 is typical, 2.0 is twice as good. Videos younger than 7 days have none.
- Best upload time compares the performance index of every video grouped by weekday and hour in the chosen timezone. Results carry `sample_size` and `confidence`. YouTube does not share when viewers are online through its API.
- A video counts as a Short when it is 180 seconds or shorter.
- Sections that YouTube refuses for a channel come back with `available: false` and a `message` instead of failing the whole response.
- Analytics results are cached for 3 hours and comments for 6 hours.

### AI insights

| Method and path | Description |
|---|---|
| `POST /yt-analytics/accounts/{id}/ai-reports` | Body: `report_type`, optional `days` (7 to 365), `language` (`en` or `hinglish`), `video_id` (required for `video_review`), `timezone`, `force`. Returns the saved report. |
| `POST /yt-analytics/ai-reports/portfolio` | Body: optional `days`, `language`, `force`. Compares all connected channels. |
| `GET /yt-analytics/ai-reports` | Saved reports, newest first. Filters: `account_id`, `video_id`, `report_type`. |
| `GET /yt-analytics/ai-reports/{report_id}` | One saved report. |
| `DELETE /yt-analytics/ai-reports/{report_id}` | Delete a saved report. |

Report types: `channel_strategy`, `content_analysis`, `publishing_schedule`, `audience_keywords`, `comments_insights`, `content_plan`, `video_review`, `portfolio`.

A report has a `headline`, `summary`, optional `score`, `strengths`, `weaknesses`, `insights` (each with finding, evidence, recommendation, action steps, priority, impact, effort and confidence), `quick_wins`, `content_ideas`, `experiments` and `data_limitations`. `data_quality` lists what was missing when it was created.

Rules: the same data returns the saved report with `cached: true` for 12 hours; `force` creates a new one but at most once every 2 minutes per report; each user can create 30 reports in 24 hours; creating a report can take up to a minute. The prompt version is part of the cache key, so improving the prompt never returns an older report.

### Health

| Method and path | Description |
|---|---|
| `GET /health` | Liveness check. |
| `GET /health/ready` | Readiness check including the database. |

### Error format

Every error uses one shape:

```json
{
  "success": false,
  "error": {
    "code": "validation_error",
    "message": "Some of the information you entered is not valid. Please review it and try again.",
    "details": [{ "field": "title", "message": "The title cannot be empty." }]
  }
}
```

`details` appears only for validation errors. Use `error.code` for logic and `error.message` for display. Codes added by the publishing flow:

| `error.code` | HTTP | Meaning |
|---|---|---|
| `upload_incomplete` | 400 | `complete` was called before every part was stored, or a part has the wrong size. `details` says how many parts are missing. Upload the missing parts and call `complete` again. |
| `upload_session_gone` | 404 | The browser upload can no longer continue (time window over or session removed). Start again. |
| `invalid_video_file` | 400 | The finished file is not a video. It was deleted. |
| `media_expired` | 409 | The file was removed after 24 hours or the upload was cancelled. Start a new upload. |
| `media_not_ready` | 409 | The video is still being uploaded. |
| `too_many_uploads` | 409 | The user already has the maximum number of videos waiting (5). |
| `staging_busy` | 503 | The temporary storage is full. Try again in a few minutes. |

## Publishing flow for the frontend

This is what the browser has to do. The backend never receives the video itself.

1. **Start.** `POST /yt-channels/uploads` with the file info and the details. Keep `job.id`, `upload.part_size_bytes` and the list of `upload.parts`. Generate one `client_request_id` per Publish click and reuse it if the request has to be repeated.
2. **Send the parts.** For every entry of `parts`, take the bytes `[(part_number - 1) * part_size_bytes, ... + size_bytes)` of the file (`File.slice`) and send them with `PUT` to the part's `url`. No `Authorization` header, no extra headers. Send up to `max_parallel_parts` at the same time. If a part fails, retry that part (3 to 5 times with growing delay), not the whole file. Show the progress as stored bytes divided by the file size.
3. **Resume.** After a page refresh, a lost connection or when URLs have expired (they last 6 hours), call `GET /yt-channels/uploads/{job.id}/media`. It returns `uploaded_part_numbers` and new URLs for the parts that are still missing. Continue with those.
4. **Complete.** When every part is sent, call `POST /yt-channels/uploads/{job.id}/media/complete`. On `upload_incomplete` repeat step 3. On success the job is `queued` and publishing starts by itself.
5. **Show progress.** Poll `GET /yt-channels/uploads/{job.id}` every 2 to 3 seconds. Each target has `status`, `progress_percent`, `attempts`, `error_code` and `error_message`. Stop polling when the job is `completed`, `partially_completed`, `failed` or `cancelled`.
6. **Retry.** `POST .../retry` for the failed channels, while `media_expires_at` is still in the future.

## Upload engine

### Statuses

| Level | Values |
|---|---|
| Job | `uploading_media`, `queued`, `running`, `completed`, `partially_completed`, `failed`, `cancelled` |
| Channel (target) | `pending`, `uploading`, `retrying`, `done`, `failed`, `cancelled` |
| Video file | `uploading`, `ready`, `deleted`, `aborted` |
| Thumbnail | `none`, `pending`, `done`, `failed` |

The job status is derived from its targets: all `done` is `completed`, some `done` and some not is `partially_completed`, none `done` with at least one `failed` is `failed`. While the browser is still uploading the file the job is `uploading_media` and the worker does not touch it.

### Resumable upload

The worker talks to YouTube's resumable upload protocol itself. For every channel:

1. It first checks that the video can be read from storage. If it cannot, nothing is sent to YouTube and no daily credit is used.
2. It reserves one daily credit and starts an upload session at YouTube. The session address is stored (encrypted) in the database before the first byte is sent.
3. It sends the video in 16 MB chunks. After any failure it asks YouTube how many bytes it has and continues from there. A chunk that fails is repeated up to 5 times with growing delay inside the same attempt.
4. If the server stops, the upload continues from the stored session after the restart. If YouTube no longer knows the session, a new one is started.
5. A channel that is cancelled stops within one chunk or within 15 seconds, whichever comes first.

Ownership of a channel is protected by a lease token: a worker that was replaced after a crash cannot overwrite the result of the worker that took over.

### Retries and errors

A channel is retried automatically, up to 6 attempts with growing delays (20 seconds, 1, 3, 5 and 10 minutes), only when the error is temporary. Because the upload resumes, a retry does not send the whole video again. Errors that cannot be fixed by waiting fail immediately with a clear message and never block other channels. `POST .../retry` runs the failed channels again with fresh attempts.

| `error_code` | Meaning | Retried automatically |
|---|---|---|
| `network_error` | Connection interrupted | Yes |
| `youtube_unavailable` | YouTube had a temporary problem or rate limit | Yes |
| `storage_unavailable` | Storage could not be reached | Yes |
| `source_unreadable` | The video could not be read from storage | Only for temporary read errors |
| `quota_exceeded` | The daily YouTube limit of the user's Google project was reached | Paused until the limit resets, if the file will still exist then. Otherwise failed |
| `channel_upload_limit` | The channel reached its own daily upload limit | No |
| `reconnect_required` | The channel permission was revoked or expired. The channel is flagged for reconnect | No |
| `account_unavailable` | The channel was disconnected or removed | No |
| `invalid_video_details` | YouTube rejected the title, description, tags or schedule | No |
| `no_youtube_channel` | The Google account has no YouTube channel | No |
| `forbidden`, `rejected_by_youtube` | YouTube refused the request | No |
| `source_missing` | The video file is no longer in storage | No |
| `media_expired` | The file was removed 24 hours after the upload | No |
| `media_upload_incomplete` | The browser upload was not finished in time (12 hours) | No |
| `invalid_video_file` | The uploaded file is not a video | No |
| `interrupted` | The server stopped repeatedly during the upload | No |
| `unexpected_error` | Anything else. Details are in the server log | No |

### Quota

YouTube counts uploads per Google Cloud project, and every user brings their own project, so the app keeps a daily counter per user (Pacific Time, matching Google's reset). The limit is `yt_upload_daily_limit` (default 100); set it to the real quota of your project.

- A credit is used when an upload session is started at YouTube, not before.
- It is given back when starting fails with an answer from YouTube, or when the connection to YouTube never opened.
- A failure while reading the video from storage never uses a credit.
- A new upload is refused up front when the selected channels need more uploads than are left.
- When YouTube reports that the limit is reached, every waiting channel of that user is paused and continues by itself after the reset, as long as the video file will still exist then. It does not use up retry attempts.

### History

History is permanent. Each job keeps its own copy of the video file name, title, description, tags and settings, and each channel entry keeps the channel name and nickname as they were at upload time. The record stays correct even if the file is deleted or the channel is renamed or disconnected later.

### File lifetime

- The video exists in R2 only after the user pressed Publish.
- When every channel has the video, the file is deleted right away.
- If some channels failed, the file is kept so that a retry is possible, and deleted 24 hours after the upload was completed. `media_expires_at` tells the user when.
- A browser upload that is not finished within 12 hours is aborted and its parts are deleted.
- A cancelled job deletes its file at once.
- A cleanup task inside the worker runs every 5 minutes and repeats any deletion that failed. A file that is being uploaded to YouTube at that moment is not removed.

### Worker behavior

- Up to 10 channels upload in parallel (`yt_upload_concurrency`), in their own thread pool so that logins and API requests are never slowed down.
- Waiting work is claimed with `FOR UPDATE SKIP LOCKED`, so more than one worker can run safely.
- A channel's result and the status of its job are written in two separate short transactions, and a transaction that loses a deadlock or serialization conflict is repeated automatically. A cleanup task repairs any job whose status is behind its channels.
- A heartbeat every 15 seconds keeps the lease. Uploads whose worker disappeared return to the queue after 2 minutes and continue from the saved session.
- On shutdown the worker asks running uploads to stop at the next chunk and puts them back in the queue with their session.
- A thumbnail is set after the video is uploaded. If it fails the video stays `done` and the channel shows `thumbnail_status: failed` with the reason.

## Limits and constraints

- **Video size:** up to 1.5 GB (`media_video_max_bytes`).
- **Channels per upload:** up to 50 (`yt_upload_max_accounts`).
- **Videos waiting per user:** up to 5. Total temporary storage: 8 GB (`media_max_staged_bytes_total`), which keeps the project inside the free R2 plan.
- **Daily upload limit:** the default is 100 uploads per day per user (see Quota). One video sent to 20 channels uses 20.
- **Speed:** the total time is decided by the network of the machine that runs the worker. 50 channels with a 1.5 GB video means 75 GB sent to YouTube. Run the worker on a server with a fast connection, not on a home line.
- **Channel limits:** YouTube also applies its own daily upload limit per channel, especially to new or unverified channels. Such a failure is reported as `channel_upload_limit`.
- **R2 usage:** with 16 MB parts a 1.5 GB video needs about 100 write operations to store and about 100 read operations per channel. Check that your traffic fits the free plan.
- **Custom thumbnails:** YouTube may require the channel to be verified. The video is still published if the thumbnail is refused.
- **Consent screen:** in Testing mode refresh tokens expire after 7 days and only listed test users can connect. Publish and verify the app before real users connect.
- **Compliance audit:** Google may require an API compliance audit before large scale use, a higher quota or public videos from API uploads.

## Security

- Passwords are hashed with Argon2 and checked in a worker thread so the event loop is never blocked.
- Password rules: at least 8 characters with at least one letter and one number.
- Access tokens are short lived JWTs. Refresh tokens are random, stored only as hashes and rotated on every use. Reusing an old refresh token revokes all sessions of that user.
- Reset codes are 6 digits, valid for 10 minutes, single use, stored as keyed hashes, locked after 5 wrong attempts, with a 60 second resend cooldown.
- Login errors and forgot-password responses do not reveal whether an email is registered.
- A successful password reset revokes every active session.
- Google access and refresh tokens and saved YouTube upload sessions are encrypted with Fernet before they are stored and are never returned by the API.
- OAuth uses a single-use `state` value stored in the database and valid for 10 minutes to prevent forged callbacks.
- Every upload, file and channel query is scoped to the logged in user.
- Files live in a private bucket. The browser can only write to the exact part URLs it was given (they expire after 6 hours) and can read library images through URLs that expire after 1 hour. The R2 token is limited to one bucket and never leaves the server.
- The content of every uploaded video and image is checked (file signature), not only its name or declared type.
- Secrets are read as `SecretStr` and never logged. Signed URLs are never logged.

## Testing

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The tests never touch Supabase, Cloudflare, Google or YouTube. By default they use a SQLite database file in a temporary folder. To run the same tests against a real PostgreSQL server (recommended before a release, because SQLite hides concurrency problems), create an empty database and run `TEST_DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1:5432/ytapp_test pytest`. The tests drop and recreate all tables in that database, so never point it at real data. Local fake servers stand in for the outside world:

- `tests/test_media_utils.py`: limits (1.5 GB, 50 channels), part arithmetic, file signatures, content types, configuration checks.
- `tests/test_bucket_r2.py`: the R2 client against a local S3 server that enforces the real multipart rules: presigned parts, resume, wrong or short parts, aborted sessions, error translation.
- `tests/test_youtube_resumable.py`: the upload engine against a fake YouTube server that implements the resumable protocol and injects failures: temporary errors, dropped connections with partial data, expired sessions, expired tokens, quota and limit answers, cancellation, resume from a stored offset.
- `tests/test_publish_flow.py`: start, limits, idempotent requests, browser upload resume, complete, rejected files, cancel, retry rules, cleanup of abandoned and expired files.
- `tests/test_worker_flow.py`: the whole pipeline with the worker: 50 channels in parallel, partial failure and retry, storage refusal without using quota, outage and resume of the same session, worker restart and crash, expired YouTube session, daily limit pause and continue, lease protection, thumbnails, cleanup.
- `tests/test_routes.py`: the HTTP API with real authentication: the whole publishing flow, error format, access control, images-only library.

## Configuration reference

Fixed values live in `app/config.py`. The ones most often changed:

| Setting | Default | Purpose |
|---|---|---|
| `log_sql` | `False` | Print every SQL statement in the log. Turn on only when debugging. |
| `media_video_max_bytes` | 1.5 GB | Largest video accepted |
| `yt_upload_max_accounts` | 50 | Channels allowed in one upload |
| `media_part_size_bytes` | 16 MB | Size of each part the browser sends to R2 (R2 needs at least 5 MB) |
| `media_client_parallel_parts` | 4 | Parts the browser should send at the same time |
| `media_part_url_ttl_seconds` | 6 hours | How long a part URL is valid |
| `media_upload_window_hours` | 12 | Time the browser has to finish the upload |
| `media_retention_hours` | 24 | How long a finished file is kept when some channels failed |
| `media_max_active_per_user` | 5 | Videos one user can have waiting |
| `media_max_staged_bytes_total` | 8 GB | Total temporary storage, protects the free plan |
| `media_reaper_interval_seconds` | 300 | How often the cleanup task runs |
| `storage_image_max_bytes` | 10 MB | Largest library image |
| `storage_signed_url_ttl_seconds` | 3600 | How long a library image URL is valid |
| `yt_upload_max_attempts` | 6 | Attempts per channel |
| `yt_upload_retry_delays_seconds` | `[20, 60, 180, 300, 600]` | Wait before each next attempt |
| `yt_upload_concurrency` | 10 | Channels uploaded in parallel |
| `yt_upload_chunk_bytes` | 16 MB | Size of each chunk sent to YouTube (multiple of 256 KB) |
| `yt_upload_chunk_retries` | 5 | Repeats of a failed chunk inside one attempt |
| `yt_upload_daily_limit` | 100 | Uploads per day for each user's Google project |
| `yt_upload_worker_enabled` | `True` | Start the worker inside the API process |
| `yt_upload_worker_poll_seconds` | 3 | How often an idle worker looks for work |
| `yt_upload_heartbeat_seconds` | 15 | How often a running upload renews its lease |
| `yt_upload_stale_after_seconds` | 120 | When an upload of a vanished worker returns to the queue |
| `yt_default_privacy` | `private` | Privacy used when the request does not set one |
| `google_scopes` | upload, readonly, analytics, force-ssl | OAuth scopes requested from Google |
| `yt_analytics_max_videos` | 2000 | Videos loaded per channel on a sync |
| `yt_analytics_sync_ttl_seconds` | 6 hours | Age after which synced data counts as stale |
| `yt_analytics_sync_stale_seconds` | 900 | When a sync that never finished can be started again |
| `yt_analytics_report_ttl_seconds` | 3 hours | How long YouTube Analytics results are cached |
| `yt_analytics_default_timezone` | `Asia/Kolkata` | Timezone for best upload time |
| `yt_ai_report_cache_hours` | 12 | How long the same data returns the saved AI report |
| `yt_ai_daily_report_limit` | 30 | AI reports per user in 24 hours |
| `yt_ai_force_cooldown_seconds` | 120 | Wait before a report can be regenerated |

On startup the app refuses to run when fixed values contradict each other (for example a YouTube chunk size that is not a multiple of 256 KB).

## Deployment

1. **Database.** Run `migrations/001_direct_upload.sql` in the Supabase SQL editor before starting the new version. It adds the new table and columns and closes uploads that were still waiting under the old flow. The two optional statements at the end remove the old library videos and a column that is no longer used; run them after you have checked the new version.
2. **Old library videos.** Videos stored by the previous version are not served any more. List them with the query in the migration file and delete them from the old bucket by hand. Library images are not migrated: copy them to the R2 bucket under the same paths, or ask users to add them again.
3. **R2.** Complete [Cloudflare R2](#cloudflare-r2), including CORS for the production frontend origin and the lifecycle rule.
4. **Environment.** Set `APP_ENV=production`, `CORS_ORIGINS` and `GOOGLE_REDIRECT_URI` as environment variables. Keep `DATABASE_URL`, `JWT_SECRET_KEY`, `SMTP_PASSWORD`, `TOKEN_ENCRYPTION_KEY`, `R2_SECRET_ACCESS_KEY` and the Gemini key in a secret store. Remove the `B2_*` variables.
5. **Start command.** `uvicorn main:app --host 0.0.0.0 --port $PORT` (no `--reload`).
6. **Worker.** The upload worker needs CPU and bandwidth while no request is running, and it decides how fast videos reach YouTube. Run it as its own service on a machine with a fast connection and CPU always allocated (`python -m app.workers.yt_channel_management`), and set `yt_upload_worker_enabled` to `False` for the API service. The API itself no longer carries videos, so request size limits of the host do not matter for videos.
7. **Background tasks.** A channel sync and an AI report run inside the API process, so they also need CPU while no request is running. A sync that is interrupted is marked failed after 15 minutes and can be started again.
8. **Schema changes.** Tables are created with `create_all`, which only creates missing tables. It does not change existing ones. Put every change in a numbered file in `migrations/`, or introduce Alembic before the schema changes often.

## Roadmap

- Fair sharing of the worker between users (limit of parallel channels per user).
- Per-channel title and description overrides in one upload.
- Scheduled publishing across channels with different times.
- Rate limiting on login, signup and forgot-password.
- Dockerfile, CI pipeline and deployment.
- Nightly automatic sync and a weekly AI report by email.
- Competitor benchmarking from public channel data.
- Structured logging and error monitoring, metrics for upload speed and queue age.
- Key rotation for the token encryption key.
- Alembic migrations once tables change often.
- Rotate every credential that was shared during development: database password, JWT secret, Gmail app password, R2 and B2 keys.

## Code conventions

- One file per feature in every layer. A new, unrelated feature gets its own file name in each layer.
- Routes stay thin. Business rules live in `service`, queries live in `crud`.
- Nothing is hardcoded outside `app/config.py`. Secrets and environment specific values come from `.env`; fixed values are constants inside `config.py`.
- No comments in code except the file path on the first line, no emojis, user friendly error messages in English.